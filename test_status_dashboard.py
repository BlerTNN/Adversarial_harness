import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

import status_dashboard


class StatusDashboardTests(unittest.TestCase):
    def write_run(self, root: Path, name: str, created_at: str, **changes) -> Path:
        run = root / "runs" / name
        run.mkdir(parents=True)
        state = {
            "schema_version": "generic-harness/v1",
            "run_id": name,
            "status": "RUNNING",
            "phase": "build",
            "request": f"request-{name}",
            "workspace": str(root / "workspaces" / name),
            "coordinator_agent": "agent-a",
            "worker_agent": "agent-b",
            "reviewer_agent": "agent-c",
            "review_index": 0,
            "max_reviews": 3,
            "active_agent": "agent-b",
            "last_error": "",
            "created_at": created_at,
            "updated_at": created_at,
            "finished_at": None,
        }
        state.update(changes)
        (run / "state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        return run

    def test_status_payload_reads_only_generic_runs_and_sorts_newest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self.write_run(root, "old", "2026-01-01T10:00:00")
            new = self.write_run(
                root,
                "new",
                "2026-01-02T10:00:00",
                phase="review",
                active_agent="agent-c",
                review_index=1,
                last_error="authorization=do-not-expose",
            )
            legacy = root / "runs" / "legacy"
            legacy.mkdir()
            (legacy / "state.json").write_text(
                json.dumps({"schema_version": "old/v1", "run_id": "legacy"}),
                encoding="utf-8",
            )
            (old / "harness.log").write_text("old log\n", encoding="utf-8")
            (new / "harness.log").write_text(
                "api_key=do-not-show\nharness ready\n", encoding="utf-8"
            )
            (new / "events.jsonl").write_text('{"event":"phase"}\n', encoding="utf-8")
            worker = new / "iterations" / "00"
            worker.mkdir(parents=True)
            (worker / "worker.log").write_text("worker output\n", encoding="utf-8")
            reviewer = new / "reviews" / "00"
            reviewer.mkdir(parents=True)
            (reviewer / "reviewer.log").write_text("review output\n", encoding="utf-8")
            (new / "FINAL_REPORT.md").write_text("# Done\n", encoding="utf-8")

            payload = status_dashboard.status_payload(root)

            self.assertEqual([run["run_id"] for run in payload["runs"]], ["new", "old"])
            self.assertEqual(payload["current"]["run_id"], "new")
            self.assertEqual(payload["current"]["phase"], "review")
            self.assertEqual(payload["current"]["active_agent"], "agent-c")
            self.assertTrue(payload["current"]["report_available"])
            self.assertEqual(
                {log["path"] for log in payload["current"]["logs"]},
                {
                    "harness.log",
                    "events.jsonl",
                    "iterations/00/worker.log",
                    "reviews/00/reviewer.log",
                },
            )
            combined = "\n".join(log["text"] for log in payload["current"]["logs"])
            self.assertIn("[REDACTED]", combined)
            self.assertNotIn("do-not-show", combined)
            self.assertEqual(payload["current"]["last_error"], "authorization=[REDACTED]")

    def test_status_payload_is_empty_without_valid_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = status_dashboard.status_payload(Path(temporary))
            self.assertIsNone(payload["current"])
            self.assertEqual(payload["runs"], [])

    def test_api_status_and_page_are_served_locally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_run(root, "run-1", "2026-01-01T10:00:00")
            server = status_dashboard.make_server(root, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
                    payload = json.load(response)
                with urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                    page = response.read().decode()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            self.assertEqual(payload["current"]["run_id"], "run-1")
            self.assertIn("Generic Task Harness", page)
            self.assertIn("通用任务 Harness", page)
            self.assertIn("setLanguage('zh')", page)
            self.assertIn("setLanguage('en')", page)
            self.assertIn("setInterval(refresh,2000)", page)

    def test_page_has_only_dynamic_generic_roles(self):
        self.assertIn("coordinator_agent", status_dashboard.PAGE)
        self.assertIn("worker_agent", status_dashboard.PAGE)
        self.assertIn("reviewer_agent", status_dashboard.PAGE)
        self.assertIn("Active agent", status_dashboard.PAGE)
        self.assertIn("当前活动角色", status_dashboard.PAGE)
        self.assertNotIn("Codex", status_dashboard.PAGE)
        self.assertNotIn("Hermes", status_dashboard.PAGE)
        self.assertNotIn("知识游戏", status_dashboard.PAGE)

    def test_dashboard_refuses_non_loopback_binding(self):
        with self.assertRaisesRegex(ValueError, "localhost"):
            status_dashboard.make_server(Path.cwd(), "0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
