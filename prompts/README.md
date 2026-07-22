# Child-role prompts

[English](README.md) | [中文](README.zh-CN.md)

The Harness reads these templates before every child-agent invocation. They are
independent of CLI profiles; commands are defined in
[`harness.config.json`](../harness.config.json). Preserve every first-line role
marker and every second-line Review v2 stage marker.

## Templates

- `worker.md`: implements or repairs the current task in the persistent isolated
  candidate.
- `review_planner.md`: starts a fresh Reviewer-role session with stage `PLAN` and
  writes only `REVIEW_PLAN.json`.
- `reviewer_v2.md`: starts another fresh Reviewer-role session with stage
  `ASSESS`, writes cited manual evidence, and writes Audit v2.
- `reviewer.md`: legacy Audit v1 template, retained only so an unfinished safe
  run-format v2 task without `review_protocol_version` can continue unchanged.

## Variables

`worker.md` receives `{request}`, `{workspace}`, `{run_dir}`, and
`{review_feedback}`.

`review_planner.md` receives the same authoritative request and disposable
artifact snapshot plus `{worker_report}`, `{verification_report}`,
`{review_dir}`, `{artifact_id}`, `{review_round}`, `{review_policy}`,
`{policy_sha256}`, and Harness-owned `{review_subjects}`. The latter contains the
canonical `REQ-REQUEST` object and exact Worker claims that the plan must copy.

`reviewer_v2.md` receives the request, disposable snapshot, Worker and
verification reports, `{review_plan}`, Harness-owned `{review_checks}`,
`{plan_sha256}`, `{review_dir}`, and `{artifact_id}`.

Preserve variable spelling. Use `{{` and `}}` for literal braces in template
text. Do not hard-code an Agent, vendor, model, local path, or task-specific
acceptance rule.

## Worker handoff

The Worker maintains `{run_dir}/PLAN.md` and overwrites
`{run_dir}/WORKER_RESULT.json` after each attempt. The Harness validates and
archives the result under the matching iteration. Its schema remains
`generic-harness/worker-result/v1`:

```json
{
  "schema_version": "generic-harness/worker-result/v1",
  "status": "complete",
  "summary": "What was delivered.",
  "changed_files": ["relative/path"],
  "checks": [
    {
      "name": "test name",
      "command": "command or manual procedure",
      "status": "pass",
      "details": "observed result"
    }
  ],
  "limitations": []
}
```

`status` is `complete` or `blocked`; check status is `pass`, `fail`, or
`not_run`. Changed paths are candidate-relative and always use `/` separators.

## Review v2 handoffs

The protocol is deliberately split so no Agent owns the final gate:

1. The Planner writes `REVIEW_PLAN.json`
   (`generic-harness/review-plan/v1`). It must preserve the complete request as
   `REQ-REQUEST`, copy every Harness-derived Worker claim exactly, include one
   entry per configured risk category, and define all coverage through
   `checks[].covers`. Command checks use argv arrays; inspection and visual
   checks describe their expected evidence.
2. The Harness validates artifact, round, policy fingerprint, IDs, fields,
   limits, and references. It resolves the exact `{python}` token and runs every
   command check in a disposable artifact copy. It owns `REVIEW_CHECKS.json`,
   each `harness-evidence/*/RESULT.json`, and bounded stdout/stderr logs.
3. The Assessor writes `AUDIT.json` (`generic-harness/audit/v2`) with exactly one
   coverage entry per required subject and one result per inspection or visual
   check. Passing manual checks cite persistent files under
   `reviewer-evidence/`. Verdict is `PASS`, `FIX`, or `INCONCLUSIVE`.
4. The Harness hashes cited manual evidence and writes `FINAL_REVIEW.json`. It
   recalculates that verdict from all underlying evidence again before
   promotion. Neither Reviewer stage may edit Harness-owned evidence.

Missing blocking evidence is valid protocol data but yields `INCONCLUSIVE`, not
a fabricated PASS. A failed deterministic verification, failed blocking check,
or confirmed blocker/major yields `FIX`. Minor findings alone do not block
PASS. Every structured object rejects unknown and missing fields and is bound to
the same artifact and plan hashes.

## Legacy Review v1

`reviewer.md` writes `generic-harness/audit/v1` with `PASS` or `FIX`, checks,
issues, and limitations. It is not selected for new runs and must not be used to
represent Review v2 coverage. It remains documented solely for safe resumption
of already-created run-format v2 tasks.
