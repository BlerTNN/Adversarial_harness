# One-Prompt Multi-Agent Harness

[中文](README.md) | [English](README.en.md)

Start your preferred coding agent in this project directory and describe what you want in one prompt. The root agent plans and coordinates the task, an independent worker implements it, and a separate reviewer audits the result. There is no command menu or task form to complete first.

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
  -> worker: implement and verify in the persistent workspace
  -> reviewer: independently check and return PASS or FIX
      |-- FIX: the worker repairs the result, then review repeats
      `-- PASS: save the report and wait for the next request
```

By default, the worker and reviewer inherit the root TUI's agent profile, but they run in separate sessions. For example, starting from Hermes produces one Hermes coordinator, one Hermes worker, and another independent Hermes reviewer. You may also explicitly request different profiles, such as “Cursor implements; Copilot reviews.”

## Requirements

You need:

- Python 3;
- at least one installed and authenticated coding-agent CLI;
- a trusted local directory, because the built-in worker profiles run unattended.

Only the CLI you plan to use must be installed:

```bash
python3 --version
hermes --version       # or codex / cursor-agent / copilot / claude
```

The Harness itself uses only the Python standard library. See [harness.config.json](harness.config.json) for the default configuration.

The default `workspace/` keeps task output separate from Harness source. To have workers modify an existing repository directly, set `workspace` to `.` or to that repository in the configuration. Do not allow an untrusted profile to operate on a directory containing sensitive data.

## From one request to a run

The root agent checks the current state first. When no unfinished run exists, it records the original goal, constraints, and necessary assumptions in `.harness-request.md`, then submits that file to the controller. Clear requests are not turned into a questionnaire; the agent asks one short question only when missing information would materially change the result.

Every run has isolated state, logs, review history, and a final report. Delivered files remain in the configured persistent `workspace/`, so later tasks can improve earlier work. Run records live under `runs/` and never overwrite previous tasks.

Handoffs do not depend on a specific CLI's terminal format. The worker maintains `PLAN.md` and writes `WORKER_RESULT.json` after every attempt. The reviewer writes `AUDIT.json` in an isolated review directory. The Harness archives these structured files and uses them to decide PASS, FIX, and where a resumed run should continue.

Only one active run is accepted at a time:

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
- `{workspace}`: the task workspace;
- `{run_dir}`: the current run record;
- `{role}`: `worker` or `reviewer`.

Adding or adjusting a CLI normally requires only [harness.config.json](harness.config.json). The CLI must be able to complete one non-interactive turn in a specified directory; GUI-only tools that cannot accept a prompt cannot be worker or reviewer profiles.

## Status, pause, and recovery

In the root TUI, say “status”, “stop”/“pause”, or “continue”/“resume”. Chinese equivalents work as well. The corresponding commands are:

```bash
./harness_control.py status --json
./harness_control.py stop
./harness_control.py continue
```

`stop` terminates the current child agent while preserving the workspace, phase, and review records already written to disk. It cannot guarantee an additional session summary before termination. `continue` resumes that exact run and restarts the appropriate saved profile. Never submit the old request with `start`, because that creates a duplicate task.

Plain CLI output follows `HARNESS_LANG` when set, then the system locale. Override it explicitly when needed:

```bash
HARNESS_LANG=en ./harness_control.py status
HARNESS_LANG=zh-CN ./harness_control.py status
```

For a read-only local dashboard, run `python3 status_dashboard.py` and open the address printed in the terminal. The page can switch between English and Chinese. It is optional and has no control buttons.

## Worker and reviewer rules

The worker implements only the current request in the workspace. It inspects existing content, makes the smallest complete change, and runs checks appropriate to the task. On a FIX round, the same worker profile resumes from the persisted plan, result, and full audit, resolving every blocker, major, and minor issue. Minor issues alone do not trigger another repair round.

The reviewer runs in an independent session and treats the workspace as read-only. It inspects the request, delivered files, and worker report, and performs suitable non-destructive checks. It does not force UI checks onto a script or game-specific checks onto an unrelated website. Only blocker and major findings trigger FIX; minor findings are reported without blocking PASS. The default review limit is three rounds.

“Read-only” means the Harness detects and rejects net reviewer changes to the workspace; it is not an operating-system sandbox. Use only trusted local agent profiles. Review checks that create caches or build files should redirect them to the review directory or a temporary directory and leave the workspace unchanged.

The complete role prompts are [worker.md](prompts/worker.md) and [reviewer.md](prompts/reviewer.md).

## Configuration summary

Important fields in [harness.config.json](harness.config.json):

| Field | Default | Meaning |
| --- | --- | --- |
| `workspace` | `workspace` | Persistent implementation directory across runs |
| `default_agent` | `hermes` | Fallback when the root agent cannot be detected |
| `worker_agent` | `null` | `null` inherits the root profile |
| `reviewer_agent` | `null` | `null` inherits the root profile |
| `max_reviews` | `3` | Maximum FIX/review rounds |
| `timeout_seconds` | `5400` | Timeout for one agent invocation |

The configuration does not store credentials. Each agent CLI manages its own authentication. Never place tokens, `.env` contents, or authorization headers in a request, prompt, log, or profile argv.

## Development and verification

When changing the Harness itself, work directly in the current coding-agent session. Never start a Harness run to modify the Harness.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
```

Prompt variables and structured artifact schemas are documented in [prompts/README.en.md](prompts/README.en.md).
