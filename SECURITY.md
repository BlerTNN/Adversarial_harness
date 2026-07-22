# Security policy

## Trust model

The Harness is a local coordination and verification layer for coding-agent
CLIs. Built-in profiles run unattended with the permissions of the current OS
user. They are suitable only for trusted agents, trusted requests, and a
workspace that contains no unrelated sensitive data.

The reviewer receives a disposable copy of the delivered workspace. This
prevents normal review commands, caches, and build output from changing the
live delivery. The Harness also verifies the live workspace content hash and
protects its control files across every child invocation. These checks provide
integrity evidence; they are not an operating-system security boundary. A
malicious process with the user's permissions can still access other local
paths or the network.

The status dashboard is deliberately limited to loopback addresses. Use an SSH
tunnel to view it remotely. Run records and raw agent logs may contain task
content or agent output and are stored with owner-only file permissions. Never
put credentials, `.env` contents, tokens, or authorization headers in a task,
prompt, profile command, or workspace exposed to an agent.

## Supported environments

The current process-control implementation supports macOS and Linux. Windows
is not a supported host.

## Reporting a vulnerability

Please report security issues privately through GitHub Security Advisories for
this repository. Include affected versions or commit IDs, reproduction steps,
impact, and any suggested mitigation. Do not open a public issue before a fix
is available.
