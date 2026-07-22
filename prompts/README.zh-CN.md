# 子角色 Prompt

[English](README.md) | [中文](README.zh-CN.md)

Harness 每次调用子 Agent 前都会读取这些模板。模板与 CLI profile 解耦，命令定义在
根目录的 [`harness.config.json`](../harness.config.json)。不得修改首行角色标记，
也不得修改 Review v2 的第二行阶段标记。

## 模板

- `worker.md`：在持久隔离候选区实施或返修当前任务。
- `review_planner.md`：启动全新的 Reviewer 角色会话，阶段为 `PLAN`，只写
  `REVIEW_PLAN.json`。
- `reviewer_v2.md`：再次启动全新的 Reviewer 角色会话，阶段为 `ASSESS`，写引用的
  人工证据和 Audit v2。
- `reviewer.md`：旧 Audit v1 模板，仅用于继续缺少 `review_protocol_version` 的安全
  run-format v2 未完成任务。

## 变量

`worker.md` 接收 `{request}`、`{workspace}`、`{run_dir}` 和
`{review_feedback}`。

`review_planner.md` 除权威需求与一次性 artifact 副本外，还接收
`{worker_report}`、`{verification_report}`、`{review_dir}`、`{artifact_id}`、
`{review_round}`、`{review_policy}`、`{policy_sha256}` 和 Harness 自有的
`{review_subjects}`。最后一项包含计划必须原样复制的 `REQ-REQUEST` 与 Worker 声明。

`reviewer_v2.md` 接收需求、一次性副本、Worker 与确定性验收报告、
`{review_plan}`、Harness 自有 `{review_checks}`、`{plan_sha256}`、
`{review_dir}` 和 `{artifact_id}`。

必须保留变量拼写。模板正文需要普通花括号时使用 `{{` 与 `}}`。不要写死 Agent、
供应商、模型、本机路径或特定任务的验收规则。

## Worker 交接

Worker 维护 `{run_dir}/PLAN.md`，并在每轮结束覆盖
`{run_dir}/WORKER_RESULT.json`。Harness 校验后归档到对应 iteration。Schema 仍为
`generic-harness/worker-result/v1`：

```json
{
  "schema_version": "generic-harness/worker-result/v1",
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

`status` 只能是 `complete` 或 `blocked`；检查状态使用 `pass`、`fail` 或
`not_run`。改动路径相对候选区，并在所有平台统一使用 `/`。

## Review v2 交接

协议刻意拆分，避免任何 Agent 独占最终门禁：

1. Planner 写 `REVIEW_PLAN.json`（`generic-harness/review-plan/v1`）。它必须把完整
   需求原样保留为 `REQ-REQUEST`，精确复制 Harness 派生的全部 Worker 声明，为策略
   中每个风险分类写一项，并且只通过 `checks[].covers` 表达覆盖关系。命令检查使用
   argv 数组；inspection/visual 检查描述预期证据。
2. Harness 校验 artifact、轮次、策略指纹、ID、字段、限制和引用，解析精确的
   `{python}` token，并在一次性 artifact 副本中执行每项 command check。
   `REVIEW_CHECKS.json`、`harness-evidence/*/RESULT.json` 以及有界 stdout/stderr 均由
   Harness 持有。
3. Assessor 写 `AUDIT.json`（`generic-harness/audit/v2`）：每个必需 subject 恰好一条
   coverage，每个 inspection/visual check 恰好一个结果。通过的人工检查必须引用
   `reviewer-evidence/` 下的持久文件。Verdict 使用 `PASS`、`FIX` 或
   `INCONCLUSIVE`。
4. Harness 对引用的人工证据计算哈希并写 `FINAL_REVIEW.json`，正式提升前还会重新
   读取全部底层证据并再次计算 verdict。两个 Reviewer 阶段都不能修改 Harness 自有
   证据。

阻塞证据缺失是合法协议数据，但结果必须为 `INCONCLUSIVE`，不能伪造 PASS。
确定性验收失败、blocking check 失败或确认存在 blocker/major 时为 `FIX`；只有 minor
不阻止 PASS。所有结构化对象都拒绝未知或缺失字段，并绑定同一 artifact 与计划哈希。

## 旧 Review v1

`reviewer.md` 写 `generic-harness/audit/v1`，verdict 只有 `PASS` 或 `FIX`，并包含
checks、issues 与 limitations。新 run 不再选择它，也不能用它冒充 Review v2 覆盖证明；
保留说明只是为了安全恢复已经创建的 run-format v2 任务。
