import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import harness


def can_create_symlinks() -> bool:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "target"
        link = root / "link"
        target.write_text("test", encoding="utf-8")
        try:
            link.symlink_to(target)
            return link.is_symlink()
        except OSError:
            return False


SYMLINKS_AVAILABLE = can_create_symlinks()


FAKE_AGENT = r'''import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

batch_run_value = os.environ.get("HARNESS_TEST_BATCH_RUN_DIR")
if batch_run_value:
    batch_prompt = sys.stdin.read()
    profile = "alpha"
    role = batch_prompt.splitlines()[0].partition(":")[2].strip()
    run_dir = Path(batch_run_value)
    workspace = Path.cwd()
    state_for_path = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    path_index = int(state_for_path.get("review_index", 0))
    prompt_path = (
        run_dir / "iterations" / f"{path_index:02d}" / "worker-prompt.md"
        if role == "TASK_WORKER"
        else run_dir / "reviews" / f"{path_index:02d}" / "reviewer-prompt.md"
    )
else:
    profile, role, run_value, workspace_value, prompt_value = sys.argv[1:]
    run_dir = Path(run_value)
    workspace = Path(workspace_value)
    prompt_path = Path(prompt_value)
state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
index = int(state.get("review_index", 0))
prompt_text = prompt_path.read_text(encoding="utf-8")
review_stage = ""
for line in prompt_text.splitlines()[:3]:
    if line.startswith("HARNESS_REVIEW_STAGE:"):
        review_stage = line.partition(":")[2].strip()
scenario_path = run_dir / "fake-scenario.json"
scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.is_file() else {}
event = {
    "pid": os.getpid(),
    "profile": profile,
    "role": role,
    "index": index,
    "workspace": str(workspace),
    "prompt": prompt_path.relative_to(run_dir).as_posix(),
    "review_stage": review_stage,
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
    if scenario.get("worker_fights_pause"):
        pause_marker = run_dir / ".operator-paused"
        if pause_marker.is_file() or pause_marker.is_symlink():
            pause_marker.unlink()
        pause_marker.mkdir(exist_ok=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if pause_marker.is_file() or pause_marker.is_symlink():
                pause_marker.unlink()
                pause_marker.mkdir(exist_ok=True)
            time.sleep(0.002)
    if index and scenario.get("worker_tampers_prior_audit"):
        prior_audit = run_dir / "reviews" / f"{index - 1:02d}" / "AUDIT.json"
        prior_audit.write_text('{"tampered": true}', encoding="utf-8")
    if scenario.get("worker_tampers_supervisor_marker"):
        (run_dir / "harness.pid").write_text('{"pid": 1, "pid_started": "forged"}', encoding="utf-8")
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
    if scenario.get("worker_make_writable") and worker_path.exists() and not worker_path.is_symlink():
        worker_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    worker_path.write_text(worker_content, encoding="utf-8")
    deleted_files = list(scenario.get("worker_delete_files", []))
    for deleted_file in deleted_files:
        target = workspace / deleted_file
        if target.is_file() or target.is_symlink():
            if os.name == "nt" and not target.is_symlink():
                target.chmod(stat.S_IREAD | stat.S_IWRITE)
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
    if scenario.get("worker_tampers_state"):
        state["request"] = "tampered"
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if scenario.get("worker_tampers_active_agent"):
        state["active_agent"] = {"pid": 1, "pid_started": "forged"}
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if scenario.get("worker_exit_nonzero"):
        raise SystemExit(7)
    raise SystemExit(0)

review_dir = prompt_path.parent
review_dir.mkdir(parents=True, exist_ok=True)
if review_stage == "PLAN":
    mode = scenario.get("plan_mode", "valid")
    if mode == "missing":
        raise SystemExit(0)
    if mode == "invalid_json":
        (review_dir / "REVIEW_PLAN.json").write_text("{not-json", encoding="utf-8")
        raise SystemExit(0)
    run_config = json.loads((run_dir / "run-config.json").read_text(encoding="utf-8"))
    policy = run_config["review_policy"]
    artifact = json.loads((review_dir / "artifact.json").read_text(encoding="utf-8"))
    worker_result = json.loads(
        (run_dir / "iterations" / f"{index:02d}" / "WORKER_RESULT.json").read_text(encoding="utf-8")
    )
    claims = [{"id": "CLAIM-SUMMARY", "statement": worker_result["summary"]}]
    for claim_index, check in enumerate(worker_result["checks"], start=1):
        claims.append(
            {
                "id": f"CLAIM-CHECK-{claim_index:03d}",
                "statement": f"{check['name']} [{str(check['status']).lower()}]: {check['details']}",
            }
        )
    risks = []
    for risk_index, category in enumerate(policy["required_risk_categories"], start=1):
        applicable = risk_index == 1
        risks.append(
            {
                "id": f"RISK-{risk_index:03d}",
                "category": category,
                "statement": f"Review the {category} risk for this task.",
                "applicability": "applicable" if applicable else "not_applicable",
                "rationale": "Relevant to the fixture." if applicable else "Not relevant to this fixture.",
                "severity_if_real": "major" if applicable else "minor",
            }
        )
    covered = ["REQ-REQUEST", *[claim["id"] for claim in claims], risks[0]["id"]]
    if scenario.get("plan_coverage_gap"):
        covered = [claim["id"] for claim in claims]
    command = scenario.get("plan_command", [sys.executable, "-c", "pass"])
    checks = [
        {
            "id": "CHK-COMMAND",
            "kind": "command",
            "purpose": "Run the fixture review check.",
            "covers": covered,
            "expected": {"exit_codes": list(scenario.get("plan_exit_codes", [0]))},
            "blocking": True,
            "steps": list(scenario.get("plan_steps", [command])),
            "timeout_seconds": int(scenario.get("plan_timeout", 5)),
        },
        {
            "id": "CHK-INSPECTION",
            "kind": "inspection",
            "purpose": "Inspect the fixture delivery.",
            "covers": ["REQ-REQUEST"],
            "expected": {"description": "The requested fixture is present."},
            "blocking": False,
        },
    ]
    if scenario.get("extra_plan_command"):
        checks.append(
            {
                "id": "CHK-COMMAND-2",
                "kind": "command",
                "purpose": "Run a second isolated fixture check.",
                "covers": ["REQ-REQUEST"],
                "expected": {"exit_codes": [0]},
                "blocking": True,
                "steps": [scenario["extra_plan_command"]],
                "timeout_seconds": int(scenario.get("plan_timeout", 5)),
            }
        )
    plan = {
        "schema_version": "generic-harness/review-plan/v1",
        "artifact_id": "b" * 64 if mode == "wrong_artifact" else artifact["sha256"],
        "policy_sha256": run_config["review_policy_sha256"],
        "round": index,
        "summary": "Review the fixture delivery.",
        "requirements": [
            {
                "id": "REQ-REQUEST",
                "source": "user_request",
                "statement": state["request"],
                "criticality": "must",
            }
        ],
        "worker_claims": claims,
        "risks": risks,
        "checks": checks,
        "limitations": [],
    }
    (review_dir / "REVIEW_PLAN.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    raise SystemExit(0)

mode = scenario.get("audit_mode", "valid")
if mode == "missing":
    raise SystemExit(0)
if mode == "invalid_json":
    (review_dir / "AUDIT.json").write_text("{not-json", encoding="utf-8")
    raise SystemExit(0)
if review_stage == "ASSESS":
    plan = json.loads((review_dir / "REVIEW_PLAN.json").read_text(encoding="utf-8"))
    check_bundle = json.loads((review_dir / "REVIEW_CHECKS.json").read_text(encoding="utf-8"))
    command_results = {result["check_id"]: result["status"] for result in check_bundle["results"]}
    manual_results = []
    reviewer_evidence = review_dir / "reviewer-evidence"
    reviewer_evidence.mkdir(parents=True, exist_ok=True)
    for check in plan["checks"]:
        if check["kind"] == "command":
            continue
        manual_status = str(scenario.get("manual_status", "pass"))
        evidence_refs = []
        if manual_status == "pass":
            evidence = reviewer_evidence / f"{check['id']}.txt"
            evidence.write_text("Inspected the fixture delivery.\n", encoding="utf-8")
            evidence_refs = [f"reviewer-evidence/{check['id']}.txt"]
        manual_results.append(
            {
                "check_id": check["id"],
                "status": manual_status,
                "details": "Observed the delivered workspace.",
                "evidence_refs": evidence_refs,
            }
        )
    outcomes = {**command_results, **{result["check_id"]: result["status"] for result in manual_results}}
    coverage = []
    subjects = [*plan["requirements"], *plan["worker_claims"]]
    subjects.extend(risk for risk in plan["risks"] if risk["applicability"] != "not_applicable")
    for subject in subjects:
        passing = [
            check["id"]
            for check in plan["checks"]
            if subject["id"] in check["covers"] and outcomes.get(check["id"]) == "pass"
        ]
        coverage.append(
            {
                "subject_id": subject["id"],
                "status": "covered" if passing else "uncovered",
                "evidence_refs": passing,
            }
        )
    if mode == "invalid_verdict":
        verdict = "MAYBE"
    else:
        verdicts = scenario.get("verdicts", [scenario.get("v2_verdict", "PASS")])
        verdict = verdicts[min(index, len(verdicts) - 1)]
    issues = []
    if verdict == "FIX":
        issues.append(
            {
                "severity": "major",
                "location": "index.html",
                "title": "Checkout needs repair",
                "evidence": "The requested checkout behavior is incomplete.",
                "evidence_refs": ["CHK-INSPECTION"],
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
                    "evidence_refs": ["CHK-INSPECTION"],
                    "required_fix": f"Resolve minor issue {minor_index + 1}.",
                    "acceptance_test": f"Verify minor issue {minor_index + 1} after repair.",
                }
            )
    if scenario.get("minor_only") and issues:
        issues[0]["severity"] = "minor"
    if scenario.get("reviewer_modifies_workspace"):
        (workspace / "reviewer-touched.txt").write_text("not allowed\n", encoding="utf-8")
    if scenario.get("reviewer_modifies_live_workspace"):
        (Path(state["workspace"]) / "reviewer-touched.txt").write_text("not allowed\n", encoding="utf-8")
    if scenario.get("reviewer_modifies_candidate_workspace"):
        (Path(state["candidate_workspace"]) / "reviewer-injected.txt").write_text("not allowed\n", encoding="utf-8")
    if scenario.get("reviewer_tampers_review_log"):
        log_path = next((review_dir / "harness-evidence").glob("*/*.log"))
        log_path.write_text("tampered\n", encoding="utf-8")
    if scenario.get("reviewer_adds_harness_evidence"):
        (review_dir / "harness-evidence" / "unexpected.txt").write_text(
            "not Harness owned\n", encoding="utf-8"
        )
    audit = {
        "schema_version": "generic-harness/audit/v2",
        "artifact_id": plan["artifact_id"],
        "plan_sha256": __import__("hashlib").sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "verdict": verdict,
        "summary": "Checkout needs repair." if verdict == "FIX" else "Independent checks completed.",
        "coverage": coverage,
        "checks": manual_results,
        "issues": issues,
        "limitations": ["Fixture limitation."] if verdict == "INCONCLUSIVE" else [],
    }
    (review_dir / "AUDIT.json").write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    if scenario.get("reviewer_exit_nonzero"):
        raise SystemExit(7)
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

    def enable_review_v2(self, **policy_overrides) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        policy = {
            "require_plan": True,
            "require_requirement_coverage": True,
            "require_worker_claim_coverage": True,
            "max_dynamic_checks": 8,
            "max_steps_per_check": 3,
            "per_check_timeout_seconds": 10,
            "total_check_timeout_seconds": 20,
            "max_log_bytes_per_step": 4096,
            "allowed_check_kinds": ["command", "inspection", "visual"],
            "required_risk_categories": ["functional_correctness"],
        }
        policy.update(policy_overrides)
        config["review_protocol_version"] = 2
        config["review_policy"] = policy
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
        if os.name != "nt":
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

    def test_review_v2_plans_runs_checks_adjudicates_and_promotes(self):
        self.environment.enable_review_v2(max_log_bytes_per_step=20)
        self.environment.set_verification(
            [[sys.executable, "-c", "import os; assert 'HARNESS_TEST_SECRET' not in os.environ"]]
        )
        run_dir = self.environment.create_run("Build a reviewed fixture.")
        first_step = (
            "import os,sys; "
            "assert 'HARNESS_TEST_SECRET' not in os.environ; "
            "open('shared-step.txt','w').write('shared'); "
            "open('review-check-only.txt','w').write('temporary'); "
            "print('stdout-' + 'x'*30); "
            "print('stderr-evidence', file=sys.stderr)"
        )
        second_step = "from pathlib import Path; assert Path('shared-step.txt').read_text() == 'shared'"
        self.environment.set_scenario(
            run_dir,
            plan_steps=[
                [sys.executable, "-c", first_step],
                [sys.executable, "-c", second_step],
            ],
            extra_plan_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; assert not Path('shared-step.txt').exists()",
            ],
        )

        with patch.dict(os.environ, {"HARNESS_TEST_SECRET": "must-not-leak"}):
            self.assertEqual(harness.execute_run(run_dir), 0)

        state = harness.read_json(run_dir / "state.json")
        review_dir = run_dir / "reviews" / "00"
        final = harness.read_json(review_dir / "FINAL_REVIEW.json")
        bundle = harness.read_json(review_dir / "REVIEW_CHECKS.json")
        step = bundle["results"][0]["steps"][0]
        self.assertEqual((state["status"], final["verdict"]), ("COMPLETE", "PASS"))
        self.assertEqual(
            [(event["role"], event["review_stage"]) for event in self.environment.events(run_dir)],
            [("TASK_WORKER", ""), ("TASK_REVIEWER", "PLAN"), ("TASK_REVIEWER", "ASSESS")],
        )
        self.assertTrue(step["stdout_truncated"])
        self.assertFalse(step["stderr_truncated"])
        stdout_path = review_dir / step["stdout_path"]
        stderr_path = review_dir / step["stderr_path"]
        self.assertEqual(stdout_path.stat().st_size, 20)
        self.assertIn("stderr-evidence", stderr_path.read_text(encoding="utf-8"))
        self.assertEqual(harness._file_sha256(stdout_path), step["stdout_sha256"])
        self.assertFalse((self.environment.workspace / "review-check-only.txt").exists())
        self.assertEqual(len(bundle["results"][0]["steps"]), 2)
        self.assertEqual(bundle["results"][1]["status"], "pass")
        self.assertTrue((review_dir / "MANUAL_EVIDENCE.json").is_file())
        self.assertTrue((review_dir / "reviewer-evidence" / "CHK-INSPECTION.txt").is_file())
        self.assertTrue(all(path.startswith("reviews/00/") or path.startswith("iterations/00/") for path in final["evidence_paths"]))

    def test_review_v2_bounds_time_and_marks_later_checks_not_run(self):
        self.environment.enable_review_v2(
            per_check_timeout_seconds=1,
            total_check_timeout_seconds=1,
        )
        run_dir = self.environment.create_run("Bound planned review checks.", max_reviews=1)
        self.environment.set_scenario(
            run_dir,
            plan_command=[sys.executable, "-c", "import time; time.sleep(5)"],
            plan_timeout=1,
            extra_plan_command=[sys.executable, "-c", "pass"],
            v2_verdict="PASS",
        )

        self.assertEqual(harness.execute_run(run_dir), 1)

        bundle = harness.read_json(run_dir / "reviews" / "00" / "REVIEW_CHECKS.json")
        self.assertEqual([result["status"] for result in bundle["results"]], ["fail", "not_run"])
        self.assertTrue(bundle["results"][0]["steps"][0]["timed_out"])
        final = harness.read_json(run_dir / "reviews" / "00" / "FINAL_REVIEW.json")
        self.assertEqual(final["verdict"], "FIX")
        self.assertIn("BLOCKING_CHECK_FAILED", final["reason_codes"])
        self.assertFalse((self.environment.workspace / "index.html").exists())

    def test_review_v2_revalidates_log_hashes_at_promotion(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run("Revalidate final review evidence.")
        with patch.object(
            harness,
            "_promote_candidate",
            side_effect=harness.HarnessError("hold promotion for evidence test"),
        ):
            with self.assertRaisesRegex(harness.HarnessError, "hold promotion"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual((state["status"], state["phase"]), ("PAUSED", "promote"))
        bundle = harness.read_json(run_dir / "reviews" / "00" / "REVIEW_CHECKS.json")
        log = run_dir / "reviews" / "00" / bundle["results"][0]["steps"][0]["stdout_path"]
        log.write_text("tampered after adjudication\n", encoding="utf-8")

        with self.assertRaisesRegex(harness.HarnessError, "hash does not match"):
            harness.execute_run(run_dir)
        self.assertFalse((self.environment.workspace / "index.html").exists())

    def test_review_v2_revalidates_complete_artifact_manifest_at_promotion(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run("Revalidate the complete artifact manifest.")
        with patch.object(
            harness,
            "_promote_candidate",
            side_effect=harness.HarnessError("hold promotion for artifact test"),
        ):
            with self.assertRaisesRegex(harness.HarnessError, "hold promotion"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual((state["status"], state["phase"]), ("PAUSED", "promote"))
        artifact_path = run_dir / "reviews" / "00" / "artifact.json"
        artifact = harness.read_json(artifact_path)
        harness.write_json(artifact_path, {"sha256": artifact["sha256"]})

        with self.assertRaisesRegex(harness.HarnessError, "artifact identity"):
            harness.execute_run(run_dir)
        self.assertFalse((self.environment.workspace / "index.html").exists())

    def test_review_v2_harness_overrides_false_pass_with_fix(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run("Reject a failing planned check.", max_reviews=1)
        self.environment.set_scenario(
            run_dir,
            plan_command=[sys.executable, "-c", "raise SystemExit(7)"],
            v2_verdict="PASS",
        )

        self.assertEqual(harness.execute_run(run_dir), 1)

        review_dir = run_dir / "reviews" / "00"
        self.assertEqual(harness.read_json(review_dir / "AUDIT.json")["verdict"], "PASS")
        final = harness.read_json(review_dir / "FINAL_REVIEW.json")
        self.assertEqual(final["verdict"], "FIX")
        self.assertIn("BLOCKING_CHECK_FAILED", final["reason_codes"])
        self.assertFalse((self.environment.workspace / "index.html").exists())

    def test_review_v2_inconclusive_retries_same_round_after_environment_recovers(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run("Retry an unavailable planned check.")
        self.environment.set_scenario(
            run_dir,
            plan_command=["definitely-missing-review-program"],
            v2_verdict="PASS",
        )

        self.assertEqual(harness.execute_run(run_dir), 2)

        state = harness.read_json(run_dir / "state.json")
        final = harness.read_json(run_dir / "reviews" / "00" / "FINAL_REVIEW.json")
        self.assertEqual((state["status"], state["phase"], state["review_index"]), ("PAUSED", "review_plan", 0))
        self.assertEqual(final["verdict"], "INCONCLUSIVE")
        self.assertIn("BLOCKING_CHECK_UNAVAILABLE", final["reason_codes"])
        self.assertFalse((self.environment.workspace / "index.html").exists())

        self.environment.set_scenario(run_dir)
        self.assertEqual(harness.execute_run(run_dir), 0)
        self.assertEqual(harness.read_json(run_dir / "state.json")["review_index"], 0)
        self.assertTrue((self.environment.workspace / "index.html").is_file())

    def test_review_v2_reexecutes_checks_after_reviewer_tampers_harness_log(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run("Protect Harness-owned review evidence.")
        self.environment.set_scenario(run_dir, reviewer_tampers_review_log=True)

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual((state["status"], state["phase"]), ("PAUSED", "review_checks"))
        self.assertFalse((self.environment.workspace / "index.html").exists())
        self.environment.set_scenario(run_dir)
        self.assertEqual(harness.execute_run(run_dir), 0)

    def test_review_v2_rejects_unexpected_files_in_harness_evidence(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run("Keep Harness evidence exclusively owned.")
        self.environment.set_scenario(run_dir, reviewer_adds_harness_evidence=True)

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual((state["status"], state["phase"]), ("PAUSED", "review_checks"))
        self.assertFalse((self.environment.workspace / "index.html").exists())

    @unittest.skipUnless(SYMLINKS_AVAILABLE, "symbolic-link creation is unavailable")
    def test_control_guard_rejects_replaced_review_directory_symlink(self):
        run_dir = self.environment.create_run("Protect review-directory identity.")
        (run_dir / "harness.pid").write_text("{}\n", encoding="utf-8")
        review_dir = run_dir / "reviews" / "00"
        log_dir = review_dir / "harness-evidence" / "CHK-COMMAND"
        log_dir.mkdir(parents=True)
        harness.write_json(review_dir / "artifact.json", {"sha256": "a" * 64})
        (log_dir / "step-1.stdout.log").write_text("unchanged evidence\n", encoding="utf-8")
        guard = harness._control_guard(run_dir)

        moved = run_dir / "00-real"
        review_dir.rename(moved)
        review_dir.symlink_to(moved, target_is_directory=True)

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness._verify_control_guard(run_dir, guard)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_control_guard_rejects_replaced_review_directory_junction(self):
        run_dir = self.environment.create_run("Protect review-directory identity on Windows.")
        (run_dir / "harness.pid").write_text("{}\n", encoding="utf-8")
        review_dir = run_dir / "reviews" / "00"
        log_dir = review_dir / "harness-evidence" / "CHK-COMMAND"
        log_dir.mkdir(parents=True)
        harness.write_json(review_dir / "artifact.json", {"sha256": "a" * 64})
        (log_dir / "step-1.stdout.log").write_text("unchanged evidence\n", encoding="utf-8")
        guard = harness._control_guard(run_dir)

        moved = run_dir / "00-real"
        review_dir.rename(moved)
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(review_dir), str(moved)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            moved.rename(review_dir)
            self.skipTest(f"junction creation is unavailable: {result.stderr or result.stdout}")
        self.addCleanup(lambda: os.path.lexists(review_dir) and os.rmdir(review_dir))

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness._verify_control_guard(run_dir, guard)

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

    def test_managed_agent_launch_safety_failure_pauses_with_recovery_evidence(self):
        run_dir = self.environment.create_run("Fail closed if managed agent setup is unsafe.")

        with patch.object(
            harness,
            "spawn_managed_process",
            side_effect=RuntimeError("simulated Job setup failure"),
        ):
            with self.assertRaisesRegex(harness.HarnessError, "Could not launch alpha"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual(state["status"], "PAUSED")
        self.assertIn("simulated Job setup failure", state["last_error"])

    def test_large_stdin_cannot_block_agent_timeout_monitoring(self):
        run_dir = self.environment.create_run("Bound a worker that never reads its input.")
        profile = {
            "command": [sys.executable, "-c", "import time; time.sleep(30)"],
            "stdin": "{prompt}",
            "timeout_seconds": 1,
        }
        started = time.monotonic()

        with self.assertRaisesRegex(harness.HarnessError, "timed out after 1 seconds"):
            harness.run_agent(
                run_dir=run_dir,
                profile_name="blocking-stdin",
                profile=profile,
                role="TASK_WORKER",
                prompt="x" * 2_000_000,
                prompt_path=run_dir / "large-prompt.md",
                log_path=run_dir / "large-stdin.log",
                timeout_seconds=10,
                workspace=run_dir / "candidate",
            )

        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(harness.read_json(run_dir / "state.json")["active_agent"])

    def test_staged_stdin_is_closed_when_process_launch_is_interrupted(self):
        run_dir = self.environment.create_run("Close staged stdin after an interrupted launch.")
        staged_input = tempfile.TemporaryFile(mode="w+b")
        profile = {
            "command": [sys.executable, "-c", "pass"],
            "stdin": "{prompt}",
        }

        with patch.object(
            harness.tempfile,
            "TemporaryFile",
            return_value=staged_input,
        ), patch.object(
            harness,
            "spawn_managed_process",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                harness.run_agent(
                    run_dir=run_dir,
                    profile_name="interrupted-stdin",
                    profile=profile,
                    role="TASK_WORKER",
                    prompt="中文 prompt",
                    prompt_path=run_dir / "interrupted-prompt.md",
                    log_path=run_dir / "interrupted-stdin.log",
                    timeout_seconds=5,
                    workspace=run_dir / "candidate",
                )

        self.assertTrue(staged_input.closed)

    def test_managed_verifier_launch_safety_failure_pauses_with_recovery_evidence(self):
        run_dir = self.environment.create_run("Fail closed if verifier containment is unsafe.")
        real_spawn = harness.spawn_managed_process

        def fail_verifier(command, **kwargs):
            if "harness-verify-" in str(kwargs.get("cwd", "")):
                raise RuntimeError("simulated verifier Job setup failure")
            return real_spawn(command, **kwargs)

        with patch.object(harness, "spawn_managed_process", side_effect=fail_verifier):
            with self.assertRaisesRegex(harness.HarnessError, "verification safely"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual(state["status"], "PAUSED")
        self.assertIn("simulated verifier Job setup failure", state["last_error"])
        self.assertFalse(harness._verification_path(run_dir, 0).exists())

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

    def test_verification_report_cannot_forge_pass_against_process_evidence(self):
        command = [sys.executable, "-c", "pass"]
        run_dir = self.environment.create_run("Reject contradictory verification evidence.")
        iteration_dir = run_dir / "iterations" / "00"
        iteration_dir.mkdir(parents=True)
        artifact_id = "a" * 64
        report = {
            "schema_version": harness.VERIFICATION_SCHEMA,
            "artifact_id": artifact_id,
            "status": "pass",
            "commands": [
                {
                    "name": "verification 1",
                    "argv": command,
                    "command": harness.format_command(command),
                    "status": "pass",
                    "returncode": 7,
                    "timed_out": False,
                    "duration_seconds": 0.1,
                    "details": "forged pass",
                    "log": "iterations/00/verification.log",
                }
            ],
        }
        harness.write_json(iteration_dir / "VERIFICATION.json", report)

        with self.assertRaisesRegex(harness.HarnessError, "Inconsistent deterministic verification"):
            harness._read_verification(run_dir, 0, artifact_id, [command])

    def test_python_verification_placeholder_is_snapshotted_portably(self):
        self.environment.set_verification([["{python}", "-c", "pass"]])

        run_dir = self.environment.create_run("Resolve the current Python interpreter once.")

        run_config = harness.read_json(run_dir / "run-config.json")
        self.assertEqual(run_config["verification_commands"], [[sys.executable, "-c", "pass"]])
        self.assertEqual(harness.execute_run(run_dir), 0)

    def test_casefolded_git_metadata_is_preserved_but_never_copied(self):
        metadata = self.environment.workspace / ".GIT"
        metadata.mkdir(parents=True)
        (metadata / "config").write_text("formal metadata\n", encoding="utf-8")
        run_dir = self.environment.create_run("Keep case-insensitive Git metadata out of child workspaces.")

        self.assertFalse((run_dir / "candidate" / ".GIT").exists())
        self.assertEqual(harness.execute_run(run_dir), 0)

        self.assertEqual((metadata / "config").read_text(encoding="utf-8"), "formal metadata\n")
        self.assertFalse((run_dir / "candidate").exists())

    def test_readonly_live_file_can_be_removed_during_verified_promotion(self):
        self.environment.workspace.mkdir()
        obsolete = self.environment.workspace / "obsolete.txt"
        obsolete.write_text("read only\n", encoding="utf-8")
        obsolete.chmod(stat.S_IREAD)
        run_dir = self.environment.create_run("Replace a release that contains a read-only obsolete file.")
        self.environment.set_scenario(
            run_dir,
            worker_delete_files=["obsolete.txt"],
            changed_files=["index.html", "obsolete.txt"],
        )

        self.assertEqual(harness.execute_run(run_dir), 0)

        self.assertFalse(obsolete.exists())
        self.assertTrue((self.environment.workspace / "index.html").is_file())

    def test_failed_readonly_replace_restores_original_content_and_mode(self):
        root = self.environment.root / "readonly-replace"
        root.mkdir()
        source = root / "source.txt"
        destination = root / "destination.txt"
        source.write_text("new\n", encoding="utf-8")
        destination.write_text("old\n", encoding="utf-8")
        destination.chmod(stat.S_IREAD)
        original_mode = stat.S_IMODE(destination.stat().st_mode)
        original_parent_mode = stat.S_IMODE(root.stat().st_mode)

        with patch.object(harness, "WINDOWS", True), patch.object(
            harness.os,
            "replace",
            side_effect=PermissionError("simulated Windows share lock"),
        ):
            with self.assertRaises(PermissionError):
                harness._replace_path(source, destination)

        self.assertEqual(destination.read_text(encoding="utf-8"), "old\n")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), original_mode)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), original_parent_mode)
        self.assertTrue(source.is_file())

    def test_windows_writable_replace_retries_access_denied_without_chmod(self):
        root = self.environment.root / "writable-replace"
        root.mkdir()
        source = root / "source.txt"
        destination = root / "destination.txt"
        source.write_text("new\n", encoding="utf-8")
        destination.write_text("old\n", encoding="utf-8")
        locked = PermissionError("simulated Windows share lock")
        locked.winerror = 5

        with patch.object(harness, "WINDOWS", True), patch.object(
            harness.os,
            "replace",
            side_effect=(locked, None),
        ) as replace, patch.object(harness, "_make_writable") as make_writable:
            harness._replace_path(source, destination)

        self.assertEqual(replace.call_count, 2)
        make_writable.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows share-lock behavior")
    def test_windows_exclusive_share_lock_preserves_destination_until_retry(self):
        import ctypes
        from ctypes import wintypes

        root = self.environment.root / "share-lock"
        root.mkdir()
        source = root / "source.txt"
        destination = root / "destination.txt"
        source.write_text("new\n", encoding="utf-8")
        destination.write_text("old\n", encoding="utf-8")
        original_mode = stat.S_IMODE(destination.stat().st_mode)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(destination),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ, deliberately no FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            self.skipTest(f"exclusive test handle is unavailable: {ctypes.WinError(ctypes.get_last_error())}")
        try:
            with self.assertRaises(PermissionError):
                harness._replace_path(source, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), original_mode)
            self.assertTrue(source.is_file())
        finally:
            kernel32.CloseHandle(handle)

        harness._replace_path(source, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "new\n")

    @unittest.skipUnless(os.name == "nt", "Windows share-lock behavior")
    def test_windows_transient_reader_does_not_break_atomic_state_replace(self):
        import ctypes
        import threading
        from ctypes import wintypes

        root = self.environment.root / "transient-share-lock"
        root.mkdir()
        source = root / "source.json"
        destination = root / "state.json"
        source.write_text('{"status": "new"}\n', encoding="utf-8")
        destination.write_text('{"status": "old"}\n', encoding="utf-8")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(destination),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ, deliberately no FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            self.skipTest(f"exclusive test handle is unavailable: {ctypes.WinError(ctypes.get_last_error())}")
        closer = threading.Thread(target=lambda: (threading.Event().wait(0.15), kernel32.CloseHandle(handle)))
        closer.start()
        try:
            harness._replace_path(source, destination)
        finally:
            closer.join(timeout=2)

        self.assertEqual(destination.read_text(encoding="utf-8"), '{"status": "new"}\n')
        self.assertFalse(source.exists())

    def test_failed_readonly_unlink_restores_file_parent_and_manifest(self):
        source = self.environment.root / "sync-source"
        destination = self.environment.root / "sync-destination"
        (source / "nested").mkdir(parents=True)
        nested = destination / "nested"
        nested.mkdir(parents=True)
        obsolete = nested / "obsolete.txt"
        obsolete.write_text("old\n", encoding="utf-8")
        obsolete.chmod(stat.S_IREAD)
        nested.chmod(stat.S_IREAD | stat.S_IEXEC)
        original_file_mode = stat.S_IMODE(obsolete.stat().st_mode)
        original_parent_mode = stat.S_IMODE(nested.stat().st_mode)
        before = harness._workspace_manifest(destination)
        real_unlink = Path.unlink

        def locked_unlink(path, *args, **kwargs):
            if Path(path) == obsolete:
                raise PermissionError("simulated Windows share lock")
            return real_unlink(path, *args, **kwargs)

        try:
            with patch.object(harness, "WINDOWS", True), patch.object(
                Path,
                "unlink",
                autospec=True,
                side_effect=locked_unlink,
            ):
                with self.assertRaises(PermissionError):
                    harness._sync_workspace(source, destination)

            self.assertEqual(obsolete.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(stat.S_IMODE(obsolete.stat().st_mode), original_file_mode)
            self.assertEqual(stat.S_IMODE(nested.stat().st_mode), original_parent_mode)
            self.assertEqual(harness._workspace_manifest(destination), before)
        finally:
            nested.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            obsolete.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_file_reparse_points_are_not_misclassified_as_directory_junctions(self):
        path = self.environment.root / "placeholder.txt"
        path.write_text("content\n", encoding="utf-8")
        details = type(
            "FileReparseStat",
            (),
            {"st_mode": stat.S_IFREG, "st_file_attributes": 0x0400},
        )()

        with patch.object(harness, "WINDOWS", True):
            self.assertTrue(harness._is_reparse_point(path, details))
            self.assertFalse(harness._is_junction(path, details))

    def test_failed_readonly_promotion_rolls_back_exactly_and_can_resume(self):
        self.environment.workspace.mkdir()
        live_file = self.environment.workspace / "app.py"
        live_file.write_text("print('old')\n", encoding="utf-8")
        live_file.chmod(stat.S_IREAD)
        run_dir = self.environment.create_run("Replace a read-only release file safely.")
        base = harness.read_json(run_dir / "base-artifact.json")
        self.environment.set_scenario(
            run_dir,
            worker_file="app.py",
            worker_contents="print('new')\n",
            worker_make_writable=True,
            changed_files=["app.py"],
        )
        real_replace = os.replace
        failures = 0

        def locked_replace(source, destination):
            nonlocal failures
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path.resolve() == live_file.resolve()
                and source_path.name.startswith(".app.py.harness-promote-")
            ):
                failures += 1
                raise PermissionError("simulated Windows share lock")
            return real_replace(source, destination)

        with patch.object(harness, "WINDOWS", True), patch.object(
            harness.os,
            "replace",
            side_effect=locked_replace,
        ):
            with self.assertRaisesRegex(harness.HarnessError, "rolled back"):
                harness.execute_run(run_dir)

        self.assertEqual(failures, 2)
        self.assertEqual(harness._workspace_manifest(self.environment.workspace), base)
        self.assertEqual(harness.read_json(run_dir / "state.json")["phase"], "promote")
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")

        self.assertEqual(harness.execute_run(run_dir), 0)
        self.assertEqual(live_file.read_text(encoding="utf-8"), "print('new')\n")

    def test_run_relative_evidence_paths_always_use_forward_slashes(self):
        run_dir = self.environment.create_run("Persist portable evidence links.")

        self.assertEqual(harness.execute_run(run_dir), 0)

        state = harness.read_json(run_dir / "state.json")
        verification = harness.read_json(run_dir / "iterations" / "00" / "VERIFICATION.json")
        self.assertEqual(state["artifact_path"], "reviews/00/artifact.json")
        self.assertEqual(verification["commands"][0]["log"], "iterations/00/verification.log")
        report = (run_dir / "FINAL_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("(iterations/00/WORKER_RESULT.json)", report)
        self.assertIn("(iterations/00/VERIFICATION.json)", report)
        self.assertIn("(reviews/00/AUDIT.json)", report)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_cannot_escape_workspace(self):
        self.environment.workspace.mkdir()
        outside = self.environment.root / "outside"
        outside.mkdir()
        junction = self.environment.workspace / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"junction creation is unavailable: {result.stderr or result.stdout}")
        self.addCleanup(lambda: os.path.lexists(junction) and os.rmdir(junction))

        with self.assertRaisesRegex(harness.HarnessError, "junction"):
            self.environment.create_run("Do not traverse a Windows directory junction.")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_initial_formal_workspace_root_junction_is_rejected(self):
        victim = self.environment.root / "initial-root-victim"
        victim.mkdir()
        (victim / "protected.txt").write_text("outside\n", encoding="utf-8")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(self.environment.workspace), str(victim)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"junction creation is unavailable: {result.stderr or result.stdout}")
        self.addCleanup(
            lambda: os.path.lexists(self.environment.workspace) and os.rmdir(self.environment.workspace)
        )

        with self.assertRaisesRegex(harness.HarnessError, "real directory"):
            self.environment.create_run("Never accept a junction as the formal workspace root.")

        self.assertEqual((victim / "protected.txt").read_text(encoding="utf-8"), "outside\n")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_replaced_formal_workspace_root_junction_cannot_redirect_promotion(self):
        self.environment.workspace.mkdir()
        (self.environment.workspace / "stable.txt").write_text("base\n", encoding="utf-8")
        run_dir = self.environment.create_run("Do not promote through a replaced root junction.")
        shutil.rmtree(self.environment.workspace)
        victim = self.environment.root / "replacement-root-victim"
        victim.mkdir()
        (victim / "stable.txt").write_text("base\n", encoding="utf-8")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(self.environment.workspace), str(victim)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"junction creation is unavailable: {result.stderr or result.stdout}")
        self.addCleanup(
            lambda: os.path.lexists(self.environment.workspace) and os.rmdir(self.environment.workspace)
        )

        with self.assertRaisesRegex(harness.HarnessError, "real directory"):
            harness.execute_run(run_dir)

        self.assertEqual((victim / "stable.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((victim / "index.html").exists())

    @unittest.skipUnless(os.name == "nt", "Windows path normalization")
    def test_windows_worker_changed_paths_accept_native_separators_and_case(self):
        run_dir = self.environment.create_run("Normalize native Windows worker paths.")
        self.environment.set_scenario(
            run_dir,
            worker_file="src/Thing.py",
            changed_files=["SRC", "SRC\\THING.PY"],
        )

        self.assertEqual(harness.execute_run(run_dir), 0)
        result = harness.read_json(run_dir / "iterations" / "00" / "WORKER_RESULT.json")
        self.assertEqual(result["changed_files"], ["SRC", "SRC/THING.PY"])
        self.assertFalse((run_dir / "WORKER_RESULT.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Unicode path handling")
    def test_windows_task_completes_in_a_unicode_path_with_spaces(self):
        root = self.environment.root / "中文 project & 100% !literal!"
        root.mkdir()
        environment = FakeHarnessEnvironment(root)
        run_dir = environment.create_run("Complete a task under a Unicode Windows path.")

        self.assertEqual(harness.execute_run(run_dir), 0)

        self.assertTrue((environment.workspace / "index.html").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows command wrappers")
    def test_windows_cmd_verification_wrapper_runs_without_a_shell_string(self):
        self.environment.workspace.mkdir()
        wrapper = self.environment.workspace / "verification-wrapper.cmd"
        wrapper.write_bytes(b"@echo off\r\nexit /b 0\r\n")
        self.environment.set_verification([[r".\verification-wrapper.cmd"]])
        run_dir = self.environment.create_run("Run an explicit candidate-local Windows wrapper.")

        self.assertEqual(harness.execute_run(run_dir), 0)

    @unittest.skipUnless(os.name == "nt", "Windows PATH lookup safety")
    def test_windows_bare_verification_command_cannot_be_shadowed_by_candidate(self):
        self.environment.workspace.mkdir()
        (self.environment.workspace / "npm.cmd").write_bytes(b"@echo off\r\nexit /b 7\r\n")
        trusted_bin = self.environment.root / "trusted-bin"
        trusted_bin.mkdir()
        (trusted_bin / "npm.cmd").write_bytes(b"@echo off\r\nexit /b 0\r\n")
        self.environment.set_verification([["npm"]])
        run_dir = self.environment.create_run("Resolve a bare verifier only through PATH.")

        with patch.dict(
            os.environ,
            {"PATH": str(trusted_bin) + os.pathsep + os.environ.get("PATH", "")},
        ):
            self.assertEqual(harness.execute_run(run_dir), 0)

    def test_windows_batch_verification_rejects_cmd_metacharacters(self):
        with patch.object(harness, "WINDOWS", True):
            self.assertTrue(
                harness._windows_batch_argv_has_metacharacters(
                    [r"C:\safe\check.cmd", "%PATH%"],
                )
            )
            self.assertFalse(
                harness._windows_batch_argv_has_metacharacters(
                    [r"C:\safe\check.cmd", "literal value"],
                )
            )

    def test_windows_command_line_limits_cover_batch_and_native_checks(self):
        with patch.object(harness, "WINDOWS", True):
            self.assertEqual(harness._windows_command_line_error([r"C:\safe\check.cmd", "short"]), "")
            self.assertIn(
                "safe Windows command-line limit",
                harness._windows_command_line_error([r"C:\safe\check.cmd", "x" * 8_000]),
            )
            self.assertIn(
                "safe Windows command-line limit",
                harness._windows_command_line_error([r"C:\safe\check.exe", "x" * 30_000]),
            )

    @unittest.skipIf(os.name == "nt", "POSIX executable-bit behavior")
    def test_candidate_local_verification_executable_is_resolved_from_its_workspace(self):
        script = self.environment.workspace / "check"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        self.environment.set_verification([["./check"]])
        run_dir = self.environment.create_run("Run the candidate's own deterministic check.")

        self.assertEqual(harness.execute_run(run_dir), 0)

    @unittest.skipUnless(os.name == "nt", "Windows batch-wrapper safety")
    def test_windows_batch_agent_rejects_every_runtime_argv_placeholder(self):
        for index, placeholder in enumerate(
            ("{prompt}", "{prompt_file}", "{workspace}", "{run_dir}", "{role}")
        ):
            with self.subTest(placeholder=placeholder):
                root = self.environment.root / f"batch-case-{index}"
                root.mkdir()
                environment = FakeHarnessEnvironment(root)
                wrapper = root / "fake agent.cmd"
                sentinel = root / "batch-ran.txt"
                wrapper.write_bytes(
                    f"@echo off\r\necho ran>\"{sentinel}\"\r\nexit /b 0\r\n".encode("utf-8")
                )
                config = json.loads(environment.config.read_text(encoding="utf-8"))
                config["agents"]["alpha"]["command"] = [str(wrapper), placeholder]
                environment.config.write_text(json.dumps(config), encoding="utf-8")

                with self.assertRaisesRegex(harness.HarnessError, "runtime placeholders"):
                    environment.create_run("中文 & echo injected %PATH% !unsafe!")

                self.assertFalse(sentinel.exists())
                self.assertFalse(environment.runs.exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch-wrapper safety")
    def test_windows_static_batch_agent_uses_stdin_under_a_shell_metacharacter_path(self):
        wrapper_dir = self.environment.root / "批处理 agent"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "stdin-agent.cmd"
        wrapper.write_bytes(
            (f"@echo off\r\n{subprocess.list2cmdline([sys.executable, str(self.environment.fake_agent)])}\r\n").encode(
                "utf-8"
            )
        )
        config = json.loads(self.environment.config.read_text(encoding="utf-8"))
        config["agents"]["alpha"]["command"] = [str(wrapper)]
        config["agents"]["alpha"]["stdin"] = "{prompt}"
        self.environment.config.write_text(json.dumps(config), encoding="utf-8")
        special_root = self.environment.root / "中文 project & 100% !literal!"
        special_root.mkdir()
        run_dir = harness.create_run(
            "Complete safely without putting any runtime value in batch argv.",
            config_path=self.environment.config,
            runs_dir=special_root / "runs",
            workspace=special_root / "workspace",
        )

        with patch.dict(os.environ, {"HARNESS_TEST_BATCH_RUN_DIR": str(run_dir)}):
            self.assertEqual(harness.execute_run(run_dir), 0)

        self.assertTrue((special_root / "workspace" / "index.html").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows batch-wrapper safety")
    def test_windows_batch_agent_rejects_static_cmd_metacharacters_before_launch(self):
        cases = (("argument", "safe-agent.cmd", ["literal&calc"]), ("path", "agent%PATH%.cmd", []))
        for index, (label, wrapper_name, extra_argv) in enumerate(cases):
            with self.subTest(case=label):
                root = self.environment.root / f"batch-meta-{index}"
                root.mkdir()
                environment = FakeHarnessEnvironment(root)
                wrapper = root / wrapper_name
                sentinel = root / "batch-ran.txt"
                wrapper.write_bytes(
                    f"@echo off\r\necho ran>\"{sentinel}\"\r\nexit /b 0\r\n".encode("utf-8")
                )
                config = json.loads(environment.config.read_text(encoding="utf-8"))
                config["agents"]["alpha"]["command"] = [str(wrapper), *extra_argv]
                config["agents"]["alpha"]["stdin"] = "{prompt}"
                environment.config.write_text(json.dumps(config), encoding="utf-8")

                with self.assertRaisesRegex(harness.HarnessError, "cmd.exe metacharacters"):
                    environment.create_run("Reject unsafe batch parsing before a run exists.")

                self.assertFalse(sentinel.exists())
                self.assertFalse(environment.runs.exists())

    def test_builtin_stdin_profiles_keep_runtime_values_out_of_argv(self):
        config = harness.load_config(harness.CONFIG_PATH)
        for name in ("codex", "claude"):
            with self.subTest(profile=name):
                profile = config["agents"][name]
                self.assertEqual(harness._command_template_fields(profile["command"]), set())
                self.assertEqual(profile["stdin"], "{prompt}")

    @unittest.skipUnless(os.name == "nt", "Windows command-line limits")
    def test_windows_oversized_prompt_argv_is_rejected_before_launch(self):
        config = json.loads(self.environment.config.read_text(encoding="utf-8"))
        config["agents"]["alpha"]["command"] = [sys.executable, "-c", "pass", "{prompt}"]
        self.environment.config.write_text(json.dumps(config), encoding="utf-8")
        run_dir = self.environment.create_run("x" * 31_000)

        with self.assertRaisesRegex(harness.HarnessError, "safe Windows command-line limit"):
            harness.execute_run(run_dir)

    @unittest.skipUnless(SYMLINKS_AVAILABLE, "symbolic-link creation is unavailable")
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

    @unittest.skipUnless(SYMLINKS_AVAILABLE, "symbolic-link creation is unavailable")
    def test_initial_formal_workspace_root_symlink_is_rejected(self):
        victim = self.environment.root / "victim"
        victim.mkdir()
        protected = victim / "protected.txt"
        protected.write_text("outside\n", encoding="utf-8")
        self.environment.workspace.symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(harness.HarnessError, "real directory"):
            self.environment.create_run("Never accept an aliased formal workspace root.")

        self.assertEqual(protected.read_text(encoding="utf-8"), "outside\n")
        self.assertFalse(self.environment.runs.exists())

    @unittest.skipUnless(SYMLINKS_AVAILABLE, "symbolic-link creation is unavailable")
    def test_replaced_formal_workspace_root_cannot_redirect_promotion(self):
        self.environment.workspace.mkdir()
        (self.environment.workspace / "stable.txt").write_text("base\n", encoding="utf-8")
        run_dir = self.environment.create_run("Do not promote through a replaced workspace root.")
        victim = self.environment.root / "victim"
        victim.mkdir()
        protected = victim / "stable.txt"
        protected.write_text("base\n", encoding="utf-8")
        shutil.rmtree(self.environment.workspace)
        self.environment.workspace.symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(harness.HarnessError, "real directory"):
            harness.execute_run(run_dir)

        self.assertEqual(protected.read_text(encoding="utf-8"), "base\n")
        self.assertFalse((victim / "index.html").exists())

    @unittest.skipUnless(SYMLINKS_AVAILABLE, "symbolic-link creation is unavailable")
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

    @unittest.skipUnless(SYMLINKS_AVAILABLE, "symbolic-link creation is unavailable")
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

    def test_worker_cannot_replace_the_supervisor_identity_marker(self):
        run_dir = self.environment.create_run("Protect the supervisor identity marker.")
        self.environment.set_scenario(run_dir, worker_tampers_supervisor_marker=True)

        with self.assertRaisesRegex(harness.HarnessError, "protected Harness control data"):
            harness.execute_run(run_dir)

        self.assertFalse((run_dir / "harness.pid").exists())

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
        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")
        self.assertFalse((run_dir / "harness.pid").exists())
        self.assertFalse((self.environment.workspace / "index.html").exists())
        self.assertTrue((self.environment.workspace / "obsolete.txt").is_file())
        (self.environment.workspace / "index.html").write_text("external user edit\n", encoding="utf-8")
        with self.assertRaisesRegex(harness.HarnessError, "Live workspace changed"):
            harness.execute_run(run_dir)
        self.assertEqual(
            (self.environment.workspace / "index.html").read_text(encoding="utf-8"),
            "external user edit\n",
        )
        (self.environment.workspace / "index.html").unlink()
        self.assertEqual(harness.execute_run(run_dir), 0)
        self.assertTrue((self.environment.workspace / "index.html").is_file())
        self.assertFalse((self.environment.workspace / "obsolete.txt").exists())
        self.assertFalse((run_dir / "candidate").exists())
        self.assertFalse((run_dir / "promotion-backup").exists())

    @unittest.skipIf(os.name == "nt", "POSIX directory mode interruption")
    def test_crash_transients_do_not_block_verified_promotion_recovery(self):
        locked = self.environment.workspace / "locked"
        locked.mkdir(parents=True)
        value = locked / "value.txt"
        value.write_text("old\n", encoding="utf-8")
        locked.chmod(0o555)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o755))
        run_dir = self.environment.create_run("Recover a promotion after process-level interruption.")
        self.environment.set_scenario(
            run_dir,
            worker_file="locked/value.txt",
            worker_contents="new\n",
            changed_files=["locked/value.txt"],
        )
        real_sync = harness._sync_workspace
        crashed = False

        def leave_real_sync_transients(source, destination, excluded=()):
            nonlocal crashed
            source_path = Path(source).resolve()
            destination_path = Path(destination).resolve()
            if not crashed and source_path == (run_dir / "candidate").resolve():
                crashed = True
                locked.chmod(0o755)
                (locked / ".value.txt.harness-promote-abcdef").write_text(
                    "new\n", encoding="utf-8"
                )
                raise KeyboardInterrupt
            if source_path == (run_dir / "promotion-backup").resolve() and destination_path == self.environment.workspace.resolve():
                raise harness.HarnessError("simulated supervisor death before rollback")
            return real_sync(source, destination, excluded)

        with patch.object(harness, "_sync_workspace", side_effect=leave_real_sync_transients):
            with self.assertRaisesRegex(harness.HarnessError, "automatic rollback also failed"):
                harness.execute_run(run_dir)

        self.assertEqual(harness.read_json(run_dir / "promotion.json")["status"], "prepared")
        self.assertEqual(stat.S_IMODE(locked.stat().st_mode), 0o755)
        self.assertTrue((locked / ".value.txt.harness-promote-abcdef").is_file())

        self.assertEqual(harness.execute_run(run_dir), 0)
        self.assertEqual(value.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(stat.S_IMODE(locked.stat().st_mode), 0o555)
        self.assertFalse((locked / ".value.txt.harness-promote-abcdef").exists())

    def test_manifest_mix_accepts_the_exact_temporary_writable_directory_mode(self):
        expected = {
            "path": "sealed",
            "kind": "directory",
            "mode": 0,
        }
        transient = {
            **expected,
            "mode": stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
        }
        before = {"entries": [expected]}
        after = {"entries": [expected]}
        current = {"entries": [transient]}

        self.assertTrue(harness._is_manifest_mix(current, before, after))

    def test_existing_pause_marker_never_leaves_a_supervisor_marker(self):
        run_dir = self.environment.create_run("Pause before any child starts.")
        harness.request_pause(run_dir)

        self.assertEqual(harness.execute_run(run_dir), 2)

        self.assertEqual(harness.read_json(run_dir / "state.json")["status"], "PAUSED")
        self.assertFalse((run_dir / "harness.pid").exists())

    def test_active_child_state_is_reloaded_after_the_run_lock_is_acquired(self):
        run_dir = self.environment.create_run("Never duplicate an active child after supervisor recovery.")
        real_acquire = harness.acquire_file_lock
        injected = False

        def acquire_then_publish_child(stream, *, blocking=True):
            nonlocal injected
            real_acquire(stream, blocking=blocking)
            if not injected:
                injected = True
                state = harness.read_json(run_dir / "state.json")
                state["active_agent"] = {
                    "profile": "alpha",
                    "role": "TASK_WORKER",
                    "pid": os.getpid(),
                    "process_group": os.getpid(),
                    "pid_started": harness.pid_start_time(os.getpid()),
                }
                harness.write_json(run_dir / "state.json", state)

        with patch.object(harness, "acquire_file_lock", side_effect=acquire_then_publish_child):
            with self.assertRaisesRegex(harness.HarnessError, "active child agent"):
                harness.execute_run(run_dir)

        self.assertFalse((run_dir / "harness.pid").exists())

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

    def test_failed_process_tree_cleanup_preserves_active_identity(self):
        run_dir = self.environment.create_run("Preserve evidence when process cleanup is uncertain.")
        self.environment.set_scenario(run_dir, worker_tampers_active_agent=True)

        with patch.object(
            harness,
            "_terminate",
            side_effect=harness.HarnessError("simulated process-tree cleanup failure"),
        ) as terminate:
            with self.assertRaisesRegex(harness.HarnessError, "cleanup failure"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertEqual(state["status"], "PAUSED")
        self.assertIsInstance(state["active_agent"], dict)
        self.assertEqual(state["active_agent"]["role"], "TASK_WORKER")
        self.assertEqual(state["request"], "Preserve evidence when process cleanup is uncertain.")
        self.assertEqual(terminate.call_count, 2)

    def test_failed_active_state_publication_and_cleanup_still_persists_identity(self):
        run_dir = self.environment.create_run("Persist identity before any fallible state publication.")
        real_update = harness._update_state
        real_terminate = harness._terminate
        publication_failed = False

        def fail_first_active_publication(target_run, **changes):
            nonlocal publication_failed
            if not publication_failed and isinstance(changes.get("active_agent"), dict):
                publication_failed = True
                raise harness.HarnessError("simulated active state publication failure")
            return real_update(target_run, **changes)

        def terminate_then_report_uncertainty(process, grace=5.0):
            real_terminate(process, grace)
            raise harness.HarnessError("simulated process-tree cleanup failure")

        with patch.object(harness, "_update_state", side_effect=fail_first_active_publication), patch.object(
            harness,
            "_terminate",
            side_effect=terminate_then_report_uncertainty,
        ):
            with self.assertRaisesRegex(harness.HarnessError, "cleanup failure"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertTrue(publication_failed)
        self.assertEqual(state["status"], "PAUSED")
        self.assertIsInstance(state["active_agent"], dict)
        self.assertEqual(state["active_agent"]["role"], "TASK_WORKER")
        self.assertTrue(state["active_agent"]["pid_started"])

    def test_failed_verifier_state_publication_and_cleanup_persists_identity(self):
        run_dir = self.environment.create_run(
            "Persist verifier identity before any fallible state publication."
        )
        real_update = harness._update_state
        real_terminate = harness._terminate
        publication_failed = False

        def fail_first_verifier_publication(target_run, **changes):
            nonlocal publication_failed
            active = changes.get("active_agent")
            if (
                not publication_failed
                and isinstance(active, dict)
                and active.get("role") == "DETERMINISTIC_VERIFIER"
            ):
                publication_failed = True
                raise harness.HarnessError("simulated verifier state publication failure")
            return real_update(target_run, **changes)

        def fail_verifier_cleanup(process, grace=5.0):
            if publication_failed:
                real_terminate(process, grace)
                raise harness.HarnessError("simulated verifier process-tree cleanup failure")
            return real_terminate(process, grace)

        with patch.object(
            harness,
            "_update_state",
            side_effect=fail_first_verifier_publication,
        ), patch.object(harness, "_terminate", side_effect=fail_verifier_cleanup):
            with self.assertRaisesRegex(harness.HarnessError, "verifier process-tree cleanup failure"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertTrue(publication_failed)
        self.assertEqual(state["status"], "PAUSED")
        self.assertIsInstance(state["active_agent"], dict)
        self.assertEqual(state["active_agent"]["role"], "DETERMINISTIC_VERIFIER")
        self.assertTrue(state["active_agent"]["pid_started"])

    def test_failed_review_check_state_publication_and_cleanup_persists_identity(self):
        self.environment.enable_review_v2()
        run_dir = self.environment.create_run(
            "Persist review-check identity before any fallible state publication."
        )
        real_update = harness._update_state
        real_terminate = harness._terminate
        publication_failed = False

        def fail_first_review_check_publication(target_run, **changes):
            nonlocal publication_failed
            active = changes.get("active_agent")
            if (
                not publication_failed
                and isinstance(active, dict)
                and active.get("role") == "REVIEW_CHECK"
            ):
                publication_failed = True
                raise harness.HarnessError("simulated review-check state publication failure")
            return real_update(target_run, **changes)

        def fail_review_check_cleanup(process, grace=5.0):
            if publication_failed:
                real_terminate(process, grace)
                raise harness.HarnessError("simulated review-check process-tree cleanup failure")
            return real_terminate(process, grace)

        with patch.object(
            harness,
            "_update_state",
            side_effect=fail_first_review_check_publication,
        ), patch.object(harness, "_terminate", side_effect=fail_review_check_cleanup):
            with self.assertRaisesRegex(harness.HarnessError, "review-check process-tree cleanup failure"):
                harness.execute_run(run_dir)

        state = harness.read_json(run_dir / "state.json")
        self.assertTrue(publication_failed)
        self.assertEqual(state["status"], "PAUSED")
        self.assertIsInstance(state["active_agent"], dict)
        self.assertEqual(state["active_agent"]["role"], "REVIEW_CHECK")
        self.assertTrue(state["active_agent"]["pid_started"])

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
                workspace=Path(Path.cwd().anchor),
            )
        with self.assertRaisesRegex(harness.HarnessError, "runs_dir cannot be the workspace"):
            harness.create_run(
                "Keep control records outside delivery root.",
                config_path=self.environment.config,
                runs_dir=self.environment.workspace,
                workspace=self.environment.workspace,
            )

    @unittest.skipUnless(SYMLINKS_AVAILABLE, "symbolic links are unavailable")
    def test_home_workspace_cannot_hide_behind_an_ancestor_symlink(self):
        fake_home = self.environment.root / "fake-home"
        fake_home.mkdir()
        protected = fake_home / "protected.txt"
        protected.write_text("keep\n", encoding="utf-8")
        alias = self.environment.root / "ancestor-alias"
        alias.symlink_to(self.environment.root, target_is_directory=True)

        with patch.object(Path, "home", return_value=fake_home):
            with self.assertRaisesRegex(harness.HarnessError, "too broad"):
                harness.create_run(
                    "Never accept the home directory through an ancestor alias.",
                    config_path=self.environment.config,
                    runs_dir=self.environment.runs,
                    workspace=alias / fake_home.name,
                )

        self.assertEqual(protected.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse(self.environment.runs.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_home_workspace_cannot_hide_behind_an_ancestor_junction(self):
        fake_home = self.environment.root / "junction-home"
        fake_home.mkdir()
        protected = fake_home / "protected.txt"
        protected.write_text("keep\n", encoding="utf-8")
        alias = self.environment.root / "ancestor-junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(self.environment.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"junction creation is unavailable: {result.stderr or result.stdout}")
        self.addCleanup(lambda: os.path.lexists(alias) and os.rmdir(alias))

        with patch.object(Path, "home", return_value=fake_home):
            with self.assertRaisesRegex(harness.HarnessError, "too broad"):
                harness.create_run(
                    "Never accept the home directory through an ancestor junction.",
                    config_path=self.environment.config,
                    runs_dir=self.environment.runs,
                    workspace=alias / fake_home.name,
                )

        self.assertEqual(protected.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse(self.environment.runs.exists())


if __name__ == "__main__":
    unittest.main()
