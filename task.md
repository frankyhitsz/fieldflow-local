# FieldFlow Local 任务记录

更新时间：2026-08-25

## 本轮目标

复核更新后的 `pro-plan.md`，完成 v0.5.7 稳定化。P0 和可在当前单机架构内闭合的 M0/P1 直接修复；审查稿自己列入 v0.6 以后的 Job Queue、Outbox、独立求解进程、修订拆分和新业务域保留清晰边界，不用占位接口冒充交付。

## 第一轮：根因修复

- [x] P0-01：删除风险模拟中按发布时间猜测状态的第二条路径，改为每个 trial/技师一份权威路线状态。
- [x] 固化 `BETWEEN_VISITS_ONLY` 和 `EARLIEST_FEASIBLE_COMPLETION`；场景集、结果和 Artifact 共同保存政策。
- [x] trial 证据增加响应技师、派出时间、完成时间和派出位置；延迟行程检查点回归已通过。
- [x] P0-02：新普通工单保留路线并标记部分覆盖；新增技师标记再优化机会；删除未分配工单保留路线。
- [x] Schema v20 增加多维 PlanApplicability；前端按路线、覆盖、指标和再优化状态分别提示。
- [x] 受影响定向回归：P0 时间线和三类数据变更 4 项通过。

## 第二轮：证明与边界修复

- [x] RiskComparison 增加不可降级触发器；依赖失效时 `result`、配对值和 delta 全部置空。
- [x] 风险比较补齐全需求 SLA、紧急完成和紧急准时配对指标；迟到页面改为当前分析范围。
- [x] Capacity Artifact 自包含正式结果可用性、结构校验、商业验证、条件假设和条件上界。
- [x] 重新认证拆分路线完整性、原求解来源和 replay 政策；增加规划等价模式及 Legacy replan 前置错误。
- [x] A 完成事务重新加载父 Plan；竞态时保存 `PARENT_PLAN_CHANGED_DURING_ANALYSIS`，不保存结果和 Artifact。
- [x] WorkOrder、Technician、Lock、Reset 支持 `If-Match: Dnnn`；网页客户端统一携带。
- [x] 场景、执行事件、ScheduleRun、Candidate 和 Experiment 的关键读取增加 quarantine 与结构化错误。
- [x] 第二轮 6 项高风险定向回归通过，Ruff 通过。

## 第三轮：完整验证与交付

- [x] 更新 OpenAPI 快照、版本号和前端锁文件。
- [x] 后端 222 项通过，覆盖率 89.68%。
- [x] React 组件 10 项、ESLint、TypeScript、生产构建通过。
- [x] Demo、Benchmark、pip-audit、npm production audit 通过；Playwright API lifecycle 通过。
- [x] 独立缺陷审计后修复严格 trial 类型、前端范围指标 mock、inactive 适用性投影和兼容锁定语义；`make verify` 除本机 Chromium 启动 SIGTRAP 外均通过，4 个页面用例未进入测试代码，交由 Linux CI 复核。
- [ ] 提交并推送 `main`，等待 GitHub Actions 全部通过。

## 裁决边界

- v0.5.7 已处理 P0-01/02 和 P1-01～15 的当前架构可落地部分。`If-Match` 暂保留旧客户端兼容路径；低风险配置表的统一坏行 loader 随模块拆分收尾。
- P1-16～25 对应审查稿的 v0.6 运行时：迁移 CLI、Job Queue/Outbox、独立求解进程、依赖注入、修订拆分、Location/时变行程、内容寻址和保留策略。本轮不改变安装及恢复契约。
- P2-01～05 是工程演进；P2-06 LICENSE 和 P2-07 GitHub 治理需要仓库所有者明确选择；P2-08～10 是 v0.7 新业务域。

逐项证据与理由见 `docs/pro-plan-v0.5.7-assessment.md`。
