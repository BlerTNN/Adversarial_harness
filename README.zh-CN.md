# 一句话多 Agent Harness

[English](README.md) | [中文](README.zh-CN.md)

在这个项目目录里直接启动你习惯的 coding agent，然后说一句要做什么。根 Agent 会规划任务，独立 Worker 只在隔离候选区实施，Harness 强制运行确定性验收命令，再由独立 Reviewer 审查同一份候选产物及其验收证据；只有全部通过的候选结果才会提升到正式工作区。不需要先进入命令菜单、填写任务文件或逐步确认。

例如：

> 给我做一个好看的电商网站，支持手机端，商品和购物车流程要完整。

一次任务完成后继续说下一句话即可。workspace 会保留，新的任务会创建新的 run，根 TUI 不需要重启。

## 直接开始

进入项目目录，启动任意一个已安装并登录的 CLI：

```bash
hermes chat --tui
codex
cursor-agent
copilot
claude
```

只运行其中一个。它就是当前根 TUI Agent，并应遵循双语的 [AGENTS.md](AGENTS.md)。如果某个 CLI 不会自动读取项目说明，可以先让它读取该文件。

随后直接用中文或英文说需求。需求文件、Prompt、状态和结构化交接产物都使用 UTF-8；Worker 和 Reviewer 会按权威需求的语言编写面向用户的内容。典型流程是：

```text
用户的一句话
  → 根 TUI Agent：理解、规划、调度和汇报
  → Worker：在隔离候选 workspace 中实施
  → Harness：运行配置的确定性验收命令
  → Reviewer：独立检查同一候选结果，返回 PASS 或 FIX
      ├─ FIX：原 Worker 修复，再次审查
      └─ PASS：提升候选结果、保存报告并等待
```

默认 worker 和 reviewer 都使用与根 TUI 相同的 Agent profile，但它们是彼此独立的会话。例如，从 Hermes 启动时默认是 Hermes 协调、Hermes 实施、另一个 Hermes 审查。也可以在需求中明确指定“Cursor 做，Copilot 审查”。

## 第一次使用

需要：

- macOS 或 Linux 上的 Python 3.10+；
- 至少一个已安装、已登录的 coding-agent CLI；
- 一个你信任的本地目录，因为内置 worker profile 采用无人值守权限完成任务。

确认目标 CLI 可用即可，不需要安装全部 Agent：

```bash
python3 --version
hermes --version       # 或 codex / cursor-agent / copilot / claude
```

项目本身只使用 Python 标准库。默认配置见 [harness.config.json](harness.config.json)。

默认 `workspace/` 是持久的正式交付目录，与 Harness 源码分开。也可以把配置中的 `workspace` 改成 `.` 或已有仓库，但 Worker 收到的仍是每个 run 独立的候选副本，不会把正式目录作为工作目录。不要向不受信任的 profile 暴露敏感数据。

## 一句话如何变成任务

根 Agent 会先读取当前状态。没有未结束任务时，它会把你的原始目标、约束和必要假设整理到 `.harness-request.md`，再交给控制器。明确的需求不会被追问成一张表；只有缺失信息会实质改变结果时才需要一个简短问题。

每个 run 都有独立状态、日志、审查历史和最终报告。创建 run 时，Harness 会先计算正式 workspace 的内容指纹，再复制到 `runs/<run>/candidate/`。Worker 与后续修复轮只修改这份持久候选结果；确定性验收和 Reviewer 各自再使用一次性副本。失败或 INCOMPLETE 不会改动正式 workspace，并保留候选区供诊断；PASS 时才在再次核对指纹后提升候选结果，同时制作备份、失败自动回滚并比较最终内容。

候选副本不会复制 `.git`、Harness 控制路径或 run 记录；Git 向上发现也会停在候选区边界，避免子进程误绑定上层正式仓库。目录软链接、绝对软链接和指向 workspace 外部的相对软链接都会被拒绝，避免被省略的正式内容在提升后通过别名重新出现。这些机制保护正常操作，但不等同于操作系统安全沙箱。

因此 Worker 与验收命令不能依赖 `git diff --check` 等 Git 元数据；提升候选结果时，正式 workspace 中的 `.git` 会原样保留。

跨 CLI 的交接不依赖终端输出格式。Worker 持续更新 run 中的 `PLAN.md`，并在每轮结束写 `WORKER_RESULT.json`；Harness 保存 `VERIFICATION.json` 与 `verification.log`；Reviewer 在独立 review 目录写 `AUDIT.json`。Harness 会以这些结构化证据决定 PASS、FIX、提升与恢复位置。

Harness 同一时间只认领一个活动 run，即使调用方选择了不同的 runs 目录：

- 活动任务存在时，重复提交会被拒绝；
- COMPLETE 或 INCOMPLETE 后可以立即提交下一句话；
- TUI 或进程意外退出时，恢复原 run，不会重新执行旧需求。

`start` 自身会 detach，因此不依赖某一家 TUI 的后台工具语义。支持后台完成通知的根 Agent 可以另外运行 `./harness_control.py wait`；其他 TUI 仍可随时用 `status` 查询，任务不会因为对话继续而停止。

## Agent 选择

内置 profile：

| Profile | 交互式入口 | 说明 |
| --- | --- | --- |
| `hermes` | `hermes chat --tui` | 默认 fallback；不固定 provider 或 model |
| `codex` | `codex` | 非交互调用从 stdin 接收 Prompt |
| `cursor` | `cursor-agent` | Cursor Agent CLI |
| `copilot` | `copilot` | GitHub Copilot CLI |
| `claude` | `claude` | Claude Code |

Harness 优先识别当前根 TUI Agent；无法识别时才使用 `default_agent`。`worker_agent` 和 `reviewer_agent` 为 `null` 时继承根 Agent，设置为 profile 名则分别覆盖。选择会写入 run 配置，恢复时继续使用原选择。

可先运行 `./harness_control.py agents` 查看识别结果。状态中的 `coordinator_detection` 会明确记录它来自启动环境、祖先进程、显式参数还是 fallback；包装器无法识别时可在 `start` 追加 `--coordinator-agent <profile>`。

一个 profile 只需要四项：描述、进程检测特征、TUI argv 和非交互 command argv；需要 stdin 的 CLI 可以额外提供 `stdin`。command 和 stdin 可使用：

- `{prompt}`：本轮完整 Prompt；
- `{prompt_file}`：保存 Prompt 的文件；
- `{workspace}`：当前角色使用的 Worker 候选区或 Reviewer 一次性副本；
- `{run_dir}`：当前 run 目录；
- `{role}`：`worker` 或 `reviewer`。

新增或调整 CLI 通常只需修改 [harness.config.json](harness.config.json)，不需要复制一套 Harness。CLI 必须支持在指定目录非交互完成一轮工作；只有 GUI、无法接收 Prompt 的工具不能直接作为 worker/reviewer profile。

## 确定性验收

`verification_commands` 是必填项，至少包含一条命令。每条命令都是 argv 数组，而不是 shell 字符串，因此引号或 shell 展开不会悄悄改变命令含义。Harness 会把命令快照进 run 配置，按顺序在当前候选结果的一次性副本中执行；每条命令都必须以状态码 0 结束，单条命令受 `verification_timeout_seconds` 限制。

项目提供的基础默认值是：

```json
"verification_commands": [["python3", "-m", "compileall", "-q", "."]]
```

它能发现 Python 语法错误，对非 Python 文件没有副作用，但只是一条基础烟雾检查。应根据正式 workspace 替换或追加真正定义验收结果的命令，例如：

```json
"verification_commands": [
  ["python3", "-m", "unittest", "discover", "-v"],
  ["npm", "test", "--", "--runInBand"]
]
```

这些命令可以生成缓存或构建文件，因为执行副本随后会被丢弃。失败证据会保存到 `iterations/NN/`，并作为 major 问题注入审查；即使 Reviewer 返回 PASS，Harness 也会强制 FIX，Reviewer 无权跳过该门禁。

## 暂停、恢复与状态

在根 TUI 中直接说“状态/status”“停止/stop”或“继续/continue”即可。对应的只读/控制命令是：

```bash
./harness_control.py status --json
./harness_control.py stop
./harness_control.py continue
```

`stop` 会终止当前子 Agent，并保留已经写入磁盘的候选 workspace、阶段和审查记录；Supervisor 崩溃后遗留的孤儿子进程也会被终止。旧子进程仍存活时，`continue` 会拒绝重复启动，停止后才恢复同一个 run。它不能保证 Agent 在被终止前额外输出一份会话总结。不要用旧需求再次执行 `start`，否则会产生重复任务。

候选隔离与提升机制把 run 格式升级为 v2。旧版本创建但未完成的 v1 run 会被新版明确拒绝恢复，避免在缺少新保障的情况下继续；升级前应使用旧版本完成或归档这些 run。

如需本机浏览器中的只读状态页，可运行 `python3 status_dashboard.py`，再打开终端显示的地址。页面可在中文和 English 之间切换；它不是完成任务所必需的，也不提供修改或控制按钮。Dashboard 会拒绝非本机监听地址；远程查看请使用 SSH 隧道。

CLI 文本会根据 `HARNESS_LANG` 或系统 locale 选择中文或英文。需要显式覆盖时：

```bash
HARNESS_LANG=zh-CN ./harness_control.py status
HARNESS_LANG=en ./harness_control.py status
```

## Worker 与 Reviewer

Worker 只在当前 run 的隔离候选 workspace 中实施需求，正式 workspace 不是它的工作目录。它先检查已有候选内容，再做最小完整改动并运行与任务匹配的验证。收到 FIX 后继续使用同一 worker profile，并从持久候选区、计划、结果和完整审计文件恢复上下文；该修复轮会同时处理审计中的全部 blocker、major 和 minor，并逐项验证可执行的验收检查。minor 单独存在时仍不会触发修复轮。

Reviewer 使用独立会话，在已经固定命令检查、并绑定验收报告的候选结果临时副本中检查需求、产物和 Worker 报告；即使固定命令失败，Reviewer 仍会运行并汇总问题，随后由 Harness 强制 FIX。审查产生的缓存、构建文件或误修改不会影响候选区或正式 workspace。Harness 会把确定性验收与审查证据绑定到同一个 SHA-256 artifact ID，再确认正式 workspace 仍与 run 启动时一致；两道门都通过后才提升。检查失败或 blocker/major 问题都会阻止 PASS；Reviewer 明确给出的 FIX 不会被 Harness 改成 PASS。默认最多审查三轮。

临时副本和完整性检查仍不是操作系统级安全沙箱。内置 profile 使用当前用户权限运行，仍可能访问其他本机路径和网络。只使用可信的本地 Agent 与可信需求；完整边界见 [SECURITY.md](SECURITY.md)。

两个角色的完整约束分别在 [worker.md](prompts/worker.md) 和 [reviewer.md](prompts/reviewer.md)。

## 配置摘要

[harness.config.json](harness.config.json) 的主要字段：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `workspace` | `workspace` | 跨 run 保留的正式交付目录 |
| `default_agent` | `hermes` | 无法识别根 Agent 时的 fallback |
| `worker_agent` | `null` | `null` 表示继承根 Agent |
| `reviewer_agent` | `null` | `null` 表示继承根 Agent |
| `max_reviews` | `3` | FIX/复审上限 |
| `timeout_seconds` | `5400` | 单次 Agent 调用超时 |
| `verification_commands` | `python3 -m compileall -q .` | 必须执行的有序 argv 命令，全部要求状态码 0 |
| `verification_timeout_seconds` | `600` | 每条确定性命令的超时 |

配置不存放密钥。凭据仍由各 Agent CLI 自己管理；不要把 token、`.env` 内容或授权头写进需求、Prompt、日志或 profile argv。Run 记录和 CLI 原始输出是本机私有证据文件，但仍可能包含任务内容。

## 开发与验证

修改 Harness 本身时应在当前 coding-agent 会话中直接完成，绝不能为了修改 Harness 而启动 Harness。基础检查：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
```

Prompt 变量说明见 [prompts/README.zh-CN.md](prompts/README.zh-CN.md)。

## 许可证

本项目使用 [MIT License](LICENSE)。安全问题请按 [SECURITY.md](SECURITY.md) 报告。
