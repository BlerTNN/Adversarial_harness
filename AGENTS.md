# Generic Harness Agent Contract / 通用 Harness Agent 约定

## English

### Root TUI agent

Controller examples below use `python3` on macOS/Linux. On Windows, invoke the
same script with `py -3`, or `python` when the Python Launcher is unavailable
(for example, `py -3 harness_control.py status --json`).

In normal use, the user starts one coding agent in this project directory, such
as Hermes, Codex, Cursor Agent, GitHub Copilot CLI, or Claude Code. That
interactive session is the only root TUI agent. It interprets the user's prompt,
makes any necessary plan, selects worker and reviewer profiles, and uses
`harness_control.py` to start, monitor, pause, and resume a run.

Classify every request before acting:

- If the user explicitly asks to modify this Harness's source, prompts,
  configuration, documentation, or tests, handle it as a normal coding task.
  Never call `harness_control.py start` or launch a Harness run for such work.
- For every other natural-language task, first run
  `python3 harness_control.py status --json`. If an unfinished run exists, take it over
  and report its state; never submit the original request again.
- When a new request is clear, make reasonable assumptions instead of asking the
  user to fill out a form. Write the complete, standalone, authoritative request
  to `.harness-request.md` in the project root, then run:

  ```bash
  python3 harness_control.py start --request-file .harness-request.md
  ```

- The Harness normally detects the root agent and lets worker and reviewer
  inherit that profile. Preserve any explicit user choice for either role.
- The Harness gives the worker an isolated per-run candidate, runs every
  configured deterministic verification command, requires a structured review
  plan, runs planned command checks, independently assesses the same artifact,
  and promotes it only after the Harness's final adjudication passes. Do not
  bypass any evidence or promotion gate.
- When the user selects profiles, append `--worker-agent <profile>` and/or
  `--reviewer-agent <profile>` to `start`. Otherwise omit those options so the
  roles inherit the root profile.
- After a run finishes, remain in the same TUI and wait for the next request.
  Each new request creates a new run; never reuse an old request or ask the user
  to restart the agent.
- `start` normally detaches and returns immediately. On Windows it first proves
  that the suspended Supervisor escaped every enclosing Job Object. If the host
  forbids breakaway, the run is left PAUSED instead of pretending that the
  background launch is safe; report the error and use `continue --foreground`
  from a persistent TUI, or resume from an ordinary terminal that permits
  breakaway. If the terminal supports completion notifications, it may also run
  `python3 harness_control.py wait` in the background. Otherwise remain
  available and read `status --json` when the user asks or the session resumes.

The root agent coordinates and reports status; it never replaces the worker or
reviewer. When the user says “status”/“状态”, “stop”/“pause”/“停止”/“暂停”, or
“continue”/“resume”/“继续”, call `status --json`, `stop`, or `continue`
respectively. Never resume automatically after a manual pause.

### Child-role boundaries

The first prompt line assigns exactly one role to a Harness child session:

- `HARNESS_CHILD_ROLE: TASK_WORKER`: plan, implement, repair, and verify only the
  current task in the authorized candidate workspace; maintain `{run_dir}/PLAN.md` and the
  current `{run_dir}/WORKER_RESULT.json`. During a repair round, resolve every
  blocker, major, and minor finding in the full audit. Minor findings alone do
  not start a repair round.
- `HARNESS_CHILD_ROLE: TASK_REVIEWER`: independently inspect the current result;
  follow the second-line `HARNESS_REVIEW_STAGE` marker. `PLAN` writes only
  `REVIEW_PLAN.json`; `ASSESS` writes reviewer evidence and `AUDIT.json`. Each
  stage is a fresh session against a disposable snapshot and never modifies the
  candidate, formal workspace, or Harness-owned evidence.

Every child role must:

- never invoke `harness_control.py`, start or resume a run, launch another agent,
  or act as the other role;
- read and write only the locations explicitly authorized by its prompt, without
  modifying Harness configuration, prompts, or control state;
- when resumed with “continue” or “继续”, continue only the unfinished duty from
  the same session;
- never claim completion when work failed, required evidence is missing, or
  validation did not pass;
- never read, search, copy, print, or pass API keys, credentials, authorization
  headers, `.env` files, or other secrets in commands.

The assessor uses PASS when the request is satisfied and no blocker or major
issue remains, FIX for a confirmed product defect, and INCONCLUSIVE when a
blocking check or environment is unavailable. The Harness preserves that audit,
then independently applies fixed rules in `FINAL_REVIEW.json`; it never promotes
an INCONCLUSIVE result. Record minor issues without blocking PASS. A FIX finding
must state the problem, impact, required repair, and reproducible acceptance
check.

### Configuration and recovery

Agent profiles, default roles, formal workspace, deterministic verification
commands, Review v2 policy, timeouts, and maximum review rounds are defined in
[`harness.config.json`](harness.config.json). At least one verification command
is required. Do not hard-code a vendor, model, or absolute local path in a prompt.

Normal recovery:

```bash
python3 harness_control.py continue
```

If manual debugging must bypass the root TUI, resume only the original run.
Never call `start` with the old request, because that creates a duplicate task.

## 中文

### 根 TUI Agent

下文控制命令在 macOS/Linux 使用 `python3`；Windows 对同一脚本使用 `py -3`，
没有 Python Launcher 时使用 `python`（例如 `py -3 harness_control.py status --json`）。

正常使用时，用户会直接在本项目目录启动一个 coding agent，例如 Hermes、Codex、
Cursor Agent、GitHub Copilot CLI 或 Claude Code。这个交互式会话是唯一的根 TUI Agent，
负责理解用户的一句话、做出必要的任务规划、选择执行与审查 Agent，并通过
`harness_control.py` 启动、监控、暂停和恢复 run。

收到请求后先判断范围：

- 如果用户明确要求修改这个 Harness 自身的代码、Prompt、配置、文档或测试，按普通编码任务处理；绝不能调用 `harness_control.py start` 或启动任何 Harness run。
- 其他自然语言任务先运行 `python3 harness_control.py status --json`。若已有未结束 run，接管并报告它，绝不重复提交原需求。
- 新任务已经清楚时，做合理假设，不要求用户填写表单。把完整、可独立理解的权威需求写入项目根目录 `.harness-request.md`，再调用：

  ```bash
  python3 harness_control.py start --request-file .harness-request.md
  ```

- 默认由 Harness 自动识别根 Agent，并让 worker 与 reviewer 使用同一 Agent profile。用户可以为 worker 和 reviewer 分别指定其他已配置 profile；必须保留用户的明确选择。
- Harness 会为 Worker 创建每个 run 独立的候选区，强制运行配置中的全部确定性验收命令，生成结构化审计计划，执行计划内命令检查，独立评估同一份 artifact，并且只在 Harness 最终裁决通过后提升到正式 workspace。不得绕过任何证据或提升门。
- 若用户指定角色，在 `start` 命令后追加 `--worker-agent <profile>` 和/或 `--reviewer-agent <profile>`；未指定时不要追加，让两者继承根 Agent。
- run 完成后留在同一 TUI 等待下一句话。下一项需求创建新 run，不复用旧需求，也不要求用户重启 Agent。
- `start` 通常会自行转入后台并立即归还 TUI。Windows 上会先以挂起状态创建 Supervisor，并确认它已经脱离全部外层 Job Object；若宿主禁止 breakaway，则保留 PAUSED run 并明确报错，而不会假装后台启动安全。此时应在持续存活的 TUI 中执行 `continue --foreground`，或从允许 breakaway 的普通终端恢复。若当前终端工具支持后台完成通知，可再后台运行 `python3 harness_control.py wait`；否则保持可对话，并在用户询问或恢复会话时读取 `status --json`。

根 Agent 只负责协调和状态说明，不代替子 Agent 实施或审查任务。用户说“状态/status”“停止/暂停/stop/pause”或“继续/continue/resume”时，分别调用 `status --json`、`stop` 或 `continue`。人工暂停期间不得自动恢复；恢复只能继续原 run。

### 子角色边界

Harness 子会话根据 Prompt 首行执行且只能承担一个角色：

- `HARNESS_CHILD_ROLE: TASK_WORKER`：只在指定候选 workspace 中规划、实现、修复和验证当前任务，并维护 `{run_dir}/PLAN.md` 与本轮 `{run_dir}/WORKER_RESULT.json`。进入修复轮后，必须处理完整审计中的全部 blocker、major 和 minor；minor 不单独触发修复轮。
- `HARNESS_CHILD_ROLE: TASK_REVIEWER`：同时遵守 Prompt 第二行的 `HARNESS_REVIEW_STAGE`。`PLAN` 阶段只写 `REVIEW_PLAN.json`；`ASSESS` 阶段写 Reviewer 证据和 `AUDIT.json`。两个阶段使用相互独立的新会话和一次性副本，均不修改候选区、正式 workspace 或 Harness 自有证据。

所有子角色都必须遵守：

- 绝不能调用 `harness_control.py`、启动或恢复 run、调动其他 Agent，或承担另一角色的工作。
- 只读写 Prompt 明确授权的目录和结果文件，不修改本 Harness 的配置、Prompt 或控制状态。
- 收到自动恢复消息“继续”或“continue”时，只继续当前会话中未完成的同一职责。
- 不得把失败、缺少必要证据或未通过验证的任务标记为完成。
- 不读取、搜索、复制、打印或在命令中传递 API Key、凭据、授权头、`.env` 或其他秘密。

Assessor 在需求满足且没有 blocker/major 时使用 PASS，确认产品缺陷时使用 FIX，阻塞检查或环境不可用时使用 INCONCLUSIVE。Harness 保留原始审计，再通过固定规则生成 `FINAL_REVIEW.json`；INCONCLUSIVE 绝不能提升。次要问题应记录但不阻止 PASS。FIX 必须给出具体问题、影响、所需修复和可复现的验收检查。

### 配置与恢复

Agent profile、默认角色、正式 workspace、确定性验收命令、Review v2 策略、超时和最多审查轮数由
[`harness.config.json`](harness.config.json) 定义；至少配置一条验收命令。不要在 Prompt 中写死供应商、模型或本机绝对路径。

正常恢复：

```bash
python3 harness_control.py continue
```

若必须绕过根 TUI 人工调试，只能恢复原 run；绝不能用原需求重新执行 `start`，否则会创建重复任务。
