# Security policy

## Trust model

The Harness is a local coordination and verification layer for coding-agent
CLIs. Built-in profiles run unattended with the permissions of the current OS
user. They are suitable only for trusted agents, trusted requests, and a
workspace that contains no unrelated sensitive data.

The worker receives a persistent per-run candidate copy, while deterministic
verification and the reviewer each receive a disposable copy of that candidate.
The formal workspace is promoted only after both gates pass. The Harness checks
content hashes across each boundary, protects control evidence across child
invocations, caps Git discovery at the isolated workspace, and rejects
directory, absolute, or escaping symlinks. These checks prevent normal child
commands, caches, and build output from changing the formal delivery, but they
are not an operating-system security boundary. A malicious process with the user's
permissions can still access other local paths or the network.

The status dashboard is deliberately limited to loopback addresses. Use an SSH
tunnel to view it remotely. Run records and raw agent logs may contain task
content or agent output and are stored with owner-only file permissions. Never
put credentials, `.env` contents, tokens, or authorization headers in a task,
prompt, profile command, or workspace exposed to an agent.

Deterministic verification commands also run with the current user's
permissions and inherited environment. They receive no interactive stdin, but
they can still access local files and the network. Configure only trusted,
non-interactive commands and never place secrets directly in their argv.

## Supported environments

The current process-control implementation supports macOS and Linux. Windows
is not a supported host.

## Reporting a vulnerability

Please report security issues privately through GitHub Security Advisories for
this repository. Include affected versions or commit IDs, reproduction steps,
impact, and any suggested mitigation. Do not open a public issue before a fix
is available.
