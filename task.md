# FieldFlow Local 任务记录

更新时间：2026-08-24

## 本轮结论

- [x] 完整读取并复核 `pro-plan.md`。该文件锁定旧提交 `ea47edc`，且审查者没有运行测试。
- [x] 修改前基线：后端 56 项、React 5 项测试通过，Ruff、TypeScript 和生产构建通过。
- [x] 第一轮：修复硬锁定发布缺口和工单执行状态绕过。
- [x] 第二轮：统一接报时间、终止状态、返程加班、行程指纹和候选谱系。
- [x] 第三轮：修复实验取消覆盖竞态、解释模板耦合、重排插单变化口径和前端严格检查。
- [x] 完整执行构建、Demo 和 Playwright；请求级流程通过，4 个页面用例因本机 Chrome 启动 `SIGABRT` 未进入断言，交由 Linux CI 补足。
- [x] 提交 `6d1d365` 并推送到 `origin/main`。
- [ ] 等待 GitHub Actions 完成 Linux Chromium 门禁。

## 已完成改动

### 第一轮

- Schema v6 增加 `work_order_execution_events`。
- 新增显式开始/完成服务接口；请求包含技师、发生时间、预期 D 和幂等键。
- 执行事件、工单状态、D 修订和幂等结果在同一 SQLite 事务中提交。
- 通用工单 PUT 不再接受状态变化；前端编辑器改为只读状态，工单详情提供执行按钮。
- Verifier 新增锁定工单缺失/未分配和服务中工单未分配检查。
- 重排上下文遇到服务中或已完成工单缺少来源分配时返回 409，不再静默跳过。

### 第二轮

- `reported_at` 成为基线、优化器、诊断器和 Verifier 共享的最早服务时间。
- OR-Tools 状态 2 改为 `FEASIBLE`；只有明确超时才使用 `TIME_LIMIT_*`。
- 加班解释和证据包含末单返程，与 KPI 口径一致。
- 行程模型增加配置/矩阵 SHA-256 指纹，并纳入实验和发布复核。
- Candidate 改为写入后不可变；发布事务校验 Run、Candidate、来源方案、场景和求解配置血缘。
- 突发工单接收键与重排尝试键分离，崩溃后可基于已保存工单重新发起求解。

### 第三轮

- 实验保存使用状态优先级，陈旧 worker 不能覆盖 `CANCEL_REQUESTED` 或终态。
- 解释模板变化降为警告；结构化证据和业务约束仍是发布阻断条件。
- 新工单插入旧路线时，后续旧工单会标记为 changed，与求解变化惩罚和通知口径一致。
- “工作量公平”说明改为准确描述 min-max 代理目标，不再声称精确优化方差。
- TypeScript 开启未使用符号和 switch 穿透检查，并清理遗留未使用导入。
- 现场执行后，调度台明确说明当前 V 仍是执行依据，不再误报为只读历史。

## 当前验证

```text
make lint          通过
make test-frontend 5 passed
make test          60 passed，coverage 88.25%
make build         通过
make demo-check    通过
git diff --check   通过
CSS 显式业务字号   未发现低于 11px
```

仅有 OR-Tools SWIG 类型缺少 `__module__` 的 3 条上游弃用提示。

`make test-e2e` 中，请求级“编辑 → 优化 → 实验 → 发布 → 回滚 → 比较 → 报告”通过。当前 macOS 沙箱内的 Google Chrome 在页面用例启动时以 `SIGABRT` 退出，4 项均未进入页面断言；对应用例由 GitHub Actions 的 Ubuntu Chromium 环境执行。

## 尚未纳入当前产品范围

- 普通排程请求仍为同步 HTTP；Run 可追踪，但没有独立进程 worker 的恢复队列。
- OR-Tools 公平策略使用标准化服务负荷的 min-max 代理，不等同于精确最小化前端展示的所有公平指标。
- 历史 replan 重新激活时的稳定性参照语义仍需独立模型支持。
- 场景哈希继续采用完整业务快照。按已确认规则，备注、技师资料等业务编辑也会使实验和正式方案过期。
- LICENSE 由仓库所有者选择。

逐项判断见 `docs/pro-plan-third-assessment.md`。
