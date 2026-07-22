# Child-role prompts

[English](README.md) | [中文](README.zh-CN.md)

The Harness reads these templates before every child-agent invocation. Templates
are independent of CLI profiles; commands are defined in
[`harness.config.json`](../harness.config.json).

## Templates

- `worker.md`: implements or repairs the current task. Do not change its first-line role marker.
- `reviewer.md`: independently reviews the delivery. It may create disposable test output, but never persists implementation changes. Do not change its first-line role marker.

## Variables

`worker.md`:

- `{request}`: the authoritative natural-language request;
- `{workspace}`: the only writable isolated candidate directory; it is not the formal delivery workspace;
- `{run_dir}`: the current run record, used for state and context;
- `{review_feedback}`: empty on the initial attempt; the full audit, including blocker, major, minor, checks, and limitations, on a repair round.

`reviewer.md`:

- `{request}`: the same authoritative request;
- `{workspace}`: an isolated disposable snapshot of the candidate;
- `{run_dir}`: the current run record;
- `{worker_report}`: the worker's reported changes and checks;
- `{verification_report}`: the Harness-owned deterministic command results bound to this artifact;
- `{review_dir}`: the reviewer's only persistent evidence directory;
- `{artifact_id}`: the SHA-256 identity of the candidate artifact being reviewed.

Preserve variable spelling. Use `{{` and `}}` for literal braces in template
text. Do not hard-code an agent, vendor, model, local path, or task-specific
acceptance rule in a prompt.

## Required artifacts

The worker maintains `{run_dir}/PLAN.md` with at least the objective,
assumptions, acceptance criteria, steps, and current progress. At the end of each
attempt it overwrites `{run_dir}/WORKER_RESULT.json`, which the Harness archives
under the matching iteration. The result must contain:

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

`status` must be `complete` or `blocked`. Each `checks[].status` must be `pass`,
`fail`, or `not_run`. Every `changed_files` entry is workspace-relative and uses
`/` separators, including when the worker runs on Windows.

The reviewer writes its structured result to `{review_dir}/AUDIT.json`:

```json
{
  "schema_version": "generic-harness/audit/v1",
  "verdict": "PASS",
  "summary": "Independent review summary.",
  "checks": [
    {
      "name": "review check",
      "command": "command or manual procedure",
      "status": "pass",
      "details": "observed result"
    }
  ],
  "issues": [
    {
      "severity": "minor",
      "location": "relative/path:line or component",
      "title": "Concise issue",
      "evidence": "What was observed",
      "required_fix": "Concrete correction",
      "acceptance_test": "How to verify the correction"
    }
  ],
  "limitations": []
}
```

`verdict` must be `PASS` or `FIX`; `severity` must be `blocker`, `major`, or
`minor`. Every field is required, including empty arrays. A failed Harness
verification is a mandatory major issue and cannot be waived by the reviewer.
The Harness records the reviewed artifact ID in the accepted audit and final
report, then promotes only that exact candidate.
