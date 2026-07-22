#!/usr/bin/env python3
"""Generic one-sentence worker/reviewer harness.

The interactive coordinator is any coding-agent TUI started by the user.  This
module only runs configured headless agents against a persistent workspace.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import string
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
CONFIG_PATH = ROOT / "harness.config.json"
PROMPTS_DIR = ROOT / "prompts"
STATE_SCHEMA = "generic-harness/v1"
WORKER_RESULT_SCHEMA = "generic-harness/worker-result/v1"
AUDIT_SCHEMA = "generic-harness/audit/v1"
ARTIFACT_SCHEMA = "generic-harness/artifact/v1"
TERMINAL_STATUSES = {"COMPLETE", "INCOMPLETE"}
PAUSE_FILE = ".operator-paused"
MAX_HANDOFF_BYTES = 1_000_000
MAX_ARG_PROMPT_BYTES = 100_000
PROTECTED_STATE_FIELDS = (
    "schema_version",
    "run_id",
    "request",
    "workspace",
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


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


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
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(payload, ensure_ascii=False) + "\n")
        path.chmod(0o600)


def pid_start_time(pid: int) -> str:
    try:
        return subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def pid_status(pid: int) -> str:
    try:
        return subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def process_matches(pid: int, started: str = "") -> bool:
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    status = pid_status(pid)
    if not status or status.startswith("Z"):
        return False
    current = pid_start_time(pid)
    return not started or not current or current == started


def active_agent_pid(state: dict[str, Any]) -> int | None:
    active = state.get("active_agent")
    if not isinstance(active, dict):
        return None
    try:
        pid = int(active.get("pid", 0))
    except (TypeError, ValueError):
        return None
    return pid if process_matches(pid, str(active.get("pid_started", ""))) else None


def terminate_process_group(pid: int, started: str = "", grace: float = 5.0) -> None:
    if not process_matches(pid, started):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while process_matches(pid, started) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_matches(pid, started):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


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
    return config


def _parent_commands(limit: int = 12) -> list[str]:
    commands: list[str] = []
    pid = os.getppid()
    for _ in range(limit):
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "ppid=,command="],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            break
        fields = result.stdout.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            break
        pid = int(fields[0])
        commands.append(fields[1])
        if pid <= 1:
            break
    return commands


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
            and re.search(rf"(?:^|[/\s]){re.escape(needle.casefold())}(?:\.js)?(?:\s|$)", process_text)
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
    candidate = Path(executable).expanduser()
    if candidate.parent != Path("."):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    else:
        found = shutil.which(executable)
        if found:
            return found
    raise HarnessError(f"Agent executable is unavailable for profile {profile_name}: {executable}")


def _new_run_dir(runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = runs_dir / stem
    if candidate.exists():
        candidate = runs_dir / f"{stem}-{uuid.uuid4().hex[:6]}"
    candidate.mkdir(mode=0o700)
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
    workspace_path = workspace_path.resolve()
    if workspace_path in {Path("/"), Path.home().resolve()}:
        raise HarnessError(f"Refusing a workspace that is too broad: {workspace_path}")
    workspace_path.mkdir(parents=True, exist_ok=True)

    selected_profiles = {name: config["agents"][name] for name in {worker, reviewer}}
    for name, profile in selected_profiles.items():
        _resolve_executable(profile["command"], name)

    runs_path = runs_dir.expanduser().resolve()
    if runs_path == workspace_path:
        raise HarnessError("runs_dir cannot be the workspace itself")
    run_dir = _new_run_dir(runs_path)
    timeout = int(config.get("timeout_seconds", 5400))
    run_config = {
        "schema_version": STATE_SCHEMA,
        "source_config": str(config_path),
        "workspace": str(workspace_path),
        "coordinator_agent": coordinator,
        "coordinator_detection": detection,
        "worker_agent": worker,
        "reviewer_agent": reviewer,
        "max_reviews": reviews,
        "timeout_seconds": timeout,
        "profiles": selected_profiles,
    }
    state = {
        "schema_version": STATE_SCHEMA,
        "run_id": run_dir.name,
        "status": "QUEUED",
        "phase": "work",
        "request": request,
        "workspace": str(workspace_path),
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
    path = run_dir / PAUSE_FILE
    if not path.exists():
        atomic_write(path, json.dumps({"requested_at": now()}, ensure_ascii=False) + "\n")
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
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait()


def _control_guard(run_dir: Path, protected_files: tuple[Path, ...] = ()) -> dict[str, Any]:
    state = read_json(run_dir / "state.json")
    return {
        "config": read_json(run_dir / "run-config.json"),
        "state": {field: state.get(field) for field in PROTECTED_STATE_FIELDS},
        "files": {str(path): path.read_bytes() for path in protected_files},
    }


def _verify_control_guard(run_dir: Path, guard: dict[str, Any]) -> None:
    changed: list[str] = []
    config_path = run_dir / "run-config.json"
    if read_json(config_path) != guard["config"]:
        write_json(config_path, guard["config"])
        changed.append("run-config.json")

    state_path = run_dir / "state.json"
    state = read_json(state_path)
    for field, expected in guard["state"].items():
        if state.get(field) != expected:
            state[field] = expected
            changed.append(f"state.json:{field}")
    state["active_agent"] = None
    if changed:
        write_json(state_path, state)

    for raw_path, expected in guard["files"].items():
        path = Path(raw_path)
        try:
            actual = path.read_bytes()
        except OSError:
            actual = b""
        if actual != expected:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
            path.chmod(0o600)
            changed.append(str(path.relative_to(run_dir)))
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
        if any("{prompt}" in item for item in profile["command"]) and len(prompt.encode()) > MAX_ARG_PROMPT_BYTES:
            raise HarnessError(
                f"Prompt is too large for argv ({len(prompt.encode())} bytes); use stdin or {{prompt_file}} for {profile_name}"
            )
        command = [item.format_map(values) for item in profile["command"]]
        stdin_text = profile.get("stdin")
        stdin_value = stdin_text.format_map(values) if isinstance(stdin_text, str) else None
    except (KeyError, ValueError) as error:
        raise HarnessError(f"Invalid agent command template for {profile_name}: {error}") from error
    command[0] = _resolve_executable(command, profile_name)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    shown = [item.replace(prompt, "<prompt>") if prompt else item for item in command]
    with log_path.open("a", encoding="utf-8") as log:
        log_path.chmod(0o600)
        log.write(f"\n[{now()}] {role} via {profile_name}\n$ {shlex.join(shown)}\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                stdin=subprocess.PIPE if stdin_value is not None else subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            raise HarnessError(f"Could not launch {profile_name}: {error}") from error
        if process.stdin is not None:
            try:
                process.stdin.write(stdin_value or "")
                process.stdin.close()
            except BrokenPipeError:
                pass

        _update_state(
            run_dir,
            active_agent={
                "profile": profile_name,
                "role": role,
                "pid": process.pid,
                "process_group": process.pid,
                "pid_started": pid_start_time(process.pid),
                "log": str(log_path.relative_to(run_dir)),
                "started_at": now(),
            },
        )
        append_event(run_dir, "agent_started", profile=profile_name, role=role, pid=process.pid)
        effective_timeout = int(profile.get("timeout_seconds", timeout_seconds))
        deadline = time.monotonic() + effective_timeout
        started_monotonic = time.monotonic()
        paused = False
        timed_out = False
        while process.poll() is None:
            if (run_dir / PAUSE_FILE).is_file():
                paused = True
                _terminate(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate(process)
                break
            time.sleep(0.25)
        returncode = process.wait()

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
    for item in result["changed_files"]:
        candidate = Path(item)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise HarnessError(f"Worker result contains an unsafe changed path: {item}")
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
    return archived


def _quarantine(path: Path) -> None:
    """Keep an invalid handoff for diagnosis while allowing `continue` to retry."""
    if not path.exists():
        return
    candidate = path.with_name(f"{path.stem}.invalid-{uuid.uuid4().hex[:6]}{path.suffix}")
    path.replace(candidate)


def _is_excluded(path: Path, excluded: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == item or item in resolved.parents for item in excluded)


def _review_exclusions(run_dir: Path) -> tuple[Path, ...]:
    return (
        run_dir.parent,
        RUNS_DIR,
        ROOT / ".harness-current",
        ROOT / ".harness-control.lock",
        ROOT / ".harness-request.md",
    )


def _workspace_manifest(workspace: Path, excluded: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Create a content-addressed identity for the delivered workspace."""
    workspace = workspace.resolve()
    excluded = tuple(path.resolve() for path in excluded)
    entries: list[dict[str, Any]] = []
    for directory, subdirectories, files in os.walk(workspace, followlinks=False):
        base = Path(directory)
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name != ".git" and not _is_excluded(base / name, excluded)
        )
        for name in sorted(files):
            path = base / name
            if _is_excluded(path, excluded):
                continue
            try:
                details = path.lstat()
                relative = path.relative_to(workspace).as_posix()
                mode = stat.S_IMODE(details.st_mode)
                if path.is_symlink():
                    entries.append({"path": relative, "kind": "link", "mode": mode, "target": os.readlink(path)})
                    continue
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


def _copy_workspace(workspace: Path, destination: Path, excluded: tuple[Path, ...] = ()) -> None:
    excluded = tuple(path.resolve() for path in excluded)

    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        return {
            name
            for name in names
            if name == ".git" or _is_excluded(base / name, excluded)
        }

    try:
        shutil.copytree(workspace, destination, symlinks=True, ignore=ignore)
    except OSError as error:
        raise HarnessError(f"Could not create isolated review snapshot: {error}") from error


def _verify_worker_changes(run_dir: Path, index: int, result: dict[str, Any]) -> None:
    iteration_dir = run_dir / "iterations" / f"{index:02d}"
    input_path = iteration_dir / "input-artifact.json"
    if not input_path.is_file():
        return
    before = read_json(input_path)
    workspace = Path(read_json(run_dir / "run-config.json")["workspace"])
    after = _workspace_manifest(workspace, _review_exclusions(run_dir))
    write_json(iteration_dir / "output-artifact.json", after)
    actual = set(_manifest_changes(before, after))
    claimed = set(result["changed_files"])
    unreported = sorted(actual - claimed)
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
        f"- Workspace: `{state.get('workspace', '')}`",
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
        lines.append(f"- [{path.parent.name} worker result]({path.relative_to(run_dir)})")
    for path in _audit_paths(run_dir):
        try:
            audit = read_json(path)
            label = f"{audit.get('verdict', 'UNKNOWN')} — {audit.get('summary', '')}"
        except HarnessError:
            label = "invalid audit"
        lines.append(f"- [{path.parent.name} audit]({path.relative_to(run_dir)}) — {label}")
    if state.get("last_error"):
        lines.extend(["", "## Last error", "", str(state["last_error"])])
    lines.extend(
        [
            "",
            "## Logs",
            "",
            "- [Harness log](harness.log)",
            "- `iterations/*/worker.log`",
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
    run_dir = run_dir.expanduser().resolve()
    state_path = run_dir / "state.json"
    config_path = run_dir / "run-config.json"
    if not state_path.is_file() or not config_path.is_file():
        raise HarnessError(f"Not a generic Harness run: {run_dir}")
    state = read_json(state_path)
    config = read_json(config_path)
    if state.get("schema_version") != STATE_SCHEMA or config.get("schema_version") != STATE_SCHEMA:
        raise HarnessError(f"Unsupported run format (legacy runs are not resumed by this runner): {run_dir}")
    if str(state.get("status", "")).upper() in TERMINAL_STATUSES:
        refresh_report(run_dir)
        return 0 if state["status"] == "COMPLETE" else 1

    lock_path = run_dir / "run.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HarnessError(f"Run already has an active supervisor: {run_dir}") from error
        orphan_pid = active_agent_pid(state)
        if orphan_pid:
            raise HarnessError(
                f"Run still has an active child agent (PID {orphan_pid}); stop it before resuming: {run_dir}"
            )
        atomic_write(run_dir / "harness.pid", f"{os.getpid()}\n")
        if (run_dir / PAUSE_FILE).is_file():
            _update_state(run_dir, status="PAUSED", active_agent=None)
            refresh_report(run_dir)
            return 2
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
        try:
            while True:
                if (run_dir / PAUSE_FILE).is_file():
                    raise OperatorPause("The run was paused by the user.")
                state = read_json(state_path)
                index = int(state.get("review_index", 0))
                timeout = int(config.get("timeout_seconds", 5400))
                worker_name = str(config["worker_agent"])
                reviewer_name = str(config["reviewer_agent"])

                if state.get("phase") == "work":
                    iteration_dir = run_dir / "iterations" / f"{index:02d}"
                    result_path = iteration_dir / "WORKER_RESULT.json"
                    input_artifact_path = iteration_dir / "input-artifact.json"
                    if not input_artifact_path.is_file():
                        write_json(
                            input_artifact_path,
                            _workspace_manifest(Path(state["workspace"]), _review_exclusions(run_dir)),
                        )
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
                            workspace=str(state["workspace"]),
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
                            )
                        finally:
                            _verify_control_guard(run_dir, guard)
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
                source_workspace = Path(state["workspace"])
                exclusions = _review_exclusions(run_dir)
                artifact_before = _workspace_manifest(source_workspace, exclusions)
                write_json(review_dir / "artifact.json", artifact_before)
                worker_report = f"Path: {worker_path}\n\n{json.dumps(worker_result, ensure_ascii=False, indent=2)}"
                with tempfile.TemporaryDirectory(prefix="harness-review-") as temporary:
                    review_workspace = Path(temporary) / "workspace"
                    _copy_workspace(source_workspace, review_workspace, exclusions)
                    prompt = render_prompt(
                        "reviewer",
                        request=str(state["request"]),
                        workspace=str(review_workspace),
                        run_dir=str(run_dir),
                        worker_report=worker_report,
                        review_dir=str(review_dir),
                        artifact_id=str(artifact_before["sha256"]),
                    )
                    guard = _control_guard(run_dir, (worker_path,))
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
                        )
                    finally:
                        _verify_control_guard(run_dir, guard)
                artifact_after = _workspace_manifest(source_workspace, exclusions)
                reviewer_changes = _manifest_changes(artifact_before, artifact_after)
                try:
                    audit = _audit_result(audit_path)
                except HarnessError:
                    _quarantine(audit_path)
                    raise
                if reviewer_changes:
                    shown_changes = reviewer_changes[:20]
                    if len(reviewer_changes) > len(shown_changes):
                        shown_changes.append(f"… and {len(reviewer_changes) - len(shown_changes)} more")
                    audit["verdict"] = "FIX"
                    audit["issues"].append(
                        {
                            "severity": "major",
                            "location": "workspace",
                            "title": "Delivered workspace changed during review",
                            "evidence": "Changed paths: " + ", ".join(shown_changes),
                            "required_fix": "Restore the intended delivery and repeat review against a stable artifact.",
                            "acceptance_test": "Repeat review and verify the workspace content hash remains unchanged.",
                        }
                    )
                audit["artifact_id"] = artifact_before["sha256"]
                write_json(audit_path, audit)
                append_event(run_dir, "review_recorded", index=index, verdict=audit["verdict"])
                if str(audit["verdict"]).upper() == "PASS":
                    _update_state(
                        run_dir,
                        status="COMPLETE",
                        active_agent=None,
                        artifact_id=artifact_before["sha256"],
                        artifact_path=str((review_dir / "artifact.json").relative_to(run_dir)),
                        finished_at=now(),
                        last_error="",
                    )
                    refresh_report(run_dir)
                    print(f"PASS: {run_dir}", flush=True)
                    return 0
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
            _update_state(run_dir, status="PAUSED", active_agent=None, last_error=str(error))
            append_event(run_dir, "run_paused", reason=str(error))
            refresh_report(run_dir)
            print(f"PAUSED: {run_dir}", flush=True)
            return 2
        except WorkerBlocked as error:
            _update_state(run_dir, status="BLOCKED", active_agent=None, last_error=str(error))
            append_event(run_dir, "run_blocked", reason=str(error))
            refresh_report(run_dir)
            raise
        except HarnessError as error:
            _update_state(run_dir, status="PAUSED", active_agent=None, last_error=str(error))
            append_event(run_dir, "run_failed", error=str(error))
            refresh_report(run_dir)
            raise
        finally:
            try:
                if (run_dir / "harness.pid").read_text(encoding="utf-8").strip() == str(os.getpid()):
                    (run_dir / "harness.pid").unlink(missing_ok=True)
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
    args = parse_args()
    if args.resume:
        return execute_run(args.resume)
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
