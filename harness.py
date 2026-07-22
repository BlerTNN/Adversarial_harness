#!/usr/bin/env python3
"""Generic one-sentence worker/reviewer harness.

The interactive coordinator is any coding-agent TUI started by the user.  This
module only runs configured headless agents against a persistent workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import review_protocol
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
MAX_MANUAL_EVIDENCE_FILES = 100
MAX_MANUAL_EVIDENCE_BYTES = 50 * 1024 * 1024
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


def read_private_json(path: Path, *, maximum_bytes: int | None = None) -> dict[str, Any]:
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or path.is_symlink()
            or _is_reparse_point(path, details)
        ):
            raise HarnessError(f"Structured handoff must be a private regular file: {path}")
        if maximum_bytes is not None and details.st_size > maximum_bytes:
            raise HarnessError(f"Structured handoff exceeds {maximum_bytes} bytes: {path}")
    except OSError as error:
        raise HarnessError(f"Invalid JSON: {path}: {error}") from error
    return read_json(path)


def read_handoff(path: Path) -> dict[str, Any]:
    return read_private_json(path, maximum_bytes=MAX_HANDOFF_BYTES)


def _has_real_parents(path: Path, root: Path) -> bool:
    """Return whether every lexical parent from root to path is a real directory."""
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if path != root and root not in path.parents:
        return False
    try:
        root_details = root.lstat()
        if (
            not stat.S_ISDIR(root_details.st_mode)
            or root.is_symlink()
            or _is_junction(root, root_details)
        ):
            return False
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current /= part
            details = current.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or current.is_symlink()
                or _is_junction(current, details)
            ):
                return False
    except (OSError, ValueError):
        return False
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    review_protocol_version = config.get("review_protocol_version", 1)
    if (
        isinstance(review_protocol_version, bool)
        or not isinstance(review_protocol_version, int)
        or review_protocol_version not in {1, 2}
    ):
        raise HarnessError("review_protocol_version must be 1 or 2")
    if review_protocol_version == 2:
        try:
            review_protocol.validate_review_policy(config.get("review_policy"))
        except review_protocol.ReviewProtocolError as error:
            raise HarnessError(f"Invalid review_policy: {error}") from error
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


def _windows_command_line_error(command: list[str]) -> str:
    if not WINDOWS:
        return ""
    windows_batch = Path(command[0]).suffix.casefold() in {".bat", ".cmd"}
    limit = MAX_WINDOWS_BATCH_COMMAND_LINE if windows_batch else MAX_WINDOWS_COMMAND_LINE
    units = len(format_command(command).encode("utf-16-le")) // 2
    return "Command exceeds the safe Windows command-line limit" if units >= limit else ""


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
    review_protocol_version = int(config.get("review_protocol_version", 1))
    run_config["review_protocol_version"] = review_protocol_version
    if review_protocol_version == 2:
        review_policy = config["review_policy"]
        run_config["review_policy"] = review_policy
        run_config["review_policy_sha256"] = review_protocol.review_policy_sha256(review_policy)
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
    """Agent environment: preserve CLI authentication while isolating Git discovery."""
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


def _review_command_environment(workspace: Path, scratch: Path) -> dict[str, str]:
    """Minimal environment for repository-controlled verification commands."""
    scratch.mkdir(parents=True, exist_ok=True)
    temporary = scratch / "tmp"
    home = scratch / "home"
    temporary.mkdir()
    home.mkdir()
    allowed = {
        "COMSPEC",
        "LANG",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
    }
    environment = {
        name.upper(): value
        for name, value in os.environ.items()
        if name.upper() in allowed or name.upper().startswith("LC_")
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(
        {
            "GIT_CEILING_DIRECTORIES": str(workspace.resolve().parent),
            "HOME": str(home),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "USERPROFILE": str(home),
        }
    )
    return environment


def _control_guard(run_dir: Path, protected_files: tuple[Path, ...] = ()) -> dict[str, Any]:
    if not _is_real_directory(run_dir):
        raise HarnessError(f"Harness run directory must remain a real directory: {run_dir}")
    state = read_json(run_dir / "state.json")
    evidence = [run_dir / "base-artifact.json", run_dir / "request.md", run_dir / "harness.pid"]
    for pattern in (
        "iterations/*/input-artifact.json",
        "iterations/*/output-artifact.json",
        "iterations/*/WORKER_RESULT.json",
        "iterations/*/VERIFICATION.json",
        "reviews/*/artifact.json",
        "reviews/*/REVIEW_PLAN.json",
        "reviews/*/REVIEW_CHECKS.json",
        "reviews/*/harness-evidence/*/RESULT.json",
        "reviews/*/MANUAL_EVIDENCE.json",
        "reviews/*/AUDIT.json",
        "reviews/*/FINAL_REVIEW.json",
    ):
        evidence.extend(sorted(run_dir.glob(pattern)))
    protected = tuple(dict.fromkeys((*protected_files, *evidence)))
    protected_contents: dict[str, bytes] = {}
    for path in protected:
        if not _has_real_parents(path, run_dir):
            raise HarnessError(f"Protected Harness evidence has an unsafe parent directory: {path}")
        try:
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise HarnessError(f"Protected Harness evidence is not a private regular file: {path}")
            protected_contents[str(path)] = path.read_bytes()
        except OSError as error:
            raise HarnessError(f"Protected Harness evidence is unavailable: {path}: {error}") from error
    hashed_files: dict[str, tuple[int, str]] = {}
    for path in sorted(run_dir.glob("reviews/*/harness-evidence/*/*.log")):
        if not _has_real_parents(path, run_dir):
            raise HarnessError(f"Protected Harness log has an unsafe parent directory: {path}")
        try:
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise HarnessError(f"Protected Harness log is not a private regular file: {path}")
            hashed_files[str(path)] = (details.st_size, _file_sha256(path))
        except OSError as error:
            raise HarnessError(f"Protected Harness log is unavailable: {path}: {error}") from error
    return {
        "config": read_json(run_dir / "run-config.json"),
        "state_document": state,
        "state": {field: state.get(field) for field in PROTECTED_STATE_FIELDS},
        "active_agent": state.get("active_agent"),
        "files": protected_contents,
        "hashed_files": hashed_files,
        "harness_evidence_inventories": _harness_evidence_inventories(run_dir),
    }


def _verify_control_guard(run_dir: Path, guard: dict[str, Any]) -> None:
    if not _is_real_directory(run_dir):
        raise HarnessError("Agent modified protected Harness control data: run directory")
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
        if not _has_real_parents(path, run_dir):
            changed.append(_run_relative(path, run_dir))
            continue
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
    for raw_path, expected in guard.get("hashed_files", {}).items():
        path = Path(raw_path)
        if not _has_real_parents(path, run_dir):
            changed.append(_run_relative(path, run_dir))
            continue
        try:
            details = path.lstat()
            actual = (
                (details.st_size, _file_sha256(path))
                if stat.S_ISREG(details.st_mode) and details.st_nlink == 1
                else None
            )
        except OSError:
            actual = None
        if actual != expected:
            changed.append(_run_relative(path, run_dir))
    try:
        inventories = _harness_evidence_inventories(run_dir)
    except HarnessError:
        inventories = None
    if inventories != guard.get("harness_evidence_inventories", {}):
        changed.append("reviews/*/harness-evidence inventory")
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
    if _windows_command_line_error(command):
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
    result = read_handoff(current)
    archived = run_dir / "iterations" / f"{index:02d}" / "WORKER_RESULT.json"
    write_json(archived, result)
    current.unlink(missing_ok=True)
    return archived


def _quarantine(path: Path) -> None:
    """Keep an invalid handoff for diagnosis while allowing `continue` to retry."""
    if not os.path.lexists(path):
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
        verification_environment = _review_command_environment(
            verification_workspace,
            Path(temporary) / "runtime",
        )
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
                        windows_error = _windows_command_line_error(launch_command)
                        if windows_error:
                            process = None
                            error_text = windows_error
                            log.write(error_text + "\n")
                        else:
                            try:
                                process = spawn_managed_process(
                                    launch_command,
                                    cwd=verification_workspace,
                                    env=verification_environment,
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
    report = read_private_json(path)
    if report.get("schema_version") != VERIFICATION_SCHEMA or report.get("artifact_id") != artifact_id:
        raise HarnessError(f"Deterministic verification does not match the reviewed artifact: {path}")
    if report.get("status") not in {"pass", "fail"} or not isinstance(report.get("commands"), list):
        raise HarnessError(f"Invalid deterministic verification report: {path}")
    commands = report["commands"]
    if not commands:
        raise HarnessError(f"Invalid deterministic verification command result: {path}")
    for item in commands:
        if (
            not isinstance(item, dict)
            or item.get("status") not in {"pass", "fail"}
            or not isinstance(item.get("argv"), list)
            or not item["argv"]
            or not all(isinstance(argument, str) and argument for argument in item["argv"])
            or not all(isinstance(item.get(field), str) for field in ("name", "command", "details", "log"))
            or item.get("command") != format_command(item["argv"])
            or not isinstance(item.get("timed_out"), bool)
            or (
                item.get("returncode") is not None
                and (
                    isinstance(item.get("returncode"), bool)
                    or not isinstance(item.get("returncode"), int)
                )
            )
            or isinstance(item.get("duration_seconds"), bool)
            or not isinstance(item.get("duration_seconds"), (int, float))
            or (
                isinstance(item.get("duration_seconds"), float)
                and not math.isfinite(item["duration_seconds"])
            )
            or item["duration_seconds"] < 0
        ):
            raise HarnessError(f"Invalid deterministic verification command result: {path}")
        expected_status = (
            "pass"
            if item["returncode"] == 0 and not item["timed_out"]
            else "fail"
        )
        if item["status"] != expected_status:
            raise HarnessError(f"Inconsistent deterministic verification command result: {path}")
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


def _review_dir(run_dir: Path, index: int) -> Path:
    return run_dir / "reviews" / f"{index:02d}"


def _review_plan_path(run_dir: Path, index: int) -> Path:
    return _review_dir(run_dir, index) / "REVIEW_PLAN.json"


def _review_checks_path(run_dir: Path, index: int) -> Path:
    return _review_dir(run_dir, index) / "REVIEW_CHECKS.json"


def _final_review_path(run_dir: Path, index: int) -> Path:
    return _review_dir(run_dir, index) / "FINAL_REVIEW.json"


def _review_protocol_version(config: dict[str, Any]) -> int:
    value = config.get("review_protocol_version", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value not in {1, 2}:
        raise HarnessError("Run has an invalid review_protocol_version")
    return value


def _review_policy(config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        policy = review_protocol.validate_review_policy(config.get("review_policy"))
        digest = review_protocol.review_policy_sha256(policy)
    except review_protocol.ReviewProtocolError as error:
        raise HarnessError(f"Run has an invalid Review v2 policy: {error}") from error
    if config.get("review_policy_sha256") != digest:
        raise HarnessError("Run Review v2 policy fingerprint does not match its snapshotted policy")
    return policy, digest


def _derived_worker_claims(worker_result: dict[str, Any]) -> list[dict[str, str]]:
    claims = [
        {
            "id": "CLAIM-SUMMARY",
            "statement": str(worker_result["summary"]),
        }
    ]
    for index, check in enumerate(worker_result["checks"], start=1):
        claims.append(
            {
                "id": f"CLAIM-CHECK-{index:03d}",
                "statement": (
                    f"{check['name']} [{str(check['status']).lower()}]: "
                    f"{check['details']}"
                ),
            }
        )
    return claims


def _read_review_plan_v2(
    run_dir: Path,
    index: int,
    *,
    state: dict[str, Any],
    config: dict[str, Any],
    artifact_id: str,
    worker_result: dict[str, Any],
) -> dict[str, Any]:
    path = _review_plan_path(run_dir, index)
    plan = read_handoff(path)
    policy, policy_digest = _review_policy(config)
    try:
        plan = review_protocol.validate_review_plan(
            plan,
            artifact_id=artifact_id,
            round_index=index,
            policy=policy,
            policy_sha256=policy_digest,
            authoritative_request=str(state["request"]),
            worker_claims=_derived_worker_claims(worker_result),
        )
    except review_protocol.ReviewProtocolError as error:
        raise HarnessError(f"Invalid structured review plan: {path}: {error}") from error
    changed = False
    for check in plan["checks"]:
        if check["kind"] != "command":
            continue
        for step in check["steps"]:
            for argument_index, argument in enumerate(step):
                if argument == "{python}":
                    step[argument_index] = sys.executable
                    changed = True
    if changed:
        write_json(path, plan)
    return plan


def _empty_log(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    set_private_permissions(path)
    return hashlib.sha256(b"").hexdigest()


def _capture_bounded_pipe(
    stream: Any,
    path: Path,
    limit: int,
    result: dict[str, Any],
) -> None:
    digest = hashlib.sha256()
    written = 0
    truncated = False
    try:
        with path.open("wb") as output:
            set_private_permissions(path)
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = max(0, limit - written)
                recorded = chunk[:remaining]
                if recorded:
                    output.write(recorded)
                    digest.update(recorded)
                    written += len(recorded)
                if len(recorded) != len(chunk):
                    truncated = True
        result.update({"sha256": digest.hexdigest(), "truncated": truncated})
    except BaseException as error:
        result["error"] = error
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _review_check_log_path(review_dir: Path, check_id: str, step: int, stream: str) -> Path:
    return review_dir / "harness-evidence" / check_id / f"step-{step:02d}.{stream}.log"


def _review_check_step(
    *,
    run_dir: Path,
    review_dir: Path,
    check: dict[str, Any],
    step_index: int,
    workspace: Path,
    environment: dict[str, str],
    deadline: float,
    log_limit: int,
) -> dict[str, Any]:
    argv = list(check["steps"][step_index])
    launch = list(argv)
    stdout_path = _review_check_log_path(review_dir, check["id"], step_index + 1, "stdout")
    stderr_path = _review_check_log_path(review_dir, check["id"], step_index + 1, "stderr")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process: subprocess.Popen[Any] | None = None
    returncode: int | None = None
    timed_out = False
    error_text = ""
    stdout_result: dict[str, Any] = {}
    stderr_result: dict[str, Any] = {}

    executable = resolve_program(launch[0], cwd=workspace)
    if executable is None:
        error_text = f"Review check executable is unavailable: {launch[0]}"
    else:
        launch[0] = executable
        if _windows_batch_argv_has_metacharacters(launch):
            error_text = (
                "Windows batch review-check argv contains cmd.exe metacharacters; "
                "use a native executable or remove the metacharacters"
            )
        else:
            error_text = _windows_command_line_error(launch)
    if error_text:
        stdout_sha = _empty_log(stdout_path)
        stderr_sha = _empty_log(stderr_path)
        return {
            "argv": argv,
            "returncode": None,
            "timed_out": False,
            "stdout_path": _run_relative(stdout_path, review_dir),
            "stderr_path": _run_relative(stderr_path, review_dir),
            "stdout_sha256": stdout_sha,
            "stderr_sha256": stderr_sha,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": error_text,
        }

    try:
        process = spawn_managed_process(
            launch,
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, RuntimeError) as error:
        error_text = str(error)
        stdout_sha = _empty_log(stdout_path)
        stderr_sha = _empty_log(stderr_path)
        return {
            "argv": argv,
            "returncode": None,
            "timed_out": False,
            "stdout_path": _run_relative(stdout_path, review_dir),
            "stderr_path": _run_relative(stderr_path, review_dir),
            "stdout_sha256": stdout_sha,
            "stderr_sha256": stderr_sha,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": error_text,
        }

    process_started = managed_process_start_time(process)
    if not process_started:
        try:
            _terminate(process)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise HarnessError("Could not establish a safe process identity for a planned review check")

    threads = [
        threading.Thread(
            target=_capture_bounded_pipe,
            args=(process.stdout, stdout_path, log_limit, stdout_result),
            daemon=True,
        ),
        threading.Thread(
            target=_capture_bounded_pipe,
            args=(process.stderr, stderr_path, log_limit, stderr_result),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    active = {
        "profile": "harness",
        "role": "REVIEW_CHECK",
        "pid": process.pid,
        "process_group": process.pid,
        "pid_started": process_started,
        "log": _run_relative(stdout_path, run_dir),
        "started_at": now(),
    }
    paused = False
    try:
        _update_state(run_dir, active_agent=active)
        append_event(
            run_dir,
            "review_check_started",
            check_id=check["id"],
            step=step_index + 1,
            pid=process.pid,
        )
        while process.poll() is None:
            if pause_requested(run_dir):
                paused = True
                _terminate(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate(process)
                break
            try:
                process.wait(timeout=max(0.01, min(0.1, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        returncode = process.wait()
        _terminate(process, grace=1.0)
        _update_state(run_dir, active_agent=None)
    except BaseException:
        try:
            _terminate(process)
        except BaseException:
            try:
                _update_state(run_dir, active_agent=active)
            except (OSError, HarnessError):
                pass
            raise
        try:
            _update_state(run_dir, active_agent=None)
        except (OSError, HarnessError):
            pass
        raise
    finally:
        for thread in threads:
            thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads):
        raise HarnessError("Could not finish capturing planned review-check output")
    for result in (stdout_result, stderr_result):
        if "error" in result:
            raise HarnessError(f"Could not record planned review-check output: {result['error']}")
    append_event(
        run_dir,
        "review_check_finished",
        check_id=check["id"],
        step=step_index + 1,
        returncode=returncode,
        timed_out=timed_out,
    )
    if paused:
        raise OperatorPause("A planned review check was stopped at the user's request.")
    return {
        "argv": argv,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_path": _run_relative(stdout_path, review_dir),
        "stderr_path": _run_relative(stderr_path, review_dir),
        "stdout_sha256": stdout_result["sha256"],
        "stderr_sha256": stderr_result["sha256"],
        "stdout_truncated": stdout_result["truncated"],
        "stderr_truncated": stderr_result["truncated"],
        "duration_seconds": round(time.monotonic() - started, 3),
        "error": error_text,
    }


def _run_planned_review_checks(
    run_dir: Path,
    index: int,
    workspace: Path,
    artifact: dict[str, Any],
    plan: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    review_dir = _review_dir(run_dir, index)
    evidence_root = review_dir / "harness-evidence"
    _quarantine(evidence_root)
    checks_path = _review_checks_path(run_dir, index)
    _quarantine(checks_path)
    policy, policy_digest = _review_policy(config)
    plan_digest = review_protocol.plan_sha256(plan)
    total_deadline = time.monotonic() + policy["total_check_timeout_seconds"]
    results: list[dict[str, Any]] = []
    command_checks = [check for check in plan["checks"] if check["kind"] == "command"]
    with _guard_candidate_workspace(workspace, artifact, "Review check runner") as trusted_workspace:
        for check in command_checks:
            _assert_live_workspace_unchanged(run_dir)
            check_started = time.monotonic()
            started_at = now()
            steps: list[dict[str, Any]] = []
            status = "pass"
            details = "All planned steps met their expected exit codes."
            if time.monotonic() >= total_deadline:
                status = "not_run"
                details = "The total planned review-check budget was exhausted before this check started."
            else:
                with tempfile.TemporaryDirectory(prefix="harness-review-check-") as temporary:
                    check_root = Path(temporary)
                    check_workspace = check_root / "workspace"
                    _copy_workspace(trusted_workspace, check_workspace)
                    if _workspace_manifest(check_workspace)["sha256"] != artifact["sha256"]:
                        raise HarnessError("Candidate changed while a planned review-check snapshot was being created")
                    environment = _review_command_environment(check_workspace, check_root / "runtime")
                    check_deadline = min(
                        total_deadline,
                        time.monotonic() + int(check["timeout_seconds"]),
                    )
                    allowed = set(check["expected"]["exit_codes"])
                    for step_index in range(len(check["steps"])):
                        step = _review_check_step(
                            run_dir=run_dir,
                            review_dir=review_dir,
                            check=check,
                            step_index=step_index,
                            workspace=check_workspace,
                            environment=environment,
                            deadline=check_deadline,
                            log_limit=policy["max_log_bytes_per_step"],
                        )
                        steps.append(step)
                        if step["error"]:
                            status = "error"
                            details = step["error"]
                            break
                        if step["timed_out"]:
                            status = "fail"
                            details = "A planned review-check step exceeded its timeout."
                            break
                        if step["returncode"] not in allowed:
                            status = "fail"
                            details = f"A planned review-check step exited with status {step['returncode']}."
                            break
            finished_at = now()
            result = {
                "schema_version": review_protocol.CHECK_RESULT_SCHEMA,
                "artifact_id": artifact["sha256"],
                "policy_sha256": policy_digest,
                "plan_sha256": plan_digest,
                "check_id": check["id"],
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": round(time.monotonic() - check_started, 3),
                "steps": steps,
                "details": details,
            }
            result_path = evidence_root / check["id"] / "RESULT.json"
            write_json(result_path, result)
            results.append(result)
            _assert_live_workspace_unchanged(run_dir)
    bundle = {
        "schema_version": review_protocol.CHECKS_SCHEMA,
        "artifact_id": artifact["sha256"],
        "policy_sha256": policy_digest,
        "plan_sha256": plan_digest,
        "round": index,
        "results": results,
    }
    write_json(checks_path, bundle)
    return _read_review_checks_v2(
        run_dir,
        index,
        plan=plan,
        artifact_id=str(artifact["sha256"]),
        config=config,
    )


def _review_evidence_path(review_dir: Path, relative: str, *, prefix: str) -> Path:
    try:
        root_details = review_dir.lstat()
    except OSError as error:
        raise HarnessError(f"Review evidence directory is unavailable: {review_dir}: {error}") from error
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or review_dir.is_symlink()
        or _is_junction(review_dir, root_details)
    ):
        raise HarnessError(f"Review evidence directory must be a real directory: {review_dir}")
    candidate = Path(relative)
    if (
        candidate.anchor
        or candidate == Path(".")
        or ".." in candidate.parts
        or any(":" in part for part in candidate.parts)
    ):
        raise HarnessError(f"Unsafe review evidence path: {relative}")
    normalized = candidate.as_posix()
    if not normalized.startswith(prefix + "/"):
        raise HarnessError(f"Review evidence must be stored under {prefix}/: {relative}")
    path = Path(os.path.abspath(review_dir / candidate))
    lexical_root = Path(os.path.abspath(review_dir))
    if lexical_root not in path.parents:
        raise HarnessError(f"Review evidence escapes its authorized directory: {relative}")
    parent = lexical_root
    for part in candidate.parts[:-1]:
        parent /= part
        try:
            details = parent.lstat()
        except OSError as error:
            raise HarnessError(f"Review evidence parent is unavailable: {relative}: {error}") from error
        if not stat.S_ISDIR(details.st_mode) or parent.is_symlink() or _is_junction(parent, details):
            raise HarnessError(f"Review evidence parent must be a real directory: {relative}")
    return path


def _verified_evidence_file(
    review_dir: Path,
    relative: str,
    *,
    prefix: str,
    expected_sha256: str | None = None,
    maximum_bytes: int,
) -> tuple[Path, os.stat_result, str]:
    path = _review_evidence_path(review_dir, relative, prefix=prefix)
    try:
        details = path.lstat()
    except OSError as error:
        raise HarnessError(f"Review evidence is unavailable: {relative}: {error}") from error
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or path.is_symlink()
        or _is_reparse_point(path, details)
    ):
        raise HarnessError(f"Review evidence must be a private regular file: {relative}")
    if details.st_size > maximum_bytes:
        raise HarnessError(f"Review evidence exceeds its size limit: {relative}")
    digest = _file_sha256(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise HarnessError(f"Review evidence hash does not match its Harness result: {relative}")
    return path, details, digest


def _review_evidence_inventory(review_dir: Path, prefix: str) -> set[str]:
    root = review_dir / prefix
    if not os.path.lexists(root):
        return set()
    try:
        root_details = root.lstat()
    except OSError as error:
        raise HarnessError(f"Review evidence directory is unavailable: {root}: {error}") from error
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or root.is_symlink()
        or _is_junction(root, root_details)
    ):
        raise HarnessError(f"Review evidence directory must be a real directory: {root}")
    files: set[str] = set()
    try:
        for current_raw, directories, names in os.walk(root, followlinks=False):
            current = Path(current_raw)
            for name in directories:
                path = current / name
                details = path.lstat()
                if (
                    not stat.S_ISDIR(details.st_mode)
                    or path.is_symlink()
                    or _is_junction(path, details)
                ):
                    raise HarnessError(f"Review evidence contains an unsafe directory: {path}")
            for name in names:
                path = current / name
                details = path.lstat()
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_nlink != 1
                    or path.is_symlink()
                    or _is_reparse_point(path, details)
                ):
                    raise HarnessError(f"Review evidence must be a private regular file: {path}")
                files.add(path.relative_to(review_dir).as_posix())
    except OSError as error:
        raise HarnessError(f"Could not inventory Harness review evidence: {root}: {error}") from error
    return files


def _harness_evidence_inventories(run_dir: Path) -> dict[str, set[str]]:
    reviews_root = run_dir / "reviews"
    if not os.path.lexists(reviews_root):
        return {}
    try:
        reviews_details = reviews_root.lstat()
        if (
            not stat.S_ISDIR(reviews_details.st_mode)
            or reviews_root.is_symlink()
            or _is_junction(reviews_root, reviews_details)
        ):
            raise HarnessError(f"Review records directory must be a real directory: {reviews_root}")
        inventories = {}
        for review_dir in reviews_root.iterdir():
            review_details = review_dir.lstat()
            if (
                not stat.S_ISDIR(review_details.st_mode)
                or review_dir.is_symlink()
                or _is_junction(review_dir, review_details)
            ):
                raise HarnessError(f"Review record must be a real directory: {review_dir}")
            evidence_root = review_dir / "harness-evidence"
            if os.path.lexists(evidence_root):
                inventories[_run_relative(evidence_root, run_dir)] = _review_evidence_inventory(
                    review_dir,
                    "harness-evidence",
                )
    except OSError as error:
        raise HarnessError(f"Could not inventory Harness review records: {error}") from error
    return inventories


def _read_review_checks_v2(
    run_dir: Path,
    index: int,
    *,
    plan: dict[str, Any],
    artifact_id: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    path = _review_checks_path(run_dir, index)
    bundle = read_handoff(path)
    policy, policy_digest = _review_policy(config)
    try:
        bundle = review_protocol.validate_review_checks(
            bundle,
            plan=plan,
            artifact_id=artifact_id,
            policy_sha256=policy_digest,
        )
    except review_protocol.ReviewProtocolError as error:
        raise HarnessError(f"Invalid Harness-owned review-check evidence: {path}: {error}") from error
    review_dir = _review_dir(run_dir, index)
    expected_files: set[str] = set()
    for result in bundle["results"]:
        result_relative = f"harness-evidence/{result['check_id']}/RESULT.json"
        expected_files.add(result_relative)
        result_path, _details, _digest = _verified_evidence_file(
            review_dir,
            result_relative,
            prefix="harness-evidence",
            maximum_bytes=MAX_HANDOFF_BYTES,
        )
        if read_handoff(result_path) != result:
            raise HarnessError(f"Review check result does not match its aggregate record: {result_path}")
        for step in result["steps"]:
            expected_files.update((step["stdout_path"], step["stderr_path"]))
            _verified_evidence_file(
                review_dir,
                step["stdout_path"],
                prefix="harness-evidence",
                expected_sha256=step["stdout_sha256"],
                maximum_bytes=policy["max_log_bytes_per_step"],
            )
            _verified_evidence_file(
                review_dir,
                step["stderr_path"],
                prefix="harness-evidence",
                expected_sha256=step["stderr_sha256"],
                maximum_bytes=policy["max_log_bytes_per_step"],
            )
    if _review_evidence_inventory(review_dir, "harness-evidence") != expected_files:
        raise HarnessError("Harness review evidence contains missing or unexpected files")
    return bundle


def _run_review_agent_stage(
    *,
    run_dir: Path,
    candidate_workspace: Path,
    artifact: dict[str, Any],
    reviewer_name: str,
    config: dict[str, Any],
    timeout: int,
    prompt_name: str,
    prompt_values: dict[str, str],
    prompt_path: Path,
    log_path: Path,
    protected_files: tuple[Path, ...],
    actor: str,
) -> None:
    if not _is_real_directory(candidate_workspace):
        raise HarnessError("The isolated candidate workspace is unavailable")
    _assert_live_workspace_unchanged(run_dir)
    with tempfile.TemporaryDirectory(prefix="harness-review-guard-") as guard_temporary:
        trusted_candidate = Path(guard_temporary) / "candidate"
        _copy_workspace(candidate_workspace, trusted_candidate)
        if _workspace_manifest(trusted_candidate)["sha256"] != artifact["sha256"]:
            raise HarnessError(f"Candidate changed while the trusted {actor.lower()} snapshot was being created")
        with tempfile.TemporaryDirectory(prefix="harness-review-") as temporary:
            review_workspace = Path(temporary) / "workspace"
            _copy_workspace(trusted_candidate, review_workspace)
            if _workspace_manifest(review_workspace)["sha256"] != artifact["sha256"]:
                raise HarnessError(f"Candidate changed while the {actor.lower()} snapshot was being created")
            prompt = render_prompt(
                prompt_name,
                workspace=str(review_workspace),
                **prompt_values,
            )
            guard = _control_guard(run_dir, protected_files)
            try:
                run_agent(
                    run_dir=run_dir,
                    profile_name=reviewer_name,
                    profile=config["profiles"][reviewer_name],
                    role="TASK_REVIEWER",
                    prompt=prompt,
                    prompt_path=prompt_path,
                    log_path=log_path,
                    timeout_seconds=timeout,
                    workspace=review_workspace,
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
                    _restore_workspace_if_changed(
                        candidate_workspace,
                        trusted_candidate,
                        artifact,
                        actor,
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


def _read_audit_v2(
    run_dir: Path,
    index: int,
    *,
    plan: dict[str, Any],
    checks: dict[str, Any],
) -> dict[str, Any]:
    path = _review_dir(run_dir, index) / "AUDIT.json"
    audit = read_handoff(path)
    try:
        return review_protocol.validate_audit_v2(audit, plan=plan, checks=checks)
    except review_protocol.ReviewProtocolError as error:
        raise HarnessError(f"Invalid Review v2 audit: {path}: {error}") from error


def _record_manual_evidence(
    run_dir: Path,
    index: int,
    *,
    plan: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    review_dir = _review_dir(run_dir, index)
    planned_check_ids = {check["id"] for check in plan["checks"]}
    references = {
        reference
        for check in audit["checks"]
        for reference in check["evidence_refs"]
    }
    references.update(
        reference
        for issue in audit["issues"]
        for reference in issue["evidence_refs"]
        if reference not in planned_check_ids
    )
    if len(references) > MAX_MANUAL_EVIDENCE_FILES:
        raise HarnessError("Reviewer cited too many persistent evidence files")
    files: list[dict[str, Any]] = []
    total = 0
    for reference in sorted(references):
        _path, details, digest = _verified_evidence_file(
            review_dir,
            reference,
            prefix="reviewer-evidence",
            maximum_bytes=MAX_MANUAL_EVIDENCE_BYTES,
        )
        total += details.st_size
        if total > MAX_MANUAL_EVIDENCE_BYTES:
            raise HarnessError("Reviewer evidence exceeds the total size limit")
        files.append({"path": Path(reference).as_posix(), "size": details.st_size, "sha256": digest})
    manifest = {
        "schema_version": "generic-harness/manual-evidence/v1",
        "artifact_id": plan["artifact_id"],
        "plan_sha256": review_protocol.plan_sha256(plan),
        "files": files,
    }
    write_json(review_dir / "MANUAL_EVIDENCE.json", manifest)
    return manifest


def _read_manual_evidence(
    run_dir: Path,
    index: int,
    *,
    plan: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    review_dir = _review_dir(run_dir, index)
    path = review_dir / "MANUAL_EVIDENCE.json"
    manifest = read_handoff(path)
    expected_keys = {"schema_version", "artifact_id", "plan_sha256", "files"}
    if set(manifest) != expected_keys:
        raise HarnessError(f"Invalid manual evidence manifest fields: {path}")
    if (
        manifest["schema_version"] != "generic-harness/manual-evidence/v1"
        or manifest["artifact_id"] != plan["artifact_id"]
        or manifest["plan_sha256"] != review_protocol.plan_sha256(plan)
        or not isinstance(manifest["files"], list)
    ):
        raise HarnessError(f"Manual evidence manifest identity is invalid: {path}")
    planned_check_ids = {check["id"] for check in plan["checks"]}
    required = {
        reference
        for check in audit["checks"]
        for reference in check["evidence_refs"]
    }
    required.update(
        reference
        for issue in audit["issues"]
        for reference in issue["evidence_refs"]
        if reference not in planned_check_ids
    )
    recorded: set[str] = set()
    total = 0
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise HarnessError(f"Invalid manual evidence entry: {path}")
        relative = item["path"]
        if not isinstance(relative, str) or relative in recorded:
            raise HarnessError(f"Duplicate or invalid manual evidence path: {path}")
        _evidence_path, details, digest = _verified_evidence_file(
            review_dir,
            relative,
            prefix="reviewer-evidence",
            expected_sha256=str(item["sha256"]),
            maximum_bytes=MAX_MANUAL_EVIDENCE_BYTES,
        )
        if item["size"] != details.st_size or digest != item["sha256"]:
            raise HarnessError(f"Manual evidence no longer matches its manifest: {relative}")
        total += details.st_size
        recorded.add(relative)
    if recorded != required or total > MAX_MANUAL_EVIDENCE_BYTES:
        raise HarnessError("Manual evidence manifest does not match the audit references")
    return manifest


def _adjudicate_review_v2(
    run_dir: Path,
    index: int,
    *,
    state: dict[str, Any],
    config: dict[str, Any],
    artifact: dict[str, Any],
    worker_result: dict[str, Any],
    write_result: bool,
) -> dict[str, Any]:
    artifact_id = str(artifact["sha256"])
    plan = _read_review_plan_v2(
        run_dir,
        index,
        state=state,
        config=config,
        artifact_id=artifact_id,
        worker_result=worker_result,
    )
    checks = _read_review_checks_v2(
        run_dir,
        index,
        plan=plan,
        artifact_id=artifact_id,
        config=config,
    )
    audit = _read_audit_v2(run_dir, index, plan=plan, checks=checks)
    manual = _read_manual_evidence(run_dir, index, plan=plan, audit=audit)
    verification = _read_verification(
        run_dir,
        index,
        artifact_id,
        config["verification_commands"],
    )
    policy, policy_digest = _review_policy(config)
    verdict, reason_codes = review_protocol.adjudicate_review(
        plan=plan,
        checks=checks,
        audit=audit,
        deterministic_verification_passed=verification["status"] == "pass",
        policy=policy,
    )
    review_dir = _review_dir(run_dir, index)
    evidence_paths = {
        _run_relative(review_dir / "artifact.json", run_dir),
        _run_relative(_verification_path(run_dir, index), run_dir),
        _run_relative(_review_plan_path(run_dir, index), run_dir),
        _run_relative(_review_checks_path(run_dir, index), run_dir),
        _run_relative(review_dir / "AUDIT.json", run_dir),
        _run_relative(review_dir / "MANUAL_EVIDENCE.json", run_dir),
    }
    for path in review_dir.glob("harness-evidence/*/*"):
        if path.is_file():
            evidence_paths.add(_run_relative(path, run_dir))
    for item in manual["files"]:
        evidence_paths.add(_run_relative(review_dir / item["path"], run_dir))
    final = {
        "schema_version": review_protocol.FINAL_SCHEMA,
        "artifact_id": artifact_id,
        "policy_sha256": policy_digest,
        "plan_sha256": review_protocol.plan_sha256(plan),
        "round": index,
        "verdict": verdict,
        "reason_codes": reason_codes,
        "plan_path": _run_relative(_review_plan_path(run_dir, index), run_dir),
        "checks_path": _run_relative(_review_checks_path(run_dir, index), run_dir),
        "audit_path": _run_relative(review_dir / "AUDIT.json", run_dir),
        "verification_path": _run_relative(_verification_path(run_dir, index), run_dir),
        "manual_evidence_path": _run_relative(review_dir / "MANUAL_EVIDENCE.json", run_dir),
        "evidence_paths": sorted(evidence_paths),
        "decided_at": now(),
    }
    if write_result:
        write_json(_final_review_path(run_dir, index), final)
    return final


def _read_final_review_v2(
    run_dir: Path,
    index: int,
    *,
    state: dict[str, Any],
    config: dict[str, Any],
    artifact: dict[str, Any],
    worker_result: dict[str, Any],
) -> dict[str, Any]:
    path = _final_review_path(run_dir, index)
    final = read_handoff(path)
    _policy, policy_digest = _review_policy(config)
    plan = _read_review_plan_v2(
        run_dir,
        index,
        state=state,
        config=config,
        artifact_id=str(artifact["sha256"]),
        worker_result=worker_result,
    )
    try:
        final = review_protocol.validate_final_review(
            final,
            artifact_id=str(artifact["sha256"]),
            round_index=index,
            policy_sha256=policy_digest,
            plan_sha256_value=review_protocol.plan_sha256(plan),
        )
    except review_protocol.ReviewProtocolError as error:
        raise HarnessError(f"Invalid Harness final review: {path}: {error}") from error
    recalculated = _adjudicate_review_v2(
        run_dir,
        index,
        state=state,
        config=config,
        artifact=artifact,
        worker_result=worker_result,
        write_result=False,
    )
    for field in (
        "schema_version",
        "artifact_id",
        "policy_sha256",
        "plan_sha256",
        "round",
        "verdict",
        "reason_codes",
        "plan_path",
        "checks_path",
        "audit_path",
        "verification_path",
        "manual_evidence_path",
        "evidence_paths",
    ):
        if final[field] != recalculated[field]:
            raise HarnessError(f"Final review no longer matches its underlying evidence: {field}")
    return final


def _review_artifact_v2(run_dir: Path, index: int, candidate_workspace: Path) -> dict[str, Any]:
    path = _review_dir(run_dir, index) / "artifact.json"
    artifact = read_private_json(path)
    current = _workspace_manifest(candidate_workspace)
    if artifact != current or artifact.get("schema_version") != ARTIFACT_SCHEMA:
        raise HarnessError("Review v2 artifact identity no longer matches the candidate workspace")
    return artifact


def _review_feedback_v2(run_dir: Path, index: int) -> str:
    review_dir = _review_dir(run_dir, index)
    sections = []
    for label, path in (
        ("Harness final review", review_dir / "FINAL_REVIEW.json"),
        ("Independent audit", review_dir / "AUDIT.json"),
        ("Harness review checks", review_dir / "REVIEW_CHECKS.json"),
        ("Review plan", review_dir / "REVIEW_PLAN.json"),
    ):
        sections.append(f"## {label}\n\n{path.read_text(encoding='utf-8') if path.is_file() else 'Unavailable.'}")
    return "\n\n".join(sections)


def _execute_review_v2_stage(
    run_dir: Path,
    *,
    state: dict[str, Any],
    config: dict[str, Any],
    candidate_workspace: Path,
    reviewer_name: str,
    timeout: int,
) -> int | None:
    index = int(state.get("review_index", 0))
    phase = str(state.get("phase", ""))
    review_dir = _review_dir(run_dir, index)
    worker_path, worker_result = _worker_result(run_dir, index)
    policy, policy_digest = _review_policy(config)

    if phase == "review_plan":
        _assert_live_workspace_unchanged(run_dir)
        artifact = _workspace_manifest(candidate_workspace)
        for path in (
            review_dir / "artifact.json",
            review_dir / "REVIEW_PLAN.json",
            review_dir / "REVIEW_CHECKS.json",
            review_dir / "AUDIT.json",
            review_dir / "MANUAL_EVIDENCE.json",
            review_dir / "FINAL_REVIEW.json",
            review_dir / "harness-evidence",
            review_dir / "reviewer-evidence",
        ):
            _quarantine(path)
        write_json(review_dir / "artifact.json", artifact)
        verification = _read_verification(
            run_dir,
            index,
            str(artifact["sha256"]),
            config["verification_commands"],
        )
        claims = _derived_worker_claims(worker_result)
        subjects = {
            "canonical_requirement": {
                "id": "REQ-REQUEST",
                "source": "user_request",
                "statement": str(state["request"]),
                "criticality": "must",
            },
            "worker_claims": claims,
        }
        _update_state(
            run_dir,
            status="PLANNING_REVIEW",
            artifact_id=artifact["sha256"],
            artifact_path=_run_relative(review_dir / "artifact.json", run_dir),
        )
        append_event(run_dir, "review_plan_started", index=index, artifact_id=artifact["sha256"])
        _run_review_agent_stage(
            run_dir=run_dir,
            candidate_workspace=candidate_workspace,
            artifact=artifact,
            reviewer_name=reviewer_name,
            config=config,
            timeout=timeout,
            prompt_name="review_planner",
            prompt_values={
                "request": str(state["request"]),
                "run_dir": str(run_dir),
                "worker_report": f"Path: {worker_path}\n\n{json.dumps(worker_result, ensure_ascii=False, indent=2)}",
                "verification_report": json.dumps(verification, ensure_ascii=False, indent=2),
                "review_dir": str(review_dir),
                "artifact_id": str(artifact["sha256"]),
                "review_round": str(index),
                "review_policy": json.dumps(policy, ensure_ascii=False, indent=2),
                "policy_sha256": policy_digest,
                "review_subjects": json.dumps(subjects, ensure_ascii=False, indent=2),
            },
            prompt_path=review_dir / "planner-prompt.md",
            log_path=review_dir / "planner.log",
            protected_files=(worker_path, _verification_path(run_dir, index), review_dir / "artifact.json"),
            actor="Review planner",
        )
        plan = _read_review_plan_v2(
            run_dir,
            index,
            state=state,
            config=config,
            artifact_id=str(artifact["sha256"]),
            worker_result=worker_result,
        )
        append_event(
            run_dir,
            "review_plan_recorded",
            index=index,
            plan_sha256=review_protocol.plan_sha256(plan),
            checks=len(plan["checks"]),
        )
        _update_state(run_dir, phase="review_checks", status="RUNNING_REVIEW_CHECKS")
        refresh_report(run_dir)
        return None

    artifact = _review_artifact_v2(run_dir, index, candidate_workspace)
    plan = _read_review_plan_v2(
        run_dir,
        index,
        state=state,
        config=config,
        artifact_id=str(artifact["sha256"]),
        worker_result=worker_result,
    )
    if phase == "review_checks":
        _update_state(run_dir, status="RUNNING_REVIEW_CHECKS")
        append_event(run_dir, "review_checks_started", index=index)
        checks = _run_planned_review_checks(
            run_dir,
            index,
            candidate_workspace,
            artifact,
            plan,
            config,
        )
        append_event(
            run_dir,
            "review_checks_recorded",
            index=index,
            pass_count=sum(result["status"] == "pass" for result in checks["results"]),
            fail_count=sum(result["status"] == "fail" for result in checks["results"]),
            error_count=sum(result["status"] == "error" for result in checks["results"]),
            not_run_count=sum(result["status"] == "not_run" for result in checks["results"]),
        )
        _update_state(run_dir, phase="review_assess", status="REVIEWING")
        refresh_report(run_dir)
        return None

    checks = _read_review_checks_v2(
        run_dir,
        index,
        plan=plan,
        artifact_id=str(artifact["sha256"]),
        config=config,
    )
    if phase == "review_assess":
        for path in (
            review_dir / "AUDIT.json",
            review_dir / "MANUAL_EVIDENCE.json",
            review_dir / "FINAL_REVIEW.json",
            review_dir / "reviewer-evidence",
        ):
            _quarantine(path)
        verification = _read_verification(
            run_dir,
            index,
            str(artifact["sha256"]),
            config["verification_commands"],
        )
        _update_state(run_dir, status="REVIEWING")
        append_event(run_dir, "review_assessment_started", index=index)
        try:
            _run_review_agent_stage(
                run_dir=run_dir,
                candidate_workspace=candidate_workspace,
                artifact=artifact,
                reviewer_name=reviewer_name,
                config=config,
                timeout=timeout,
                prompt_name="reviewer_v2",
                prompt_values={
                    "request": str(state["request"]),
                    "run_dir": str(run_dir),
                    "worker_report": f"Path: {worker_path}\n\n{json.dumps(worker_result, ensure_ascii=False, indent=2)}",
                    "verification_report": json.dumps(verification, ensure_ascii=False, indent=2),
                    "review_dir": str(review_dir),
                    "artifact_id": str(artifact["sha256"]),
                    "review_plan": json.dumps(plan, ensure_ascii=False, indent=2),
                    "review_checks": json.dumps(checks, ensure_ascii=False, indent=2),
                    "plan_sha256": review_protocol.plan_sha256(plan),
                },
                prompt_path=review_dir / "reviewer-prompt.md",
                log_path=review_dir / "reviewer.log",
                protected_files=(
                    worker_path,
                    _verification_path(run_dir, index),
                    review_dir / "artifact.json",
                    review_dir / "REVIEW_PLAN.json",
                    review_dir / "REVIEW_CHECKS.json",
                ),
                actor="Reviewer",
            )
        except HarnessError:
            _update_state(run_dir, phase="review_checks", status="PAUSED")
            raise
        audit = _read_audit_v2(run_dir, index, plan=plan, checks=checks)
        _record_manual_evidence(run_dir, index, plan=plan, audit=audit)
        append_event(run_dir, "review_audit_recorded", index=index, verdict=audit["verdict"])
        _update_state(run_dir, phase="adjudicate", status="ADJUDICATING")
        refresh_report(run_dir)
        return None

    if phase != "adjudicate":
        raise HarnessError(f"Unknown Review v2 phase: {phase!r}")
    _quarantine(_final_review_path(run_dir, index))
    final = _adjudicate_review_v2(
        run_dir,
        index,
        state=state,
        config=config,
        artifact=artifact,
        worker_result=worker_result,
        write_result=True,
    )
    append_event(
        run_dir,
        "final_review_decided",
        index=index,
        verdict=final["verdict"],
        reason_codes=final["reason_codes"],
    )
    if final["verdict"] == "PASS":
        _update_state(run_dir, phase="promote", status="PROMOTING", active_agent=None, last_error="")
        refresh_report(run_dir)
        return None
    if final["verdict"] == "INCONCLUSIVE":
        reason = "Review is inconclusive: " + ", ".join(final["reason_codes"] or ["unspecified limitation"])
        _update_state(
            run_dir,
            phase="review_plan",
            status="PAUSED",
            active_agent=None,
            last_error=reason,
        )
        append_event(run_dir, "review_inconclusive", index=index, reason_codes=final["reason_codes"])
        refresh_report(run_dir)
        print(f"INCONCLUSIVE: {run_dir}", flush=True)
        return 2
    if index + 1 >= int(config["max_reviews"]):
        _update_state(
            run_dir,
            status="INCOMPLETE",
            active_agent=None,
            finished_at=now(),
            last_error=f"Review limit reached with unresolved issues: {final['reason_codes']}",
        )
        refresh_report(run_dir)
        print(f"INCOMPLETE: {run_dir}", flush=True)
        return 1
    (run_dir / "WORKER_RESULT.json").unlink(missing_ok=True)
    _update_state(
        run_dir,
        phase="work",
        status="REPAIRING",
        review_index=index + 1,
        artifact_id=None,
        artifact_path=None,
    )
    refresh_report(run_dir)
    return None


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
    config = read_json(run_dir / "run-config.json")
    protocol_version = _review_protocol_version(config)
    final_review_verdict = None
    for final_path in reversed(sorted((run_dir / "reviews").glob("*/FINAL_REVIEW.json"))):
        try:
            final_review_verdict = read_json(final_path).get("verdict")
            break
        except HarnessError:
            continue
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
        f"- Review protocol: v{protocol_version}",
        f"- Current phase: `{state.get('phase', '')}`",
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
    for path in sorted((run_dir / "reviews").glob("*/REVIEW_PLAN.json")):
        try:
            plan = read_json(path)
            label = (
                f"{len(plan.get('requirements', []))} requirements, "
                f"{len(plan.get('worker_claims', []))} Worker claims, "
                f"{len(plan.get('risks', []))} risks, {len(plan.get('checks', []))} checks"
            )
        except HarnessError:
            label = "invalid review plan"
        lines.append(f"- [{path.parent.name} review plan]({_run_relative(path, run_dir)}) — {label}")
    for path in sorted((run_dir / "reviews").glob("*/REVIEW_CHECKS.json")):
        try:
            checks = read_json(path)
            counts = {
                status: sum(result.get("status") == status for result in checks.get("results", []))
                for status in ("pass", "fail", "error", "not_run")
            }
            label = ", ".join(f"{status}={count}" for status, count in counts.items())
        except HarnessError:
            label = "invalid review checks"
        lines.append(f"- [{path.parent.name} Harness review checks]({_run_relative(path, run_dir)}) — {label}")
    for path in _audit_paths(run_dir):
        try:
            audit = read_json(path)
            label = f"{audit.get('verdict', 'UNKNOWN')} — {audit.get('summary', '')}"
        except HarnessError:
            label = "invalid audit"
        lines.append(f"- [{path.parent.name} audit]({_run_relative(path, run_dir)}) — {label}")
    for path in sorted((run_dir / "reviews").glob("*/FINAL_REVIEW.json")):
        try:
            final = read_json(path)
            label = f"{final.get('verdict', 'UNKNOWN')} — {', '.join(final.get('reason_codes', [])) or 'all gates passed'}"
        except HarnessError:
            label = "invalid final review"
        lines.append(f"- [{path.parent.name} Harness final review]({_run_relative(path, run_dir)}) — {label}")
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
            "- `reviews/*/planner.log`",
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
            "review_protocol_version": protocol_version,
            "phase": state.get("phase"),
            "artifact_id": state.get("artifact_id"),
            "artifact_path": state.get("artifact_path"),
            "review_count": len(_audit_paths(run_dir)),
            "final_review_verdict": final_review_verdict,
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
        protocol_version = _review_protocol_version(config)
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
                    accepted_artifact = (
                        _review_artifact_v2(run_dir, index, candidate_workspace)
                        if protocol_version == 2
                        else read_private_json(review_dir / "artifact.json")
                    )
                    accepted_id = str(accepted_artifact.get("sha256", ""))
                    if protocol_version == 2:
                        _worker_path, worker_result = _worker_result(run_dir, index)
                        final = _read_final_review_v2(
                            run_dir,
                            index,
                            state=state,
                            config=config,
                            artifact=accepted_artifact,
                            worker_result=worker_result,
                        )
                        if final["verdict"] != "PASS":
                            raise HarnessError("The Review v2 promotion gate is not PASS")
                    else:
                        audit = _audit_result(review_dir / "AUDIT.json")
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
                            if protocol_version == 2:
                                feedback = _review_feedback_v2(run_dir, index - 1)
                            else:
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
                    if protocol_version == 2:
                        _update_state(run_dir, phase="verify", status="VERIFYING")
                        refresh_report(run_dir)
                        continue
                    try:
                        _run_verification(run_dir, index, candidate_workspace, config)
                    finally:
                        _assert_live_workspace_unchanged(run_dir)
                    _update_state(run_dir, phase="review", status="REVIEWING")
                    refresh_report(run_dir)
                    continue

                if protocol_version == 2 and state.get("phase") == "verify":
                    if not _is_real_directory(candidate_workspace):
                        raise HarnessError("The isolated candidate workspace is unavailable")
                    _assert_live_workspace_unchanged(run_dir)
                    _update_state(run_dir, status="VERIFYING")
                    try:
                        _run_verification(run_dir, index, candidate_workspace, config)
                    finally:
                        _assert_live_workspace_unchanged(run_dir)
                    _update_state(run_dir, phase="review_plan", status="PLANNING_REVIEW")
                    refresh_report(run_dir)
                    continue

                if protocol_version == 2:
                    outcome = _execute_review_v2_stage(
                        run_dir,
                        state=state,
                        config=config,
                        candidate_workspace=candidate_workspace,
                        reviewer_name=reviewer_name,
                        timeout=timeout,
                    )
                    if outcome is not None:
                        return outcome
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
