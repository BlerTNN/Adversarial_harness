#!/usr/bin/env python3
"""Detached control surface used by any interactive coding-agent TUI."""

from __future__ import annotations

import argparse
import json
import locale
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness import (
    CONFIG_PATH,
    ROOT,
    RUNS_DIR,
    STATE_SCHEMA,
    TERMINAL_STATUSES,
    HarnessError,
    active_agent_identity,
    append_event,
    clear_pause_request,
    create_run,
    detect_coordinator_agent,
    execute_run,
    load_config,
    now,
    pause_requested,
    read_json,
    read_process_marker,
    remove_owned_process_marker,
    request_pause,
    supervisor_marker_path,
    supervisor_launch_lock_path,
    terminate_process_group,
    validate_agent_profile,
    write_json,
)
from platform_support import (
    configure_utf8_stdio,
    file_lock,
    format_command,
    is_real_directory,
    managed_process_start_time,
    process_identity_status,
    set_private_permissions,
    spawn_detached_process,
)


HARNESS = ROOT / "harness.py"
CURRENT_FILE = ROOT / ".harness-current"
CONTROL_LOCK = ROOT / ".harness-control.lock"
_DETACHED_PROCESSES: dict[int, subprocess.Popen[str]] = {}


class ControlError(RuntimeError):
    pass


def control_invocation() -> str:
    return format_command([sys.executable, "harness_control.py"])


def _reap_detached_processes() -> None:
    for pid, process in tuple(_DETACHED_PROCESSES.items()):
        if process.poll() is not None:
            _DETACHED_PROCESSES.pop(pid, None)


def ui_language() -> str:
    value = (
        os.environ.get("HARNESS_LANG")
        or os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
    )
    if not value:
        try:
            value = locale.getlocale()[0]
        except (ValueError, locale.Error):
            value = ""
    value = value or ""
    normalized = value.casefold().replace("_", "-")
    return "zh" if normalized.startswith("zh") or "chinese" in normalized else "en"


def tr(english: str, chinese: str) -> str:
    return chinese if ui_language() == "zh" else english


def safe_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def real_control_directory(path: Path) -> Path | None:
    lexical = Path(os.path.abspath(path.expanduser()))
    return lexical.resolve() if is_real_directory(lexical) else None


def generic_runs(runs_dir: Path = RUNS_DIR) -> list[Path]:
    runs_root = real_control_directory(runs_dir)
    if runs_root is None:
        return []
    paths = []
    for path in runs_root.iterdir():
        if is_real_directory(path) and safe_json(path / "state.json").get("schema_version") == STATE_SCHEMA:
            resolved = path.resolve()
            if resolved.parent == runs_root:
                paths.append(resolved)
    return sorted(paths, key=lambda item: str(safe_json(item / "state.json").get("created_at", item.name)), reverse=True)


def current_run(runs_dir: Path = RUNS_DIR) -> Path | None:
    candidate: Path | None = None
    configured_root = real_control_directory(runs_dir)
    try:
        raw = CURRENT_FILE.read_text(encoding="utf-8").strip()
        marker = json.loads(raw)
        if isinstance(marker, dict):
            configured_root = real_control_directory(Path(str(marker["runs_dir"])))
            candidate = real_control_directory(Path(str(marker["run_dir"])))
        else:
            candidate = real_control_directory(Path(raw))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        try:
            candidate = real_control_directory(Path(CURRENT_FILE.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            candidate = None
    if (
        candidate
        and configured_root
        and candidate.parent == configured_root
        and safe_json(candidate / "state.json").get("schema_version") == STATE_SCHEMA
    ):
        return candidate
    runs = generic_runs(runs_dir)
    return runs[0] if runs else None


def write_current(run_dir: Path, runs_dir: Path) -> None:
    real_run = real_control_directory(run_dir)
    real_runs = real_control_directory(runs_dir)
    if real_run is None or real_runs is None:
        raise ControlError("Run and runs directory must be real directories, not links or junctions")
    run_dir = real_run
    runs_dir = real_runs
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


def supervisor_identity(run_dir: Path) -> tuple[str, int | None]:
    _reap_detached_processes()
    trusted_marker = supervisor_marker_path(run_dir)
    marker = read_process_marker(trusted_marker)
    if marker is None and not os.path.lexists(trusted_marker):
        marker = read_process_marker(run_dir / "harness.pid")
    if marker is None:
        return ("unknown", None) if os.path.lexists(trusted_marker) else ("gone", None)
    pid, started = marker
    if not started:
        return "unknown", pid
    return process_identity_status(pid, started), pid


def supervisor_pid(run_dir: Path) -> int | None:
    status, pid = supervisor_identity(run_dir)
    return pid if status == "match" else None


def run_lock_available(run_dir: Path) -> bool:
    try:
        with file_lock(run_dir / "run.lock", blocking=False):
            return True
    except BlockingIOError:
        return False


def spawn_run(run_dir: Path) -> int:
    _reap_detached_processes()
    log_path = run_dir / "harness.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    with file_lock(supervisor_launch_lock_path(run_dir)):
        with log_path.open("a", encoding="utf-8") as log:
            set_private_permissions(log_path)
            try:
                process = spawn_detached_process(
                    [sys.executable, str(HARNESS), "--resume", str(run_dir)],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=environment,
                )
            except (OSError, RuntimeError) as error:
                message = tr(
                    f"Could not start a safe detached Supervisor: {error}. "
                    f"The run is PAUSED; resume it with `{control_invocation()} continue --foreground`.",
                    f"无法安全启动后台 Supervisor：{error}。"
                    f"Run 已保留为 PAUSED；请执行 `{control_invocation()} continue --foreground` 恢复。",
                )
                state = read_json(run_dir / "state.json")
                state.update({"status": "PAUSED", "last_error": message, "updated_at": now()})
                write_json(run_dir / "state.json", state)
                append_event(run_dir, "supervisor_spawn_failed", error=str(error))
                raise ControlError(message) from error
        if process.poll() is not None:
            process.wait(timeout=0)
            message = tr(
                "Detached Supervisor exited before accepting the run",
                "后台 Supervisor 在接管 run 前已退出",
            )
            state = read_json(run_dir / "state.json")
            state.update({"status": "PAUSED", "last_error": message, "updated_at": now()})
            write_json(run_dir / "state.json", state)
            append_event(run_dir, "supervisor_spawn_failed", error=message)
            raise ControlError(message)
        started = managed_process_start_time(process)
        if not started:
            process.kill()
            process.wait(timeout=5)
            message = tr(
                "Detached Supervisor has no verifiable process identity",
                "后台 Supervisor 没有可验证的进程身份",
            )
            state = read_json(run_dir / "state.json")
            state.update({"status": "PAUSED", "last_error": message, "updated_at": now()})
            write_json(run_dir / "state.json", state)
            append_event(run_dir, "supervisor_spawn_failed", error=message)
            raise ControlError(message)
        marker = {"pid": process.pid, "pid_started": started}
        marker_identity = (process.pid, started)
        marker_paths = (supervisor_marker_path(run_dir), run_dir / "harness.pid")
        try:
            for marker_path in marker_paths:
                write_json(marker_path, marker)
            append_event(run_dir, "supervisor_spawned", pid=process.pid)
        except BaseException as error:
            cleanup_errors: list[str] = []
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
            for marker_path in marker_paths:
                try:
                    remove_owned_process_marker(marker_path, marker_identity)
                    marker_path.with_name(
                        f"{marker_path.name}.tmp-{os.getpid()}"
                    ).unlink(missing_ok=True)
                except OSError as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            cleanup_note = (
                f" Cleanup also reported: {'; '.join(cleanup_errors)}"
                if cleanup_errors
                else ""
            )
            message = tr(
                f"Could not publish the detached Supervisor identity safely: {error}. "
                f"The Supervisor was stopped and the run is PAUSED.{cleanup_note}",
                f"无法安全发布后台 Supervisor 身份：{error}。"
                f"Supervisor 已停止，Run 已保留为 PAUSED。{cleanup_note}",
            )
            try:
                state = read_json(run_dir / "state.json")
                state.update({"status": "PAUSED", "last_error": message, "updated_at": now()})
                write_json(run_dir / "state.json", state)
            except (OSError, HarnessError):
                pass
            try:
                append_event(run_dir, "supervisor_spawn_failed", error=str(error))
            except (OSError, HarnessError):
                pass
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise ControlError(message) from error
    if process.poll() is None:
        _DETACHED_PROCESSES[process.pid] = process
    else:
        try:
            process.wait(timeout=0)
        except (OSError, subprocess.SubprocessError):
            pass
        for marker_path in marker_paths:
            try:
                remove_owned_process_marker(marker_path, marker_identity)
                marker_path.with_name(
                    f"{marker_path.name}.tmp-{os.getpid()}"
                ).unlink(missing_ok=True)
            except OSError:
                pass
        state = safe_json(run_dir / "state.json")
        if str(state.get("status", "")).upper() not in TERMINAL_STATUSES:
            message = str(state.get("last_error", "")).strip() or tr(
                "Detached Supervisor exited before accepting the run",
                "后台 Supervisor 在接管 run 前已退出",
            )
            try:
                state.update({"status": "PAUSED", "last_error": message, "updated_at": now()})
                write_json(run_dir / "state.json", state)
            except (OSError, HarnessError):
                pass
            try:
                append_event(run_dir, "supervisor_spawn_failed", error=message)
            except (OSError, HarnessError):
                pass
            raise ControlError(message)
    return process.pid


def start(args: argparse.Namespace) -> int:
    request = args.request
    if args.request_file:
        try:
            request = args.request_file.expanduser().resolve().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ControlError(tr(f"Could not read request file: {error}", f"无法读取需求文件：{error}")) from error
    with file_lock(CONTROL_LOCK):
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
        if not args.foreground:
            pid = spawn_run(run_dir)
    if args.foreground:
        print(tr(f"Task created: {run_dir}", f"已创建任务：{run_dir}"), flush=True)
        return execute_run(run_dir)
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
            f"The TUI can keep chatting; use `{control_invocation()} status` to check progress.",
            f"TUI 可继续对话；用 `{control_invocation()} status` 查看进度。",
        )
    )
    return 0


def continue_run(args: argparse.Namespace) -> int:
    with file_lock(CONTROL_LOCK):
        run_dir = current_run()
        if run_dir is None:
            raise ControlError(tr("There is no Harness task to resume.", "没有通用 Harness 任务可恢复。"))
        state = read_json(run_dir / "state.json")
        status = str(state.get("status", "UNKNOWN")).upper()
        if status in TERMINAL_STATUSES:
            print(tr(f"Task is already {status}: {run_dir}", f"任务已经是 {status}：{run_dir}"))
            return 0
        supervisor_status, pid = supervisor_identity(run_dir)
        if supervisor_status == "unknown":
            raise ControlError(
                tr(
                    "The recorded Supervisor identity cannot be verified; its state was preserved.",
                    "无法验证已记录的 Supervisor 身份；现有状态已保留。",
                )
            )
        if supervisor_status == "match" and pid:
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
        active_status, active_pid = active_agent_identity(state)
        if active_status == "unknown":
            raise ControlError(
                tr(
                    "The recorded child process identity cannot be verified; its state was preserved.",
                    "无法验证已记录的子进程身份；现有状态已保留。",
                )
            )
        if active_status == "match" and active_pid:
            raise ControlError(
                tr(
                    f"The previous supervisor stopped but child agent PID {active_pid} is still running. Use stop, then continue.",
                    f"旧 Supervisor 已停止，但子 Agent PID {active_pid} 仍在运行。请先 stop，再 continue。",
                )
            )
        if state.get("active_agent") is not None:
            state["active_agent"] = None
            write_json(run_dir / "state.json", state)
        clear_pause_request(run_dir)
        _state = read_json(run_dir / "state.json")
        _state.update({"status": "QUEUED", "last_error": "", "updated_at": now()})
        write_json(run_dir / "state.json", _state)
        if not args.foreground:
            pid = spawn_run(run_dir)
    if args.foreground:
        return execute_run(run_dir)
    print(tr(f"Resumed original task: {run_dir} (PID {pid})", f"已恢复原任务：{run_dir}（PID {pid}）"))
    return 0


def stop_run() -> int:
    with file_lock(CONTROL_LOCK):
        run_dir = current_run()
        if run_dir is None:
            raise ControlError(tr("There is no current Harness task.", "当前没有通用 Harness 任务。"))
        state = read_json(run_dir / "state.json")
        status = str(state.get("status", "UNKNOWN")).upper()
        if status in TERMINAL_STATUSES:
            print(tr(f"Task is already {status}; no stop is needed: {run_dir}", f"任务已经是 {status}，无需停止：{run_dir}"))
            return 0
        try:
            request_pause(run_dir)
        except HarnessError as error:
            raise ControlError(str(error)) from error
        supervisor_status, pid = supervisor_identity(run_dir)
        if supervisor_status == "unknown":
            raise ControlError(
                tr(
                    "The recorded Supervisor identity cannot be verified; the pause request and state were preserved.",
                    "无法验证已记录的 Supervisor 身份；暂停请求与现有状态已保留。",
                )
            )
        if supervisor_status != "match":
            try:
                with file_lock(run_dir / "run.lock", blocking=False):
                    state = read_json(run_dir / "state.json")
                    active = state.get("active_agent")
                    child_status, child_pid = active_agent_identity(state)
                    if child_status == "unknown":
                        raise ControlError(
                            tr(
                                "The recorded child process identity cannot be verified; its state was preserved.",
                                "无法验证已记录的子进程身份；现有状态已保留。",
                            )
                        )
                    if child_status == "match" and child_pid and isinstance(active, dict):
                        try:
                            terminate_process_group(child_pid, str(active.get("pid_started", "")))
                        except (OSError, RuntimeError) as error:
                            raise ControlError(
                                tr(
                                    f"The orphan process tree could not be terminated safely: {error}",
                                    f"无法安全终止遗留进程树：{error}",
                                )
                            ) from error
                    final_status, _final_pid = active_agent_identity(
                        read_json(run_dir / "state.json")
                    )
                    if final_status == "unknown":
                        raise ControlError(
                            tr(
                                "The child process identity became unverifiable; its state was preserved.",
                                "子进程身份变得无法验证；现有状态已保留。",
                            )
                        )
                    if final_status == "match":
                        raise ControlError(
                            tr(
                                "The child process is still running; its state was preserved.",
                                "子进程仍在运行；其状态已保留。",
                            )
                        )
                    state.update({"status": "PAUSED", "active_agent": None, "updated_at": now()})
                    write_json(run_dir / "state.json", state)
            except BlockingIOError:
                # A supervisor owns the lock and will observe the pause marker.
                pass
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
    supervisor_status, supervisor_process = supervisor_identity(run_dir)
    child_status, child_process = active_agent_identity(state)
    pid = supervisor_process if supervisor_status == "match" else None
    child_pid = child_process if child_status == "match" else None
    config = safe_json(run_dir / "run-config.json")
    review_dir = run_dir / "reviews" / f"{int(state.get('review_index', 0)):02d}"
    plan = safe_json(review_dir / "REVIEW_PLAN.json")
    review_checks = safe_json(review_dir / "REVIEW_CHECKS.json")
    audit = safe_json(review_dir / "AUDIT.json")
    final_review = safe_json(review_dir / "FINAL_REVIEW.json")
    check_results = review_checks.get("results", []) if isinstance(review_checks.get("results"), list) else []
    reviewer_verdict = audit.get("verdict")
    final_verdict = final_review.get("verdict")
    final_reasons = final_review.get("reason_codes")
    return {
        **state,
        "run_dir": str(run_dir),
        "harness_pid": pid,
        "harness_running": pid is not None,
        "supervisor_identity_status": supervisor_status,
        "child_agent_running": child_pid is not None,
        "child_agent_pid": child_pid,
        "child_agent_identity_status": child_status,
        "operator_paused": pause_requested(run_dir),
        "review_protocol_version": config.get("review_protocol_version", 1),
        "review_plan": {
            "requirements": len(plan.get("requirements", [])) if isinstance(plan.get("requirements"), list) else 0,
            "claims": len(plan.get("worker_claims", [])) if isinstance(plan.get("worker_claims"), list) else 0,
            "risks": len(plan.get("risks", [])) if isinstance(plan.get("risks"), list) else 0,
            "checks": len(plan.get("checks", [])) if isinstance(plan.get("checks"), list) else 0,
        },
        "review_checks": {
            status: sum(isinstance(result, dict) and result.get("status") == status for result in check_results)
            for status in ("pass", "fail", "error", "not_run")
        },
        "reviewer_verdict": reviewer_verdict if isinstance(reviewer_verdict, str) else None,
        "final_review_verdict": final_verdict if isinstance(final_verdict, str) else None,
        "final_review_reason_codes": final_reasons if isinstance(final_reasons, list) else [],
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
        if payload.get("review_protocol_version") == 2:
            print(
                tr(
                    f"Review v2: reviewer={payload.get('reviewer_verdict') or 'pending'} · "
                    f"Harness={payload.get('final_review_verdict') or 'pending'}",
                    f"Review v2：Reviewer={payload.get('reviewer_verdict') or '待定'} · "
                    f"Harness={payload.get('final_review_verdict') or '待定'}",
                )
            )
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
        reason = ""
        try:
            validate_agent_profile(name, profile)
            available = True
        except HarnessError as error:
            available = False
            reason = str(error)
        rows.append(
            {
                "name": name,
                "available": available,
                "executable": executable,
                "description": profile.get("description", ""),
                "detected_coordinator": name == coordinator,
                "unavailable_reason": reason or None,
            }
        )
    if as_json:
        print(json.dumps({"coordinator": coordinator, "agents": rows}, ensure_ascii=False, indent=2))
    else:
        print(tr(f"Detected coordinator agent: {coordinator}", f"检测到的协调 Agent：{coordinator}"))
        for row in rows:
            print(f"{'✓' if row['available'] else '·'} {row['name']}: {row['executable']} — {row['description']}")
            if row["unavailable_reason"]:
                print(f"  {row['unavailable_reason']}")
    return 0


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
    configure_utf8_stdio()
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
