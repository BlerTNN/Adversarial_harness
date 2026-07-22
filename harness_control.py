#!/usr/bin/env python3
"""Detached control surface used by any interactive coding-agent TUI."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness import (
    CONFIG_PATH,
    PAUSE_FILE,
    ROOT,
    RUNS_DIR,
    STATE_SCHEMA,
    TERMINAL_STATUSES,
    HarnessError,
    active_agent_pid,
    append_event,
    atomic_write,
    create_run,
    detect_coordinator_agent,
    execute_run,
    load_config,
    now,
    read_json,
    request_pause,
    terminate_process_group,
    write_json,
)


HARNESS = ROOT / "harness.py"
CURRENT_FILE = ROOT / ".harness-current"
CONTROL_LOCK = ROOT / ".harness-control.lock"


class ControlError(RuntimeError):
    pass


def ui_language() -> str:
    value = (
        os.environ.get("HARNESS_LANG")
        or os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    )
    return "zh" if value.lower().replace("_", "-").startswith("zh") else "en"


def tr(english: str, chinese: str) -> str:
    return chinese if ui_language() == "zh" else english


def safe_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def generic_runs(runs_dir: Path = RUNS_DIR) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    paths = []
    for path in runs_dir.iterdir():
        if path.is_dir() and safe_json(path / "state.json").get("schema_version") == STATE_SCHEMA:
            paths.append(path.resolve())
    return sorted(paths, key=lambda item: str(safe_json(item / "state.json").get("created_at", item.name)), reverse=True)


def current_run(runs_dir: Path = RUNS_DIR) -> Path | None:
    candidate: Path | None = None
    configured_root = runs_dir.expanduser().resolve()
    try:
        raw = CURRENT_FILE.read_text(encoding="utf-8").strip()
        marker = json.loads(raw)
        if isinstance(marker, dict):
            configured_root = Path(str(marker["runs_dir"])).expanduser().resolve()
            candidate = Path(str(marker["run_dir"])).expanduser().resolve()
        else:
            candidate = Path(raw).expanduser().resolve()
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        try:
            candidate = Path(CURRENT_FILE.read_text(encoding="utf-8").strip()).expanduser().resolve()
        except (OSError, ValueError):
            candidate = None
    if (
        candidate
        and candidate.parent == configured_root
        and safe_json(candidate / "state.json").get("schema_version") == STATE_SCHEMA
    ):
        return candidate
    runs = generic_runs(runs_dir)
    return runs[0] if runs else None


def write_current(run_dir: Path, runs_dir: Path) -> None:
    run_dir = run_dir.resolve()
    runs_dir = runs_dir.expanduser().resolve()
    if run_dir.parent != runs_dir:
        raise ControlError(f"Run is not a direct child of its configured runs directory: {run_dir}")
    write_json(
        CURRENT_FILE,
        {"schema_version": STATE_SCHEMA, "run_dir": str(run_dir), "runs_dir": str(runs_dir)},
    )


def unfinished_run(runs_dir: Path = RUNS_DIR) -> Path | None:
    return next(
        (
            path
            for path in generic_runs(runs_dir)
            if str(safe_json(path / "state.json").get("status", "")).upper() not in TERMINAL_STATUSES
        ),
        None,
    )


def _pid_command(pid: int) -> str:
    try:
        return subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def supervisor_pid(run_dir: Path) -> int | None:
    try:
        pid = int((run_dir / "harness.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    command = _pid_command(pid)
    if "harness.py" in command and str(run_dir) in command:
        return pid
    return None


def run_lock_available(run_dir: Path) -> bool:
    with (run_dir / "run.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return True


def spawn_run(run_dir: Path) -> int:
    log_path = run_dir / "harness.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log_path.chmod(0o600)
        process = subprocess.Popen(
            [sys.executable, str(HARNESS), "--resume", str(run_dir)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    atomic_write(run_dir / "harness.pid", f"{process.pid}\n")
    append_event(run_dir, "supervisor_spawned", pid=process.pid)
    return process.pid


def start(args: argparse.Namespace) -> int:
    request = args.request
    if args.request_file:
        try:
            request = args.request_file.expanduser().resolve().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ControlError(tr(f"Could not read request file: {error}", f"无法读取需求文件：{error}")) from error
    with CONTROL_LOCK.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = current_run(args.runs_dir)
        if existing and str(safe_json(existing / "state.json").get("status", "")).upper() in TERMINAL_STATUSES:
            existing = None
        existing = (
            existing
            or unfinished_run(RUNS_DIR)
            or unfinished_run(args.runs_dir.expanduser().resolve())
        )
        if existing:
            state = safe_json(existing / "state.json")
            pid = supervisor_pid(existing)
            activity = f"PID {pid}" if pid else tr("paused or waiting to resume", "已暂停或等待恢复")
            raise ControlError(
                tr(
                    f"An unfinished task already exists: {existing} "
                    f"({state.get('status', 'UNKNOWN')}, {activity}). "
                    "Wait for it, use stop/continue, or resolve that task first.",
                    f"已有未结束任务：{existing}（{state.get('status', 'UNKNOWN')}，{activity}）。"
                    "请先等待、stop/continue，或处理该任务。",
                )
            )
        try:
            run_dir = create_run(
                str(request or ""),
                config_path=args.config,
                runs_dir=args.runs_dir,
                workspace=args.workspace,
                coordinator_agent=args.coordinator_agent,
                worker_agent=args.worker_agent,
                reviewer_agent=args.reviewer_agent,
                max_reviews=args.max_reviews,
            )
        except HarnessError as error:
            raise ControlError(str(error)) from error
        write_current(run_dir, args.runs_dir)
        if args.foreground:
            print(tr(f"Task created: {run_dir}", f"已创建任务：{run_dir}"), flush=True)
            return execute_run(run_dir)
        pid = spawn_run(run_dir)
    state = read_json(run_dir / "state.json")
    print(tr(f"Submitted: {run_dir}", f"已提交：{run_dir}"))
    print(
        tr(
            f"coordinator={state['coordinator_agent']}, worker={state['worker_agent']}, "
            f"reviewer={state['reviewer_agent']}, supervisor PID={pid}",
            f"协调={state['coordinator_agent']}，执行={state['worker_agent']}，"
            f"独立审计={state['reviewer_agent']}，Supervisor PID={pid}",
        )
    )
    print(
        tr(
            "The TUI can keep chatting; use `./harness_control.py status` to check progress.",
            "TUI 可继续对话；用 `./harness_control.py status` 查看进度。",
        )
    )
    return 0


def continue_run(args: argparse.Namespace) -> int:
    with CONTROL_LOCK.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        run_dir = current_run()
        if run_dir is None:
            raise ControlError(tr("There is no Harness task to resume.", "没有通用 Harness 任务可恢复。"))
        state = read_json(run_dir / "state.json")
        status = str(state.get("status", "UNKNOWN")).upper()
        if status in TERMINAL_STATUSES:
            print(tr(f"Task is already {status}: {run_dir}", f"任务已经是 {status}：{run_dir}"))
            return 0
        pid = supervisor_pid(run_dir)
        if pid:
            raise ControlError(
                tr(
                    f"Task is still running (PID {pid}); it will not be started twice.",
                    f"任务仍在运行（PID {pid}），不会重复启动。",
                )
            )
        if not run_lock_available(run_dir):
            raise ControlError(
                tr(
                    "The previous supervisor has not released the run lock; the pause marker is unchanged. Try continue again shortly.",
                    "旧 Supervisor 尚未释放 run lock；暂停标记保持不变，请稍后再执行 continue。",
                )
            )
        active_pid = active_agent_pid(state)
        if active_pid:
            raise ControlError(
                tr(
                    f"The previous supervisor stopped but child agent PID {active_pid} is still running. Use stop, then continue.",
                    f"旧 Supervisor 已停止，但子 Agent PID {active_pid} 仍在运行。请先 stop，再 continue。",
                )
            )
        if state.get("active_agent") is not None:
            state["active_agent"] = None
            write_json(run_dir / "state.json", state)
        (run_dir / PAUSE_FILE).unlink(missing_ok=True)
        _state = read_json(run_dir / "state.json")
        _state.update({"status": "QUEUED", "last_error": "", "updated_at": now()})
        write_json(run_dir / "state.json", _state)
        if args.foreground:
            return execute_run(run_dir)
        pid = spawn_run(run_dir)
    print(tr(f"Resumed original task: {run_dir} (PID {pid})", f"已恢复原任务：{run_dir}（PID {pid}）"))
    return 0


def stop_run() -> int:
    with CONTROL_LOCK.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        run_dir = current_run()
        if run_dir is None:
            raise ControlError(tr("There is no current Harness task.", "当前没有通用 Harness 任务。"))
        state = read_json(run_dir / "state.json")
        status = str(state.get("status", "UNKNOWN")).upper()
        if status in TERMINAL_STATUSES:
            print(tr(f"Task is already {status}; no stop is needed: {run_dir}", f"任务已经是 {status}，无需停止：{run_dir}"))
            return 0
        request_pause(run_dir)
        pid = supervisor_pid(run_dir)
        if pid is None and run_lock_available(run_dir):
            active = state.get("active_agent")
            child_pid = active_agent_pid(state)
            if child_pid and isinstance(active, dict):
                terminate_process_group(child_pid, str(active.get("pid_started", "")))
            state.update({"status": "PAUSED", "active_agent": None, "updated_at": now()})
            write_json(run_dir / "state.json", state)
    print(tr(f"Safe pause requested: {run_dir}", f"已请求安全暂停：{run_dir}"))
    if pid:
        print(
            tr(
                f"Supervisor PID {pid} will stop the active child agent and preserve saved progress; use continue afterward.",
                f"Supervisor PID {pid} 会终止当前子 Agent，并保留已写入磁盘的阶段；随后可执行 continue。",
            )
        )
    return 0


def status_payload() -> dict[str, Any]:
    run_dir = current_run()
    if run_dir is None:
        return {"schema_version": STATE_SCHEMA, "status": "IDLE", "run_dir": None, "harness_running": False}
    state = read_json(run_dir / "state.json")
    pid = supervisor_pid(run_dir)
    child_pid = active_agent_pid(state)
    return {
        **state,
        "run_dir": str(run_dir),
        "harness_pid": pid,
        "harness_running": pid is not None,
        "child_agent_running": child_pid is not None,
        "child_agent_pid": child_pid,
        "operator_paused": (run_dir / PAUSE_FILE).is_file(),
        "final_report": str(run_dir / "FINAL_REPORT.md") if (run_dir / "FINAL_REPORT.md").is_file() else None,
    }


def print_status(as_json: bool) -> int:
    payload = status_payload()
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(tr(f"Status: {payload['status']}", f"状态：{payload['status']}"))
    if payload.get("run_dir"):
        print(tr(f"Run: {payload['run_dir']}", f"Run：{payload['run_dir']}"))
        print(
            tr(
                f"Coordinator: {payload.get('coordinator_agent')} ({payload.get('coordinator_detection', 'unknown')}) · "
                f"Worker: {payload.get('worker_agent')} · Reviewer: {payload.get('reviewer_agent')}",
                f"协调：{payload.get('coordinator_agent')}（{payload.get('coordinator_detection', 'unknown')}） · "
                f"执行：{payload.get('worker_agent')} · 审计：{payload.get('reviewer_agent')}",
            )
        )
        print(
            tr(
                f"Phase: {payload.get('phase')} · Review round: {int(payload.get('review_index', 0)) + 1}/{payload.get('max_reviews')}",
                f"阶段：{payload.get('phase')} · 审计轮次：{int(payload.get('review_index', 0)) + 1}/{payload.get('max_reviews')}",
            )
        )
        active = payload.get("active_agent")
        if isinstance(active, dict):
            print(
                tr(
                    f"Active agent: {active.get('profile')} / {active.get('role')} / PID {active.get('pid')}",
                    f"当前 Agent：{active.get('profile')} / {active.get('role')} / PID {active.get('pid')}",
                )
            )
        if payload.get("last_error"):
            print(tr(f"Latest error: {payload['last_error']}", f"最近错误：{payload['last_error']}"))
        if payload.get("final_report"):
            print(tr(f"Report: {payload['final_report']}", f"报告：{payload['final_report']}"))
    return 0


def wait_for_run(timeout: int, interval: float) -> int:
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        payload = status_payload()
        status = str(payload.get("status", "IDLE")).upper()
        if status == "IDLE":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if status in TERMINAL_STATUSES or (
            status in {"PAUSED", "BLOCKED"} and not payload.get("harness_running")
        ):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if status == "COMPLETE" else 1
        if deadline is not None and time.monotonic() >= deadline:
            raise ControlError(
                tr(
                    f"Task did not finish within {timeout} seconds.",
                    f"等待 {timeout} 秒后任务仍未结束。",
                )
            )
        time.sleep(max(0.1, interval))


def list_agents(config_path: Path, as_json: bool) -> int:
    try:
        config = load_config(config_path)
        coordinator = detect_coordinator_agent(config)
    except HarnessError as error:
        raise ControlError(str(error)) from error
    rows = []
    for name, profile in config["agents"].items():
        executable = profile["command"][0]
        available = bool(shutil_which(executable))
        rows.append(
            {
                "name": name,
                "available": available,
                "executable": executable,
                "description": profile.get("description", ""),
                "detected_coordinator": name == coordinator,
            }
        )
    if as_json:
        print(json.dumps({"coordinator": coordinator, "agents": rows}, ensure_ascii=False, indent=2))
    else:
        print(tr(f"Detected coordinator agent: {coordinator}", f"检测到的协调 Agent：{coordinator}"))
        for row in rows:
            print(f"{'✓' if row['available'] else '·'} {row['name']}: {row['executable']} — {row['description']}")
    return 0


def shutil_which(executable: str) -> str | None:
    candidate = Path(executable).expanduser()
    if candidate.parent != Path("."):
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    from shutil import which

    return which(executable)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start", help="submit one new task and return immediately")
    request = start_parser.add_mutually_exclusive_group(required=True)
    request.add_argument("--request")
    request.add_argument("--request-file", type=Path)
    start_parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    start_parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    start_parser.add_argument("--workspace", type=Path)
    start_parser.add_argument("--coordinator-agent")
    start_parser.add_argument("--worker-agent")
    start_parser.add_argument("--reviewer-agent")
    start_parser.add_argument("--max-reviews", type=int)
    start_parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    status_parser = commands.add_parser("status", help="show the latest task")
    status_parser.add_argument("--json", action="store_true")
    commands.add_parser("stop", help="safely stop the current child agent")
    continue_parser = commands.add_parser("continue", help="resume the exact saved task")
    continue_parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    wait_parser = commands.add_parser("wait", help="wait until the current task stops")
    wait_parser.add_argument("--timeout", type=int, default=0, help="seconds; 0 waits indefinitely")
    wait_parser.add_argument("--interval", type=float, default=1.0)
    agents_parser = commands.add_parser("agents", help="show configured agent profiles without launching them")
    agents_parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    agents_parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "start":
        return start(args)
    if args.command == "status":
        return print_status(args.json)
    if args.command == "stop":
        return stop_run()
    if args.command == "continue":
        return continue_run(args)
    if args.command == "wait":
        return wait_for_run(args.timeout, args.interval)
    return list_agents(args.config, args.json)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ControlError, HarnessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
