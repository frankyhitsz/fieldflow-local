# FieldFlow Local 任务记录

更新时间：2026-08-24

## 本轮目标

复核 `pro-plan.md` 锁定的 `f3e0a58`，优先修正计划与执行事实混用的问题，并在不引入占位 worker、库存或移动端模块的前提下收紧发布与历史操作边界。

## 已完成

### 执行事实与重排

- [x] 为执行事件增加场景内单调序号，并在 PlanningContext 中保存当前事件水位。
- [x] Candidate 发布时由 Verifier 重新读取权威执行上下文，核对来源 V、事件水位和全部服务中工单。
- [x] 重排从实际开始/完成时间和现场位置继续计算，不再只使用计划完成时间。
- [x] 禁止跳过路线前序开始服务；禁止同一技师同时服务两张工单。
- [x] 已完成工单退出未来排程，但继续保留执行事件与来源关系。
- [x] 有执行状态时，备注、技师或锁定编辑保留当前执行 V；策略实验和普通优化不能绕过执行上下文。

### 历史操作与迁移

- [x] 有执行事件的场景不能 Reset。
- [x] Rollback 预览列出 started/completed 重开、执行工单删除和受影响事件；正式恢复阻止这些冲突。
- [x] Clone 定义为独立规划副本，不复制执行事件，并把执行状态归一为 `pending`。
- [x] Schema v8 删除读取时数据修补；旧默认值在启动迁移中一次性持久化，并新增正式 D 修订。

### 求解血缘、幂等与事务

- [x] 新增 SolverPolicySnapshot，记录 Profile、完整权重、未分配倍率、实际 drop penalty、时限和 OR-Tools 搜索参数。
- [x] Run、Candidate、Schedule 和发布事务核对同一个政策指纹。
- [x] baseline、optimize 和普通 replan 在计算前抢占幂等命令；并发重复请求不再重复求解。
- [x] Run 终态不可覆盖；实验 winner 与 PlanVersion 在同一个 SQLite 事务提交。
- [x] 执行动作幂等重放返回当前聚合和原事件，不再返回旧 Scenario 快照。

### 界面、指标和 CI

- [x] 用户界面与报告统一使用“计划覆盖率”“计划 SLA 达成率”和“计划占用利用率”。
- [x] 页头服务时段和技能种类从当前场景计算，移除硬编码。
- [x] Ruff 格式检查加入 `make lint`；GitHub Actions 在浏览器失败时保留 Playwright trace 和报告。
- [x] 第四轮逐项判断记录在 `docs/pro-plan-fourth-assessment.md`。

## 当前验证

```text
make lint                         通过（Ruff check + format、TypeScript）
make test                         67 passed，coverage 87.56%
make test-frontend               5 passed
make build                       通过
make demo-check                  通过
Playwright API 主流程             1 passed
git diff --check                 通过
```

本机页面级 Playwright 仍受 Codex macOS 沙箱限制：Google Chrome 在创建页面前以 `SIGABRT` 退出，四项测试均未进入应用断言。这与此前复现一致；Linux CI 使用 Playwright Chromium 运行完整五项，并会在失败时上传诊断产物。

仅有 OR-Tools SWIG 类型缺少 `__module__` 的三条上游弃用提示。

## 明确延期

- 持久 Outbox、独立 worker、断线恢复和进程级硬取消需要一次完整的异步执行架构改造。
- D 为兼容现有 API 继续表示聚合修订；新增执行事件序号已把验证水位独立出来，但没有破坏性拆分公开修订模型。
- 历史 replan 的“血缘来源”和“下一次稳定性比较基准”仍需拆成两个持久字段。
- Mypy、属性测试、LICENSE、Booking、资产、库存、成本和技师移动端均不以占位实现计入完成。
