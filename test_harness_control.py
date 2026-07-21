import json
import fcntl
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import harness
import harness_control
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
        with (run_dir / "run.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(harness_control.ControlError, "run lock"):
                harness_control.continue_run(SimpleNamespace(foreground=True))

        self.assertTrue((run_dir / harness.PAUSE_FILE).is_file())
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
