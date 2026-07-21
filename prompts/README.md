# 子角色 Prompt

[中文](README.md) | [English](README.en.md)

Harness 每次调用子 Agent 前读取这里的模板。模板与具体 CLI profile 无关；Agent 命令定义在根目录的 [`harness.config.json`](../harness.config.json)。

## 模板

- `worker.md`：实施或修复当前任务。首行角色标记不可修改。
- `reviewer.md`：独立只读审查。首行角色标记不可修改。

## 变量

`worker.md`：

- `{request}`：用户的权威自然语言需求；
- `{workspace}`：唯一允许修改的工作目录；
- `{run_dir}`：当前 run 目录，只作状态和上下文定位；
- `{review_feedback}`：首次执行为空；修复轮为 reviewer 的完整审计，包括 blocker、major、minor、检查与限制。

`reviewer.md`：

- `{request}`：同一份权威需求；
- `{workspace}`：必须保持只读的实现目录；
- `{run_dir}`：当前 run 目录；
- `{worker_report}`：worker 报告的改动与验证；
- `{review_dir}`：reviewer 唯一允许写入证据的目录。

保留变量拼写。模板正文需要普通花括号时使用 `{{` 与 `}}`。不要在 Prompt 中写死 Agent 名称、供应商、模型、本机路径或特定任务类型的验收规则。

## 固定产物

Worker 持续维护 `{run_dir}/PLAN.md`，至少记录目标、假设、验收条件、步骤和当前进度。每轮结束覆盖 `{run_dir}/WORKER_RESULT.json`；Harness 会把它归档到对应 iteration。结果对象必须包含：

```json
{
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

`status` 只能是 `complete` 或 `blocked`。`checks[].status` 使用 `pass`、`fail` 或 `not_run`。

Reviewer 把结构化结论写到 `{review_dir}/AUDIT.json`：

```json
{
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

`verdict` 只能是 `PASS` 或 `FIX`；`severity` 只能是 `blocker`、`major` 或 `minor`。所有字段都必须存在，即使数组为空。
