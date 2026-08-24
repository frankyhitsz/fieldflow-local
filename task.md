# FieldFlow Local 任务记录

更新时间：2026-08-24

## 本轮目标

复核 `pro-plan.md` 锁定的 `ea98e33`，先完成 M0 的计划—执行闭环，再把能在当前本地单机场景中严谨实现的 M3 经营决策能力接到公开版本快照上。静态审查中需要新领域模型的建议不做占位实现。

## 已完成

### M0：重复重排与 Booking 身份

- [x] 修复 `complete → replan → replan`：完成工单通过不可变事件和历史 V 追溯，不要求继续出现在 future plan。
- [x] 非首项 started 在未来路线中重编号为 1，来源序号和来源分配指纹另存，所有未来路线序号连续。
- [x] 已完成工单不会重新出现在未来 assignments；失败候选不占 V，也不替换 active V。
- [x] 开始/完成事件保存 Booking ID、来源分配、计划时间、实际时长、迟到分钟、提前授权原因和备注。
- [x] Schema v9 对 `(scenario_id, execution sequence)` 建立唯一约束。

### M0：实际时间、位置与过期门禁

- [x] 前端使用独立的实际执行时间对话框，不再复用“重排时点”。早于客户允许时间开始时必须说明授权原因。
- [x] 完成时间必须严格晚于开始事件；零时长服务返回结构化冲突。
- [x] pending assignment 保存 planning fingerprint。技能、位置、时长、时间窗、班次、锁定和行程模型变化后，旧计划不能开工；姓名和备注变化仍允许执行。
- [x] 前序完成后的下一段从实际完工位置和时间校验，删除前序时返回结构化错误而不是 500。
- [x] started 超过计划完成时间后统一按至少 15 分钟剩余容量投影，重排、Verifier 和执行门禁使用同一服务。

### M0：求解政策与命令恢复

- [x] SolverPolicySnapshot 升级为 V2，分别保存 original/effective drop penalty，避免 completion 与 low-travel 倍率二次应用。
- [x] 贪心 baseline 的 routing time limit 为 null；OR-Tools 的 Run、Result 和 Policy 时限一致并由发布事务交叉校验。
- [x] 历史激活和业务恢复记录为 `plan-activation` / `plan-restore`，保留原策略证据但不冒充重新执行 OR-Tools。
- [x] 启动时把没有发布结果的遗留 RUNNING 命令对账为 `FAILED_RETRYABLE`；同键可以重新获取。已有发布结果的命令对账为完成。
- [x] 规范化后没有变化的保存不增加 D。

### M3：经营决策能力

- [x] 新增整数分成本模型，分别输出人工、行程、加班溢价、SLA 损失、未服务收入损失、外包和总成本。
- [x] Schema v10 把历史 `cost_per_minute` 浮点值按原值转换为 `cost_per_minute_cents`，同步当前场景、D 历史、V 快照和实验快照。
- [x] 新增六类容量 what-if：增加技师、补充稀缺技能、延长班次、增加加班、外包未服务单、增加服务站点；统一输出边际成本及完成率/SLA/行程/加班变化。
- [x] 新增固定 seed 风险仿真，覆盖旅行延误、服务时长、技师缺勤、突发单和客户不在场，输出 SLA、迟到 P50/P90/P95、加班、未服务数与计划失效概率。
- [x] “运营复盘”接入冻结 V 的成本与风险分析，并可按需计算六类容量方案；分析不会生成 D 或 V。
- [x] 新增 `make benchmark-smoke` 并加入 CI，检查小型、中型、窄时间窗、技能短缺、突发和无可行分配场景，以及 M0 回归索引。

### CI 与浏览器流程

- [x] E2E 主流程和“锁定后方案过期”用例各自创建独立场景，不依赖共享 `main` 的历史是否为空。
- [x] 修复优化方案历史恢复时错误继承 routing time limit 导致的 409；API 生命周期 E2E 已通过。
- [x] 本地 Playwright 优先使用配套 headless Chromium；Linux CI 仍使用工作流安装的 Chromium。

## 当前验证

三轮“发现—修复—回归”已完成。最终证据：

```text
make test                            104 passed，coverage 88.74%
M3 决策专项                          6 passed
make test-frontend                   7 passed
make lint                            通过（Ruff check/format、TypeScript）
make build                           通过
make demo-check                      通过
make benchmark-smoke                 通过
Playwright API 生命周期              1 passed
git diff --check                     通过
```

本机页面级 Playwright 受 Codex macOS 进程沙箱限制：Chromium 在创建页面前收到 `SIGTRAP`，没有进入应用断言。页面级测试将在 Linux CI 运行并上传失败 trace；本地 API 生命周期不需要启动页面，已实际通过。

## 明确延期或反驳

- 持久 Outbox、独立 worker、断线恢复和 OR-Tools 子进程硬取消需要一次完整的异步执行架构改造；当前不以线程目录冒充完成。
- D 为兼容现有 API 继续表示聚合业务修订，执行事件已有独立唯一水位；彻底拆成 planning/metadata/execution 三套公开修订需单独迁移版本。
- `active` 与 `coverage_status` 仍保留在 PlanVersion 投影中。不可变性建议合理，但迁移为独立 applicability 表会影响所有历史查询，不与本轮 M0 修复混做。
- Crew 同时到场、跨日排班和零件库存不是现有单日 Technician/WorkOrder 模型的小扩展；Benchmark 明确标记 unsupported。每技师独立出发点已支持，新增服务站点目前是透明假设的 what-if，不声称具备完整多站点库存。
- Mypy/Pyright、OpenAPI 生成客户端和完整属性测试仍是工程路线图；本轮先用严格 Pydantic、TypeScript、迁移/并发/确定性回归覆盖实际改动。
- LICENSE 由仓库所有者选择，未擅自添加法律文本。
