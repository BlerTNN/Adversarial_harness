import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import harness


FAKE_AGENT = r'''import json
import os
import shutil
import sys
from pathlib import Path

profile, role, run_value, workspace_value, prompt_value = sys.argv[1:]
run_dir = Path(run_value)
workspace = Path(workspace_value)
prompt_path = Path(prompt_value)
state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
index = int(state.get("review_index", 0))
scenario_path = run_dir / "fake-scenario.json"
scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.is_file() else {}
event = {
    "pid": os.getpid(),
    "profile": profile,
    "role": role,
    "index": index,
    "workspace": str(workspace),
    "prompt": str(prompt_path.relative_to(run_dir)),
    "workspace_git_exists": (workspace / ".git").exists(),
}
live_workspace = Path(state["workspace"])
probe = scenario.get("live_probe")
if probe:
    probe_path = live_workspace / probe
    event["live_probe_exists"] = probe_path.is_file()
    event["live_probe_text"] = probe_path.read_text(encoding="utf-8") if probe_path.is_file() else None
candidate_probe = scenario.get("candidate_probe")
if candidate_probe:
    candidate_probe_path = workspace / candidate_probe
    event["candidate_probe_text"] = (
        candidate_probe_path.read_text(encoding="utf-8") if candidate_probe_path.is_file() else None
    )
with (run_dir / "fake-processes.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(event) + "\n")

if role == "TASK_WORKER":
    mode = scenario.get("worker_mode", "valid")
    if mode == "missing":
        raise SystemExit(0)
    if index and scenario.get("worker_tampers_prior_audit"):
        prior_audit = run_dir / "reviews" / f"{index - 1:02d}" / "AUDIT.json"
        prior_audit.write_text('{"tampered": true}', encoding="utf-8")
    (run_dir / "PLAN.md").write_text(
        f"# Plan\n\nImplement the requested task.\n\nRound: {index + 1}\n",
        encoding="utf-8",
    )
    if mode == "invalid_json":
        (run_dir / "WORKER_RESULT.json").write_text("{not-json", encoding="utf-8")
        raise SystemExit(0)
    workspace.mkdir(parents=True, exist_ok=True)
    worker_file = scenario.get("worker_file", "index.html")
    worker_contents = scenario.get("worker_contents")
    if isinstance(worker_contents, list):
        worker_content = worker_contents[min(index, len(worker_contents) - 1)]
    elif isinstance(worker_contents, str):
        worker_content = worker_contents
    else:
        worker_content = f"<!doctype html><title>Shop</title><main data-round='{index + 1}'>Storefront and cart</main>\n"
    worker_path = workspace / worker_file
    worker_path.parent.mkdir(parents=True, exist_ok=True)
    worker_path.write_text(worker_content, encoding="utf-8")
    deleted_files = list(scenario.get("worker_delete_files", []))
    for deleted_file in deleted_files:
        target = workspace / deleted_file
        if target.is_file() or target.is_symlink():
            target.unlink()
    if scenario.get("worker_unreported_file"):
        (workspace / "unreported.txt").write_text("missing from handoff\n", encoding="utf-8")
    if scenario.get("worker_modifies_live_workspace"):
        live_workspace.mkdir(parents=True, exist_ok=True)
        (live_workspace / "worker-touched.txt").write_text("not allowed\n", encoding="utf-8")
    result = {
        "schema_version": "generic-harness/worker-result/v1",
        "status": "blocked" if mode == "blocked" else "complete",
        "summary": f"Implemented the requested website in round {index + 1}.",
        "changed_files": list(scenario.get("changed_files", [worker_file, *deleted_files])),
        "checks": [
            {
                "name": "fixture check",
                "command": "inspect index.html",
                "status": "fail" if mode == "failed_check" else "pass",
                "details": "Storefront exists.",
            }
        ],
        "limitations": [],
    }
    (run_dir / "WORKER_RESULT.json").write_text(
        json.dumps(result, ensure_ascii=False),
        encoding="utf-8",
    )
    if scenario.get("worker_prewrites_audit"):
        forged = run_dir / "reviews" / f"{index:02d}"
        forged.mkdir(parents=True, exist_ok=True)
        (forged / "AUDIT.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    if scenario.get("worker_exit_nonzero"):
        raise SystemExit(7)
    raise SystemExit(0)

mode = scenario.get("audit_mode", "valid")
review_dir = prompt_path.parent
review_dir.mkdir(parents=True, exist_ok=True)
if mode == "missing":
    raise SystemExit(0)
if mode == "invalid_json":
    (review_dir / "AUDIT.json").write_text("{not-json", encoding="utf-8")
    raise SystemExit(0)
if mode == "invalid_verdict":
    verdict = "MAYBE"
else:
    verdicts = scenario.get("verdicts", ["PASS"])
    verdict = verdicts[min(index, len(verdicts) - 1)]
issues = []
if verdict == "FIX":
    issues.append(
        {
            "severity": "major",
            "location": "index.html",
            "title": "Checkout needs repair",
            "evidence": "The requested checkout behavior is incomplete.",
            "required_fix": "Complete checkout behavior.",
            "acceptance_test": "Exercise checkout from cart to confirmation.",
        }
    )
    for minor_index in range(int(scenario.get("minor_count", 0))):
        issues.append(
            {
                "severity": "minor",
                "location": "index.html",
                "title": f"Minor issue {minor_index + 1} also needs repair",
                "evidence": f"Observed minor issue {minor_index + 1}.",
                "required_fix": f"Resolve minor issue {minor_index + 1}.",
                "acceptance_test": f"Verify minor issue {minor_index + 1} after repair.",
            }
        )
if scenario.get("minor_only"):
    issues[0]["severity"] = "minor"
if scenario.get("reviewer_modifies_workspace"):
    (workspace / "reviewer-touched.txt").write_text("not allowed\n", encoding="utf-8")
if scenario.get("reviewer_modifies_live_workspace"):
    (Path(state["workspace"]) / "reviewer-touched.txt").write_text("not allowed\n", encoding="utf-8")
if scenario.get("reviewer_modifies_candidate_workspace"):
    (Path(state["candidate_workspace"]) / "reviewer-injected.txt").write_text("not allowed\n", encoding="utf-8")
if scenario.get("reviewer_replaces_candidate_root"):
    candidate_root = Path(state["candidate_workspace"])
    shutil.rmtree(candidate_root)
    candidate_root.symlink_to(Path(scenario["candidate_symlink_target"]), target_is_directory=True)
if scenario.get("agent_tampers_state"):
    state["request"] = "tampered"
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
if scenario.get("reviewer_tampers_evidence"):
    verification_path = run_dir / "iterations" / f"{index:02d}" / "VERIFICATION.json"
    verification_path.unlink()
    verification_path.symlink_to(Path(scenario["evidence_symlink_target"]))
    (review_dir / "artifact.json").write_text('{"tampered": true}', encoding="utf-8")
audit = {
    "schema_version": "generic-harness/audit/v1",
    "verdict": verdict,
    "summary": "Checkout needs repair." if verdict == "FIX" else "Independent checks passed.",
    "checks": [
        {
            "name": "independent fixture check",
            "command": "inspect index.html",
            "status": "fail" if verdict == "FIX" or scenario.get("audit_failed_check") else "pass",
            "details": "Observed the delivered workspace.",
        }
    ],
    "issues": issues,
    "limitations": [],
}
(review_dir / "AUDIT.json").write_text(
    json.dumps(audit, ensure_ascii=False),
    encoding="utf-8",
)
if scenario.get("reviewer_exit_nonzero"):
    raise SystemExit(7)
'''


class FakeHarnessEnvironment:
    """One local Python executable stands in for every configured agent."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs = root / "runs"
        self.workspace = root / "workspace"
        self.fake_agent = root / "fake_agent.py"
        self.fake_agent.write_text(FAKE_AGENT, encoding="utf-8")
        self.config = root / "harness.config.json"
        profiles = {}
        for name in ("alpha", "beta", "gamma"):
            profiles[name] = {
                "description": f"fake {name}",
                "detect": [name],
                "tui": [name],
                "command": [
                    sys.executable,
                    str(self.fake_agent),
                    name,
                    "{role}",
                    "{run_dir}",
                    "{workspace}",
                    "{prompt_file}",
                ],
            }
        self.config.write_text(
            json.dumps(
                {
                    "workspace": "workspace",
                    "default_agent": "alpha",
                    "worker_agent": None,
                    "reviewer_agent": None,
                    "max_reviews": 3,
                    "timeout_seconds": 10,
                    "verification_commands": [[sys.executable, "-c", "pass"]],
                    "verification_timeout_seconds": 5,
                    "agents": profiles,
                }
            ),
            encoding="utf-8",
        )

    def create_run(self, request: str, **overrides) -> Path:
        return harness.create_run(
            request,
            config_path=self.config,
            runs_dir=self.runs,
            workspace=self.workspace,
            **overrides,
        )

    def set_verification(self, commands: list[list[str]], timeout: int = 5) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["verification_commands"] = commands
        config["verification_timeout_seconds"] = timeout
        self.config.write_text(json.dumps(config), encoding="utf-8")

    @staticmethod
    def set_scenario(run_dir: Path, **scenario) -> None:
        (run_dir / "fake-scenario.json").write_text(json.dumps(scenario), encoding="utf-8")

    @staticmethod
    def events(run_dir: Path) -> list[dict]:
        path = run_dir / "fake-processes.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = FakeHarnessEnvironment(Path(self.temporary.name))
        coordinator = patch.dict(os.environ, {"HARNESS_COORDINATOR_AGENT": "alpha"})
        coordinator.start()
        self.addCleanup(coordinator.stop)

    def test_default_profile_runs_independent_worker_and_reviewer_and_passes_website(self):
        self.environment.workspace.mkdir()
        (self.environment.workspace / ".git").mkdir()
        (self.environment.workspace / ".git" / "preserved").write_text("metadata\n", encoding="utf-8")
        (self.environment.workspace / "obsolete.txt").write_text("remove me\n", encoding="utf-8")
        self.environment.set_verification(
            [
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert 'Storefront' in Path('index.html').read_text(); Path('verification-only.tmp').write_text('discard')",
                ]
            ]
        )
        run_dir = self.environment.create_run(
            "Build a responsive ecommerce website with a storefront, cart, and checkout."
        )
        self.environment.set_scenario(
            run_dir,
            worker_delete_files=["obsolete.txt"],
            changed_files=["index.html", "obsolete.txt"],
            live_probe="index.html",
        )

        self.assertEqual(harness.execute_run(run_dir), 0)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual(state["status"], "COMPLETE")
        self.assertEqual(
            (state["coordinator_agent"], state["worker_agent"], state["reviewer_agent"]),
            ("alpha", "alpha", "alpha"),
        )
        events = self.environment.events(run_dir)
        self.assertEqual([event["role"] for event in events], ["TASK_WORKER", "TASK_REVIEWER"])
        self.assertEqual([event["profile"] for event in events], ["alpha", "alpha"])
        self.assertTrue(all(event["live_probe_exists"] is False for event in events))
        self.assertTrue(all(event["workspace_git_exists"] is False for event in events))
        self.assertNotEqual(events[0]["pid"], events[1]["pid"])
        self.assertEqual(harness.read_json(run_dir / "reviews" / "00" / "AUDIT.json")["verdict"], "PASS")
        self.assertTrue((self.environment.workspace / "index.html").is_file())
        self.assertFalse((self.environment.workspace / "obsolete.txt").exists())
        self.assertTrue((self.environment.workspace / ".git" / "preserved").is_file())
        self.assertFalse((self.environment.workspace / "verification-only.tmp").exists())
        self.assertFalse((run_dir / "candidate").exists())
        verification = harness.read_json(run_dir / "iterations" / "00" / "VERIFICATION.json")
        self.assertEqual(verification["status"], "pass")
        self.assertEqual(len(state["artifact_id"]), 64)
        artifact = harness.read_json(run_dir / state["artifact_path"])
        self.assertEqual(artifact["sha256"], state["artifact_id"])
        self.assertNotEqual(events[0]["workspace"], events[1]["workspace"])
        self.assertFalse(Path(events[1]["workspace"]).exists())
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((run_dir / "state.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((run_dir / "iterations" / "00" / "worker.log").stat().st_mode), 0o600)

        prompts = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (run_dir / "iterations" / "00" / "worker-prompt.md", run_dir / "reviews" / "00" / "reviewer-prompt.md")
        ).casefold()
        for obsolete_gate in (
            "gameplay_core_loop",
            "knowledge_centrality",
            "full-playthrough-trace",
            "build-knowledge-game",
            "one-screen",
        ):
            self.assertNotIn(obsolete_gate, prompts)
        self.assertFalse((run_dir / "manifest.json").exists())
        self.assertFalse((run_dir / "review-schema.json").exists())

    def test_failing_cli_verification_blocks_promotion(self):
        self.environment.workspace.mkdir()
        original = "print('stable release')\n"
        (self.environment.workspace / "app.py").write_text(original, encoding="utf-8")
        self.environment.set_verification([[sys.executable, "app.py"]])
        run_dir = self.environment.create_run("Repair the command-line application.", max_reviews=1)
        self.environment.set_scenario(
            run_dir,
            worker_file="app.py",
            worker_contents="raise SystemExit(1)\n",
            live_probe="app.py",
        )

        self.assertEqual(harness.execute_run(run_dir), 1)

        self.assertEqual((self.environment.workspace / "app.py").read_text(encoding="utf-8"), original)
        self.assertTrue((run_dir / "candidate").is_dir())
        self.assertEqual(harness.read_json(run_dir / "iterations" / "00" / "VERIFICATION.json")["status"], "fail")
        audit = harness.read_json(run_dir / "reviews" / "00" / "AUDIT.json")
        self.assertEqual(audit["verdict"], "FIX")
        self.assertIn("Harness-enforced verification failed", [issue["title"] for issue in audit["issues"]])
        self.assertTrue(
            all(event["live_probe_text"] == original for event in self.environment.events(run_dir))
        )

    def test_data_task_repairs_failed_verification_before_promotion(self):
        self.environment.workspace.mkdir()
        original = '{"ok": false, "source": "live"}\n'
        (self.environment.workspace / "data.json").write_text(original, encoding="utf-8")
        self.environment.set_verification(
            [[sys.executable, "-c", "import json; assert json.load(open('data.json'))['ok'] is True"]]
        )
        run_dir = self.environment.create_run("Produce validated JSON data.", max_reviews=2)
        self.environment.set_scenario(
            run_dir,
            worker_file="data.json",
            worker_contents=[
                '{"ok": false, "source": "candidate-1"}\n',
                '{"ok": true, "source": "candidate-2"}\n',
            ],
            live_probe="data.json",
            candidate_probe="data.json",
        )

        self.assertEqual(harness.execute_run(run_dir), 0)

        events = self.environment.events(run_dir)
        self.assertEqual(
            [event["role"] for event in events],
            ["TASK_WORKER", "TASK_REVIEWER", "TASK_WORKER", "TASK_REVIEWER"],
        )
        self.assertTrue(all(event["live_probe_text"] == original for event in events))
        worker_events = [event for event in events if event["role"] == "TASK_WORKER"]
        self.assertEqual(
            [event["candidate_probe_text"] for event in worker_events],
            [original, '{"ok": false, "source": "candidate-1"}\n'],
        )
        self.assertEqual(
            [harness.read_json(run_dir / "iterations" / f"{index:02d}" / "VERIFICATION.json")["status"] for index in range(2)],
            ["fail", "pass"],
        )
        self.assertEqual(
            [harness.read_json(run_dir / "reviews" / f"{index:02d}" / "AUDIT.json")["verdict"] for index in range(2)],
            ["FIX", "PASS"],
        )
        delivered = json.loads((self.environment.workspace / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(delivered, {"ok": True, "source": "candidate-2"})
        self.assertFalse((run_dir / "candidate").exists())

    def test_verification_configuration_is_mandatory_and_snapshotted(self):
        for invalid in (None, [], "python3 -m unittest"):
            with self.subTest(invalid=invalid):
                config = json.loads(self.environment.config.read_text(encoding="utf-8"))
                if invalid is None:
                    config.pop("verification_commands", None)
                else:
                    config["verification_commands"] = invalid
                self.environment.config.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(harness.HarnessError, "verification_commands"):
                    self.environment.create_run("Do not run without a deterministic gate.")

        passing = [[sys.executable, "-c", "pass"]]
        self.environment.set_verification(passing)
        run_dir = self.environment.create_run("Use the verification snapshot saved with this run.")
        self.environment.set_verification([[sys.executable, "-c", "raise SystemExit(1)"]])

        self.assertEqual(harness.execute_run(run_dir), 0)
        verification = harness.read_json(run_dir / "iterations" / "00" / "VERIFICATION.json")
        self.assertEqual([item["argv"] for item in verification["commands"]], passing)

    def test_workspace_symlink_boundaries_are_enforced(self):
        self.environment.workspace.mkdir()
        outside = self.environment.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (self.environment.workspace / "escape").symlink_to(Path("..") / outside.name)

        with self.assertRaisesRegex(harness.HarnessError, "symlink escapes"):
            self.environment.create_run("Do not follow a workspace escape.")

        (self.environment.workspace / "escape").unlink()
        (self.environment.workspace / ".git").mkdir()
        (self.environment.workspace / ".git" / "config").write_text("private metadata\n", encoding="utf-8")
        (self.environment.workspace / "visible-link").symlink_to(Path(".git") / "config")
        with self.assertRaisesRegex(harness.HarnessError, "omitted from isolated runs"):
            self.environment.create_run("Do not expose preserved Git metadata after promotion.")

        (self.environment.workspace / "visible-link").unlink()
        (self.environment.workspace / "directory-alias").symlink_to(Path("."), target_is_directory=True)
        with self.assertRaisesRegex(harness.HarnessError, "Directory workspace symlinks"):
            self.environment.create_run("Do not expose omitted content through a directory alias.")

    def test_reviewer_cannot_tamper_with_bound_verification_evidence(self):
        victim = self.environment.root / "victim.txt"
        victim.write_text("must remain unchanged\n", encoding="utf-8")
        run_dir = self.environment.create_run("Protect verification and artifact evidence.")
        self.environment.set_scenario(
            run_dir,
            reviewer_tampers_evidence=True,
            evidence_symlink_target=str(victim),
        )

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness.execute_run(run_dir)

        self.assertEqual(victim.read_text(encoding="utf-8"), "must remain unchanged\n")
        self.assertFalse((run_dir / "iterations" / "00" / "VERIFICATION.json").is_symlink())
        self.assertEqual(
            harness.read_json(run_dir / "iterations" / "00" / "VERIFICATION.json")["status"],
            "pass",
        )
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")

    def test_failed_children_still_enforce_workspace_postconditions(self):
        worker_run = self.environment.create_run("Detect a failed worker touching the formal workspace.")
        self.environment.set_scenario(
            worker_run,
            worker_modifies_live_workspace=True,
            worker_exit_nonzero=True,
        )
        with self.assertRaisesRegex(harness.HarnessError, "Live workspace changed"):
            harness.execute_run(worker_run)
        self.assertEqual(harness.read_json(worker_run / "state.json")["status"], "PAUSED")

        self.environment.workspace = self.environment.root / "second-workspace"
        reviewer_run = self.environment.create_run("Restore candidate changes made by a failed reviewer.")
        self.environment.set_scenario(
            reviewer_run,
            reviewer_modifies_candidate_workspace=True,
            reviewer_exit_nonzero=True,
        )
        with self.assertRaisesRegex(harness.HarnessError, "candidate was restored"):
            harness.execute_run(reviewer_run)
        self.assertFalse((reviewer_run / "candidate" / "reviewer-injected.txt").exists())
        self.assertFalse((self.environment.workspace / "index.html").exists())
        self.assertEqual(harness.read_json(reviewer_run / "state.json")["status"], "PAUSED")

        self.environment.set_scenario(reviewer_run)
        self.assertEqual(harness.execute_run(reviewer_run), 0)
        self.assertFalse((self.environment.workspace / "reviewer-injected.txt").exists())

    def test_reviewer_candidate_root_symlink_is_safely_restored(self):
        victim = self.environment.root / "victim-directory"
        victim.mkdir()
        (victim / "user.txt").write_text("do not overwrite\n", encoding="utf-8")
        run_dir = self.environment.create_run("Do not follow a replaced candidate root.")
        self.environment.set_scenario(
            run_dir,
            reviewer_replaces_candidate_root=True,
            candidate_symlink_target=str(victim),
        )

        with self.assertRaisesRegex(harness.HarnessError, "candidate was restored"):
            harness.execute_run(run_dir)

        candidate = run_dir / "candidate"
        self.assertTrue(candidate.is_dir())
        self.assertFalse(candidate.is_symlink())
        self.assertTrue((candidate / "index.html").is_file())
        self.assertEqual((victim / "user.txt").read_text(encoding="utf-8"), "do not overwrite\n")
        self.assertFalse((victim / "index.html").exists())

    def test_verifier_cannot_persist_candidate_changes(self):
        run_dir = self.environment.create_run("Discard deterministic verifier side effects.")
        candidate_injection = run_dir / "candidate" / "verifier-injected.txt"
        run_config = harness.read_json(run_dir / "run-config.json")
        run_config["verification_commands"] = [
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(candidate_injection)!r}).write_text('not allowed')",
            ]
        ]
        harness.write_json(run_dir / "run-config.json", run_config)

        with self.assertRaisesRegex(harness.HarnessError, "Deterministic verifier modified.*candidate was restored"):
            harness.execute_run(run_dir)

        self.assertFalse(candidate_injection.exists())
        self.assertFalse((self.environment.workspace / "index.html").exists())
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")

    def test_repair_worker_cannot_rewrite_prior_review_evidence(self):
        run_dir = self.environment.create_run("Preserve prior review evidence.")
        self.environment.set_scenario(
            run_dir,
            verdicts=["FIX", "PASS"],
            worker_tampers_prior_audit=True,
        )

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness.execute_run(run_dir)

        prior = harness.read_json(run_dir / "reviews" / "00" / "AUDIT.json")
        self.assertEqual(prior["verdict"], "FIX")
        self.assertNotIn("tampered", prior)

    def test_interrupted_promotion_recovers_from_backup(self):
        self.environment.workspace.mkdir()
        (self.environment.workspace / "obsolete.txt").write_text("old\n", encoding="utf-8")
        run_dir = self.environment.create_run("Promote safely after an interrupted copy.")
        self.environment.set_scenario(
            run_dir,
            worker_delete_files=["obsolete.txt"],
            changed_files=["index.html", "obsolete.txt"],
        )
        real_sync = harness._sync_workspace
        interrupted = False

        def interrupt_first_promotion(source, destination, excluded=()):
            nonlocal interrupted
            if (
                not interrupted
                and Path(source).resolve() == (run_dir / "candidate").resolve()
                and Path(destination).resolve() == self.environment.workspace.resolve()
            ):
                interrupted = True
                (Path(destination) / "index.html").write_text(
                    (Path(source) / "index.html").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                raise KeyboardInterrupt
            return real_sync(source, destination, excluded)

        with patch.object(harness, "_sync_workspace", side_effect=interrupt_first_promotion):
            with self.assertRaises(KeyboardInterrupt):
                harness.execute_run(run_dir)

        self.assertEqual(harness.read_json(run_dir / "state.json")["phase"], "promote")
        self.assertTrue((self.environment.workspace / "index.html").is_file())
        self.assertTrue((self.environment.workspace / "obsolete.txt").is_file())
        (self.environment.workspace / "index.html").write_text("external user edit\n", encoding="utf-8")
        with self.assertRaisesRegex(harness.HarnessError, "Live workspace changed"):
            harness.execute_run(run_dir)
        self.assertEqual(
            (self.environment.workspace / "index.html").read_text(encoding="utf-8"),
            "external user edit\n",
        )
        (self.environment.workspace / "index.html").write_text(
            (run_dir / "candidate" / "index.html").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.assertEqual(harness.execute_run(run_dir), 0)
        self.assertTrue((self.environment.workspace / "index.html").is_file())
        self.assertFalse((self.environment.workspace / "obsolete.txt").exists())
        self.assertFalse((run_dir / "candidate").exists())
        self.assertFalse((run_dir / "promotion-backup").exists())

    def test_explicit_mixed_profiles_are_persisted_and_used(self):
        run_dir = self.environment.create_run(
            "Create a command-line utility.",
            coordinator_agent="alpha",
            worker_agent="beta",
            reviewer_agent="gamma",
        )

        self.assertEqual(harness.execute_run(run_dir), 0)

        state = harness.read_json(run_dir / "state.json")
        config = harness.read_json(run_dir / "run-config.json")
        self.assertEqual(
            (state["coordinator_agent"], state["worker_agent"], state["reviewer_agent"]),
            ("alpha", "beta", "gamma"),
        )
        self.assertEqual((config["worker_agent"], config["reviewer_agent"]), ("beta", "gamma"))
        self.assertEqual(set(config["profiles"]), {"beta", "gamma"})
        self.assertEqual(
            [event["profile"] for event in self.environment.events(run_dir)],
            ["beta", "gamma"],
        )

    def test_chinese_and_english_requests_round_trip_through_both_roles(self):
        for request in (
            "创建一个包含帮助信息的命令行工具。",
            "Create a command-line tool with help text.",
        ):
            with self.subTest(request=request):
                run_dir = self.environment.create_run(request)
                self.assertEqual(harness.execute_run(run_dir), 0)
                self.assertEqual(harness.read_json(run_dir / "state.json")["request"], request)
                prompts = [
                    (run_dir / "iterations" / "00" / "worker-prompt.md").read_text(encoding="utf-8"),
                    (run_dir / "reviews" / "00" / "reviewer-prompt.md").read_text(encoding="utf-8"),
                ]
                for prompt in prompts:
                    self.assertIn(request, prompt)
                    self.assertIn("authoritative request's language", prompt)

    def test_fix_runs_repair_then_independent_pass(self):
        run_dir = self.environment.create_run("Build a small website.")
        self.environment.set_scenario(run_dir, verdicts=["FIX", "PASS"], minor_count=7)

        self.assertEqual(harness.execute_run(run_dir), 0)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual((state["status"], state["review_index"]), ("COMPLETE", 1))
        self.assertEqual(
            [event["role"] for event in self.environment.events(run_dir)],
            ["TASK_WORKER", "TASK_REVIEWER", "TASK_WORKER", "TASK_REVIEWER"],
        )
        self.assertEqual(
            harness.read_json(run_dir / "reviews" / "00" / "AUDIT.json")["verdict"],
            "FIX",
        )
        self.assertEqual(
            harness.read_json(run_dir / "reviews" / "01" / "AUDIT.json")["verdict"],
            "PASS",
        )
        self.assertTrue((run_dir / "iterations" / "00" / "WORKER_RESULT.json").is_file())
        self.assertTrue((run_dir / "iterations" / "01" / "WORKER_RESULT.json").is_file())
        repair_prompt = (run_dir / "iterations" / "01" / "worker-prompt.md").read_text(encoding="utf-8")
        audit_text = (run_dir / "reviews" / "00" / "AUDIT.json").read_text(encoding="utf-8")
        self.assertIn(audit_text, repair_prompt)
        self.assertIn("Checkout needs repair", repair_prompt)
        self.assertIn("Minor issue 1 also needs repair", repair_prompt)
        self.assertIn("Minor issue 7 also needs repair", repair_prompt)
        self.assertIn("resolve every blocker, major, and minor issue", repair_prompt)

    def test_review_limit_finishes_incomplete(self):
        run_dir = self.environment.create_run("Build a website.", max_reviews=2)
        self.environment.set_scenario(run_dir, verdicts=["FIX"])

        self.assertEqual(harness.execute_run(run_dir), 1)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual((state["status"], state["review_index"]), ("INCOMPLETE", 1))
        self.assertIn("Review limit reached", state["last_error"])
        self.assertEqual(len(list((run_dir / "reviews").glob("*/AUDIT.json"))), 2)
        self.assertIn("INCOMPLETE", (run_dir / "FINAL_REPORT.md").read_text(encoding="utf-8"))

    def test_forged_audit_never_skips_reviewer_and_review_uses_isolated_snapshot(self):
        forged = self.environment.create_run("Review the real delivery.")
        self.environment.set_scenario(forged, worker_prewrites_audit=True)
        self.assertEqual(harness.execute_run(forged), 0)
        self.assertEqual(
            [event["role"] for event in self.environment.events(forged)],
            ["TASK_WORKER", "TASK_REVIEWER"],
        )
        self.assertTrue(list((forged / "reviews" / "00").glob("AUDIT.invalid-*.json")))

        isolated = self.environment.create_run("Keep the live delivery unchanged.")
        self.environment.set_scenario(isolated, reviewer_modifies_workspace=True)
        self.assertEqual(harness.execute_run(isolated), 0)
        self.assertFalse((self.environment.workspace / "reviewer-touched.txt").exists())

        modified = self.environment.create_run("Detect changes to the live delivery.", max_reviews=1)
        self.environment.set_scenario(modified, reviewer_modifies_live_workspace=True)
        with self.assertRaisesRegex(harness.HarnessError, "Live workspace changed"):
            harness.execute_run(modified)
        self.assertEqual(harness.read_json(modified / "state.json")["status"], "PAUSED")

    def test_explicit_fix_is_never_rewritten_to_pass(self):
        run_dir = self.environment.create_run("Keep the reviewer's explicit verdict.", max_reviews=1)
        self.environment.set_scenario(run_dir, verdicts=["FIX"], minor_only=True)

        self.assertEqual(harness.execute_run(run_dir), 1)

        self.assertEqual(harness.read_json(run_dir / "reviews" / "00" / "AUDIT.json")["verdict"], "FIX")
        self.assertEqual(len(self.environment.events(run_dir)), 2)

    def test_failed_checks_cannot_pass(self):
        worker = self.environment.create_run("Reject a failed worker check.")
        self.environment.set_scenario(worker, worker_mode="failed_check")
        with self.assertRaisesRegex(harness.HarnessError, "failed check"):
            harness.execute_run(worker)

        reviewer = self.environment.create_run("Reject a failed reviewer check.", max_reviews=1)
        self.environment.set_scenario(reviewer, audit_failed_check=True)
        self.assertEqual(harness.execute_run(reviewer), 1)
        self.assertEqual(harness.read_json(reviewer / "reviews" / "00" / "AUDIT.json")["verdict"], "FIX")

    def test_worker_cannot_hide_changed_files(self):
        run_dir = self.environment.create_run("Require a factual changed-file handoff.")
        self.environment.set_scenario(run_dir, worker_unreported_file=True)

        with self.assertRaisesRegex(harness.HarnessError, "omitted changed paths"):
            harness.execute_run(run_dir)

    def test_content_manifest_detects_same_size_change_with_restored_mtime(self):
        self.environment.workspace.mkdir()
        target = self.environment.workspace / "value.txt"
        target.write_text("AAAA", encoding="utf-8")
        details = target.stat()
        before = harness._workspace_manifest(self.environment.workspace)
        target.write_text("BBBB", encoding="utf-8")
        os.utime(target, ns=(details.st_atime_ns, details.st_mtime_ns))
        after = harness._workspace_manifest(self.environment.workspace)

        self.assertNotEqual(before["sha256"], after["sha256"])
        self.assertEqual(harness._manifest_changes(before, after), ["value.txt"])

    def test_reviewer_cannot_modify_control_state(self):
        run_dir = self.environment.create_run("Protect control state.")
        self.environment.set_scenario(run_dir, agent_tampers_state=True)

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness.execute_run(run_dir)

        self.assertEqual(harness.read_json(run_dir / "state.json")["request"], "Protect control state.")

    def test_invalid_worker_or_audit_pauses_run(self):
        cases = (
            ({"worker_mode": "missing"}, "Invalid JSON"),
            ({"audit_mode": "invalid_verdict"}, "verdict must be PASS or FIX"),
        )
        for scenario, expected in cases:
            with self.subTest(scenario=scenario):
                run_dir = self.environment.create_run("Complete a generic task.")
                self.environment.set_scenario(run_dir, **scenario)
                with self.assertRaisesRegex(harness.HarnessError, expected):
                    harness.execute_run(run_dir)
                state = harness.read_json(run_dir / "state.json")
                self.assertEqual(state["status"], "PAUSED")
                self.assertIn(expected.split()[0], state["last_error"])

    def test_worker_blocker_is_preserved_and_continue_retries_work(self):
        run_dir = self.environment.create_run("Complete after an external blocker clears.")
        self.environment.set_scenario(run_dir, worker_mode="blocked")

        with self.assertRaises(harness.WorkerBlocked):
            harness.execute_run(run_dir)

        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "BLOCKED")
        self.assertTrue((run_dir / "iterations" / "00" / "WORKER_RESULT.blocked.json").is_file())
        self.environment.set_scenario(run_dir, worker_mode="valid")
        self.assertEqual(harness.execute_run(run_dir), 0)
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "COMPLETE")

    def test_legacy_state_is_rejected_without_running_an_agent(self):
        run_dir = self.environment.root / "legacy"
        run_dir.mkdir()
        (run_dir / "state.json").write_text(json.dumps({"version": 2, "status": "PAUSED"}), encoding="utf-8")
        (run_dir / "run-config.json").write_text(json.dumps({"version": 4}), encoding="utf-8")

        with self.assertRaisesRegex(harness.HarnessError, "legacy runs are not resumed"):
            harness.execute_run(run_dir)
        self.assertFalse((run_dir / "fake-processes.jsonl").exists())

    def test_dangerously_broad_workspace_and_overlapping_runs_are_rejected(self):
        with self.assertRaisesRegex(harness.HarnessError, "too broad"):
            harness.create_run(
                "Do not use the filesystem root.",
                config_path=self.environment.config,
                runs_dir=self.environment.runs,
                workspace=Path("/"),
            )
        with self.assertRaisesRegex(harness.HarnessError, "runs_dir cannot be the workspace"):
            harness.create_run(
                "Keep control records outside delivery root.",
                config_path=self.environment.config,
                runs_dir=self.environment.workspace,
                workspace=self.environment.workspace,
            )


if __name__ == "__main__":
    unittest.main()
