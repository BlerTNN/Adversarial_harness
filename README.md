# 一句话多 Agent Harness

[中文](README.md) | [English](README.en.md)

在这个项目目录里直接启动你习惯的 coding agent，然后说一句要做什么。根 Agent 会规划任务，调用一个独立 worker 完成工作，再调用另一个独立 reviewer 审查；不需要先进入命令菜单、填写任务文件或逐步确认。

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
  → Worker：在持久 workspace 中实施并验证
  → Reviewer：独立检查，返回 PASS 或 FIX
      ├─ FIX：原 Worker 修复，再次审查
      └─ PASS：保存报告，等待下一句话
```

默认 worker 和 reviewer 都使用与根 TUI 相同的 Agent profile，但它们是彼此独立的会话。例如，从 Hermes 启动时默认是 Hermes 协调、Hermes 实施、另一个 Hermes 审查。也可以在需求中明确指定“Cursor 做，Copilot 审查”。

## 第一次使用

需要：

- Python 3；
- 至少一个已安装、已登录的 coding-agent CLI；
- 一个你信任的本地目录，因为内置 worker profile 采用无人值守权限完成任务。

确认目标 CLI 可用即可，不需要安装全部 Agent：

```bash
python3 --version
hermes --version       # 或 codex / cursor-agent / copilot / claude
```

项目本身只使用 Python 标准库。默认配置见 [harness.config.json](harness.config.json)。

默认 `workspace/` 会把任务产物与 Harness 自身隔开。若把 Harness 放进已有代码仓库并希望 worker 直接修改该仓库，可把配置中的 `workspace` 改成 `.` 或目标目录；不要让不受信任的 profile 操作包含敏感数据的目录。

## 一句话如何变成任务

根 Agent 会先读取当前状态。没有未结束任务时，它会把你的原始目标、约束和必要假设整理到 `.harness-request.md`，再交给控制器。明确的需求不会被追问成一张表；只有缺失信息会实质改变结果时才需要一个简短问题。

每个 run 都有独立状态、日志、审查历史和最终报告。实现结果保存在配置指定的持久 `workspace/` 中，因此后续任务可以继续改进前一项成果。运行记录保存在 `runs/`，不会覆盖以前的任务。

跨 CLI 的交接不依赖终端输出格式。Worker 持续更新 run 中的 `PLAN.md`，并在每轮结束写 `WORKER_RESULT.json`；Reviewer 在独立 review 目录写 `AUDIT.json`。Harness 会归档每轮结果并以这些结构化文件决定 PASS、FIX 和恢复位置。

Harness 同一时间只认领一个活动 run：

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
- `{workspace}`：任务工作目录；
- `{run_dir}`：当前 run 目录；
- `{role}`：`worker` 或 `reviewer`。

新增或调整 CLI 通常只需修改 [harness.config.json](harness.config.json)，不需要复制一套 Harness。CLI 必须支持在指定目录非交互完成一轮工作；只有 GUI、无法接收 Prompt 的工具不能直接作为 worker/reviewer profile。

## 暂停、恢复与状态

在根 TUI 中直接说“状态/status”“停止/stop”或“继续/continue”即可。对应的只读/控制命令是：

```bash
./harness_control.py status --json
./harness_control.py stop
./harness_control.py continue
```

`stop` 会终止当前子 Agent，并保留已经写入磁盘的 workspace、阶段和审查记录；它不能保证 Agent 在被终止前额外输出一份会话总结。`continue` 只恢复同一个 run，并从这些持久文件重新启动对应 profile。不要用旧需求再次执行 `start`，否则会产生重复任务。

如需本机浏览器中的只读状态页，可运行 `python3 status_dashboard.py`，再打开终端显示的地址。页面可在中文和 English 之间切换；它不是完成任务所必需的，也不提供修改或控制按钮。

CLI 文本会根据 `HARNESS_LANG` 或系统 locale 选择中文或英文。需要显式覆盖时：

```bash
HARNESS_LANG=zh-CN ./harness_control.py status
HARNESS_LANG=en ./harness_control.py status
```

## Worker 与 Reviewer

Worker 只在 workspace 中实施当前需求，先检查已有内容，再做最小完整改动并运行与任务匹配的验证。收到 FIX 后继续使用同一 worker profile，并从持久计划、结果和完整审计文件恢复上下文；该修复轮会同时处理审计中的全部 blocker、major 和 minor，并逐项验证可执行的验收检查。minor 单独存在时仍不会触发修复轮。

Reviewer 使用独立会话，只读检查需求、产物和 worker 的验证结果。它应实际运行适合该任务的非破坏性检查，但不会把网站任务强制套成浏览器游戏测试，也不会把普通脚本任务强制套成 UI 审查。只有 blocker/major 问题触发 FIX；minor 会进入报告但不阻止 PASS。默认最多审查三轮。

这里的“只读”是 Harness 会检测并拒绝 reviewer 对 workspace 的净修改，不是操作系统级安全沙箱；profile 仍应只使用你信任的本地 Agent 和权限。Reviewer 运行会生成缓存或构建文件的检查时，应把输出放到 review 目录或临时目录，结束前保持 workspace 与审查前一致。

两个角色的完整约束分别在 [worker.md](prompts/worker.md) 和 [reviewer.md](prompts/reviewer.md)。

## 配置摘要

[harness.config.json](harness.config.json) 的主要字段：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `workspace` | `workspace` | 跨 run 保留的实现目录 |
| `default_agent` | `hermes` | 无法识别根 Agent 时的 fallback |
| `worker_agent` | `null` | `null` 表示继承根 Agent |
| `reviewer_agent` | `null` | `null` 表示继承根 Agent |
| `max_reviews` | `3` | FIX/复审上限 |
| `timeout_seconds` | `5400` | 单次 Agent 调用超时 |

配置不存放密钥。凭据仍由各 Agent CLI 自己管理；不要把 token、`.env` 内容或授权头写进需求、Prompt、日志或 profile argv。

## 开发与验证

修改 Harness 本身时应在当前 coding-agent 会话中直接完成，绝不能为了修改 Harness 而启动 Harness。基础检查：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
```

Prompt 变量说明见 [prompts/README.md](prompts/README.md)。
