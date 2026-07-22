#!/usr/bin/env python3
"""Generic one-sentence worker/reviewer harness.

The interactive coordinator is any coding-agent TUI started by the user.  This
module only runs configured headless agents against a persistent workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from platform_support import (
    WINDOWS,
    acquire_file_lock,
    configure_utf8_stdio,
    file_lock,
    format_command,
    managed_process_start_time,
    parent_commands,
    process_group_identity_status,
    process_identity_status,
    pid_start_time,
    resolve_program,
    set_private_permissions,
    spawn_managed_process,
    terminate_managed_process,
    terminate_process_group,
)


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
CONFIG_PATH = ROOT / "harness.config.json"
PROMPTS_DIR = ROOT / "prompts"
STATE_SCHEMA = "generic-harness/v2"
WORKER_RESULT_SCHEMA = "generic-harness/worker-result/v1"
AUDIT_SCHEMA = "generic-harness/audit/v1"
ARTIFACT_SCHEMA = "generic-harness/artifact/v1"
VERIFICATION_SCHEMA = "generic-harness/verification/v1"
TERMINAL_STATUSES = {"COMPLETE", "INCOMPLETE"}
PAUSE_FILE = ".operator-paused"
MAX_HANDOFF_BYTES = 1_000_000
MAX_ARG_PROMPT_BYTES = 100_000
MAX_WINDOWS_COMMAND_LINE = 30_000
MAX_WINDOWS_BATCH_COMMAND_LINE = 8_000
WINDOWS_BATCH_METACHARACTERS = "%!^&|<>()\r\n\""
WINDOWS_RETRYABLE_REPLACE_ERRORS = {5, 32, 33}
MAX_VERIFICATION_DETAILS = 8_000
PROTECTED_STATE_FIELDS = (
    "schema_version",
    "run_id",
    "request",
    "workspace",
    "candidate_workspace",
    "coordinator_agent",
    "coordinator_detection",
    "worker_agent",
    "reviewer_agent",
    "review_index",
    "max_reviews",
    "phase",
    "status",
    "artifact_id",
    "artifact_path",
    "last_error",
    "created_at",
    "started_at",
    "finished_at",
)


class HarnessError(RuntimeError):
    """A recoverable failure. The run remains on disk for `continue`."""


class OperatorPause(HarnessError):
    """The coordinator asked the active run to stop safely."""


class WorkerBlocked(HarnessError):
    """The worker reported a real blocker instead of a completed delivery."""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _make_writable(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
        if WINDOWS:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        else:
            writable = stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR
            if stat.S_ISDIR(mode):
                writable |= stat.S_IXUSR
            os.chmod(path, writable)
    except OSError:
        return


def _restore_mode(path: Path, original: os.stat_result) -> None:
    """Restore permissions only when the same non-link entry still owns the path."""
    try:
        current = path.lstat()
        if (
            current.st_dev == original.st_dev
            and current.st_ino == original.st_ino
            and not path.is_symlink()
            and not _is_junction(path, current)
        ):
            os.chmod(path, stat.S_IMODE(original.st_mode))
    except OSError:
        return


def _replace_path(source: Path, destination: Path) -> None:
    retry_deadline = time.monotonic() + 0.75
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if not WINDOWS or not os.path.lexists(destination):
                raise
            if destination.is_symlink() or _is_junction(destination):
                raise PermissionError(f"Refusing to change permissions through a link: {destination}") from error
            original = destination.lstat()
            if not original.st_mode & stat.S_IWRITE:
                break
            if (
                getattr(error, "winerror", None) in WINDOWS_RETRYABLE_REPLACE_ERRORS
                and time.monotonic() < retry_deadline
            ):
                time.sleep(0.025)
                continue
            raise PermissionError(f"Could not replace writable destination: {destination}") from error
    _make_writable(destination)
    try:
        while True:
            try:
                os.replace(source, destination)
                return
            except PermissionError as error:
                if (
                    getattr(error, "winerror", None) not in WINDOWS_RETRYABLE_REPLACE_ERRORS
                    or time.monotonic() >= retry_deadline
                ):
                    raise
                time.sleep(0.025)
    except BaseException:
        _restore_mode(destination, original)
        raise


def _is_reparse_point(path: Path, details: os.stat_result | None = None) -> bool:
    if not WINDOWS:
        return False
    try:
        details = details or path.lstat()
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(int(getattr(details, "st_file_attributes", 0)) & flag)


def _is_junction(path: Path, details: os.stat_result | None = None) -> bool:
    try:
        details = details or path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(details.st_mode) and _is_reparse_point(path, details) and not path.is_symlink()


def _is_real_directory(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(details.st_mode) and not path.is_symlink() and not _is_junction(path, details)


def _real_directory_root(path: Path, label: str) -> Path:
    """Return a lexical absolute root after rejecting a replaced link/junction."""
    root = Path(os.path.abspath(path.expanduser()))
    try:
        details = root.lstat()
    except OSError as error:
        raise HarnessError(f"{label} is unavailable: {root}: {error}") from error
    if not stat.S_ISDIR(details.st_mode) or root.is_symlink() or _is_junction(root, details):
        raise HarnessError(f"{label} must be a real directory, not a symbolic link or junction: {root}")
    return root


def _runtime_control_path(run_dir: Path, suffix: str) -> Path:
    """Return a Harness-owned control path that is never authorized to child roles."""
    lexical_run = os.path.normcase(os.path.abspath(run_dir.expanduser()))
    key = hashlib.sha256(lexical_run.encode("utf-8")).hexdigest()
    runtime_root = Path(lexical_run).parent / ".harness-runtime"
    if not os.path.lexists(runtime_root):
        runtime_root.mkdir(parents=True, exist_ok=True)
        set_private_permissions(runtime_root, directory=True)
    root = _real_directory_root(runtime_root, "Runtime control directory")
    return root / f"{key}.{suffix}"


def supervisor_marker_path(run_dir: Path) -> Path:
    return _runtime_control_path(run_dir, "pid")


def supervisor_launch_lock_path(run_dir: Path) -> Path:
    return _runtime_control_path(run_dir, "launch.lock")


def pause_request_path(run_dir: Path) -> Path:
    return _runtime_control_path(run_dir, "pause")


def pause_requested(run_dir: Path) -> bool:
    return pause_request_path(run_dir).is_file()


def clear_pause_request(run_dir: Path) -> None:
    pause_request_path(run_dir).unlink(missing_ok=True)
    legacy = run_dir / PAUSE_FILE
    if legacy.is_file() or legacy.is_symlink():
        legacy.unlink(missing_ok=True)


def _is_git_name(name: str) -> bool:
    return name.casefold() == ".git"


def _contains_git_part(path: Path) -> bool:
    return any(_is_git_name(part) for part in path.parts)


def _run_relative(path: Path, run_dir: Path) -> str:
    return path.relative_to(run_dir).as_posix()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    set_private_permissions(temporary)
    _replace_path(temporary, path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(content)
    set_private_permissions(temporary)
    _replace_path(temporary, path)


def write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessError(f"Invalid JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise HarnessError(f"Expected a JSON object: {path}")
    return value


def write_process_marker(path: Path, pid: int) -> tuple[int, str]:
    started = pid_start_time(pid)
    if not started:
        raise HarnessError(f"Could not establish a safe process identity for PID {pid}")
    write_json(path, {"pid": pid, "pid_started": started})
    return pid, started


def read_process_marker(path: Path) -> tuple[int, str] | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        value = json.loads(raw)
        if isinstance(value, dict):
            pid = int(value.get("pid", 0))
            started = str(value.get("pid_started", ""))
        else:
            pid = int(value)
            started = ""
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return (pid, started) if pid > 0 else None


def remove_owned_process_marker(path: Path, identity: tuple[int, str]) -> None:
    """Remove a marker only when both PID and creation token still match."""
    if read_process_marker(path) == identity:
        path.unlink(missing_ok=True)


def read_handoff(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_HANDOFF_BYTES:
            raise HarnessError(f"Structured handoff exceeds {MAX_HANDOFF_BYTES} bytes: {path}")
    except OSError as error:
        raise HarnessError(f"Invalid JSON: {path}: {error}") from error
    return read_json(path)


def append_event(run_dir: Path, event: str, **details: Any) -> None:
    path = run_dir / "events.jsonl"
    lock_path = run_dir / "events.lock"
    payload = {"time": now(), "event": event, **details}
    with file_lock(lock_path):
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(payload, ensure_ascii=False) + "\n")
        set_private_permissions(path)


def active_agent_pid(state: dict[str, Any]) -> int | None:
    status, pid = active_agent_identity(state)
    return pid if status == "match" else None


def active_agent_identity(state: dict[str, Any]) -> tuple[str, int | None]:
    active = state.get("active_agent")
    if active is None:
        return "gone", None
    if not isinstance(active, dict):
        return "unknown", None
    try:
        pid = int(active.get("pid", 0))
        process_group = int(active.get("process_group", pid))
    except (TypeError, ValueError):
        return "unknown", None
    if pid < 1 or process_group != pid:
        return "unknown", pid if pid > 0 else None
    started = str(active.get("pid_started", ""))
    if not started:
        return "unknown", process_group
    status = (
        process_identity_status(pid, started)
        if WINDOWS
        else process_group_identity_status(process_group, started)
    )
    return status, process_group


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    path = path.expanduser().resolve()
    config = read_json(path)
    agents = config.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise HarnessError(f"Config has no agent profiles: {path}")
    for name, profile in agents.items():
        command = profile.get("command") if isinstance(profile, dict) else None
        if not isinstance(name, str) or not name or not isinstance(command, list) or not command:
            raise HarnessError(f"Invalid agent profile: {name!r}")
        if not all(isinstance(item, str) and item for item in command):
            raise HarnessError(f"Agent command must be a non-empty argv array: {name}")
        if "{" in command[0] or "}" in command[0]:
            raise HarnessError(f"Agent executable cannot use placeholders: {name}")
        if "stdin" in profile and not isinstance(profile["stdin"], str):
            raise HarnessError(f"Agent stdin template must be text: {name}")
        for field in ("detect", "tui"):
            values = profile.get(field)
            if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
                raise HarnessError(f"Agent {field} must be a non-empty text array: {name}")
        if any("{" in item or "}" in item for item in profile["tui"]):
            raise HarnessError(f"Agent tui argv cannot use placeholders: {name}")
        allowed = {"prompt", "prompt_file", "workspace", "run_dir", "role"}
        templates = list(command) + ([profile["stdin"]] if "stdin" in profile else [])
        for template in templates:
            try:
                placeholders = [field for _literal, field, _spec, _conversion in string.Formatter().parse(template) if field]
            except ValueError as error:
                raise HarnessError(f"Invalid agent template for {name}: {error}") from error
            unknown = set(placeholders) - allowed
            if unknown:
                raise HarnessError(f"Unknown agent placeholder(s) for {name}: {', '.join(sorted(unknown))}")
            try:
                template.format_map({field: "value" for field in allowed})
            except (KeyError, ValueError) as error:
                raise HarnessError(f"Invalid agent template for {name}: {error}") from error
        if "timeout_seconds" in profile and (
            isinstance(profile["timeout_seconds"], bool)
            or not isinstance(profile["timeout_seconds"], int)
            or profile["timeout_seconds"] < 1
        ):
            raise HarnessError(f"Agent timeout_seconds must be a positive integer: {name}")
    default_agent = config.get("default_agent", "hermes")
    if default_agent not in agents:
        raise HarnessError(f"Unknown default_agent: {default_agent}")
    for field in ("worker_agent", "reviewer_agent"):
        if config.get(field) is not None and config[field] not in agents:
            raise HarnessError(f"Unknown {field}: {config[field]}")
    for field, default in (("max_reviews", 3), ("timeout_seconds", 5400)):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise HarnessError(f"{field} must be a positive integer")
    if not isinstance(config.get("workspace", "workspace"), str):
        raise HarnessError("workspace must be a path string")
    commands = config.get("verification_commands")
    if not isinstance(commands, list) or not commands or not all(
        isinstance(command, list)
        and command
        and all(isinstance(argument, str) and argument for argument in command)
        for command in commands
    ):
        raise HarnessError("verification_commands must contain at least one non-empty argv array")
    verification_timeout = config.get("verification_timeout_seconds", 600)
    if isinstance(verification_timeout, bool) or not isinstance(verification_timeout, int) or verification_timeout < 1:
        raise HarnessError("verification_timeout_seconds must be a positive integer")
    return config


def _parent_commands(limit: int = 12) -> list[str]:
    return parent_commands(limit)


def detect_coordinator(config: dict[str, Any]) -> tuple[str, str]:
    agents = config["agents"]
    explicit = os.environ.get("HARNESS_COORDINATOR_AGENT", "").strip()
    if explicit:
        if explicit not in agents:
            raise HarnessError(f"HARNESS_COORDINATOR_AGENT names an unknown profile: {explicit}")
        return explicit, "environment"

    process_text = "\n".join(_parent_commands()).casefold()
    for name, profile in agents.items():
        needles = profile.get("detect", [name])
        if isinstance(needles, list) and any(
            isinstance(needle, str)
            and re.search(
                rf"(?:^|[\\/\s\"']){re.escape(needle.casefold())}(?:\.js|\.exe|\.cmd|\.bat)?(?=[\s\"']|$)",
                process_text,
            )
            for needle in needles
        ):
            return name, "process"
    return str(config.get("default_agent", "hermes")), "fallback"


def detect_coordinator_agent(config: dict[str, Any]) -> str:
    return detect_coordinator(config)[0]


def _selected_agent(config: dict[str, Any], value: str | None, fallback: str) -> str:
    selected = value or fallback
    if selected not in config["agents"]:
        known = ", ".join(sorted(config["agents"]))
        raise HarnessError(f"Unknown agent profile {selected!r}; configured profiles: {known}")
    return selected


def _resolve_executable(command: list[str], profile_name: str) -> str:
    executable = command[0]
    if "{" in executable:
        raise HarnessError(f"The executable itself cannot be a placeholder: {profile_name}")
    found = resolve_program(executable)
    if found:
        return found
    raise HarnessError(f"Agent executable is unavailable for profile {profile_name}: {executable}")


def _command_template_fields(command: list[str]) -> set[str]:
    return {
        field
        for item in command
        for _literal, field, _spec, _conversion in string.Formatter().parse(item)
        if field
    }


def _windows_batch_argv_has_metacharacters(command: list[str]) -> bool:
    return WINDOWS and Path(command[0]).suffix.casefold() in {".bat", ".cmd"} and any(
        any(character in item for character in WINDOWS_BATCH_METACHARACTERS)
        for item in command
    )


def validate_agent_profile(profile_name: str, profile: dict[str, Any]) -> str:
    """Resolve one profile and reject Windows batch argv that cmd could reinterpret."""
    command = profile["command"]
    executable = _resolve_executable(command, profile_name)
    if not WINDOWS or Path(executable).suffix.casefold() not in {".bat", ".cmd"}:
        return executable
    fields = _command_template_fields(command)
    if fields:
        raise HarnessError(
            f"Windows batch-wrapper profile {profile_name} cannot receive runtime placeholders in argv "
            f"({', '.join(sorted(fields))}); configure a static command with stdin or use a native executable"
        )
    resolved_command = [executable, *command[1:]]
    if _windows_batch_argv_has_metacharacters(resolved_command):
        raise HarnessError(
            f"Windows batch-wrapper profile {profile_name} has cmd.exe metacharacters in its executable or argv; "
            "use a native executable"
        )
    return executable


def _new_run_dir(runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = runs_dir / stem
    if candidate.exists():
        candidate = runs_dir / f"{stem}-{uuid.uuid4().hex[:6]}"
    candidate.mkdir(mode=0o700)
    set_private_permissions(candidate, directory=True)
    return candidate.resolve()


def create_run(
    request: str,
    *,
    config_path: Path = CONFIG_PATH,
    runs_dir: Path = RUNS_DIR,
    workspace: Path | None = None,
    coordinator_agent: str | None = None,
    worker_agent: str | None = None,
    reviewer_agent: str | None = None,
    max_reviews: int | None = None,
) -> Path:
    request = request.strip()
    if not request:
        raise HarnessError("Task request cannot be empty.")
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path)
    detected, detection = detect_coordinator(config)
    coordinator = _selected_agent(config, coordinator_agent, detected)
    if coordinator_agent:
        detection = "explicit"
    worker = _selected_agent(config, worker_agent or config.get("worker_agent"), coordinator)
    reviewer = _selected_agent(config, reviewer_agent or config.get("reviewer_agent"), coordinator)
    reviews = int(max_reviews if max_reviews is not None else config.get("max_reviews", 3))
    if reviews < 1:
        raise HarnessError("max_reviews must be at least 1.")

    configured_workspace = Path(str(config.get("workspace", "workspace"))).expanduser()
    workspace_path = workspace.expanduser() if workspace else configured_workspace
    if not workspace_path.is_absolute():
        workspace_path = config_path.parent / workspace_path
    lexical_workspace = Path(os.path.abspath(workspace_path))
    lexical_home = Path(os.path.abspath(Path.home()))
    if lexical_workspace.parent == lexical_workspace or lexical_workspace == lexical_home:
        raise HarnessError(f"Refusing a workspace that is too broad: {lexical_workspace}")
    if not os.path.lexists(lexical_workspace):
        lexical_workspace.mkdir(parents=True, exist_ok=True)
    workspace_path = _real_directory_root(lexical_workspace, "Workspace").resolve()
    resolved_home = Path.home().resolve()
    if workspace_path.parent == workspace_path or workspace_path == resolved_home:
        raise HarnessError(f"Refusing a workspace that is too broad: {workspace_path}")

    selected_profiles = {name: config["agents"][name] for name in {worker, reviewer}}
    for name, profile in selected_profiles.items():
        validate_agent_profile(name, profile)

    lexical_runs = Path(os.path.abspath(runs_dir.expanduser()))
    if not os.path.lexists(lexical_runs):
        lexical_runs.mkdir(parents=True, exist_ok=True)
    runs_path = _real_directory_root(lexical_runs, "Runs directory").resolve()
    if runs_path == workspace_path:
        raise HarnessError("runs_dir cannot be the workspace itself")
    if runs_path in workspace_path.parents:
        raise HarnessError("runs_dir cannot contain the workspace")
    run_dir = _new_run_dir(runs_path)
    timeout = int(config.get("timeout_seconds", 5400))
    candidate_workspace = run_dir / "candidate"
    exclusions = _review_exclusions(run_dir)
    base_artifact = _workspace_manifest(workspace_path, exclusions)
    write_json(run_dir / "base-artifact.json", base_artifact)
    _copy_workspace(workspace_path, candidate_workspace, exclusions)
    copied_artifact = _workspace_manifest(candidate_workspace)
    live_after_copy = _workspace_manifest(workspace_path, exclusions)
    if copied_artifact["sha256"] != base_artifact["sha256"] or live_after_copy["sha256"] != base_artifact["sha256"]:
        raise HarnessError("Workspace changed while the isolated candidate was being created")
    verification_commands = [
        [sys.executable if argument == "{python}" else argument for argument in command]
        for command in config.get("verification_commands", [])
    ]
    run_config = {
        "schema_version": STATE_SCHEMA,
        "source_config": str(config_path),
        "workspace": str(workspace_path),
        "candidate_workspace": str(candidate_workspace),
        "coordinator_agent": coordinator,
        "coordinator_detection": detection,
        "worker_agent": worker,
        "reviewer_agent": reviewer,
        "max_reviews": reviews,
        "timeout_seconds": timeout,
        "verification_commands": verification_commands,
        "verification_timeout_seconds": int(config.get("verification_timeout_seconds", 600)),
        "profiles": selected_profiles,
    }
    state = {
        "schema_version": STATE_SCHEMA,
        "run_id": run_dir.name,
        "status": "QUEUED",
        "phase": "work",
        "request": request,
        "workspace": str(workspace_path),
        "candidate_workspace": str(candidate_workspace),
        "coordinator_agent": coordinator,
        "coordinator_detection": detection,
        "worker_agent": worker,
        "reviewer_agent": reviewer,
        "review_index": 0,
        "max_reviews": reviews,
        "active_agent": None,
        "artifact_id": None,
        "artifact_path": None,
        "last_error": "",
        "created_at": now(),
        "updated_at": now(),
        "started_at": None,
        "finished_at": None,
    }
    atomic_write(run_dir / "request.md", request + "\n")
    write_json(run_dir / "run-config.json", run_config)
    write_json(run_dir / "state.json", state)
    append_event(run_dir, "run_created", coordinator=coordinator, worker=worker, reviewer=reviewer)
    refresh_report(run_dir)
    return run_dir


def request_pause(run_dir: Path) -> Path:
    payload = json.dumps({"requested_at": now()}, ensure_ascii=False) + "\n"
    path = pause_request_path(run_dir)
    atomic_write(path, payload)
    # Keep the historical marker as a human-visible breadcrumb only. Child
    # roles can reach their run directory, so execution never trusts it.
    legacy = run_dir / PAUSE_FILE
    try:
        if not legacy.exists():
            atomic_write(legacy, payload)
    except OSError:
        pass
    return path


def _update_state(run_dir: Path, **changes: Any) -> dict[str, Any]:
    path = run_dir / "state.json"
    state = read_json(path)
    state.update(changes)
    state["updated_at"] = now()
    write_json(path, state)
    return state


class _FormatValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise HarnessError(f"Unknown command/prompt placeholder: {key}")


def render_prompt(name: str, **values: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HarnessError(f"Prompt template is unavailable: {path}") from error
    try:
        return template.format_map(_FormatValues(values)).strip()
    except (KeyError, ValueError) as error:
        raise HarnessError(f"Invalid prompt template {path}: {error}") from error


def _terminate(process: subprocess.Popen[str], grace: float = 5.0) -> None:
    try:
        terminate_managed_process(process, grace)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise HarnessError(f"Could not terminate managed process tree for PID {process.pid}: {error}") from error


def _isolated_process_environment(workspace: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(name, None)
    environment["GIT_CEILING_DIRECTORIES"] = str(workspace.resolve().parent)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _control_guard(run_dir: Path, protected_files: tuple[Path, ...] = ()) -> dict[str, Any]:
    state = read_json(run_dir / "state.json")
    evidence = [run_dir / "base-artifact.json", run_dir / "request.md", run_dir / "harness.pid"]
    for pattern in (
        "iterations/*/input-artifact.json",
        "iterations/*/output-artifact.json",
        "iterations/*/WORKER_RESULT.json",
        "iterations/*/VERIFICATION.json",
        "reviews/*/artifact.json",
        "reviews/*/AUDIT.json",
    ):
        evidence.extend(sorted(run_dir.glob(pattern)))
    protected = tuple(dict.fromkeys((*protected_files, *evidence)))
    protected_contents: dict[str, bytes] = {}
    for path in protected:
        try:
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise HarnessError(f"Protected Harness evidence is not a private regular file: {path}")
            protected_contents[str(path)] = path.read_bytes()
        except OSError as error:
            raise HarnessError(f"Protected Harness evidence is unavailable: {path}: {error}") from error
    return {
        "config": read_json(run_dir / "run-config.json"),
        "state_document": state,
        "state": {field: state.get(field) for field in PROTECTED_STATE_FIELDS},
        "active_agent": state.get("active_agent"),
        "files": protected_contents,
    }


def _verify_control_guard(run_dir: Path, guard: dict[str, Any]) -> None:
    changed: list[str] = []
    config_path = run_dir / "run-config.json"
    try:
        details = config_path.lstat()
        config = read_json(config_path) if stat.S_ISREG(details.st_mode) and details.st_nlink == 1 else None
    except (OSError, HarnessError):
        config = None
    if config != guard["config"]:
        write_json(config_path, guard["config"])
        changed.append("run-config.json")

    state_path = run_dir / "state.json"
    try:
        details = state_path.lstat()
        state = read_json(state_path) if stat.S_ISREG(details.st_mode) and details.st_nlink == 1 else None
    except (OSError, HarnessError):
        state = None
    if state is None:
        state = dict(guard["state_document"])
        state["active_agent"] = guard.get("active_agent")
        write_json(state_path, state)
        changed.append("state.json")
    for field, expected in guard["state"].items():
        if state.get(field) != expected:
            state[field] = expected
            changed.append(f"state.json:{field}")
    expected_active = guard.get("active_agent")
    if state.get("active_agent") != expected_active:
        state["active_agent"] = expected_active
        changed.append("state.json:active_agent")
    if changed:
        write_json(state_path, state)

    for raw_path, expected in guard["files"].items():
        path = Path(raw_path)
        try:
            details = path.lstat()
            actual = (
                path.read_bytes()
                if stat.S_ISREG(details.st_mode) and details.st_nlink == 1 and details.st_size == len(expected)
                else None
            )
        except OSError:
            actual = None
        if actual != expected:
            atomic_write_bytes(path, expected)
            changed.append(_run_relative(path, run_dir))
    if changed:
        raise HarnessError("Agent modified protected Harness control data: " + ", ".join(changed))


def run_agent(
    *,
    run_dir: Path,
    profile_name: str,
    profile: dict[str, Any],
    role: str,
    prompt: str,
    prompt_path: Path,
    log_path: Path,
    timeout_seconds: int,
    workspace: Path | None = None,
    guard: dict[str, Any] | None = None,
) -> None:
    workspace = workspace or Path(read_json(run_dir / "run-config.json")["workspace"])
    atomic_write(prompt_path, prompt + "\n")
    values = _FormatValues(
        prompt=prompt,
        prompt_file=str(prompt_path),
        workspace=str(workspace),
        run_dir=str(run_dir),
        role=role,
    )
    try:
        argv_fields = _command_template_fields(profile["command"])
        prompt_in_argv = "prompt" in argv_fields
        if prompt_in_argv and not WINDOWS and len(prompt.encode()) > MAX_ARG_PROMPT_BYTES:
            raise HarnessError(
                f"Prompt is too large for argv ({len(prompt.encode())} bytes); use stdin or {{prompt_file}} for {profile_name}"
            )
        command = [item.format_map(values) for item in profile["command"]]
        stdin_text = profile.get("stdin")
        stdin_value = stdin_text.format_map(values) if isinstance(stdin_text, str) else None
    except (KeyError, ValueError) as error:
        raise HarnessError(f"Invalid agent command template for {profile_name}: {error}") from error
    command[0] = validate_agent_profile(profile_name, profile)
    windows_batch = WINDOWS and Path(command[0]).suffix.casefold() in {".bat", ".cmd"}
    windows_command_units = (
        len(format_command(command).encode("utf-16-le")) // 2 if WINDOWS else 0
    )
    windows_limit = MAX_WINDOWS_BATCH_COMMAND_LINE if windows_batch else MAX_WINDOWS_COMMAND_LINE
    if WINDOWS and windows_command_units >= windows_limit:
        raise HarnessError(
            f"Command exceeds the safe Windows command-line limit; use stdin or {{prompt_file}} for {profile_name}"
        )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    shown = [item.replace(prompt, "<prompt>") if prompt else item for item in command]
    with log_path.open("a", encoding="utf-8") as log:
        set_private_permissions(log_path)
        log.write(f"\n[{now()}] {role} via {profile_name}\n$ {format_command(shown)}\n")
        log.flush()
        stdin_stream = None
        try:
            if stdin_value is not None:
                stdin_stream = tempfile.TemporaryFile(mode="w+b")
                stdin_stream.write(stdin_value.encode("utf-8"))
                stdin_stream.seek(0)
            process = spawn_managed_process(
                command,
                cwd=workspace,
                env=_isolated_process_environment(workspace),
                stdin=stdin_stream if stdin_stream is not None else subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
            )
        except (OSError, RuntimeError) as error:
            if stdin_stream is not None:
                stdin_stream.close()
            raise HarnessError(f"Could not launch {profile_name}: {error}") from error
        except BaseException:
            if stdin_stream is not None:
                stdin_stream.close()
            raise
        try:
            process_started = managed_process_start_time(process)
            if not process_started:
                raise HarnessError(f"Could not establish a safe process identity for {profile_name}")
            active_agent = {
                "profile": profile_name,
                "role": role,
                "pid": process.pid,
                "process_group": process.pid,
                "pid_started": process_started,
                "log": _run_relative(log_path, run_dir),
                "started_at": now(),
            }
            if guard is not None:
                guard["active_agent"] = active_agent
            _update_state(run_dir, active_agent=active_agent)
            append_event(run_dir, "agent_started", profile=profile_name, role=role, pid=process.pid)
            effective_timeout = int(profile.get("timeout_seconds", timeout_seconds))
            deadline = time.monotonic() + effective_timeout
            started_monotonic = time.monotonic()
            paused = False
            timed_out = False
            while process.poll() is None:
                if pause_requested(run_dir):
                    paused = True
                    _terminate(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate(process)
                    break
                wait_seconds = max(0.01, min(0.25, deadline - time.monotonic()))
                try:
                    process.wait(timeout=wait_seconds)
                except subprocess.TimeoutExpired:
                    pass
            returncode = process.wait()
            _terminate(process, grace=1.0)
        except BaseException:
            _terminate(process)
            if guard is not None:
                guard["active_agent"] = None
            try:
                _update_state(run_dir, active_agent=None)
            except (OSError, HarnessError):
                pass
            raise
        finally:
            if stdin_stream is not None:
                stdin_stream.close()

    if guard is not None:
        guard["active_agent"] = None
    _update_state(run_dir, active_agent=None)
    append_event(
        run_dir,
        "agent_finished",
        profile=profile_name,
        role=role,
        returncode=returncode,
        timed_out=timed_out,
        paused=paused,
        duration_seconds=round(time.monotonic() - started_monotonic, 3),
    )
    if paused:
        raise OperatorPause("The active agent was stopped at the user's request.")
    if timed_out:
        raise HarnessError(f"{profile_name} timed out after {effective_timeout} seconds; see {log_path}")
    if returncode:
        raise HarnessError(f"{profile_name} exited {returncode}; see {log_path}")


def _worker_result(run_dir: Path, index: int) -> tuple[Path, dict[str, Any]]:
    plan = run_dir / "PLAN.md"
    result_path = run_dir / "iterations" / f"{index:02d}" / "WORKER_RESULT.json"
    if not plan.is_file() or not plan.read_text(encoding="utf-8").strip():
        raise HarnessError(f"Worker did not create a non-empty plan: {plan}")
    result = read_handoff(result_path)
    if result.get("schema_version") != WORKER_RESULT_SCHEMA:
        raise HarnessError(f"Worker result has an unsupported schema: {result_path}")
    result_status = str(result.get("status", "")).lower()
    if result_status == "blocked":
        summary = str(result.get("summary", "")).strip() or "Worker reported an unspecified blocker."
        raise WorkerBlocked(summary)
    if result_status not in {"complete", "completed", "done"}:
        raise HarnessError(f"Worker result does not report completion: {result_path}")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise HarnessError(f"Worker result omitted its summary: {result_path}")
    for field in ("changed_files", "checks", "limitations"):
        if not isinstance(result.get(field), list):
            raise HarnessError(f"Worker result field {field!r} must be an array: {result_path}")
    if not all(isinstance(item, str) and item.strip() for item in result["changed_files"] + result["limitations"]):
        raise HarnessError(f"Worker result path/limitation entries must be text: {result_path}")
    normalized_changes: list[str] = []
    for item in result["changed_files"]:
        candidate = Path(item)
        if candidate.anchor or candidate == Path(".") or ".." in candidate.parts:
            raise HarnessError(f"Worker result contains an unsafe changed path: {item}")
        normalized_changes.append(candidate.as_posix())
    if normalized_changes != result["changed_files"]:
        result["changed_files"] = normalized_changes
        write_json(result_path, result)
    for check in result["checks"]:
        if not isinstance(check, dict) or str(check.get("status", "")).lower() not in {"pass", "fail", "not_run"}:
            raise HarnessError(f"Worker result has an invalid check: {result_path}")
        if not all(isinstance(check.get(field), str) for field in ("name", "command", "details")):
            raise HarnessError(f"Worker check omitted required text fields: {result_path}")
    if any(str(check.get("status", "")).lower() == "fail" for check in result["checks"]):
        raise HarnessError(f"Worker reported completion with a failed check: {result_path}")
    return result_path, result


def _archive_worker_result(run_dir: Path, index: int) -> Path:
    """Archive the worker's fixed handoff path for this review round."""
    current = run_dir / "WORKER_RESULT.json"
    result = read_json(current)
    archived = run_dir / "iterations" / f"{index:02d}" / "WORKER_RESULT.json"
    write_json(archived, result)
    current.unlink(missing_ok=True)
    return archived


def _quarantine(path: Path) -> None:
    """Keep an invalid handoff for diagnosis while allowing `continue` to retry."""
    if not path.exists():
        return
    candidate = path.with_name(f"{path.stem}.invalid-{uuid.uuid4().hex[:6]}{path.suffix}")
    path.replace(candidate)


def _is_excluded(path: Path, excluded: tuple[Path, ...]) -> bool:
    absolute = Path(os.path.abspath(path))
    normalized = tuple(Path(os.path.abspath(item)) for item in excluded)
    return any(absolute == item or item in absolute.parents for item in normalized)


def _review_exclusions(run_dir: Path) -> tuple[Path, ...]:
    return (
        run_dir.parent,
        RUNS_DIR,
        ROOT / ".harness-current",
        ROOT / ".harness-control.lock",
        ROOT / ".harness-request.md",
    )


def _candidate_workspace(run_dir: Path) -> Path:
    candidate = Path(os.path.abspath(str(read_json(run_dir / "run-config.json").get("candidate_workspace", ""))))
    expected = Path(os.path.abspath(run_dir / "candidate"))
    if candidate != expected:
        raise HarnessError(f"Invalid candidate workspace for run: {candidate}")
    if os.path.lexists(candidate):
        try:
            details = candidate.lstat()
        except OSError as error:
            raise HarnessError(f"The isolated candidate workspace is unavailable: {error}") from error
        if candidate.is_symlink() or _is_junction(candidate, details):
            raise HarnessError("The isolated candidate workspace cannot be a symbolic link or junction")
    return candidate


def _validate_workspace_links(
    workspace: Path,
    excluded: tuple[Path, ...] = (),
    *,
    projected_workspace: Path | None = None,
    projected_excluded: tuple[Path, ...] = (),
) -> None:
    root = _real_directory_root(workspace, "Workspace")
    excluded = tuple(Path(os.path.abspath(path)) for path in excluded)
    projected_root = (
        _real_directory_root(projected_workspace, "Projected workspace")
        if projected_workspace is not None
        else None
    )
    projected_excluded = tuple(Path(os.path.abspath(path)) for path in projected_excluded)

    def validate(path: Path) -> None:
        target = Path(os.readlink(path))
        if target.is_absolute():
            raise HarnessError(f"Absolute workspace symlinks are not allowed in isolated runs: {path}")
        try:
            resolved = (path.parent / target).resolve()
        except (OSError, RuntimeError) as error:
            raise HarnessError(f"Workspace symlink cannot be resolved safely: {path}: {error}") from error
        if resolved != root and root not in resolved.parents:
            raise HarnessError(f"Workspace symlink escapes the isolated workspace: {path}")
        if resolved.is_dir():
            raise HarnessError(f"Directory workspace symlinks are not allowed in isolated runs: {path}")
        relative_target = resolved.relative_to(root)
        if _contains_git_part(relative_target) or _is_excluded(resolved, excluded):
            raise HarnessError(f"Workspace symlink targets content omitted from isolated runs: {path}")
        if projected_root is None:
            return
        projected_parent = projected_root / path.parent.relative_to(root)
        projected_target = Path(os.path.abspath(projected_parent / target))
        if projected_target != projected_root and projected_root not in projected_target.parents:
            raise HarnessError(f"Workspace symlink escapes the promoted workspace: {path}")
        projected_relative = projected_target.relative_to(projected_root)
        if _contains_git_part(projected_relative) or any(
            projected_target == item or item in projected_target.parents for item in projected_excluded
        ):
            raise HarnessError(f"Workspace symlink would expose protected formal-workspace content: {path}")

    for directory, subdirectories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        traversable: list[str] = []
        for name in subdirectories:
            path = base / name
            if _is_git_name(name) or _is_excluded(path, excluded):
                continue
            try:
                details = path.lstat()
            except OSError as error:
                raise HarnessError(f"Could not inspect workspace path {path}: {error}") from error
            if _is_junction(path, details):
                raise HarnessError(f"Directory workspace junctions are not allowed in isolated runs: {path}")
            if not path.is_symlink():
                traversable.append(name)
                continue
            validate(path)
        subdirectories[:] = traversable
        for name in files:
            path = base / name
            if _is_git_name(name) or _is_excluded(path, excluded) or not path.is_symlink():
                continue
            validate(path)


def _workspace_manifest(workspace: Path, excluded: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Create a content-addressed identity for the delivered workspace."""
    workspace = _real_directory_root(workspace, "Workspace")
    excluded = tuple(Path(os.path.abspath(path)) for path in excluded)
    entries: list[dict[str, Any]] = []
    for directory, subdirectories, files in os.walk(workspace, followlinks=False):
        base = Path(directory)
        traversable: list[str] = []
        for name in sorted(subdirectories):
            path = base / name
            if _is_git_name(name) or _is_excluded(path, excluded):
                continue
            try:
                details = path.lstat()
                if _is_junction(path, details):
                    raise HarnessError(f"Directory workspace junctions are not allowed in isolated runs: {path}")
                if path.is_symlink():
                    entries.append(
                        {
                            "path": path.relative_to(workspace).as_posix(),
                            "kind": "link",
                            "mode": stat.S_IMODE(path.lstat().st_mode),
                            "target": os.readlink(path),
                        }
                    )
                else:
                    if not stat.S_ISDIR(details.st_mode):
                        raise HarnessError(f"Unsupported workspace entry type: {path}")
                    entries.append(
                        {
                            "path": path.relative_to(workspace).as_posix(),
                            "kind": "directory",
                            "mode": stat.S_IMODE(details.st_mode),
                        }
                    )
                    traversable.append(name)
            except OSError as error:
                raise HarnessError(f"Could not fingerprint workspace path {path}: {error}") from error
        subdirectories[:] = traversable
        for name in sorted(files):
            path = base / name
            if _is_git_name(name) or _is_excluded(path, excluded):
                continue
            try:
                details = path.lstat()
                relative = path.relative_to(workspace).as_posix()
                mode = stat.S_IMODE(details.st_mode)
                if path.is_symlink():
                    entries.append({"path": relative, "kind": "link", "mode": mode, "target": os.readlink(path)})
                    continue
                if not stat.S_ISREG(details.st_mode):
                    raise HarnessError(f"Unsupported workspace entry type: {path}")
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                entries.append(
                    {"path": relative, "kind": "file", "mode": mode, "size": details.st_size, "sha256": digest.hexdigest()}
                )
            except OSError as error:
                raise HarnessError(f"Could not fingerprint workspace file {path}: {error}") from error
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "entry_count": len(entries),
        "entries": entries,
    }


def _manifest_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    old = {entry["path"]: entry for entry in before.get("entries", [])}
    new = {entry["path"]: entry for entry in after.get("entries", [])}
    return sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))


def _is_manifest_mix(current: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> bool:
    current_entries = {entry["path"]: entry for entry in current.get("entries", [])}
    before_entries = {entry["path"]: entry for entry in before.get("entries", [])}
    after_entries = {entry["path"]: entry for entry in after.get("entries", [])}
    paths = current_entries.keys() | before_entries.keys() | after_entries.keys()
    temporary_name = re.compile(r"^\..+\.harness-promote-[0-9a-f]{6}$")
    for path in paths:
        current_entry = current_entries.get(path)
        before_entry = before_entries.get(path)
        after_entry = after_entries.get(path)
        if current_entry in (before_entry, after_entry):
            continue
        if (
            before_entry is None
            and after_entry is None
            and current_entry is not None
            and temporary_name.fullmatch(Path(path).name)
        ):
            continue
        if current_entry is None and before_entry != after_entry:
            continue
        if current_entry is not None and current_entry.get("kind") == "directory":
            transient = False
            for expected in (before_entry, after_entry):
                if not isinstance(expected, dict) or expected.get("kind") != "directory":
                    continue
                writable_mode = (
                    int(expected["mode"])
                    | stat.S_IRUSR
                    | stat.S_IWUSR
                    | stat.S_IXUSR
                )
                if current_entry == {**expected, "mode": writable_mode}:
                    transient = True
                    break
            if transient:
                continue
        return False
    return True


def _copy_workspace(workspace: Path, destination: Path, excluded: tuple[Path, ...] = ()) -> None:
    workspace = _real_directory_root(workspace, "Source workspace")
    excluded = tuple(Path(os.path.abspath(path)) for path in excluded)
    _validate_workspace_links(workspace, excluded)

    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        return {
            name
            for name in names
            if _is_git_name(name) or _is_excluded(base / name, excluded)
        }

    try:
        shutil.copytree(workspace, destination, symlinks=True, ignore=ignore)
    except OSError as error:
        raise HarnessError(f"Could not create isolated review snapshot: {error}") from error


def _restore_workspace_if_changed(
    workspace: Path,
    trusted_snapshot: Path,
    expected_artifact: dict[str, Any],
    actor: str,
) -> None:
    expected_id = str(expected_artifact.get("sha256", ""))
    try:
        trusted_details = trusted_snapshot.lstat()
    except OSError as error:
        raise HarnessError(f"Trusted {actor.lower()} snapshot is unavailable: {error}") from error
    if (
        not stat.S_ISDIR(trusted_details.st_mode)
        or trusted_snapshot.is_symlink()
        or _is_junction(trusted_snapshot, trusted_details)
    ):
        raise HarnessError(f"Trusted {actor.lower()} snapshot is not a real directory")
    if _workspace_manifest(trusted_snapshot)["sha256"] != expected_id:
        raise HarnessError(f"Trusted {actor.lower()} snapshot changed")

    try:
        real_directory = _is_real_directory(workspace)
    except OSError:
        real_directory = False
    if real_directory:
        try:
            if _workspace_manifest(workspace)["sha256"] == expected_id:
                return
        except HarnessError:
            pass
        restored = _sync_workspace(trusted_snapshot, workspace)
    else:
        if os.path.lexists(workspace):
            _remove_path(workspace)
        _copy_workspace(trusted_snapshot, workspace)
        restored = _workspace_manifest(workspace)
    if restored["sha256"] != expected_id:
        raise HarnessError(f"Could not restore the candidate after {actor.lower()} modification")
    raise HarnessError(f"{actor} modified the candidate workspace; the candidate was restored")


@contextmanager
def _guard_candidate_workspace(
    workspace: Path,
    expected_artifact: dict[str, Any],
    actor: str,
):
    with tempfile.TemporaryDirectory(prefix="harness-candidate-guard-") as temporary:
        trusted_snapshot = Path(temporary) / "candidate"
        _copy_workspace(workspace, trusted_snapshot)
        if _workspace_manifest(trusted_snapshot)["sha256"] != expected_artifact["sha256"]:
            raise HarnessError(f"Candidate changed while the {actor.lower()} guard was being created")
        try:
            yield trusted_snapshot
        finally:
            _restore_workspace_if_changed(workspace, trusted_snapshot, expected_artifact, actor)


def _verification_path(run_dir: Path, index: int) -> Path:
    return run_dir / "iterations" / f"{index:02d}" / "VERIFICATION.json"


def _run_verification(run_dir: Path, index: int, workspace: Path, config: dict[str, Any]) -> dict[str, Any]:
    commands = config.get("verification_commands", [])
    artifact = _workspace_manifest(workspace)
    report_path = _verification_path(run_dir, index)
    if not commands:
        raise HarnessError("This run has no Harness-enforced deterministic verification command")

    timeout = int(config.get("verification_timeout_seconds", 600))
    log_path = report_path.with_name("verification.log")
    results: list[dict[str, Any]] = []
    with _guard_candidate_workspace(workspace, artifact, "Deterministic verifier") as trusted_workspace, tempfile.TemporaryDirectory(
        prefix="harness-verify-"
    ) as temporary:
        verification_workspace = Path(temporary) / "workspace"
        _copy_workspace(trusted_workspace, verification_workspace)
        if _workspace_manifest(verification_workspace)["sha256"] != artifact["sha256"]:
            raise HarnessError("Candidate changed while the deterministic verification snapshot was being created")
        for number, raw_command in enumerate(commands, start=1):
            command = list(raw_command)
            command_text = format_command(command)
            launch_command = list(command)
            started = time.monotonic()
            timed_out = False
            paused = False
            returncode: int | None = None
            error_text = ""
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                set_private_permissions(log_path)
                log.write(f"\n[{now()}] verification {number}\n$ {command_text}\n")
                log.flush()
                executable = resolve_program(launch_command[0], cwd=verification_workspace)
                if executable is None:
                    process = None
                    error_text = f"Verification executable is unavailable: {launch_command[0]}"
                    log.write(error_text + "\n")
                else:
                    launch_command[0] = executable
                    if _windows_batch_argv_has_metacharacters(launch_command):
                        process = None
                        error_text = (
                            "Windows batch verification argv contains cmd.exe metacharacters; "
                            "use a native executable or remove the metacharacters"
                        )
                        log.write(error_text + "\n")
                    else:
                        try:
                            process = spawn_managed_process(
                                launch_command,
                                cwd=verification_workspace,
                                env=_isolated_process_environment(verification_workspace),
                                stdin=subprocess.DEVNULL,
                                stdout=log,
                                stderr=subprocess.STDOUT,
                                text=True,
                                encoding="utf-8",
                            )
                        except OSError as error:
                            process = None
                            error_text = str(error)
                            log.write(error_text + "\n")
                        except RuntimeError as error:
                            raise HarnessError(
                                f"Could not launch deterministic verification safely: {error}"
                            ) from error
                if process is not None:
                    active_verifier: dict[str, Any] | None = None
                    try:
                        process_started = managed_process_start_time(process)
                        if not process_started:
                            raise HarnessError("Could not establish a safe process identity for deterministic verification")
                        active_verifier = {
                            "profile": "harness",
                            "role": "DETERMINISTIC_VERIFIER",
                            "pid": process.pid,
                            "process_group": process.pid,
                            "pid_started": process_started,
                            "log": _run_relative(log_path, run_dir),
                            "started_at": now(),
                        }
                        _update_state(run_dir, active_agent=active_verifier)
                        append_event(run_dir, "verification_started", command=command_text, pid=process.pid)
                        deadline = time.monotonic() + timeout
                        while process.poll() is None:
                            if pause_requested(run_dir):
                                paused = True
                                _terminate(process)
                                break
                            if time.monotonic() >= deadline:
                                timed_out = True
                                _terminate(process)
                                break
                            time.sleep(0.1)
                        returncode = process.wait()
                        _terminate(process, grace=1.0)
                        _update_state(run_dir, active_agent=None)
                        append_event(
                            run_dir,
                            "verification_finished",
                            command=command_text,
                            returncode=returncode,
                            timed_out=timed_out,
                        )
                    except BaseException:
                        try:
                            _terminate(process)
                        except BaseException:
                            if active_verifier is not None:
                                try:
                                    _update_state(run_dir, active_agent=active_verifier)
                                except (OSError, HarnessError):
                                    pass
                            raise
                        try:
                            _update_state(run_dir, active_agent=None)
                        except (OSError, HarnessError):
                            pass
                        raise
            if paused:
                raise OperatorPause("The deterministic verifier was stopped at the user's request.")
            try:
                details = log_path.read_text(encoding="utf-8", errors="replace")[-MAX_VERIFICATION_DETAILS:]
            except OSError:
                details = error_text or "Verification output is unavailable."
            status = "pass" if returncode == 0 and not timed_out and not error_text else "fail"
            results.append(
                {
                    "name": f"verification {number}",
                    "argv": command,
                    "command": command_text,
                    "status": status,
                    "returncode": returncode,
                    "timed_out": timed_out,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "details": details.strip() or f"Command exited with status {returncode}.",
                    "log": _run_relative(log_path, run_dir),
                }
            )
    report = {
        "schema_version": VERIFICATION_SCHEMA,
        "artifact_id": artifact["sha256"],
        "status": "pass" if all(result["status"] == "pass" for result in results) else "fail",
        "commands": results,
    }
    write_json(report_path, report)
    return report


def _read_verification(
    run_dir: Path,
    index: int,
    artifact_id: str,
    expected_commands: list[list[str]] | None = None,
) -> dict[str, Any]:
    path = _verification_path(run_dir, index)
    report = read_json(path)
    if report.get("schema_version") != VERIFICATION_SCHEMA or report.get("artifact_id") != artifact_id:
        raise HarnessError(f"Deterministic verification does not match the reviewed artifact: {path}")
    if report.get("status") not in {"pass", "fail"} or not isinstance(report.get("commands"), list):
        raise HarnessError(f"Invalid deterministic verification report: {path}")
    commands = report["commands"]
    if not commands or any(
        not isinstance(item, dict)
        or item.get("status") not in {"pass", "fail"}
        or not isinstance(item.get("argv"), list)
        or not all(isinstance(argument, str) and argument for argument in item["argv"])
        for item in commands
    ):
        raise HarnessError(f"Invalid deterministic verification command result: {path}")
    calculated = "pass" if all(item["status"] == "pass" for item in commands) else "fail"
    if report["status"] != calculated:
        raise HarnessError(f"Inconsistent deterministic verification result: {path}")
    if expected_commands is not None and [item["argv"] for item in commands] != expected_commands:
        raise HarnessError(f"Deterministic verification commands do not match the run configuration: {path}")
    return report


def _apply_verification_gate(audit: dict[str, Any], verification: dict[str, Any]) -> None:
    if verification.get("status") != "fail":
        return
    failed = [item for item in verification["commands"] if item.get("status") != "pass"]
    audit["verdict"] = "FIX"
    audit["summary"] = f"{audit['summary']} Harness verification failed."
    audit["checks"].extend(
        {
            "name": str(item.get("name", "Harness verification")),
            "command": str(item.get("command", "")),
            "status": "fail",
            "details": str(item.get("details", "Verification failed.")),
        }
        for item in failed
    )
    commands = ", ".join(str(item.get("command", "")) for item in failed)
    audit["issues"].append(
        {
            "severity": "major",
            "location": "workspace",
            "title": "Harness-enforced verification failed",
            "evidence": f"Failed command(s): {commands}. See the persisted verification log.",
            "required_fix": "Repair the candidate until every configured verification command passes.",
            "acceptance_test": "Run the same configured verification commands and require exit status 0 for each.",
        }
    )


def _remove_path(path: Path) -> None:
    def retry_readonly(function: Any, value: str, error: Any) -> None:
        target = Path(value)
        if target.is_symlink() or _is_junction(target):
            raise error[1]
        try:
            original = target.lstat()
        except OSError:
            raise error[1]
        _make_writable(target)
        try:
            if function is os.scandir:
                shutil.rmtree(target, onerror=retry_readonly)
            else:
                function(value)
        except BaseException:
            _restore_mode(target, original)
            raise

    if _is_junction(path):
        path.rmdir()
    elif path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except PermissionError:
            if path.is_symlink():
                raise
            original = path.lstat()
            _make_writable(path)
            try:
                path.unlink()
            except BaseException:
                _restore_mode(path, original)
                raise
    elif path.is_dir():
        shutil.rmtree(path, onerror=retry_readonly)


def _sync_workspace(source: Path, destination: Path, excluded: tuple[Path, ...] = ()) -> dict[str, Any]:
    source = _real_directory_root(source, "Source workspace")
    destination = _real_directory_root(destination, "Destination workspace")
    excluded = tuple(Path(os.path.abspath(path)) for path in excluded)
    _validate_workspace_links(
        source,
        projected_workspace=destination,
        projected_excluded=excluded,
    )
    source_manifest = _workspace_manifest(source)
    destination_manifest = _workspace_manifest(destination, excluded)
    source_entries = {entry["path"]: entry for entry in source_manifest["entries"]}
    destination_entries = {entry["path"]: entry for entry in destination_manifest["entries"]}

    if not WINDOWS:
        for relative, entry in destination_entries.items():
            if entry["kind"] == "directory":
                _make_writable(destination / relative)

    for relative in sorted(destination_entries.keys() - source_entries.keys(), key=lambda value: (-value.count("/"), value)):
        target = destination / relative
        if _is_excluded(target, excluded):
            continue
        _remove_path(target)

    directories = sorted(
        ((relative, entry) for relative, entry in source_entries.items() if entry["kind"] == "directory"),
        key=lambda item: (item[0].count("/"), item[0]),
    )
    for relative, _entry in directories:
        target = destination / relative
        if _is_excluded(target, excluded):
            raise HarnessError(f"Candidate path collides with protected workspace data: {relative}")
        if os.path.lexists(target) and (
            target.is_symlink() or _is_junction(target) or not target.is_dir()
        ):
            _remove_path(target)
        target.mkdir(parents=True, exist_ok=True)
        if not WINDOWS:
            os.chmod(target, int(_entry["mode"]) | stat.S_IWUSR)

    for relative, entry in sorted(source_entries.items()):
        if entry["kind"] == "directory":
            continue
        if destination_entries.get(relative) == entry:
            continue
        source_path = source / relative
        target = destination / relative
        if _is_excluded(target, excluded):
            raise HarnessError(f"Candidate path collides with protected workspace data: {relative}")
        current = destination
        for part in Path(relative).parts[:-1]:
            current /= part
            if os.path.lexists(current) and (
                current.is_symlink() or _is_junction(current) or not current.is_dir()
            ):
                _remove_path(current)
            current.mkdir(exist_ok=True)
        if target.is_dir() and not target.is_symlink():
            _remove_path(target)
        temporary = target.with_name(f".{target.name}.harness-promote-{uuid.uuid4().hex[:6]}")
        try:
            if entry["kind"] == "link":
                os.symlink(os.readlink(source_path), temporary)
            else:
                shutil.copy2(source_path, temporary, follow_symlinks=False)
            _replace_path(temporary, target)
        finally:
            if os.path.lexists(temporary):
                _remove_path(temporary)

    for relative, entry in reversed(directories):
        os.chmod(destination / relative, int(entry["mode"]))

    promoted = _workspace_manifest(destination, excluded)
    if promoted["sha256"] != source_manifest["sha256"]:
        raise HarnessError("Promoted workspace does not match the accepted candidate artifact")
    return promoted


def _promote_candidate(run_dir: Path, accepted_artifact: dict[str, Any]) -> dict[str, Any]:
    config = read_json(run_dir / "run-config.json")
    live = Path(config["workspace"])
    candidate = _candidate_workspace(run_dir)
    exclusions = _review_exclusions(run_dir)
    base = read_json(run_dir / "base-artifact.json")
    live_before = _workspace_manifest(live, exclusions)
    accepted_id = str(accepted_artifact.get("sha256", ""))
    journal_path = run_dir / "promotion.json"
    backup = run_dir / "promotion-backup"
    if live_before["sha256"] == accepted_id:
        if journal_path.is_file():
            journal = read_json(journal_path)
            journal.update({"status": "complete", "completed_at": now(), "recovered": True})
            write_json(journal_path, journal)
        return live_before
    if live_before["sha256"] != base.get("sha256"):
        journal = read_json(journal_path) if journal_path.is_file() else {}
        recoverable = (
            journal.get("status") == "prepared"
            and journal.get("base_artifact_id") == base.get("sha256")
            and journal.get("accepted_artifact_id") == accepted_id
            and _is_real_directory(backup)
            and _workspace_manifest(backup)["sha256"] == base.get("sha256")
            and _is_manifest_mix(live_before, base, accepted_artifact)
        )
        if not recoverable:
            raise HarnessError("Live workspace changed after this run started; refusing to overwrite it")
        _sync_workspace(backup, live, exclusions)
        journal.update({"status": "recovered_rollback", "recovered_at": now()})
        write_json(journal_path, journal)
        live_before = _workspace_manifest(live, exclusions)
        if live_before["sha256"] != base.get("sha256"):
            raise HarnessError("Could not restore the live workspace after interrupted promotion")
    if not _is_real_directory(candidate):
        raise HarnessError("Accepted candidate workspace is unavailable for promotion")
    candidate_artifact = _workspace_manifest(candidate)
    if candidate_artifact["sha256"] != accepted_id:
        raise HarnessError("Candidate changed after review; refusing to promote it")

    for stale_backup in run_dir.glob(".promotion-backup-*"):
        if stale_backup.parent == run_dir:
            _remove_path(stale_backup)
    if not backup.exists():
        temporary_backup = run_dir / f".promotion-backup-{uuid.uuid4().hex[:6]}"
        try:
            _copy_workspace(live, temporary_backup, exclusions)
            if _workspace_manifest(temporary_backup)["sha256"] != base.get("sha256"):
                raise HarnessError("Promotion backup does not match the original live workspace")
            _replace_path(temporary_backup, backup)
        finally:
            if os.path.lexists(temporary_backup):
                _remove_path(temporary_backup)
    if _workspace_manifest(backup)["sha256"] != base.get("sha256"):
        raise HarnessError("Promotion backup does not match the original live workspace")
    journal = {
        "schema_version": STATE_SCHEMA,
        "status": "prepared",
        "base_artifact_id": base.get("sha256"),
        "accepted_artifact_id": accepted_id,
        "prepared_at": now(),
    }
    write_json(journal_path, journal)
    try:
        promoted = _sync_workspace(candidate, live, exclusions)
    except BaseException as error:
        try:
            _sync_workspace(backup, live, exclusions)
            journal.update({"status": "rolled_back", "error": str(error), "rolled_back_at": now()})
            write_json(journal_path, journal)
        except BaseException as rollback_error:
            raise HarnessError(
                f"Promotion failed and automatic rollback also failed; candidate and backup were preserved: {rollback_error}"
            ) from error
        if not isinstance(error, Exception):
            raise
        raise HarnessError(f"Candidate promotion failed and was rolled back: {error}") from error
    journal.update({"status": "complete", "completed_at": now()})
    write_json(journal_path, journal)
    return promoted


def _cleanup_promotion(run_dir: Path) -> None:
    paths = [run_dir / "candidate", run_dir / "promotion-backup", *run_dir.glob(".promotion-backup-*")]
    for path in paths:
        if path.parent == run_dir and os.path.lexists(path):
            _remove_path(path)


def _assert_live_workspace_unchanged(run_dir: Path) -> None:
    config = read_json(run_dir / "run-config.json")
    live = Path(config["workspace"])
    current = _workspace_manifest(live, _review_exclusions(run_dir))
    base = read_json(run_dir / "base-artifact.json")
    if current["sha256"] != base.get("sha256"):
        raise HarnessError("Live workspace changed after this run started; refusing to continue or overwrite it")


def _verify_worker_changes(run_dir: Path, index: int, result: dict[str, Any]) -> None:
    iteration_dir = run_dir / "iterations" / f"{index:02d}"
    input_path = iteration_dir / "input-artifact.json"
    if not input_path.is_file():
        return
    before = read_json(input_path)
    workspace = _candidate_workspace(run_dir)
    after = _workspace_manifest(workspace)
    write_json(iteration_dir / "output-artifact.json", after)
    actual_paths = _manifest_changes(before, after)

    def identity(value: str) -> str:
        return os.path.normcase(str(Path(value))) if WINDOWS else Path(value).as_posix()

    claimed = {identity(value) for value in result["changed_files"]}
    unreported = sorted(value for value in actual_paths if identity(value) not in claimed)
    if unreported:
        raise HarnessError("Worker omitted changed paths from its result: " + ", ".join(unreported[:20]))


def _audit_result(path: Path) -> dict[str, Any]:
    audit = read_handoff(path)
    if audit.get("schema_version") != AUDIT_SCHEMA:
        raise HarnessError(f"Reviewer audit has an unsupported schema: {path}")
    verdict = str(audit.get("verdict", "")).upper()
    if verdict not in {"PASS", "FIX"}:
        raise HarnessError(f"Reviewer verdict must be PASS or FIX: {path}")
    if not isinstance(audit.get("summary"), str) or not audit["summary"].strip():
        raise HarnessError(f"Reviewer omitted its summary: {path}")
    issues = audit.get("issues")
    if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
        raise HarnessError(f"Reviewer issues must be an array of objects: {path}")
    if not isinstance(audit.get("checks"), list) or not isinstance(audit.get("limitations"), list):
        raise HarnessError(f"Reviewer checks and limitations must be arrays: {path}")
    if not all(isinstance(item, str) and item.strip() for item in audit["limitations"]):
        raise HarnessError(f"Reviewer limitation entries must be text: {path}")
    for check in audit["checks"]:
        if not isinstance(check, dict) or str(check.get("status", "")).lower() not in {"pass", "fail", "not_run"}:
            raise HarnessError(f"Reviewer has an invalid check: {path}")
        if not all(isinstance(check.get(field), str) for field in ("name", "command", "details")):
            raise HarnessError(f"Reviewer check omitted required text fields: {path}")
    severities = {"blocker", "major", "minor"}
    for issue in issues:
        if str(issue.get("severity", "")).lower() not in severities:
            raise HarnessError(f"Reviewer issue has invalid severity: {path}")
        if not all(
            isinstance(issue.get(field), str) and str(issue.get(field)).strip()
            for field in ("title", "location", "evidence", "required_fix", "acceptance_test")
        ):
            raise HarnessError(f"Reviewer issue omitted required text fields: {path}")
    has_blocking_issue = any(str(issue.get("severity", "")).lower() in {"blocker", "major"} for issue in issues)
    has_failed_check = any(str(check.get("status", "")).lower() == "fail" for check in audit["checks"])
    if verdict == "PASS" and (has_blocking_issue or has_failed_check):
        audit["verdict"] = "FIX"
        audit["summary"] += " (Harness changed PASS to FIX because blocking evidence remains.)"
        write_json(path, audit)
    return audit


def _audit_paths(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "reviews").glob("*/AUDIT.json")) if (run_dir / "reviews").is_dir() else []


def refresh_report(run_dir: Path) -> None:
    state = read_json(run_dir / "state.json")
    lines = [
        "# Task report",
        "",
        f"- Status: **{state.get('status', 'UNKNOWN')}**",
        f"- Formal workspace: `{state.get('workspace', '')}`",
        f"- Isolated candidate: `{state.get('candidate_workspace', '')}`",
        f"- Coordinator: `{state.get('coordinator_agent', '')}`",
        f"- Coordinator detection: `{state.get('coordinator_detection', '')}`",
        f"- Worker: `{state.get('worker_agent', '')}`",
        f"- Independent reviewer: `{state.get('reviewer_agent', '')}`",
        f"- Review round: {int(state.get('review_index', 0)) + 1} / {state.get('max_reviews', 0)}",
        f"- Reviewed artifact: `{state.get('artifact_id', 'pending')}`",
        "",
        "## Request",
        "",
        str(state.get("request", "")),
        "",
        "## Artifacts",
        "",
        "- [Plan](PLAN.md)" if (run_dir / "PLAN.md").is_file() else "- Plan: pending",
    ]
    for path in sorted((run_dir / "iterations").glob("*/WORKER_RESULT.json")):
        lines.append(f"- [{path.parent.name} worker result]({_run_relative(path, run_dir)})")
    for path in sorted((run_dir / "iterations").glob("*/VERIFICATION.json")):
        try:
            verification = read_json(path)
            label = str(verification.get("status", "unknown"))
        except HarnessError:
            label = "invalid verification report"
        lines.append(f"- [{path.parent.name} Harness verification]({_run_relative(path, run_dir)}) — {label}")
    for path in _audit_paths(run_dir):
        try:
            audit = read_json(path)
            label = f"{audit.get('verdict', 'UNKNOWN')} — {audit.get('summary', '')}"
        except HarnessError:
            label = "invalid audit"
        lines.append(f"- [{path.parent.name} audit]({_run_relative(path, run_dir)}) — {label}")
    if state.get("last_error"):
        lines.extend(["", "## Last error", "", str(state["last_error"])])
    lines.extend(
        [
            "",
            "## Logs",
            "",
            "- [Harness log](harness.log)",
            "- `iterations/*/worker.log`",
            "- `iterations/*/verification.log`",
            "- `reviews/*/reviewer.log`",
            "",
        ]
    )
    atomic_write(run_dir / "FINAL_REPORT.md", "\n".join(lines))
    write_json(
        run_dir / "run-summary.json",
        {
            "schema_version": STATE_SCHEMA,
            "run_id": state.get("run_id"),
            "status": state.get("status"),
            "request": state.get("request"),
            "workspace": state.get("workspace"),
            "candidate_workspace": state.get("candidate_workspace"),
            "coordinator_agent": state.get("coordinator_agent"),
            "coordinator_detection": state.get("coordinator_detection"),
            "worker_agent": state.get("worker_agent"),
            "reviewer_agent": state.get("reviewer_agent"),
            "artifact_id": state.get("artifact_id"),
            "artifact_path": state.get("artifact_path"),
            "review_count": len(_audit_paths(run_dir)),
            "finished_at": state.get("finished_at"),
        },
    )


def execute_run(run_dir: Path) -> int:
    configure_utf8_stdio()
    requested_run_dir = Path(os.path.abspath(run_dir.expanduser()))
    if not _is_real_directory(requested_run_dir):
        raise HarnessError(f"Not a real Harness run directory: {requested_run_dir}")
    run_dir = requested_run_dir.resolve()
    state_path = run_dir / "state.json"
    config_path = run_dir / "run-config.json"
    if not state_path.is_file() or not config_path.is_file():
        raise HarnessError(f"Not a generic Harness run: {run_dir}")

    lock_path = run_dir / "run.lock"
    with lock_path.open("a+b") as lock:
        try:
            acquire_file_lock(lock, blocking=False)
        except BlockingIOError as error:
            raise HarnessError(f"Run already has an active supervisor: {run_dir}") from error
        state = read_json(state_path)
        config = read_json(config_path)
        if state.get("schema_version") != STATE_SCHEMA or config.get("schema_version") != STATE_SCHEMA:
            raise HarnessError(f"Unsupported run format (legacy runs are not resumed by this runner): {run_dir}")
        if str(state.get("status", "")).upper() in TERMINAL_STATUSES:
            refresh_report(run_dir)
            return 0 if state["status"] == "COMPLETE" else 1
        orphan_status, orphan_pid = active_agent_identity(state)
        if orphan_status == "unknown":
            raise HarnessError(
                "Run has a recorded child agent whose process identity cannot be verified; stop it safely before resuming"
            )
        if orphan_status == "match" and orphan_pid:
            raise HarnessError(
                f"Run still has an active child agent (PID {orphan_pid}); stop it before resuming: {run_dir}"
            )
        marker_path = supervisor_marker_path(run_dir)
        marker_identity = write_process_marker(marker_path, os.getpid())
        legacy_marker_path = run_dir / "harness.pid"
        legacy_marker_identity = write_process_marker(legacy_marker_path, os.getpid())
        if pause_requested(run_dir):
            try:
                _update_state(run_dir, status="PAUSED", active_agent=None)
                refresh_report(run_dir)
                return 2
            finally:
                remove_owned_process_marker(marker_path, marker_identity)
                remove_owned_process_marker(legacy_marker_path, legacy_marker_identity)
        try:
            state = _update_state(
                run_dir,
                status="RUNNING",
                active_agent=None,
                last_error="",
                started_at=state.get("started_at") or now(),
            )
            append_event(run_dir, "run_started", pid=os.getpid())
            print(f"Run: {run_dir}", flush=True)
            print(
                f"Workspace: {state['workspace']} | worker={state['worker_agent']} | reviewer={state['reviewer_agent']}",
                flush=True,
            )
        except KeyboardInterrupt:
            try:
                _update_state(run_dir, status="PAUSED", active_agent=None, last_error="Interrupted by the operator")
            finally:
                remove_owned_process_marker(marker_path, marker_identity)
                remove_owned_process_marker(legacy_marker_path, legacy_marker_identity)
            raise
        except BaseException:
            remove_owned_process_marker(marker_path, marker_identity)
            remove_owned_process_marker(legacy_marker_path, legacy_marker_identity)
            raise
        try:
            while True:
                if pause_requested(run_dir):
                    raise OperatorPause("The run was paused by the user.")
                state = read_json(state_path)
                index = int(state.get("review_index", 0))
                timeout = int(config.get("timeout_seconds", 5400))
                worker_name = str(config["worker_agent"])
                reviewer_name = str(config["reviewer_agent"])
                candidate_workspace = _candidate_workspace(run_dir)

                if state.get("phase") == "promote":
                    review_dir = run_dir / "reviews" / f"{index:02d}"
                    audit = _audit_result(review_dir / "AUDIT.json")
                    accepted_artifact = read_json(review_dir / "artifact.json")
                    accepted_id = str(accepted_artifact.get("sha256", ""))
                    verification = _read_verification(
                        run_dir,
                        index,
                        accepted_id,
                        config["verification_commands"],
                    )
                    if (
                        str(audit.get("verdict", "")).upper() != "PASS"
                        or audit.get("artifact_id") != accepted_id
                        or verification.get("status") != "pass"
                    ):
                        raise HarnessError("The promotion gate no longer matches the accepted review evidence")
                    promoted = _promote_candidate(run_dir, accepted_artifact)
                    if promoted["sha256"] != accepted_id:
                        raise HarnessError("Promoted workspace does not match the accepted artifact")
                    _update_state(
                        run_dir,
                        status="COMPLETE",
                        active_agent=None,
                        artifact_id=accepted_id,
                        artifact_path=_run_relative(review_dir / "artifact.json", run_dir),
                        finished_at=now(),
                        last_error="",
                    )
                    append_event(run_dir, "candidate_promoted", artifact_id=accepted_id)
                    try:
                        _cleanup_promotion(run_dir)
                    except OSError as error:
                        append_event(run_dir, "promotion_cleanup_failed", error=str(error))
                    refresh_report(run_dir)
                    print(f"PASS: {run_dir}", flush=True)
                    return 0

                if state.get("phase") == "work":
                    if not _is_real_directory(candidate_workspace):
                        raise HarnessError("The isolated candidate workspace is unavailable")
                    _assert_live_workspace_unchanged(run_dir)
                    iteration_dir = run_dir / "iterations" / f"{index:02d}"
                    result_path = iteration_dir / "WORKER_RESULT.json"
                    input_artifact_path = iteration_dir / "input-artifact.json"
                    if not input_artifact_path.is_file():
                        write_json(input_artifact_path, _workspace_manifest(candidate_workspace))
                    if not result_path.is_file():
                        if (run_dir / "WORKER_RESULT.json").is_file():
                            try:
                                _archive_worker_result(run_dir, index)
                            except HarnessError:
                                _quarantine(run_dir / "WORKER_RESULT.json")
                                raise
                    if not result_path.is_file():
                        feedback = "No review feedback; this is the initial implementation."
                        if index:
                            previous = run_dir / "reviews" / f"{index - 1:02d}" / "AUDIT.json"
                            feedback = previous.read_text(encoding="utf-8") if previous.is_file() else "Previous audit is unavailable."
                        status = "WORKING" if index == 0 else "REPAIRING"
                        _update_state(run_dir, status=status)
                        print(f"[{index + 1}] {status.lower()} with {worker_name}", flush=True)
                        prompt = render_prompt(
                            "worker",
                            request=str(state["request"]),
                            workspace=str(candidate_workspace),
                            run_dir=str(run_dir),
                            review_feedback=feedback,
                        )
                        guard = _control_guard(run_dir)
                        try:
                            run_agent(
                                run_dir=run_dir,
                                profile_name=worker_name,
                                profile=config["profiles"][worker_name],
                                role="TASK_WORKER",
                                prompt=prompt,
                                prompt_path=iteration_dir / "worker-prompt.md",
                                log_path=iteration_dir / "worker.log",
                                timeout_seconds=timeout,
                                workspace=candidate_workspace,
                                guard=guard,
                            )
                        finally:
                            agent_error = sys.exc_info()[1]
                            postcondition_errors: list[str] = []
                            try:
                                _verify_control_guard(run_dir, guard)
                            except HarnessError as error:
                                postcondition_errors.append(str(error))
                            try:
                                _assert_live_workspace_unchanged(run_dir)
                            except HarnessError as error:
                                postcondition_errors.append(str(error))
                            if postcondition_errors:
                                details = "; ".join(postcondition_errors)
                                if agent_error is not None:
                                    details = f"{agent_error}; {details}"
                                raise HarnessError(details) from agent_error
                        try:
                            _archive_worker_result(run_dir, index)
                        except HarnessError:
                            _quarantine(run_dir / "WORKER_RESULT.json")
                            raise
                    try:
                        _worker_path, verified_worker_result = _worker_result(run_dir, index)
                        _verify_worker_changes(run_dir, index, verified_worker_result)
                    except WorkerBlocked:
                        blocked = result_path.with_name("WORKER_RESULT.blocked.json")
                        result_path.replace(blocked)
                        (run_dir / "WORKER_RESULT.json").unlink(missing_ok=True)
                        raise
                    except HarnessError:
                        _quarantine(result_path)
                        _quarantine(run_dir / "WORKER_RESULT.json")
                        raise
                    try:
                        _run_verification(run_dir, index, candidate_workspace, config)
                    finally:
                        _assert_live_workspace_unchanged(run_dir)
                    _update_state(run_dir, phase="review", status="REVIEWING")
                    refresh_report(run_dir)
                    continue

                if state.get("phase") != "review":
                    raise HarnessError(f"Unknown run phase: {state.get('phase')!r}")
                review_dir = run_dir / "reviews" / f"{index:02d}"
                audit_path = review_dir / "AUDIT.json"
                worker_path, worker_result = _worker_result(run_dir, index)
                # A worker can write in the run directory to hand off its plan
                # and result. Never trust an AUDIT.json that predates this
                # reviewer invocation; a crash simply causes a fresh review.
                _quarantine(audit_path)
                _update_state(run_dir, status="REVIEWING")
                print(f"[{index + 1}] independent review with {reviewer_name}", flush=True)
                if not _is_real_directory(candidate_workspace):
                    raise HarnessError("The isolated candidate workspace is unavailable")
                _assert_live_workspace_unchanged(run_dir)
                artifact_before = _workspace_manifest(candidate_workspace)
                try:
                    verification = _read_verification(
                        run_dir,
                        index,
                        str(artifact_before["sha256"]),
                        config["verification_commands"],
                    )
                except HarnessError:
                    try:
                        verification = _run_verification(run_dir, index, candidate_workspace, config)
                    finally:
                        _assert_live_workspace_unchanged(run_dir)
                write_json(review_dir / "artifact.json", artifact_before)
                worker_report = f"Path: {worker_path}\n\n{json.dumps(worker_result, ensure_ascii=False, indent=2)}"
                with tempfile.TemporaryDirectory(prefix="harness-review-guard-") as guard_temporary:
                    trusted_candidate = Path(guard_temporary) / "candidate"
                    _copy_workspace(candidate_workspace, trusted_candidate)
                    if _workspace_manifest(trusted_candidate)["sha256"] != artifact_before["sha256"]:
                        raise HarnessError("Candidate changed while the trusted review snapshot was being created")
                    with tempfile.TemporaryDirectory(prefix="harness-review-") as temporary:
                        review_workspace = Path(temporary) / "workspace"
                        _copy_workspace(trusted_candidate, review_workspace)
                        if _workspace_manifest(review_workspace)["sha256"] != artifact_before["sha256"]:
                            raise HarnessError("Candidate changed while the review snapshot was being created")
                        prompt = render_prompt(
                            "reviewer",
                            request=str(state["request"]),
                            workspace=str(review_workspace),
                            run_dir=str(run_dir),
                            worker_report=worker_report,
                            verification_report=json.dumps(verification, ensure_ascii=False, indent=2),
                            review_dir=str(review_dir),
                            artifact_id=str(artifact_before["sha256"]),
                        )
                        guard = _control_guard(
                            run_dir,
                            (worker_path, _verification_path(run_dir, index), review_dir / "artifact.json"),
                        )
                        try:
                            run_agent(
                                run_dir=run_dir,
                                profile_name=reviewer_name,
                                profile=config["profiles"][reviewer_name],
                                role="TASK_REVIEWER",
                                prompt=prompt,
                                prompt_path=review_dir / "reviewer-prompt.md",
                                log_path=review_dir / "reviewer.log",
                                timeout_seconds=timeout,
                                workspace=review_workspace,
                                guard=guard,
                            )
                        finally:
                            agent_error = sys.exc_info()[1]
                            postcondition_errors = []
                            try:
                                _verify_control_guard(run_dir, guard)
                            except HarnessError as error:
                                postcondition_errors.append(str(error))
                            try:
                                _restore_workspace_if_changed(
                                    candidate_workspace,
                                    trusted_candidate,
                                    artifact_before,
                                    "Reviewer",
                                )
                            except HarnessError as error:
                                postcondition_errors.append(str(error))
                            try:
                                _assert_live_workspace_unchanged(run_dir)
                            except HarnessError as error:
                                postcondition_errors.append(str(error))
                            if postcondition_errors:
                                details = "; ".join(postcondition_errors)
                                if agent_error is not None:
                                    details = f"{agent_error}; {details}"
                                raise HarnessError(details) from agent_error
                artifact_after = _workspace_manifest(candidate_workspace)
                if artifact_after["sha256"] != artifact_before["sha256"]:
                    raise HarnessError("Candidate changed after review postconditions completed")
                try:
                    audit = _audit_result(audit_path)
                except HarnessError:
                    _quarantine(audit_path)
                    raise
                _apply_verification_gate(audit, verification)
                audit["artifact_id"] = artifact_before["sha256"]
                write_json(audit_path, audit)
                append_event(run_dir, "review_recorded", index=index, verdict=audit["verdict"])
                if str(audit["verdict"]).upper() == "PASS":
                    _update_state(
                        run_dir,
                        phase="promote",
                        status="PROMOTING",
                        active_agent=None,
                        last_error="",
                    )
                    refresh_report(run_dir)
                    continue
                if index + 1 >= int(config["max_reviews"]):
                    _update_state(
                        run_dir,
                        status="INCOMPLETE",
                        active_agent=None,
                        finished_at=now(),
                        last_error=f"Review limit reached with unresolved issues: {audit.get('summary', '')}",
                    )
                    refresh_report(run_dir)
                    print(f"INCOMPLETE: {run_dir}", flush=True)
                    return 1
                (run_dir / "WORKER_RESULT.json").unlink(missing_ok=True)
                _update_state(run_dir, phase="work", status="REPAIRING", review_index=index + 1)
                refresh_report(run_dir)
        except OperatorPause as error:
            _update_state(run_dir, status="PAUSED", last_error=str(error))
            append_event(run_dir, "run_paused", reason=str(error))
            refresh_report(run_dir)
            print(f"PAUSED: {run_dir}", flush=True)
            return 2
        except WorkerBlocked as error:
            _update_state(run_dir, status="BLOCKED", last_error=str(error))
            append_event(run_dir, "run_blocked", reason=str(error))
            refresh_report(run_dir)
            raise
        except HarnessError as error:
            _update_state(run_dir, status="PAUSED", last_error=str(error))
            append_event(run_dir, "run_failed", error=str(error))
            refresh_report(run_dir)
            raise
        except KeyboardInterrupt as error:
            _update_state(run_dir, status="PAUSED", last_error="Interrupted by the operator")
            append_event(run_dir, "run_paused", reason="KeyboardInterrupt")
            refresh_report(run_dir)
            raise error
        finally:
            try:
                remove_owned_process_marker(marker_path, marker_identity)
                remove_owned_process_marker(legacy_marker_path, legacy_marker_identity)
            except OSError:
                pass


def _request_from_args(args: argparse.Namespace) -> str:
    if args.request_file:
        try:
            return args.request_file.expanduser().resolve().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise HarnessError(f"Could not read request file: {error}") from error
    return str(args.request or "").strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", help="one natural-language task")
    source.add_argument("--request-file", type=Path, help="UTF-8 task file")
    source.add_argument("--resume", type=Path, help="resume one exact generic run")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--coordinator-agent")
    parser.add_argument("--worker-agent")
    parser.add_argument("--reviewer-agent")
    parser.add_argument("--max-reviews", type=int)
    return parser.parse_args()


def main() -> int:
    configure_utf8_stdio()
    args = parse_args()
    if args.resume:
        with file_lock(supervisor_launch_lock_path(args.resume)):
            pass
        supervisor_identity = (os.getpid(), pid_start_time(os.getpid()))
        try:
            return execute_run(args.resume)
        finally:
            remove_owned_process_marker(
                supervisor_marker_path(args.resume), supervisor_identity
            )
            remove_owned_process_marker(args.resume / "harness.pid", supervisor_identity)
    run_dir = create_run(
        _request_from_args(args),
        config_path=args.config,
        runs_dir=args.runs_dir,
        workspace=args.workspace,
        coordinator_agent=args.coordinator_agent,
        worker_agent=args.worker_agent,
        reviewer_agent=args.reviewer_agent,
        max_reviews=args.max_reviews,
    )
    return execute_run(run_dir)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted. The run can be continued from its saved state.", file=sys.stderr)
        raise SystemExit(130)
    except HarnessError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
