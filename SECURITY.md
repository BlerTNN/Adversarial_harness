# Security policy

## Trust model

The Harness is a local coordination and verification layer for coding-agent
CLIs. Built-in profiles run unattended with the permissions of the current OS
user. They are suitable only for trusted agents, trusted requests, and a
workspace that contains no unrelated sensitive data.

The worker receives a persistent per-run candidate copy. Deterministic
verification, every planned command check, the review planner, and the assessor
receive disposable copies of the same artifact. The formal workspace is
promoted only after the Harness revalidates all evidence and independently
recalculates a passing final verdict. The Harness checks content hashes across
each boundary, protects control evidence across child invocations, caps Git
discovery at the isolated workspace, and rejects directory, absolute, or
escaping symlinks. These checks prevent normal child commands, caches, and build
output from changing the formal delivery, but they are not an operating-system
security boundary. A malicious process with the user's permissions can still
access other local paths or the network.

The status dashboard is deliberately limited to IPv4 or IPv6 loopback
addresses. Use an SSH tunnel to view it remotely. Run records and raw agent logs
may contain task content or agent output. On POSIX they are written with
owner-only modes; on Windows they inherit the ACL of the project and run
directories because Windows `chmod` does not provide POSIX ACL semantics. Keep
the repository in a private user directory. Never put credentials, `.env`
contents, tokens, or authorization headers in a task, prompt, profile command,
or workspace exposed to an agent.

Agent CLI processes retain the current login environment because provider
authentication may depend on it. Deterministic verification and planned review
commands receive no interactive stdin and use an allowlisted environment:
`PATH`, required OS runtime variables, locale, UTF-8 settings, and fresh
HOME/TEMP directories. API keys and arbitrary inherited variables are omitted.
The commands still run with the current user's permissions and can read files
available to that account, including sensitive content deliberately placed in
the candidate, or access the network. Configure only trusted, non-interactive
commands and never place secrets in their argv, repository, request, or output.

Review v2 command stdout and stderr are drained separately, saved only up to the
configured byte limit, and bound to the plan and artifact with SHA-256 digests.
Structured handoffs, result files, logs, and cited manual evidence must be
ordinary single-link files; symlinks, junction/reparse parents, hard links,
escaping paths, oversized files, and identity mismatches are rejected. This is
integrity checking under the current user account, not protection from hostile
code already running as that same user.

## Supported environments

The Harness supports Python 3.10+ on macOS, Linux, Windows 10/11, and modern
Windows Server releases. CI runs the same suite on Ubuntu, macOS, and Windows.

POSIX uses `flock`, process sessions, and process-group signals. Windows uses a
one-byte `msvcrt` lock and WinAPI liveness plus process-creation tokens. Each
managed Agent, verifier, and planned command check is created suspended, assigned to a non-inheritable
kill-on-close Job Object, and resumed only after assignment succeeds. Explicit
Job termination cleans normally inherited descendants after every attempt, and
closing the last handle provides crash cleanup. An external process broker can
create work outside that Job, so this remains containment for trusted profiles,
not an OS sandbox. The controller does not use PID-only `taskkill`
or `os.kill(pid, 0)` on Windows; an unmanaged legacy orphan is preserved for
manual diagnosis instead of risking termination after PID reuse.

The Windows controller creates a background Supervisor suspended, requests Job
breakaway only when the immediate host Job permits it, and verifies the child
is outside every Job before resuming it. A restricted or nested host Job that
prevents complete breakaway is reported as an error and leaves the run PAUSED;
use `continue --foreground` from a persistent TUI or resume from a normal
terminal. The Harness never silently claims that `DETACHED_PROCESS` escaped a
Job Object.

Authoritative Supervisor identity and pause-request files live in the
Harness-owned `.harness-runtime` directory inside each runs directory, beside
the run records and outside paths authorized to Worker and Reviewer roles.
Run-local compatibility markers are never trusted for stopping, so a child
deleting or replacing one cannot cancel an accepted pause request. These files
are protected by the same user-account boundary as the rest of the Harness;
they do not create a privilege boundary against hostile code running as that
user.

Workspace boundaries reject filesystem roots, formal-root aliases, directory
symlinks, escaping or absolute symlinks, and Windows junctions/reparse
directories. Stored formal roots are checked lexically again before manifest,
resume, rollback, and promotion. `.git` is excluded case-insensitively. A
Windows read-only attribute is cleared only after the first operation fails and
the destination is confirmed read-only; a failed retry restores the original
attribute. Transient sharing violations from ordinary concurrent readers
receive a short bounded retry without any permission change. An unrelated
program holding a persistent exclusive Windows share lock can still block
replacement. Promotion then attempts exact rollback; unchanged locked entries
are skipped, and if the same lock also blocks rollback, the candidate and backup
remain for diagnosis and recovery rather than being reported as accepted.

Creating ordinary symlinks on Windows can require Developer Mode or elevated
rights. The artifact identity covers entry paths, regular-file content, link
targets, and the mode information exposed by Python; it does not preserve or
compare NTFS ACLs, alternate data streams, hard-link topology, or every
platform-specific extended attribute. Very long prompts must use stdin or a
prompt file on Windows because the OS command-line limit is smaller. A Windows
batch agent must use completely static argv; prompt, path, and role values must
go through stdin, or the profile must use a native executable. Batch executable
paths and static argv containing `cmd.exe` metacharacters are also rejected.

## Reporting a vulnerability

Please report security issues privately through GitHub Security Advisories for
this repository. Include affected versions or commit IDs, reproduction steps,
impact, and any suggested mitigation. Do not open a public issue before a fix
is available.
