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

- Python 3.10+ on macOS, Linux, Windows 10/11, or a modern Windows Server;
- at least one installed and authenticated coding-agent CLI;
- a trusted local directory, because the built-in worker profiles run unattended.

Only the CLI you plan to use must be installed:

```bash
# macOS / Linux
python3 --version
hermes --version       # or codex / cursor-agent / copilot / claude
```

```powershell
# Windows PowerShell
py -3 --version   # or: python --version
hermes --version       # or codex / cursor-agent / copilot / claude
```

The Harness itself uses only the Python standard library. Command examples below use `python3` on macOS/Linux; on Windows use `py -3` when the Python launcher is installed, otherwise use `python`. Status output prints commands with the interpreter that is actually running the Harness. See [harness.config.json](harness.config.json) for the default configuration.

The default `workspace/` keeps formal task output separate from Harness source. You may set `workspace` to `.` or another existing repository, but the worker still receives a per-run candidate copy rather than that formal directory. Do not expose sensitive data to an untrusted profile.

## From one request to a run

The root agent checks the current state first. When no unfinished run exists, it records the original goal, constraints, and necessary assumptions in `.harness-request.md`, then submits that file to the controller. Clear requests are not turned into a questionnaire; the agent asks one short question only when missing information would materially change the result.

Every run has isolated state, logs, review history, and a final report. At creation time the Harness fingerprints the formal workspace and copies it to `runs/<run>/candidate/`. The worker and all repair rounds modify that persistent candidate only. Deterministic verification and review each use a fresh disposable copy of it. A failed or incomplete run leaves the formal workspace unchanged and retains the candidate for diagnosis; PASS promotes the accepted candidate with a fingerprint check, backup, rollback on error, and final content comparison.

The candidate deliberately excludes `.git` case-insensitively, Harness control paths, and run records. Git discovery is also capped at the candidate boundary so a child command cannot accidentally bind to an ancestor repository. Worker and verification commands therefore must not depend on repository metadata such as `git diff --check`; the formal workspace's `.git` remains untouched during promotion. Directory symlinks, Windows junctions, absolute symlinks, and relative symlinks that escape the workspace are rejected so omitted formal content cannot reappear through an alias after promotion. These measures protect normal operation; they are not an OS sandbox.

Handoffs do not depend on a specific CLI's terminal format. The worker maintains `PLAN.md` and writes `WORKER_RESULT.json` after every attempt. The Harness records `VERIFICATION.json` plus `verification.log`, and the reviewer writes `AUDIT.json` in an isolated review directory. The Harness archives these structured files and uses them to decide PASS, FIX, promotion, and where a resumed run should continue.

Only one active run is accepted at a time, including when callers choose different run directories:

- a duplicate submission is rejected while an unfinished run exists;
- a new request can start immediately after COMPLETE or INCOMPLETE;
- after a TUI or process interruption, resume the original run instead of resubmitting its request.

`start` normally detaches by itself and does not depend on a particular TUI's background-task syntax. On Windows, the Supervisor is created suspended and is resumed only after the Harness proves that it escaped every enclosing Job Object. A host Job that forbids complete breakaway makes background start fail closed and leaves the run PAUSED; use `continue --foreground` from a persistent TUI, or resume from an ordinary terminal that permits breakaway. A root agent with completion notifications may also run `python3 harness_control.py wait`; all other TUIs can query `status` at any time.

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

Run `python3 harness_control.py agents` to inspect detection and availability; an unusable Windows batch profile includes its preflight reason. The `coordinator_detection` field records whether the choice came from the launch environment, an ancestor process, an explicit option, or fallback. When a wrapper cannot be detected, add `--coordinator-agent <profile>` to `start`.

A profile needs a description, process-detection patterns, interactive TUI argv, and non-interactive command argv. CLIs that read stdin may also define `stdin`. On Windows, `.cmd`/`.bat` agent argv must be completely static: every dynamic prompt, path, and role value must travel through stdin, or the profile must use a native executable. Python's argv quoting does not safely preserve arbitrary values through `cmd.exe`; `{prompt_file}` is a path placeholder and is therefore not an exception. Profiles that require prompt argv need a native Windows launcher. Command and stdin templates support:

- `{prompt}`: the rendered prompt for this attempt;
- `{prompt_file}`: the saved prompt path;
- `{workspace}`: the worker candidate or disposable reviewer workspace for that role;
- `{run_dir}`: the current run record;
- `{role}`: `TASK_WORKER` or `TASK_REVIEWER`.

Adding or adjusting a CLI normally requires only [harness.config.json](harness.config.json). The CLI must be able to complete one non-interactive turn in a specified directory; GUI-only tools that cannot accept a prompt cannot be worker or reviewer profiles.

## Deterministic acceptance

`verification_commands` is mandatory and must contain at least one command. Each command is an argv array, not a shell string. Native executables therefore receive the configured argv without shell expansion. Windows necessarily runs `.cmd`/`.bat` files through its command processor, so the Harness rejects batch executable paths or arguments containing `cmd.exe` metacharacters. The Harness snapshots these commands into the run, executes them in order from a disposable copy of the current candidate, and requires exit status 0 from every command before promotion. Each command has the configured `verification_timeout_seconds` limit.

The shipped baseline is:

```json
"verification_commands": [["{python}", "-m", "compileall", "-q", "."]]
```

The exact `{python}` argv token is resolved to the current `sys.executable` when a run is created, so the saved command is deterministic without assuming a platform-specific executable name. This catches Python syntax errors and remains harmless for non-Python files, but it is only a baseline. Replace or extend it with deterministic checks that actually define acceptance for your workspace, for example:

```json
"verification_commands": [
  ["{python}", "-m", "unittest", "discover", "-v"],
  ["npm", "test", "--", "--runInBand"]
]
```

Commands may create caches or build output because their workspace is discarded afterward. An executable with an explicit relative path, such as `./scripts/check` on POSIX or `.\check.cmd` on Windows, is resolved from that disposable workspace, not from the Harness source directory. A bare executable such as `npm` is always resolved through `PATH` on every platform; candidate files cannot shadow it. A failure is persisted under `iterations/NN/`, is injected into the review as a major issue, and forces FIX even if the reviewer returned PASS. The reviewer cannot waive this gate.

## Status, pause, and recovery

In the root TUI, say “status”, “stop”/“pause”, or “continue”/“resume”. Chinese equivalents work as well. The corresponding commands are:

```bash
python3 harness_control.py status --json
python3 harness_control.py stop
python3 harness_control.py continue
```

```powershell
py -3 harness_control.py status --json   # use `python` instead when `py` is unavailable
py -3 harness_control.py stop
py -3 harness_control.py continue
```

`stop` terminates the current managed child tree while preserving the candidate workspace, phase, and review records already written to disk. Its authoritative pause request lives in a Harness-owned runtime control directory outside the child-authorized run record, so a Worker cannot cancel it by deleting or replacing the visible compatibility marker. A crashed Windows Supervisor closes its Job handle, so managed descendants are terminated by the OS; POSIX recovery terminates the saved process group. For safety, Windows refuses PID-only termination of an unmanaged or legacy orphan and preserves its identity for manual diagnosis. `continue` refuses to create a duplicate while a recorded child is alive, then resumes the exact saved run after it is stopped. It cannot guarantee an additional session summary before termination. Never submit the old request with `start`, because that creates a duplicate task.

Run format v2 introduced candidate isolation and promotion. Unfinished v1 runs created by an earlier version are intentionally rejected rather than resumed without those guarantees; finish or archive them with the earlier version before upgrading.

Plain CLI output follows `HARNESS_LANG` when set, then the system locale. Override it explicitly when needed:

```bash
HARNESS_LANG=en python3 harness_control.py status
HARNESS_LANG=zh-CN python3 harness_control.py status
```

```powershell
$env:HARNESS_LANG = "en"; py -3 harness_control.py status
$env:HARNESS_LANG = "zh-CN"; py -3 harness_control.py status
```

For a read-only local dashboard, run `python3 status_dashboard.py` on macOS/Linux or `py -3 status_dashboard.py`/`python status_dashboard.py` on Windows, then open the printed address. IPv4 loopback is supported everywhere; IPv6 loopback is supported when the host enables IPv6. The page can switch between English and Chinese, has no control buttons, and refuses non-loopback bindings. Use an SSH tunnel for remote viewing.

## Platform parity and Windows notes

The same run states, candidate isolation, verification gate, review loop, pause/resume behavior, promotion, rollback, and dashboard are used on all three operating-system families. POSIX hosts use `flock`, process sessions, creation-token checks, and process-group signals. Windows uses a one-byte `msvcrt` lock and WinAPI PID creation-time checks; every Worker, Reviewer, and verifier is created suspended, assigned to a kill-on-close Job Object, and only then allowed to execute. This contains descendants created through normal child-process inheritance and cleans them after normal completion, pause, timeout, or Supervisor failure without a PID-only `taskkill` race. It is not an OS sandbox, and a trusted profile must not use an external process broker that deliberately creates work outside the Job. CLI, redirected logs, Agent pipes, JSON, and Markdown use UTF-8 independently of the host locale. Run-relative evidence paths always use `/`, while absolute workspace paths keep the host's native format. CI executes the full suite on Ubuntu, macOS, and Windows with Python 3.10 and 3.13.

Windows-specific boundaries are explicit: formal workspace roots and internal paths are checked lexically before resolution, NTFS junctions/reparse directories are rejected again on resume and promotion, and a drive or UNC share root cannot be selected. Read-only attributes are changed only after an operation actually fails for a read-only destination; a failed retry restores the original attribute. A short-lived sharing violation from a concurrent Dashboard/status reader is retried for a bounded interval without changing permissions. A persistent exclusive share lock makes promotion pause and attempt exact rollback; unchanged locked entries need no replacement, while any rollback also blocked by the lock leaves the candidate and backup intact for diagnosis. After the lock is released, `continue` resumes the same promotion. Creating ordinary symlinks may require Developer Mode or administrator rights. Very long prompts should use stdin or `{prompt_file}` with a native executable; batch-wrapper argv containing any runtime placeholder is rejected before a run is created. Windows file privacy follows the ACL inherited from the project directory rather than POSIX `0600`/`0700` mode bits; see [SECURITY.md](SECURITY.md).

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
| `verification_commands` | `{python} -m compileall -q .` | Mandatory ordered argv commands; all must exit 0; `{python}` snapshots the current interpreter |
| `verification_timeout_seconds` | `600` | Timeout for each deterministic command |

The configuration does not store credentials. Each agent CLI manages its own authentication. Never place tokens, `.env` contents, or authorization headers in a request, prompt, log, or profile argv. Run records and raw CLI output are private local evidence files and may still contain task content.

## Development and verification

When changing the Harness itself, work directly in the current coding-agent session. Never start a Harness run to modify the Harness.

```bash
python3 -B -m unittest -v
```

```powershell
py -3 -B -m unittest -v
```

Prompt variables and structured artifact schemas are documented in [prompts/README.md](prompts/README.md).

## License

Released under the [MIT License](LICENSE). Security reports should follow [SECURITY.md](SECURITY.md).
