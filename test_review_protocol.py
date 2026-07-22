import unittest

import review_protocol as protocol


EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def policy() -> dict:
    return {
        "require_plan": True,
        "require_requirement_coverage": True,
        "require_worker_claim_coverage": True,
        "max_dynamic_checks": 8,
        "max_steps_per_check": 3,
        "per_check_timeout_seconds": 30,
        "total_check_timeout_seconds": 60,
        "max_log_bytes_per_step": 1024,
        "allowed_check_kinds": ["command", "inspection", "visual"],
        "required_risk_categories": ["security"],
    }


def worker_claims() -> list[dict[str, str]]:
    return [{"id": "CLAIM-SUMMARY", "statement": "Implemented the requested change."}]


def plan() -> dict:
    current_policy = policy()
    return {
        "schema_version": protocol.PLAN_SCHEMA,
        "artifact_id": "a" * 64,
        "policy_sha256": protocol.review_policy_sha256(current_policy),
        "round": 0,
        "summary": "Check the delivery and its security boundary.",
        "requirements": [
            {
                "id": "REQ-REQUEST",
                "source": "user_request",
                "statement": "Implement the requested change.",
                "criticality": "must",
            }
        ],
        "worker_claims": worker_claims(),
        "risks": [
            {
                "id": "RISK-SECURITY",
                "category": "security",
                "statement": "The change could weaken a trust boundary.",
                "applicability": "applicable",
                "rationale": "The task changes review behavior.",
                "severity_if_real": "major",
            }
        ],
        "checks": [
            {
                "id": "CHK-COMMAND",
                "kind": "command",
                "purpose": "Run a deterministic check.",
                "covers": ["REQ-REQUEST", "CLAIM-SUMMARY", "RISK-SECURITY"],
                "expected": {"exit_codes": [0]},
                "blocking": True,
                "steps": [["python", "-c", "pass"]],
                "timeout_seconds": 10,
            },
            {
                "id": "CHK-INSPECTION",
                "kind": "inspection",
                "purpose": "Inspect the changed boundary.",
                "covers": ["REQ-REQUEST"],
                "expected": {"description": "No trust boundary is weakened."},
                "blocking": False,
            },
        ],
        "limitations": [],
    }


def validate_plan(value: dict | None = None) -> dict:
    value = value or plan()
    return protocol.validate_review_plan(
        value,
        artifact_id="a" * 64,
        round_index=0,
        policy=policy(),
        policy_sha256=protocol.review_policy_sha256(policy()),
        authoritative_request="Implement the requested change.",
        worker_claims=worker_claims(),
    )


def checks(value: dict | None = None) -> dict:
    current_plan = value or plan()
    return {
        "schema_version": protocol.CHECKS_SCHEMA,
        "artifact_id": "a" * 64,
        "policy_sha256": protocol.review_policy_sha256(policy()),
        "plan_sha256": protocol.plan_sha256(current_plan),
        "round": 0,
        "results": [
            {
                "schema_version": protocol.CHECK_RESULT_SCHEMA,
                "artifact_id": "a" * 64,
                "policy_sha256": protocol.review_policy_sha256(policy()),
                "plan_sha256": protocol.plan_sha256(current_plan),
                "check_id": "CHK-COMMAND",
                "status": "pass",
                "started_at": "2026-07-22T00:00:00Z",
                "finished_at": "2026-07-22T00:00:01Z",
                "duration_seconds": 1.0,
                "steps": [
                    {
                        "argv": ["python", "-c", "pass"],
                        "returncode": 0,
                        "timed_out": False,
                        "stdout_path": "evidence/CHK-COMMAND/step-01.stdout.log",
                        "stderr_path": "evidence/CHK-COMMAND/step-01.stderr.log",
                        "stdout_sha256": EMPTY_SHA256,
                        "stderr_sha256": EMPTY_SHA256,
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                        "duration_seconds": 1.0,
                        "error": "",
                    }
                ],
                "details": "All planned steps passed.",
            }
        ],
    }


def audit(current_plan: dict | None = None) -> dict:
    current_plan = current_plan or plan()
    return {
        "schema_version": protocol.AUDIT_SCHEMA,
        "artifact_id": "a" * 64,
        "plan_sha256": protocol.plan_sha256(current_plan),
        "verdict": "PASS",
        "summary": "Independent review passed.",
        "coverage": [
            {"subject_id": "REQ-REQUEST", "status": "covered", "evidence_refs": ["CHK-COMMAND"]},
            {"subject_id": "CLAIM-SUMMARY", "status": "covered", "evidence_refs": ["CHK-COMMAND"]},
            {"subject_id": "RISK-SECURITY", "status": "covered", "evidence_refs": ["CHK-COMMAND"]},
        ],
        "checks": [
            {
                "check_id": "CHK-INSPECTION",
                "status": "pass",
                "details": "Inspected the boundary.",
                "evidence_refs": ["evidence/CHK-INSPECTION/inspection.txt"],
            }
        ],
        "issues": [],
        "limitations": [],
    }


class ReviewProtocolTests(unittest.TestCase):
    def test_valid_plan_checks_and_audit(self):
        current_plan = validate_plan()
        current_checks = protocol.validate_review_checks(
            checks(current_plan),
            plan=current_plan,
            artifact_id="a" * 64,
            policy_sha256=protocol.review_policy_sha256(policy()),
        )
        current_audit = protocol.validate_audit_v2(audit(current_plan), plan=current_plan, checks=current_checks)

        self.assertEqual(
            protocol.adjudicate_review(
                plan=current_plan,
                checks=current_checks,
                audit=current_audit,
                deterministic_verification_passed=True,
                policy=policy(),
            ),
            ("PASS", []),
        )

    def test_policy_is_strict_and_bool_is_not_an_integer(self):
        invalid = policy()
        invalid["unknown"] = True
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "unknown unknown"):
            protocol.validate_review_policy(invalid)

        invalid = policy()
        invalid["max_dynamic_checks"] = True
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "integer"):
            protocol.validate_review_policy(invalid)

    def test_rounds_and_durations_reject_bool_and_non_finite_numbers(self):
        invalid_plan = plan()
        invalid_plan["round"] = False
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "round"):
            validate_plan(invalid_plan)

        current_plan = validate_plan()
        invalid_checks = checks(current_plan)
        invalid_checks["round"] = False
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "round"):
            protocol.validate_review_checks(
                invalid_checks,
                plan=current_plan,
                artifact_id="a" * 64,
                policy_sha256=protocol.review_policy_sha256(policy()),
            )

        invalid_checks = checks(current_plan)
        invalid_checks["results"][0]["duration_seconds"] = float("nan")
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "duration"):
            protocol.validate_review_checks(
                invalid_checks,
                plan=current_plan,
                artifact_id="a" * 64,
                policy_sha256=protocol.review_policy_sha256(policy()),
            )

    def test_plan_rejects_unknown_fields_duplicate_ids_and_unknown_references(self):
        invalid = plan()
        invalid["extra"] = None
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "unknown extra"):
            validate_plan(invalid)

        invalid = plan()
        invalid["checks"][0]["id"] = "REQ-REQUEST"
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "Duplicate plan ID"):
            validate_plan(invalid)

        invalid = plan()
        invalid["checks"][0]["covers"] = ["REQ-MISSING"]
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "unknown subjects"):
            validate_plan(invalid)

    def test_plan_preserves_authoritative_request_and_harness_claims(self):
        invalid = plan()
        invalid["requirements"][0]["statement"] = "A smaller request."
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "authoritative request"):
            validate_plan(invalid)

        invalid = plan()
        invalid["worker_claims"] = []
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "Harness-derived"):
            validate_plan(invalid)

    def test_identity_mismatches_are_rejected(self):
        invalid = plan()
        invalid["artifact_id"] = "b" * 64
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "artifact"):
            validate_plan(invalid)

        current_plan = validate_plan()
        invalid_checks = checks(current_plan)
        invalid_checks["plan_sha256"] = "b" * 64
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "plan hash"):
            protocol.validate_review_checks(
                invalid_checks,
                plan=current_plan,
                artifact_id="a" * 64,
                policy_sha256=protocol.review_policy_sha256(policy()),
            )

    def test_missing_planned_coverage_is_inconclusive_not_malformed(self):
        current_plan = plan()
        current_plan["checks"][0]["covers"] = ["CLAIM-SUMMARY"]
        current_plan["checks"][1]["covers"] = ["CLAIM-SUMMARY"]
        current_plan = validate_plan(current_plan)
        current_checks = checks(current_plan)
        current_checks["plan_sha256"] = protocol.plan_sha256(current_plan)
        current_checks["results"][0]["plan_sha256"] = protocol.plan_sha256(current_plan)
        current_checks = protocol.validate_review_checks(
            current_checks,
            plan=current_plan,
            artifact_id="a" * 64,
            policy_sha256=protocol.review_policy_sha256(policy()),
        )
        current_audit = audit(current_plan)
        current_audit["coverage"] = [
            {"subject_id": "REQ-REQUEST", "status": "uncovered", "evidence_refs": []},
            {"subject_id": "CLAIM-SUMMARY", "status": "covered", "evidence_refs": ["CHK-COMMAND"]},
            {"subject_id": "RISK-SECURITY", "status": "uncovered", "evidence_refs": []},
        ]
        current_audit = protocol.validate_audit_v2(current_audit, plan=current_plan, checks=current_checks)

        verdict, reasons = protocol.adjudicate_review(
            plan=current_plan,
            checks=current_checks,
            audit=current_audit,
            deterministic_verification_passed=True,
            policy=policy(),
        )
        self.assertEqual(verdict, "INCONCLUSIVE")
        self.assertIn("MUST_REQUIREMENT_UNCOVERED", reasons)
        self.assertIn("APPLICABLE_RISK_UNCOVERED", reasons)

    def test_assessor_uncovered_status_cannot_be_overridden_by_a_passing_check(self):
        current_plan = validate_plan()
        current_checks = protocol.validate_review_checks(
            checks(current_plan),
            plan=current_plan,
            artifact_id="a" * 64,
            policy_sha256=protocol.review_policy_sha256(policy()),
        )
        current_audit = audit(current_plan)
        current_audit["coverage"][0] = {
            "subject_id": "REQ-REQUEST",
            "status": "uncovered",
            "evidence_refs": [],
        }
        current_audit["coverage"][2] = {
            "subject_id": "RISK-SECURITY",
            "status": "uncovered",
            "evidence_refs": [],
        }
        current_audit = protocol.validate_audit_v2(
            current_audit,
            plan=current_plan,
            checks=current_checks,
        )

        verdict, reasons = protocol.adjudicate_review(
            plan=current_plan,
            checks=current_checks,
            audit=current_audit,
            deterministic_verification_passed=True,
            policy=policy(),
        )
        self.assertEqual(verdict, "INCONCLUSIVE")
        self.assertIn("MUST_REQUIREMENT_UNCOVERED", reasons)
        self.assertIn("APPLICABLE_RISK_UNCOVERED", reasons)

    def test_fix_and_inconclusive_verdicts_require_a_concrete_basis(self):
        current_plan = validate_plan()
        current_checks = protocol.validate_review_checks(
            checks(current_plan),
            plan=current_plan,
            artifact_id="a" * 64,
            policy_sha256=protocol.review_policy_sha256(policy()),
        )
        empty_fix = audit(current_plan)
        empty_fix["verdict"] = "FIX"
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "concrete issue"):
            protocol.validate_audit_v2(empty_fix, plan=current_plan, checks=current_checks)

        empty_inconclusive = audit(current_plan)
        empty_inconclusive["verdict"] = "INCONCLUSIVE"
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "unavailable evidence"):
            protocol.validate_audit_v2(
                empty_inconclusive,
                plan=current_plan,
                checks=current_checks,
            )

    def test_check_result_cannot_forge_pass_or_fail(self):
        current_plan = validate_plan()
        forged = checks(current_plan)
        forged["results"][0]["steps"][0]["returncode"] = 3
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "contradicts"):
            protocol.validate_review_checks(
                forged,
                plan=current_plan,
                artifact_id="a" * 64,
                policy_sha256=protocol.review_policy_sha256(policy()),
            )

        forged = checks(current_plan)
        forged["results"][0]["status"] = "fail"
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "no failing step"):
            protocol.validate_review_checks(
                forged,
                plan=current_plan,
                artifact_id="a" * 64,
                policy_sha256=protocol.review_policy_sha256(policy()),
            )

    def test_audit_cannot_claim_coverage_without_passing_evidence(self):
        current_plan = validate_plan()
        current_checks = checks(current_plan)
        current_checks["results"][0]["status"] = "error"
        current_checks["results"][0]["steps"][0]["returncode"] = None
        current_checks["results"][0]["steps"][0]["error"] = "missing program"
        current_checks = protocol.validate_review_checks(
            current_checks,
            plan=current_plan,
            artifact_id="a" * 64,
            policy_sha256=protocol.review_policy_sha256(policy()),
        )
        with self.assertRaisesRegex(protocol.ReviewProtocolError, "without passing evidence"):
            protocol.validate_audit_v2(audit(current_plan), plan=current_plan, checks=current_checks)

    def test_fix_has_priority_over_inconclusive(self):
        current_plan = validate_plan()
        current_checks = checks(current_plan)
        current_checks["results"][0]["status"] = "error"
        current_checks["results"][0]["steps"][0]["returncode"] = None
        current_checks["results"][0]["steps"][0]["error"] = "executor unavailable"
        current_checks = protocol.validate_review_checks(
            current_checks,
            plan=current_plan,
            artifact_id="a" * 64,
            policy_sha256=protocol.review_policy_sha256(policy()),
        )
        current_audit = audit(current_plan)
        current_audit["verdict"] = "FIX"
        current_audit["coverage"] = [
            {"subject_id": "REQ-REQUEST", "status": "uncovered", "evidence_refs": []},
            {"subject_id": "CLAIM-SUMMARY", "status": "uncovered", "evidence_refs": []},
            {"subject_id": "RISK-SECURITY", "status": "uncovered", "evidence_refs": []},
        ]
        current_audit["issues"] = [
            {
                "severity": "major",
                "location": "module.py:1",
                "title": "Confirmed defect",
                "evidence": "The defect is visible in the implementation.",
                "evidence_refs": ["CHK-INSPECTION"],
                "required_fix": "Correct the implementation.",
                "acceptance_test": "Repeat the inspection.",
            }
        ]
        current_audit = protocol.validate_audit_v2(current_audit, plan=current_plan, checks=current_checks)

        verdict, reasons = protocol.adjudicate_review(
            plan=current_plan,
            checks=current_checks,
            audit=current_audit,
            deterministic_verification_passed=True,
            policy=policy(),
        )
        self.assertEqual(verdict, "FIX")
        self.assertIn("BLOCKING_ISSUE_RECORDED", reasons)

    def test_explicit_fix_is_preserved_even_for_minor_only(self):
        current_plan = validate_plan()
        current_checks = protocol.validate_review_checks(
            checks(current_plan),
            plan=current_plan,
            artifact_id="a" * 64,
            policy_sha256=protocol.review_policy_sha256(policy()),
        )
        current_audit = audit(current_plan)
        current_audit["verdict"] = "FIX"
        current_audit["issues"] = [
            {
                "severity": "minor",
                "location": "README.md:1",
                "title": "Minor wording issue",
                "evidence": "The wording is unclear.",
                "evidence_refs": ["CHK-INSPECTION"],
                "required_fix": "Clarify the wording.",
                "acceptance_test": "Read the updated paragraph.",
            }
        ]
        current_audit = protocol.validate_audit_v2(current_audit, plan=current_plan, checks=current_checks)
        verdict, reasons = protocol.adjudicate_review(
            plan=current_plan,
            checks=current_checks,
            audit=current_audit,
            deterministic_verification_passed=True,
            policy=policy(),
        )
        self.assertEqual(verdict, "FIX")
        self.assertEqual(reasons, ["REVIEWER_REQUESTED_FIX"])


if __name__ == "__main__":
    unittest.main()
