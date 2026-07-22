"""Validation and adjudication for the structured Review v2 protocol."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


PLAN_SCHEMA = "generic-harness/review-plan/v1"
CHECKS_SCHEMA = "generic-harness/review-checks/v1"
CHECK_RESULT_SCHEMA = "generic-harness/review-check-result/v1"
AUDIT_SCHEMA = "generic-harness/audit/v2"
FINAL_SCHEMA = "generic-harness/final-review/v1"

SUPPORTED_CHECK_KINDS = {"command", "inspection", "visual"}
REQUIRED_RISK_CATEGORIES = (
    "functional_correctness",
    "regression",
    "error_handling",
    "data_integrity",
    "security",
    "concurrency_lifecycle",
    "portability",
    "performance_resources",
    "usability_accessibility",
    "installation_configuration",
    "documentation_contract",
    "observability_recovery",
)
IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReviewProtocolError(ValueError):
    """Structured review data is invalid or inconsistent."""


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fail(message: str) -> None:
    raise ReviewProtocolError(message)


def _exact_object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        _fail(f"{label} has invalid fields ({'; '.join(details)})")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text")
    return value


def _text_list(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        _fail(f"{label} must be {'a non-empty' if not allow_empty else 'an'} array")
    if not all(isinstance(item, str) and item.strip() for item in value):
        _fail(f"{label} entries must be non-empty text")
    if len(set(value)) != len(value):
        _fail(f"{label} entries must be unique")
    return value


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        _fail(f"{label} must be an integer from 1 to {maximum}")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _text(value, label)
    if not IDENTIFIER.fullmatch(value):
        _fail(f"{label} must match {IDENTIFIER.pattern}")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def validate_review_policy(policy: Any) -> dict[str, Any]:
    fields = {
        "require_plan",
        "require_requirement_coverage",
        "require_worker_claim_coverage",
        "max_dynamic_checks",
        "max_steps_per_check",
        "per_check_timeout_seconds",
        "total_check_timeout_seconds",
        "max_log_bytes_per_step",
        "allowed_check_kinds",
        "required_risk_categories",
    }
    policy = _exact_object(policy, fields, "review_policy")
    for field in (
        "require_plan",
        "require_requirement_coverage",
        "require_worker_claim_coverage",
    ):
        if not isinstance(policy[field], bool):
            _fail(f"review_policy.{field} must be boolean")
    if not policy["require_plan"]:
        _fail("Review v2 requires review_policy.require_plan=true")
    _positive_int(policy["max_dynamic_checks"], "review_policy.max_dynamic_checks", 50)
    _positive_int(policy["max_steps_per_check"], "review_policy.max_steps_per_check", 10)
    per_check = _positive_int(
        policy["per_check_timeout_seconds"],
        "review_policy.per_check_timeout_seconds",
        3600,
    )
    total = _positive_int(
        policy["total_check_timeout_seconds"],
        "review_policy.total_check_timeout_seconds",
        7200,
    )
    if total < per_check:
        _fail("review_policy.total_check_timeout_seconds cannot be smaller than the per-check limit")
    _positive_int(
        policy["max_log_bytes_per_step"],
        "review_policy.max_log_bytes_per_step",
        50 * 1024 * 1024,
    )
    kinds = set(_text_list(policy["allowed_check_kinds"], "review_policy.allowed_check_kinds", allow_empty=False))
    if not kinds <= SUPPORTED_CHECK_KINDS:
        _fail("review_policy.allowed_check_kinds contains an unsupported kind")
    categories = _text_list(
        policy["required_risk_categories"],
        "review_policy.required_risk_categories",
        allow_empty=False,
    )
    if len(categories) > 50:
        _fail("review_policy.required_risk_categories exceeds 50 entries")
    return policy


def review_policy_sha256(policy: dict[str, Any]) -> str:
    validate_review_policy(policy)
    return canonical_sha256(policy)


def validate_review_plan(
    plan: Any,
    *,
    artifact_id: str,
    round_index: int,
    policy: dict[str, Any],
    policy_sha256: str,
    authoritative_request: str,
    worker_claims: list[dict[str, str]],
) -> dict[str, Any]:
    validate_review_policy(policy)
    fields = {
        "schema_version",
        "artifact_id",
        "policy_sha256",
        "round",
        "summary",
        "requirements",
        "worker_claims",
        "risks",
        "checks",
        "limitations",
    }
    plan = _exact_object(plan, fields, "REVIEW_PLAN")
    if plan["schema_version"] != PLAN_SCHEMA:
        _fail(f"REVIEW_PLAN.schema_version must be {PLAN_SCHEMA}")
    _sha256(plan["artifact_id"], "REVIEW_PLAN.artifact_id")
    if plan["artifact_id"] != artifact_id:
        _fail("REVIEW_PLAN artifact does not match the current candidate")
    _sha256(plan["policy_sha256"], "REVIEW_PLAN.policy_sha256")
    if plan["policy_sha256"] != policy_sha256:
        _fail("REVIEW_PLAN policy does not match the snapshotted run policy")
    if (
        isinstance(plan["round"], bool)
        or not isinstance(plan["round"], int)
        or plan["round"] != round_index
    ):
        _fail("REVIEW_PLAN round does not match the current review round")
    _text(plan["summary"], "REVIEW_PLAN.summary")
    _text_list(plan["limitations"], "REVIEW_PLAN.limitations")

    requirements = plan["requirements"]
    claims = plan["worker_claims"]
    risks = plan["risks"]
    checks = plan["checks"]
    if not isinstance(requirements, list) or not requirements:
        _fail("REVIEW_PLAN.requirements must be a non-empty array")
    if not isinstance(claims, list) or not isinstance(risks, list) or not isinstance(checks, list):
        _fail("REVIEW_PLAN worker_claims, risks, and checks must be arrays")
    if not checks:
        _fail("REVIEW_PLAN.checks must be a non-empty array")
    if len(checks) > policy["max_dynamic_checks"]:
        _fail("REVIEW_PLAN exceeds review_policy.max_dynamic_checks")

    subjects: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, requirement in enumerate(requirements):
        requirement = _exact_object(
            requirement,
            {"id", "source", "statement", "criticality"},
            f"REVIEW_PLAN.requirements[{index}]",
        )
        subject_id = _identifier(requirement["id"], f"requirements[{index}].id")
        _text(requirement["source"], f"requirements[{index}].source")
        _text(requirement["statement"], f"requirements[{index}].statement")
        if requirement["criticality"] not in {"must", "should"}:
            _fail(f"requirements[{index}].criticality must be must or should")
        if subject_id in subjects:
            _fail(f"Duplicate review subject ID: {subject_id}")
        subjects[subject_id] = ("requirement", requirement)

    canonical_requirement = {
        "id": "REQ-REQUEST",
        "source": "user_request",
        "statement": authoritative_request,
        "criticality": "must",
    }
    if canonical_requirement not in requirements:
        _fail("REVIEW_PLAN must preserve the complete authoritative request as REQ-REQUEST")

    for index, claim in enumerate(claims):
        claim = _exact_object(claim, {"id", "statement"}, f"REVIEW_PLAN.worker_claims[{index}]")
        subject_id = _identifier(claim["id"], f"worker_claims[{index}].id")
        _text(claim["statement"], f"worker_claims[{index}].statement")
        if subject_id in subjects:
            _fail(f"Duplicate review subject ID: {subject_id}")
        subjects[subject_id] = ("claim", claim)
    if claims != worker_claims:
        _fail("REVIEW_PLAN.worker_claims must exactly match the Harness-derived Worker claims")

    risk_categories: set[str] = set()
    for index, risk in enumerate(risks):
        risk = _exact_object(
            risk,
            {"id", "category", "statement", "applicability", "rationale", "severity_if_real"},
            f"REVIEW_PLAN.risks[{index}]",
        )
        subject_id = _identifier(risk["id"], f"risks[{index}].id")
        category = _text(risk["category"], f"risks[{index}].category")
        _text(risk["statement"], f"risks[{index}].statement")
        _text(risk["rationale"], f"risks[{index}].rationale")
        if risk["applicability"] not in {"applicable", "not_applicable", "unknown"}:
            _fail(f"risks[{index}].applicability is invalid")
        if risk["severity_if_real"] not in {"blocker", "major", "minor"}:
            _fail(f"risks[{index}].severity_if_real is invalid")
        if subject_id in subjects:
            _fail(f"Duplicate review subject ID: {subject_id}")
        if category in risk_categories:
            _fail(f"Duplicate risk category: {category}")
        subjects[subject_id] = ("risk", risk)
        risk_categories.add(category)
    if risk_categories != set(policy["required_risk_categories"]):
        _fail("REVIEW_PLAN must contain exactly one entry for every required risk category")

    checks_by_id: dict[str, dict[str, Any]] = {}
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            _fail(f"REVIEW_PLAN.checks[{index}] must be an object")
        kind = check.get("kind")
        base_fields = {"id", "kind", "purpose", "covers", "expected", "blocking"}
        expected_fields = base_fields | ({"steps", "timeout_seconds"} if kind == "command" else set())
        check = _exact_object(check, expected_fields, f"REVIEW_PLAN.checks[{index}]")
        check_id = _identifier(check["id"], f"checks[{index}].id")
        if check_id in subjects or check_id in checks_by_id:
            _fail(f"Duplicate plan ID: {check_id}")
        if kind not in policy["allowed_check_kinds"]:
            _fail(f"checks[{index}].kind is not allowed by the run policy")
        _text(check["purpose"], f"checks[{index}].purpose")
        if not isinstance(check["blocking"], bool):
            _fail(f"checks[{index}].blocking must be boolean")
        covers = _text_list(check["covers"], f"checks[{index}].covers", allow_empty=False)
        unknown = set(covers) - set(subjects)
        if unknown:
            _fail(f"checks[{index}].covers references unknown subjects: {', '.join(sorted(unknown))}")
        expected = check["expected"]
        if kind == "command":
            expected = _exact_object(expected, {"exit_codes"}, f"checks[{index}].expected")
            exit_codes = expected["exit_codes"]
            if (
                not isinstance(exit_codes, list)
                or not exit_codes
                or not all(isinstance(code, int) and not isinstance(code, bool) for code in exit_codes)
                or len(set(exit_codes)) != len(exit_codes)
            ):
                _fail(f"checks[{index}].expected.exit_codes must be unique integers")
            steps = check["steps"]
            if not isinstance(steps, list) or not steps or len(steps) > policy["max_steps_per_check"]:
                _fail(f"checks[{index}].steps must contain 1..max_steps_per_check argv arrays")
            for step_index, argv in enumerate(steps):
                if (
                    not isinstance(argv, list)
                    or not argv
                    or not all(isinstance(argument, str) and argument for argument in argv)
                ):
                    _fail(f"checks[{index}].steps[{step_index}] must be a non-empty argv array")
            timeout = _positive_int(
                check["timeout_seconds"],
                f"checks[{index}].timeout_seconds",
                policy["per_check_timeout_seconds"],
            )
            if timeout > policy["total_check_timeout_seconds"]:
                _fail(f"checks[{index}].timeout_seconds exceeds the total review-check budget")
        else:
            expected = _exact_object(expected, {"description"}, f"checks[{index}].expected")
            _text(expected["description"], f"checks[{index}].expected.description")
        checks_by_id[check_id] = check

    # Missing or non-blocking coverage is a valid but inconclusive plan, not a
    # malformed handoff.  The deterministic adjudicator handles those gaps.
    return plan


def plan_sha256(plan: dict[str, Any]) -> str:
    return canonical_sha256(plan)


def validate_review_checks(
    bundle: Any,
    *,
    plan: dict[str, Any],
    artifact_id: str,
    policy_sha256: str,
) -> dict[str, Any]:
    bundle = _exact_object(
        bundle,
        {"schema_version", "artifact_id", "policy_sha256", "plan_sha256", "round", "results"},
        "REVIEW_CHECKS",
    )
    if bundle["schema_version"] != CHECKS_SCHEMA:
        _fail(f"REVIEW_CHECKS.schema_version must be {CHECKS_SCHEMA}")
    if bundle["artifact_id"] != artifact_id or bundle["artifact_id"] != plan["artifact_id"]:
        _fail("REVIEW_CHECKS artifact does not match the review plan")
    if bundle["policy_sha256"] != policy_sha256 or bundle["policy_sha256"] != plan["policy_sha256"]:
        _fail("REVIEW_CHECKS policy does not match the review plan")
    expected_plan_sha = plan_sha256(plan)
    if bundle["plan_sha256"] != expected_plan_sha:
        _fail("REVIEW_CHECKS plan hash does not match REVIEW_PLAN")
    if (
        isinstance(bundle["round"], bool)
        or not isinstance(bundle["round"], int)
        or bundle["round"] != plan["round"]
    ):
        _fail("REVIEW_CHECKS round does not match REVIEW_PLAN")
    results = bundle["results"]
    if not isinstance(results, list):
        _fail("REVIEW_CHECKS.results must be an array")
    command_checks = {check["id"]: check for check in plan["checks"] if check["kind"] == "command"}
    if len(results) != len(command_checks):
        _fail("REVIEW_CHECKS must contain exactly one result for every command check")
    seen: set[str] = set()
    for index, result in enumerate(results):
        result = _exact_object(
            result,
            {
                "schema_version",
                "artifact_id",
                "policy_sha256",
                "plan_sha256",
                "check_id",
                "status",
                "started_at",
                "finished_at",
                "duration_seconds",
                "steps",
                "details",
            },
            f"REVIEW_CHECKS.results[{index}]",
        )
        if result["schema_version"] != CHECK_RESULT_SCHEMA:
            _fail(f"Review check result {index} has an unsupported schema")
        if (
            result["artifact_id"] != artifact_id
            or result["policy_sha256"] != policy_sha256
            or result["plan_sha256"] != expected_plan_sha
        ):
            _fail(f"Review check result {index} has mismatched identity")
        check_id = result["check_id"]
        if check_id not in command_checks or check_id in seen:
            _fail(f"Review check result has an unknown or duplicate check ID: {check_id}")
        seen.add(check_id)
        if result["status"] not in {"pass", "fail", "error", "not_run"}:
            _fail(f"Review check {check_id} has an invalid status")
        _text(result["started_at"], f"review check {check_id}.started_at")
        _text(result["finished_at"], f"review check {check_id}.finished_at")
        if (
            isinstance(result["duration_seconds"], bool)
            or not isinstance(result["duration_seconds"], (int, float))
            or (
                isinstance(result["duration_seconds"], float)
                and not math.isfinite(result["duration_seconds"])
            )
            or result["duration_seconds"] < 0
        ):
            _fail(f"Review check {check_id} has an invalid duration")
        _text(result["details"], f"review check {check_id}.details")
        steps = result["steps"]
        if not isinstance(steps, list) or len(steps) > len(command_checks[check_id]["steps"]):
            _fail(f"Review check {check_id} has an invalid step list")
        if result["status"] == "pass" and len(steps) != len(command_checks[check_id]["steps"]):
            _fail(f"Passing review check {check_id} did not run every planned step")
        if result["status"] == "not_run" and steps:
            _fail(f"Not-run review check {check_id} cannot contain executed steps")
        for step_index, step in enumerate(steps):
            step = _exact_object(
                step,
                {
                    "argv",
                    "returncode",
                    "timed_out",
                    "stdout_path",
                    "stderr_path",
                    "stdout_sha256",
                    "stderr_sha256",
                    "stdout_truncated",
                    "stderr_truncated",
                    "duration_seconds",
                    "error",
                },
                f"review check {check_id}.steps[{step_index}]",
            )
            if step["argv"] != command_checks[check_id]["steps"][step_index]:
                _fail(f"Review check {check_id} step argv does not match the plan")
            if step["returncode"] is not None and (
                isinstance(step["returncode"], bool) or not isinstance(step["returncode"], int)
            ):
                _fail(f"Review check {check_id} step returncode is invalid")
            if not isinstance(step["timed_out"], bool):
                _fail(f"Review check {check_id} step timed_out must be boolean")
            for field in ("stdout_path", "stderr_path"):
                _text(step[field], f"review check {check_id} step {field}")
            _sha256(step["stdout_sha256"], f"review check {check_id} step stdout_sha256")
            _sha256(step["stderr_sha256"], f"review check {check_id} step stderr_sha256")
            for field in ("stdout_truncated", "stderr_truncated"):
                if not isinstance(step[field], bool):
                    _fail(f"Review check {check_id} step {field} must be boolean")
            if (
                isinstance(step["duration_seconds"], bool)
                or not isinstance(step["duration_seconds"], (int, float))
                or (
                    isinstance(step["duration_seconds"], float)
                    and not math.isfinite(step["duration_seconds"])
                )
                or step["duration_seconds"] < 0
            ):
                _fail(f"Review check {check_id} step duration is invalid")
            if not isinstance(step["error"], str):
                _fail(f"Review check {check_id} step error must be text")
        if result["status"] == "pass":
            allowed = set(command_checks[check_id]["expected"]["exit_codes"])
            if any(step["timed_out"] or step["error"] or step["returncode"] not in allowed for step in steps):
                _fail(f"Passing review check {check_id} contradicts its step evidence")
        elif result["status"] == "fail":
            allowed = set(command_checks[check_id]["expected"]["exit_codes"])
            if not any(step["timed_out"] or (not step["error"] and step["returncode"] not in allowed) for step in steps):
                _fail(f"Failed review check {check_id} has no failing step evidence")
        elif result["status"] == "error" and not any(step["error"] for step in steps):
            _fail(f"Errored review check {check_id} has no executor error evidence")
    return bundle


def validate_audit_v2(
    audit: Any,
    *,
    plan: dict[str, Any],
    checks: dict[str, Any],
) -> dict[str, Any]:
    audit = _exact_object(
        audit,
        {
            "schema_version",
            "artifact_id",
            "plan_sha256",
            "verdict",
            "summary",
            "coverage",
            "checks",
            "issues",
            "limitations",
        },
        "AUDIT",
    )
    if audit["schema_version"] != AUDIT_SCHEMA:
        _fail(f"AUDIT.schema_version must be {AUDIT_SCHEMA}")
    if audit["artifact_id"] != plan["artifact_id"]:
        _fail("AUDIT artifact does not match REVIEW_PLAN")
    if audit["plan_sha256"] != plan_sha256(plan):
        _fail("AUDIT plan hash does not match REVIEW_PLAN")
    if audit["verdict"] not in {"PASS", "FIX", "INCONCLUSIVE"}:
        _fail("AUDIT.verdict must be PASS, FIX, or INCONCLUSIVE")
    _text(audit["summary"], "AUDIT.summary")
    _text_list(audit["limitations"], "AUDIT.limitations")

    planned_checks = {check["id"]: check for check in plan["checks"]}
    command_results = {result["check_id"]: result for result in checks["results"]}
    manual_plans = {check_id: check for check_id, check in planned_checks.items() if check["kind"] != "command"}
    manual_results: dict[str, dict[str, Any]] = {}
    if not isinstance(audit["checks"], list) or len(audit["checks"]) != len(manual_plans):
        _fail("AUDIT.checks must contain exactly one result for every inspection or visual check")
    for index, result in enumerate(audit["checks"]):
        result = _exact_object(
            result,
            {"check_id", "status", "details", "evidence_refs"},
            f"AUDIT.checks[{index}]",
        )
        check_id = result["check_id"]
        if check_id not in manual_plans or check_id in manual_results:
            _fail(f"AUDIT has an unknown or duplicate manual check: {check_id}")
        if result["status"] not in {"pass", "fail", "error", "not_run"}:
            _fail(f"AUDIT manual check {check_id} has an invalid status")
        _text(result["details"], f"AUDIT manual check {check_id}.details")
        refs = _text_list(result["evidence_refs"], f"AUDIT manual check {check_id}.evidence_refs")
        if result["status"] == "pass" and not refs:
            _fail(f"Passing manual check {check_id} must cite persistent evidence")
        manual_results[check_id] = result

    outcome = {**command_results, **manual_results}
    subject_checks: dict[str, set[str]] = {}
    for check in plan["checks"]:
        for subject_id in check["covers"]:
            subject_checks.setdefault(subject_id, set()).add(check["id"])
    required_subjects = {
        requirement["id"] for requirement in plan["requirements"]
    } | {claim["id"] for claim in plan["worker_claims"]} | {
        risk["id"] for risk in plan["risks"] if risk["applicability"] != "not_applicable"
    }
    if not isinstance(audit["coverage"], list) or len(audit["coverage"]) != len(required_subjects):
        _fail("AUDIT.coverage must contain exactly one entry for every planned review subject")
    seen_subjects: set[str] = set()
    for index, item in enumerate(audit["coverage"]):
        item = _exact_object(
            item,
            {"subject_id", "status", "evidence_refs"},
            f"AUDIT.coverage[{index}]",
        )
        subject_id = item["subject_id"]
        if subject_id not in required_subjects or subject_id in seen_subjects:
            _fail(f"AUDIT coverage has an unknown or duplicate subject: {subject_id}")
        seen_subjects.add(subject_id)
        if item["status"] not in {"covered", "uncovered"}:
            _fail(f"AUDIT coverage status is invalid for {subject_id}")
        refs = _text_list(item["evidence_refs"], f"AUDIT coverage {subject_id}.evidence_refs")
        if not set(refs) <= subject_checks.get(subject_id, set()):
            _fail(f"AUDIT coverage for {subject_id} cites an unrelated check")
        has_passing_evidence = any(outcome.get(check_id, {}).get("status") == "pass" for check_id in refs)
        if item["status"] == "covered" and not has_passing_evidence:
            _fail(f"AUDIT claims {subject_id} is covered without passing evidence")

    if not isinstance(audit["issues"], list):
        _fail("AUDIT.issues must be an array")
    for index, issue in enumerate(audit["issues"]):
        issue = _exact_object(
            issue,
            {
                "severity",
                "location",
                "title",
                "evidence",
                "evidence_refs",
                "required_fix",
                "acceptance_test",
            },
            f"AUDIT.issues[{index}]",
        )
        if issue["severity"] not in {"blocker", "major", "minor"}:
            _fail(f"AUDIT issue {index} has an invalid severity")
        for field in ("location", "title", "evidence", "required_fix", "acceptance_test"):
            _text(issue[field], f"AUDIT issue {index}.{field}")
        _text_list(issue["evidence_refs"], f"AUDIT issue {index}.evidence_refs", allow_empty=False)
    if audit["verdict"] == "FIX" and not audit["issues"]:
        _fail("AUDIT verdict FIX must include at least one concrete issue")
    if (
        audit["verdict"] == "INCONCLUSIVE"
        and not audit["limitations"]
        and not any(item["status"] == "uncovered" for item in audit["coverage"])
        and not any(result["status"] in {"error", "not_run"} for result in outcome.values())
    ):
        _fail("AUDIT verdict INCONCLUSIVE must identify unavailable evidence or a limitation")
    return audit


def adjudicate_review(
    *,
    plan: dict[str, Any],
    checks: dict[str, Any],
    audit: dict[str, Any],
    deterministic_verification_passed: bool,
    policy: dict[str, Any],
) -> tuple[str, list[str]]:
    """Return the Harness-owned verdict and stable reason codes."""
    outcomes = {result["check_id"]: result["status"] for result in checks["results"]}
    outcomes.update({result["check_id"]: result["status"] for result in audit["checks"]})
    planned = {check["id"]: check for check in plan["checks"]}
    coverage = {item["subject_id"]: item for item in audit["coverage"]}
    fix: list[str] = []
    inconclusive: list[str] = []

    def covered_by_passing_evidence(subject_id: str, *, blocking: bool) -> bool:
        item = coverage.get(subject_id)
        if not item or item["status"] != "covered":
            return False
        return any(
            outcomes.get(check_id) == "pass"
            and check_id in planned
            and (not blocking or planned[check_id]["blocking"])
            for check_id in item["evidence_refs"]
        )

    if not deterministic_verification_passed:
        fix.append("DETERMINISTIC_VERIFICATION_FAILED")
    if audit["verdict"] == "FIX":
        fix.append("REVIEWER_REQUESTED_FIX")
    if any(issue["severity"] in {"blocker", "major"} for issue in audit["issues"]):
        fix.append("BLOCKING_ISSUE_RECORDED")
    if any(outcomes.get(check_id) == "fail" and check["blocking"] for check_id, check in planned.items()):
        fix.append("BLOCKING_CHECK_FAILED")

    if audit["verdict"] == "INCONCLUSIVE":
        inconclusive.append("REVIEWER_INCONCLUSIVE")
    if any(
        outcomes.get(check_id) in {"error", "not_run", None} and check["blocking"]
        for check_id, check in planned.items()
    ):
        inconclusive.append("BLOCKING_CHECK_UNAVAILABLE")
    if policy["require_requirement_coverage"] and any(
        requirement["criticality"] == "must"
        and not covered_by_passing_evidence(requirement["id"], blocking=True)
        for requirement in plan["requirements"]
    ):
        inconclusive.append("MUST_REQUIREMENT_UNCOVERED")
    if policy["require_worker_claim_coverage"] and any(
        not covered_by_passing_evidence(claim["id"], blocking=False)
        for claim in plan["worker_claims"]
    ):
        inconclusive.append("WORKER_CLAIM_UNCOVERED")
    if any(
        risk["applicability"] == "applicable"
        and not covered_by_passing_evidence(
            risk["id"],
            blocking=risk["severity_if_real"] != "minor",
        )
        for risk in plan["risks"]
    ):
        inconclusive.append("APPLICABLE_RISK_UNCOVERED")
    if any(
        risk["applicability"] == "unknown"
        and risk["severity_if_real"] in {"blocker", "major"}
        and not covered_by_passing_evidence(risk["id"], blocking=True)
        for risk in plan["risks"]
    ):
        inconclusive.append("SERIOUS_UNKNOWN_RISK_UNRESOLVED")

    if fix:
        return "FIX", list(dict.fromkeys(fix))
    if inconclusive:
        return "INCONCLUSIVE", list(dict.fromkeys(inconclusive))
    return "PASS", []


def validate_final_review(
    final: Any,
    *,
    artifact_id: str,
    round_index: int,
    policy_sha256: str,
    plan_sha256_value: str,
) -> dict[str, Any]:
    final = _exact_object(
        final,
        {
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
            "decided_at",
        },
        "FINAL_REVIEW",
    )
    if final["schema_version"] != FINAL_SCHEMA:
        _fail(f"FINAL_REVIEW.schema_version must be {FINAL_SCHEMA}")
    if final["artifact_id"] != artifact_id:
        _fail("FINAL_REVIEW artifact does not match the accepted candidate")
    if final["policy_sha256"] != policy_sha256:
        _fail("FINAL_REVIEW policy does not match the run policy")
    if final["plan_sha256"] != plan_sha256_value:
        _fail("FINAL_REVIEW plan hash does not match REVIEW_PLAN")
    if (
        isinstance(final["round"], bool)
        or not isinstance(final["round"], int)
        or final["round"] != round_index
    ):
        _fail("FINAL_REVIEW round does not match the current review round")
    if final["verdict"] not in {"PASS", "FIX", "INCONCLUSIVE"}:
        _fail("FINAL_REVIEW verdict is invalid")
    _text_list(final["reason_codes"], "FINAL_REVIEW.reason_codes")
    for field in (
        "plan_path",
        "checks_path",
        "audit_path",
        "verification_path",
        "manual_evidence_path",
        "decided_at",
    ):
        _text(final[field], f"FINAL_REVIEW.{field}")
    _text_list(final["evidence_paths"], "FINAL_REVIEW.evidence_paths")
    return final
