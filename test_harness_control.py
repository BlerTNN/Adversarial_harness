import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import harness
import harness_control
import platform_support
from platform_support import WINDOWS, file_lock, process_group_kwargs
from test_harness import FakeHarnessEnvironment


class HarnessControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = FakeHarnessEnvironment(self.root)
        self.current_file = self.root / ".harness-current"
        self.control_lock = self.root / ".harness-control.lock"
        for target, value in (
            ("CURRENT_FILE", self.current_file),
            ("CONTROL_LOCK", self.control_lock),
        ):
            mocked = patch.object(harness_control, target, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        coordinator = patch.dict(
            os.environ,
            {"HARNESS_COORDINATOR_AGENT": "alpha", "HARNESS_LANG": "en"},
        )
        coordinator.start()
        self.addCleanup(coordinator.stop)

    def start_args(self, request: str, **overrides) -> SimpleNamespace:
        values = {
            "request": request,
            "request_file": None,
            "config": self.environment.config,
            "runs_dir": self.environment.runs,
            "workspace": self.environment.workspace,
            "coordinator_agent": None,
            "worker_agent": None,
            "reviewer_agent": None,
            "max_reviews": None,
            "foreground": True,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def detached_supervisor_available(self) -> bool:
        if not WINDOWS:
            return True
        try:
            platform_support._windows_detached_creation_flags()
        except RuntimeError as error:
            if "does not permit" in str(error):
                return False
            raise
        return True

    def require_detached_supervisor(self) -> None:
        if not self.detached_supervisor_available():
            self.skipTest("host Job Object forbids a detached Supervisor")

    def test_terminal_run_allows_a_second_distinct_run(self):
        self.assertEqual(harness_control.start(self.start_args("Build the first website.")), 0)
        first = Path(json.loads(self.current_file.read_text(encoding="utf-8"))["run_dir"])
        self.assertEqual(harness.read_json(first / "state.json")["status"], "COMPLETE")

        self.assertEqual(harness_control.start(self.start_args("Add a second independent tool.")), 0)
        second = Path(json.loads(self.current_file.read_text(encoding="utf-8"))["run_dir"])

        self.assertNotEqual(first, second)
        self.assertEqual(harness.read_json(second / "state.json")["status"], "COMPLETE")
        self.assertEqual(len(harness_control.generic_runs(self.environment.runs)), 2)
        self.assertEqual(
            {harness.read_json(path / "state.json")["request"] for path in (first, second)},
            {"Build the first website.", "Add a second independent tool."},
        )

    def test_unfinished_run_rejects_duplicate_start(self):
        existing = self.environment.create_run("Keep this task queued.")
        self.current_file.write_text(str(existing) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(harness_control.ControlError, "unfinished task"):
            harness_control.start(self.start_args("Do not create this duplicate."))

        self.assertEqual(harness_control.generic_runs(self.environment.runs), [existing])

    def test_unfinished_current_run_blocks_a_different_runs_directory(self):
        existing = self.environment.create_run("Keep the global task queued.")
        harness_control.write_current(existing, self.environment.runs)

        with self.assertRaisesRegex(harness_control.ControlError, "unfinished task"):
            harness_control.start(
                self.start_args(
                    "Do not start in another runs directory.",
                    runs_dir=self.root / "other-runs",
                )
            )

        self.assertFalse((self.root / "other-runs").exists())

    def test_stop_then_continue_resumes_the_exact_run(self):
        run_dir = self.environment.create_run("Build a resumable task.")
        self.current_file.write_text(str(run_dir) + "\n", encoding="utf-8")
        current = patch.object(harness_control, "current_run", return_value=run_dir)
        current.start()
        self.addCleanup(current.stop)

        self.assertEqual(harness_control.stop_run(), 0)
        self.assertTrue((run_dir / harness.PAUSE_FILE).is_file())
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")

        self.assertEqual(harness_control.continue_run(SimpleNamespace(foreground=True)), 0)

        self.assertFalse((run_dir / harness.PAUSE_FILE).exists())
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "COMPLETE")
        self.assertEqual(harness_control.generic_runs(self.environment.runs), [run_dir])

    def test_unverifiable_child_identity_blocks_continue_without_clearing_state(self):
        run_dir = self.environment.create_run("Keep an unverifiable child record.")
        state = harness.read_json(run_dir / "state.json")
        state["active_agent"] = {
            "profile": "alpha",
            "role": "TASK_WORKER",
            "pid": os.getpid(),
            "process_group": os.getpid(),
            "pid_started": "saved-token",
        }
        harness.write_json(run_dir / "state.json", state)

        with patch.object(harness_control, "current_run", return_value=run_dir), patch.object(
            harness,
            "process_group_identity_status",
            return_value="unknown",
        ), patch.object(
            harness,
            "process_identity_status",
            return_value="unknown",
        ):
            with self.assertRaisesRegex(harness_control.ControlError, "cannot be verified"):
                harness_control.continue_run(SimpleNamespace(foreground=False))

        self.assertIsInstance(harness.read_json(run_dir / "state.json")["active_agent"], dict)

    def test_unverifiable_child_identity_blocks_stop_without_clearing_state(self):
        run_dir = self.environment.create_run("Keep an unverifiable orphan record.")
        state = harness.read_json(run_dir / "state.json")
        state["active_agent"] = {
            "profile": "alpha",
            "role": "TASK_WORKER",
            "pid": os.getpid(),
            "process_group": os.getpid(),
            "pid_started": "saved-token",
        }
        harness.write_json(run_dir / "state.json", state)

        with patch.object(harness_control, "current_run", return_value=run_dir), patch.object(
            harness,
            "process_group_identity_status",
            return_value="unknown",
        ), patch.object(
            harness,
            "process_identity_status",
            return_value="unknown",
        ):
            with self.assertRaisesRegex(harness_control.ControlError, "cannot be verified"):
                harness_control.stop_run()

        self.assertIsInstance(harness.read_json(run_dir / "state.json")["active_agent"], dict)

    def test_foreground_execution_does_not_hold_the_global_control_lock(self):
        def execute_without_control_lock(_run_dir):
            with file_lock(self.control_lock, blocking=False):
                return 0

        with patch.object(harness_control, "execute_run", side_effect=execute_without_control_lock):
            self.assertEqual(
                harness_control.start(self.start_args("Run safely in the foreground.")),
                0,
            )

    def test_stop_cannot_be_cancelled_by_a_worker_replacing_the_legacy_marker(self):
        self.require_detached_supervisor()
        run_dir = self.environment.create_run("Stop even if a worker fights the visible pause marker.")
        self.environment.set_scenario(run_dir, worker_fights_pause=True)
        harness_control.write_current(run_dir, self.environment.runs)
        self.addCleanup(lambda: harness.clear_pause_request(run_dir))

        supervisor = harness_control.spawn_run(run_dir)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            active = harness.read_json(run_dir / "state.json").get("active_agent")
            if isinstance(active, dict) and active.get("role") == "TASK_WORKER":
                break
            time.sleep(0.05)
        else:
            self.fail("worker did not become active")

        self.assertEqual(harness_control.stop_run(), 0)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = harness.read_json(run_dir / "state.json")
            if state.get("status") == "PAUSED" and harness_control.supervisor_pid(run_dir) is None:
                break
            time.sleep(0.05)
        else:
            self.fail("trusted pause request did not stop the supervisor")

        self.assertEqual(state["status"], "PAUSED")
        self.assertIsNone(state["active_agent"])
        self.assertTrue(harness.pause_request_path(run_dir).is_file())
        deadline = time.monotonic() + 5
        while (
            supervisor in harness_control._DETACHED_PROCESSES
            and time.monotonic() < deadline
        ):
            harness_control._reap_detached_processes()
            time.sleep(0.02)
        self.assertNotIn(supervisor, harness_control._DETACHED_PROCESSES)
        self.assertTrue((run_dir / harness.PAUSE_FILE).is_dir())

    def test_status_reports_persisted_role_selection(self):
        run_dir = self.environment.create_run(
            "Build with mixed profiles.",
            coordinator_agent="alpha",
            worker_agent="beta",
            reviewer_agent="gamma",
        )
        current = patch.object(harness_control, "current_run", return_value=run_dir)
        current.start()
        self.addCleanup(current.stop)
        with patch.object(harness_control, "supervisor_pid", return_value=None):
            payload = harness_control.status_payload()

        self.assertEqual(payload["status"], "QUEUED")
        self.assertEqual(
            (payload["coordinator_agent"], payload["worker_agent"], payload["reviewer_agent"]),
            ("alpha", "beta", "gamma"),
        )
        self.assertFalse(payload["harness_running"])
        self.assertEqual(payload["run_dir"], str(run_dir))

    def test_status_reports_structured_review_progress(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run("Report Review v2 evidence.")
        review_dir = run_dir / "reviews" / "00"
        review_dir.mkdir(parents=True)
        (review_dir / "REVIEW_PLAN.json").write_text(
            json.dumps(
                {
                    "requirements": [{"id": "REQ-REQUEST"}],
                    "worker_claims": [{"id": "CLAIM-SUMMARY"}],
                    "risks": [{"id": "RISK-001"}],
                    "checks": [{"id": "CHK-001"}, {"id": "CHK-002"}],
                }
            ),
            encoding="utf-8",
        )
        (review_dir / "REVIEW_CHECKS.json").write_text(
            json.dumps({"results": [{"status": "pass"}, {"status": "not_run"}]}),
            encoding="utf-8",
        )
        (review_dir / "AUDIT.json").write_text(
            json.dumps({"verdict": "INCONCLUSIVE"}), encoding="utf-8"
        )
        (review_dir / "FINAL_REVIEW.json").write_text(
            json.dumps(
                {
                    "verdict": "INCONCLUSIVE",
                    "reason_codes": ["BLOCKING_CHECK_UNAVAILABLE"],
                }
            ),
            encoding="utf-8",
        )
        with patch.object(harness_control, "current_run", return_value=run_dir), patch.object(
            harness_control, "supervisor_pid", return_value=None
        ):
            payload = harness_control.status_payload()

        self.assertEqual(payload["review_protocol_version"], 2)
        self.assertEqual(payload["review_plan"], {"requirements": 1, "claims": 1, "risks": 1, "checks": 2})
        self.assertEqual(payload["review_checks"], {"pass": 1, "fail": 0, "error": 0, "not_run": 1})
        self.assertEqual(payload["reviewer_verdict"], "INCONCLUSIVE")
        self.assertEqual(payload["final_review_reason_codes"], ["BLOCKING_CHECK_UNAVAILABLE"])

    def test_plain_status_output_supports_english_and_chinese(self):
        with patch.object(harness_control, "current_run", return_value=None):
            for language, expected in (("en", "Status: IDLE"), ("zh-CN", "状态：IDLE")):
                with self.subTest(language=language), patch.dict(
                    os.environ, {"HARNESS_LANG": language}
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(harness_control.print_status(False), 0)
                    self.assertIn(expected, output.getvalue())

    def test_cli_forces_utf8_when_redirected_under_an_ascii_code_page(self):
        environment = os.environ.copy()
        environment.update(
            {
                "HARNESS_LANG": "zh-CN",
                "PYTHONIOENCODING": "ascii",
                "PYTHONUTF8": "0",
            }
        )
        result = subprocess.run(
            [sys.executable, str(Path(harness_control.__file__).resolve()), "status"],
            cwd=Path(harness_control.__file__).resolve().parent,
            env=environment,
            capture_output=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn("状态：", result.stdout.decode("utf-8"))

    def test_unsafe_detached_supervisor_failure_pauses_the_run(self):
        run_dir = self.environment.create_run("Preserve a run when safe detachment is unavailable.")

        with patch.object(
            harness_control,
            "spawn_detached_process",
            side_effect=RuntimeError("host Job does not allow breakaway"),
        ):
            with self.assertRaisesRegex(harness_control.ControlError, "safe detached Supervisor"):
                harness_control.spawn_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual(state["status"], "PAUSED")
        self.assertIn("host Job does not allow breakaway", state["last_error"])
        self.assertFalse((run_dir / "harness.pid").exists())

    @unittest.skipUnless(WINDOWS, "Windows restricted-Job foreground recovery")
    def test_windows_restricted_host_job_recovers_the_same_run_in_foreground(self):
        if self.detached_supervisor_available():
            self.skipTest("host permits a detached Supervisor")
        run_dir = self.environment.create_run("Recover a restricted Windows run in foreground.")

        with self.assertRaisesRegex(
            harness_control.ControlError,
            "does not permit a detached Supervisor",
        ):
            harness_control.spawn_run(run_dir)

        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")
        self.assertFalse(harness.supervisor_marker_path(run_dir).exists())
        self.assertFalse((run_dir / "harness.pid").exists())
        self.assertEqual(harness.execute_run(run_dir), 0)
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "COMPLETE")
        self.assertTrue((self.environment.workspace / "index.html").is_file())

    def test_supervisor_marker_publication_failures_stop_the_unpublished_process(self):
        real_write_json = harness_control.write_json

        class UnpublishedProcess:
            pid = 424242

            def __init__(self):
                self.killed = False
                self.waited = False

            def poll(self):
                return 1 if self.killed else None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                self.waited = True
                self.killed = True
                return 1

        for failed_marker in ("trusted", "legacy"):
            with self.subTest(failed_marker=failed_marker):
                run_dir = self.environment.create_run(
                    f"Fail {failed_marker} Supervisor marker publication."
                )
                trusted = harness.supervisor_marker_path(run_dir)
                legacy = run_dir / "harness.pid"
                target = trusted if failed_marker == "trusted" else legacy
                process = UnpublishedProcess()

                def fail_target(path, value):
                    if path == target:
                        raise PermissionError("simulated marker publication failure")
                    return real_write_json(path, value)

                with patch.object(
                    harness_control,
                    "spawn_detached_process",
                    return_value=process,
                ), patch.object(
                    harness_control,
                    "managed_process_start_time",
                    return_value="saved-token",
                ), patch.object(
                    harness_control,
                    "write_json",
                    side_effect=fail_target,
                ):
                    with self.assertRaisesRegex(
                        harness_control.ControlError,
                        "publish the detached Supervisor identity safely",
                    ):
                        harness_control.spawn_run(run_dir)

                self.assertTrue(process.killed)
                self.assertTrue(process.waited)
                self.assertFalse(trusted.exists())
                self.assertFalse(legacy.exists())
                state = harness.read_json(run_dir / "state.json")
                self.assertEqual(state["status"], "PAUSED")
                self.assertIn("marker publication failure", state["last_error"])

    def test_supervisor_exit_during_publication_is_not_reported_as_submitted(self):
        run_dir = self.environment.create_run("Detect an early Supervisor exit.")

        class ExitingProcess:
            pid = 424243

            def __init__(self):
                self.polls = 0
                self.waited = False

            def poll(self):
                self.polls += 1
                return None if self.polls == 1 else 7

            def wait(self, timeout=None):
                self.waited = True
                return 7

        process = ExitingProcess()
        with patch.object(
            harness_control,
            "spawn_detached_process",
            return_value=process,
        ), patch.object(
            harness_control,
            "managed_process_start_time",
            return_value="saved-token",
        ):
            with self.assertRaisesRegex(
                harness_control.ControlError,
                "exited before accepting the run",
            ):
                harness_control.spawn_run(run_dir)

        self.assertTrue(process.waited)
        self.assertFalse(harness.supervisor_marker_path(run_dir).exists())
        self.assertFalse((run_dir / "harness.pid").exists())
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")

    def test_agents_json_reports_an_unsafe_windows_batch_profile_as_unavailable(self):
        wrapper = self.root / "unsafe-agent.cmd"
        wrapper.write_text("exit 0\n", encoding="utf-8")
        wrapper.chmod(0o755)
        config = harness.read_json(self.environment.config)
        config["agents"]["alpha"]["command"] = [str(wrapper), "{prompt}"]
        harness.write_json(self.environment.config, config)
        output = io.StringIO()

        with patch.object(harness, "WINDOWS", True), redirect_stdout(output):
            self.assertEqual(harness_control.list_agents(self.environment.config, True), 0)

        payload = json.loads(output.getvalue())
        alpha = next(row for row in payload["agents"] if row["name"] == "alpha")
        self.assertFalse(alpha["available"])
        self.assertIn("runtime placeholders", alpha["unavailable_reason"])

    def test_language_falls_back_to_the_system_locale(self):
        for locale_name in ("zh_CN", "Chinese (Simplified)_China"):
            with self.subTest(locale_name=locale_name), patch.dict(os.environ, {}, clear=True), patch.object(
                harness_control.locale, "getlocale", return_value=(locale_name, "UTF-8")
            ):
                self.assertEqual(harness_control.ui_language(), "zh")

    def test_supervisor_marker_rejects_the_wrong_creation_token(self):
        run_dir = self.environment.create_run("Verify supervisor process identity.")
        marker = run_dir / "harness.pid"
        harness.write_process_marker(marker, os.getpid())

        self.assertEqual(harness_control.supervisor_pid(run_dir), os.getpid())
        record = harness.read_json(marker)
        record["pid_started"] += "-wrong"
        harness.write_json(marker, record)
        self.assertIsNone(harness_control.supervisor_pid(run_dir))

    def test_detached_supervisor_completes_the_original_run(self):
        self.require_detached_supervisor()
        run_dir = self.environment.create_run("Complete in the detached supervisor.")
        launcher = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; import harness_control; "
                "harness_control.spawn_run(Path(sys.argv[1]))",
                str(run_dir),
            ],
            cwd=Path(harness_control.__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(launcher.returncode, 0, launcher.stderr)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = harness.read_json(run_dir / "state.json")
            if state["status"] in harness.TERMINAL_STATUSES:
                break
            time.sleep(0.1)
        else:
            marker = harness.read_process_marker(run_dir / "harness.pid")
            if marker:
                harness.terminate_process_group(*marker, grace=0.2)
            self.fail("detached supervisor did not finish")

        self.assertEqual(state["status"], "COMPLETE")
        self.assertTrue((self.environment.workspace / "index.html").is_file())
        deadline = time.monotonic() + 5
        while (run_dir / "harness.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse((run_dir / "harness.pid").exists())
        harness_control._reap_detached_processes()

    def test_fast_detached_supervisor_never_leaves_a_stale_marker(self):
        self.require_detached_supervisor()
        for index in range(5):
            run_dir = self.environment.create_run(f"Finish immediately {index}.")
            state = harness.read_json(run_dir / "state.json")
            state.update({"status": "COMPLETE", "finished_at": harness.now()})
            harness.write_json(run_dir / "state.json", state)

            harness_control.spawn_run(run_dir)

            deadline = time.monotonic() + 5
            while (
                harness_control.supervisor_pid(run_dir) is not None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            while (run_dir / "harness.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse((run_dir / "harness.pid").exists())
        harness_control._reap_detached_processes()

    def test_current_marker_cannot_escape_its_recorded_runs_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "state.json").write_text(
            json.dumps({"schema_version": harness.STATE_SCHEMA, "status": "PAUSED"}),
            encoding="utf-8",
        )
        self.current_file.write_text(
            json.dumps({"run_dir": str(outside), "runs_dir": str(self.environment.runs)}),
            encoding="utf-8",
        )

        self.assertIsNone(harness_control.current_run(self.environment.runs))

    def test_continue_keeps_pause_marker_until_old_run_lock_is_free(self):
        run_dir = self.environment.create_run("Wait for the old supervisor.")
        harness.request_pause(run_dir)
        current = patch.object(harness_control, "current_run", return_value=run_dir)
        current.start()
        self.addCleanup(current.stop)
        with file_lock(run_dir / "run.lock", blocking=False):
            with self.assertRaisesRegex(harness_control.ControlError, "run lock"):
                harness_control.continue_run(SimpleNamespace(foreground=True))

        self.assertTrue((run_dir / harness.PAUSE_FILE).is_file())
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "QUEUED")

    @unittest.skipIf(WINDOWS, "Windows unmanaged PID termination is intentionally refused")
    def test_continue_refuses_a_live_orphan_child_and_stop_terminates_it(self):
        run_dir = self.environment.create_run("Recover without duplicate workers.")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **process_group_kwargs(),
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        state = harness.read_json(run_dir / "state.json")
        state["active_agent"] = {
            "profile": "alpha",
            "role": "TASK_WORKER",
            "pid": child.pid,
            "process_group": child.pid,
            "pid_started": harness.pid_start_time(child.pid),
        }
        harness.write_json(run_dir / "state.json", state)
        current = patch.object(harness_control, "current_run", return_value=run_dir)
        current.start()
        self.addCleanup(current.stop)

        with self.assertRaisesRegex(harness_control.ControlError, "still running"):
            harness_control.continue_run(SimpleNamespace(foreground=True))
        self.assertIsNone(child.poll())

        self.assertEqual(harness_control.stop_run(), 0)
        child.wait(timeout=5)
        stopped = harness.read_json(run_dir / "state.json")
        self.assertEqual(stopped["status"], "PAUSED")
        self.assertIsNone(stopped["active_agent"])

    @unittest.skipUnless(WINDOWS, "Windows Job Object safety")
    def test_windows_stop_preserves_a_live_unmanaged_orphan_identity(self):
        run_dir = self.environment.create_run("Refuse unsafe PID-only termination.")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

        def cleanup_child() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

        self.addCleanup(cleanup_child)
        state = harness.read_json(run_dir / "state.json")
        state["active_agent"] = {
            "profile": "legacy",
            "role": "TASK_WORKER",
            "pid": child.pid,
            "process_group": child.pid,
            "pid_started": harness.pid_start_time(child.pid),
        }
        harness.write_json(run_dir / "state.json", state)
        current = patch.object(harness_control, "current_run", return_value=run_dir)
        current.start()
        self.addCleanup(current.stop)

        with self.assertRaisesRegex(harness_control.ControlError, "could not be terminated safely"):
            harness_control.stop_run()

        self.assertIsNone(child.poll())
        self.assertIsNotNone(harness.read_json(run_dir / "state.json")["active_agent"])


if __name__ == "__main__":
    unittest.main()
