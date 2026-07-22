# One-Prompt Multi-Agent Harness

[English](README.md) | [中文](README.zh-CN.md)

Start your preferred coding agent in this project directory and describe what you want in one prompt. The root agent plans and coordinates the task, an independent worker implements it in an isolated candidate, the Harness runs mandatory deterministic commands, and a separate reviewer audits that exact candidate together with the verification evidence. Only a passing candidate is promoted into the formal workspace. There is no command menu or task form to complete first.

For example:

> Build a polished ecommerce website with a complete responsive storefront and cart flow.

After one task finishes, simply describe the next one. The workspace persists, each request creates a new run, and the root TUI does not need to restart.

## Quick start

From this directory, start any installed and authenticated CLI:

```bash
hermes chat --tui
codex
cursor-agent
copilot
claude
```

Run only one. It becomes the root TUI agent and should follow the bilingual [AGENTS.md](AGENTS.md). If a CLI does not load repository instructions automatically, ask it to read that file first.

Then describe the task in English or Chinese. Requests, prompts, state, and structured handoffs are UTF-8, and workers and reviewers use the authoritative request's language for user-facing content.

```text
one user request
  -> root TUI agent: understand, plan, coordinate, and report
  -> worker: implement in an isolated candidate workspace
  -> Harness: run configured deterministic verification commands
  -> reviewer: independently check the same candidate and return PASS or FIX
      |-- FIX: the worker repairs the result, then review repeats
      `-- PASS: promote the candidate, save the report, and wait
```

By default, the worker and reviewer inherit the root TUI's agent profile, but they run in separate sessions. For example, starting from Hermes produces one Hermes coordinator, one Hermes worker, and another independent Hermes reviewer. You may also explicitly request different profiles, such as “Cursor implements; Copilot reviews.”

## Requirements

You need:

- Python 3.10+ on macOS or Linux;
- at least one installed and authenticated coding-agent CLI;
- a trusted local directory, because the built-in worker profiles run unattended.

Only the CLI you plan to use must be installed:

```bash
python3 --version
hermes --version       # or codex / cursor-agent / copilot / claude
```

The Harness itself uses only the Python standard library. See [harness.config.json](harness.config.json) for the default configuration.

The default `workspace/` keeps formal task output separate from Harness source. You may set `workspace` to `.` or another existing repository, but the worker still receives a per-run candidate copy rather than that formal directory. Do not expose sensitive data to an untrusted profile.

## From one request to a run

The root agent checks the current state first. When no unfinished run exists, it records the original goal, constraints, and necessary assumptions in `.harness-request.md`, then submits that file to the controller. Clear requests are not turned into a questionnaire; the agent asks one short question only when missing information would materially change the result.

Every run has isolated state, logs, review history, and a final report. At creation time the Harness fingerprints the formal workspace and copies it to `runs/<run>/candidate/`. The worker and all repair rounds modify that persistent candidate only. Deterministic verification and review each use a fresh disposable copy of it. A failed or incomplete run leaves the formal workspace unchanged and retains the candidate for diagnosis; PASS promotes the accepted candidate with a fingerprint check, backup, rollback on error, and final content comparison.

The candidate deliberately excludes `.git`, Harness control paths, and run records. Git discovery is also capped at the candidate boundary so a child command cannot accidentally bind to an ancestor repository. Worker and verification commands therefore must not depend on repository metadata such as `git diff --check`; the formal workspace's `.git` remains untouched during promotion. Directory symlinks, absolute symlinks, and relative symlinks that escape the workspace are rejected so omitted formal content cannot reappear through an alias after promotion. These measures protect normal operation; they are not an OS sandbox.

Handoffs do not depend on a specific CLI's terminal format. The worker maintains `PLAN.md` and writes `WORKER_RESULT.json` after every attempt. The Harness records `VERIFICATION.json` plus `verification.log`, and the reviewer writes `AUDIT.json` in an isolated review directory. The Harness archives these structured files and uses them to decide PASS, FIX, promotion, and where a resumed run should continue.

Only one active run is accepted at a time, including when callers choose different run directories:

- a duplicate submission is rejected while an unfinished run exists;
- a new request can start immediately after COMPLETE or INCOMPLETE;
- after a TUI or process interruption, resume the original run instead of resubmitting its request.

`start` detaches by itself and does not depend on background-task behavior from a particular TUI. A root agent with completion notifications may also run `./harness_control.py wait`; all other TUIs can query `status` at any time.

## Agent selection

Built-in profiles:

| Profile | Interactive entry point | Description |
| --- | --- | --- |
| `hermes` | `hermes chat --tui` | Default fallback; does not pin a provider or model |
| `codex` | `codex` | Receives its non-interactive prompt through stdin |
| `cursor` | `cursor-agent` | Cursor Agent CLI |
| `copilot` | `copilot` | GitHub Copilot CLI |
| `claude` | `claude` | Claude Code |

The Harness first detects the current root TUI profile and uses `default_agent` only as a fallback. When `worker_agent` and `reviewer_agent` are `null`, both inherit the root profile. Explicit profile choices are persisted with the run and reused after recovery.

Run `./harness_control.py agents` to inspect detection and availability. The `coordinator_detection` field records whether the choice came from the launch environment, an ancestor process, an explicit option, or fallback. When a wrapper cannot be detected, add `--coordinator-agent <profile>` to `start`.

A profile needs a description, process-detection patterns, interactive TUI argv, and non-interactive command argv. CLIs that read stdin may also define `stdin`. Command and stdin templates support:

- `{prompt}`: the rendered prompt for this attempt;
- `{prompt_file}`: the saved prompt path;
- `{workspace}`: the worker candidate or disposable reviewer workspace for that role;
- `{run_dir}`: the current run record;
- `{role}`: `worker` or `reviewer`.

Adding or adjusting a CLI normally requires only [harness.config.json](harness.config.json). The CLI must be able to complete one non-interactive turn in a specified directory; GUI-only tools that cannot accept a prompt cannot be worker or reviewer profiles.

## Deterministic acceptance

`verification_commands` is mandatory and must contain at least one command. Each command is an argv array, not a shell string, so quoting and shell expansion cannot silently change its meaning. The Harness snapshots these commands into the run, executes them in order from a disposable copy of the current candidate, and requires exit status 0 from every command before promotion. Each command has the configured `verification_timeout_seconds` limit.

The shipped baseline is:

```json
"verification_commands": [["python3", "-m", "compileall", "-q", "."]]
```

This catches Python syntax errors and remains harmless for non-Python files, but it is only a baseline. Replace or extend it with deterministic checks that actually define acceptance for your workspace, for example:

```json
"verification_commands": [
  ["python3", "-m", "unittest", "discover", "-v"],
  ["npm", "test", "--", "--runInBand"]
]
```

Commands may create caches or build output because their workspace is discarded afterward. A failure is persisted under `iterations/NN/`, is injected into the review as a major issue, and forces FIX even if the reviewer returned PASS. The reviewer cannot waive this gate.

## Status, pause, and recovery

In the root TUI, say “status”, “stop”/“pause”, or “continue”/“resume”. Chinese equivalents work as well. The corresponding commands are:

```bash
./harness_control.py status --json
./harness_control.py stop
./harness_control.py continue
```

`stop` terminates the current child agent while preserving the candidate workspace, phase, and review records already written to disk. It also terminates an orphan child left behind by a crashed supervisor. `continue` refuses to create a duplicate while that child is alive, then resumes the exact saved run after it is stopped. It cannot guarantee an additional session summary before termination. Never submit the old request with `start`, because that creates a duplicate task.

Run format v2 introduced candidate isolation and promotion. Unfinished v1 runs created by an earlier version are intentionally rejected rather than resumed without those guarantees; finish or archive them with the earlier version before upgrading.

Plain CLI output follows `HARNESS_LANG` when set, then the system locale. Override it explicitly when needed:

```bash
HARNESS_LANG=en ./harness_control.py status
HARNESS_LANG=zh-CN ./harness_control.py status
```

For a read-only local dashboard, run `python3 status_dashboard.py` and open the address printed in the terminal. The page can switch between English and Chinese. It is optional, has no control buttons, and deliberately refuses non-loopback bindings. Use an SSH tunnel for remote viewing.

## Worker and reviewer rules

The worker implements only the current request in the run's isolated candidate workspace. The formal workspace is not its working directory. It inspects existing content, makes the smallest complete change, and runs checks appropriate to the task. On a FIX round, the same worker profile resumes from the persisted candidate, plan, result, and full audit, resolving every blocker, major, and minor issue. Minor issues alone do not trigger another repair round.

The reviewer runs in an independent session against another disposable copy of the candidate that was checked by the Harness. Review caches, builds, and accidental edits therefore cannot change either the candidate or formal delivery. The Harness binds verification and review evidence to the same SHA-256 artifact ID, checks that the formal workspace still matches its starting fingerprint, and promotes only after both gates pass. A failed check or any blocker/major finding prevents PASS. Minor findings may be reported without blocking PASS when the reviewer itself returns PASS. The Harness never rewrites an explicit FIX into PASS. The default review limit is three rounds.

The snapshot and integrity checks are not an operating-system sandbox. Built-in profiles still run with the current user's permissions and can access other local paths or the network. Use only trusted local agent profiles and trusted requests. See [SECURITY.md](SECURITY.md) for the complete trust model.

The complete role prompts are [worker.md](prompts/worker.md) and [reviewer.md](prompts/reviewer.md).

## Configuration summary

Important fields in [harness.config.json](harness.config.json):

| Field | Default | Meaning |
| --- | --- | --- |
| `workspace` | `workspace` | Formal persistent delivery directory across runs |
| `default_agent` | `hermes` | Fallback when the root agent cannot be detected |
| `worker_agent` | `null` | `null` inherits the root profile |
| `reviewer_agent` | `null` | `null` inherits the root profile |
| `max_reviews` | `3` | Maximum FIX/review rounds |
| `timeout_seconds` | `5400` | Timeout for one agent invocation |
| `verification_commands` | `python3 -m compileall -q .` | Mandatory ordered argv commands; all must exit 0 |
| `verification_timeout_seconds` | `600` | Timeout for each deterministic command |

The configuration does not store credentials. Each agent CLI manages its own authentication. Never place tokens, `.env` contents, or authorization headers in a request, prompt, log, or profile argv. Run records and raw CLI output are private local evidence files and may still contain task content.

## Development and verification

When changing the Harness itself, work directly in the current coding-agent session. Never start a Harness run to modify the Harness.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
```

Prompt variables and structured artifact schemas are documented in [prompts/README.md](prompts/README.md).

## License

Released under the [MIT License](LICENSE). Security reports should follow [SECURITY.md](SECURITY.md).
