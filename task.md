# FieldFlow Local 任务记录

更新时间：2026-08-24

## 本轮目标

逐条复核新版 `pro-plan.md`，完成 v0.5.4 的 FF-1101～FF-1109。能在现有单机架构内闭环的建议直接实现；需要新运行时或新领域数据的 M1～M4 项保留到对应版本，不添加无法工作的占位类型。

## 第一轮：发布上下文、外部覆盖与共同场景

- [x] replan V 保存 `PublicationPlanningContext`：每位技师的 route entry、可用时间、返回点、发布执行水位、来源方案和冻结分配身份。
- [x] 发布清单绑定场景快照、正式排程、PlanningContext 和发布验证 Artifact；上下文篡改会阻止分析。
- [x] 风险从历史 route entry 开始；selected-plan 容量复核使用同一入口；replan 的受控重算稳定返回 `REPLAN_CONTROLLED_REOPTIMIZATION_NOT_SUPPORTED`。
- [x] 旧 replan 仅保留带警告的成本分析，容量和风险返回 `REPLAN_ANALYSIS_CONTEXT_NOT_AVAILABLE`。
- [x] 外包反事实增加 `ExternalAssignment`、逐工单 disposition、外部 SLA 假设和统一 KPI；Formal result 绑定 Artifact 哈希。
- [x] 共同随机场景先于计划生成，突发目标不依赖已用路线，并冻结事件时间、位置、时长和技能；新增配对风险比较接口及场景集 Artifact。

## 第二轮：重试、证明链和成本来源

- [x] 精确 retry 校验原 build、发布上下文、旅行模型、排程和请求；运行时漂移返回稳定冲突并提示 current rerun。
- [x] current rerun 创建新的 logical analysis 并保存 `supersedes_analysis_id`，不冒充 retry。
- [x] Schema v17 增加 `(logical_analysis_id, attempt_number)` 唯一表；attempt 在 `BEGIN IMMEDIATE` 事务内分配，重试幂等键合并双击。
- [x] A 终态保存 result hash、Artifact manifest 和 analysis manifest；读取时检测结果篡改、Artifact 篡改或缺失，旧 A 标记 `LEGACY_UNATTESTED`。
- [x] 发布时保存 Candidate、PlanningContext、事务复核报告、验证政策和正式排程哈希；分析、报告和历史激活统一验证。
- [x] 新增技师成本显式区分 `WAGE_ONLY`、`FIXED_ONLY`、`WAGE_PLUS_FIXED`；外包费用只从成本政策或容量政策二选一。

## 第三轮：API、界面和边界审计

- [x] deprecated 同步分析接口内部创建/复用 A，不再绕过审计。
- [x] 前端 `ApiError` 保留 status、code 和 details；RUNNING A 自动轮询，不再把 202 当失败终态。
- [x] 容量表可读取 Artifact，展示完整性、内部/外部/未服务计数和外部承接清单。
- [x] 补充 route-entry 历史一致性、受控重算拒绝、外包 Artifact、计划无关随机事件、配对比较、并发 retry、运行时漂移、结果/Artifact 篡改、Legacy 证明和成本来源测试。
- [x] 额外发现并修正风险仿真中突发事件只在班次起点加固定延迟的简化：现在按冻结事件时间和位置插入行程与服务时长。
- [x] `make verify` 除本机 Chromium SIGTRAP 外全部通过；API Playwright 1 项通过，三轮 auto-review 记录已更新。核心提交 `9d4f613` 的 GitHub Actions `32742927633` 已由 Linux Playwright 补证。

## 经复核不在 v0.5.4 实现

- M1 的持久 Job Queue、子进程硬取消、Lease、完整依赖注入、三类 D 修订、严格不可变展示元数据、Location/Depot 实体、显式迁移 CLI 和数据生命周期需要运行时或存储契约升级。
- M2 的 Booking、工单收件箱、技师端、资产、库存、通知和 Crew 需要新的业务实体，不能用现有 assignment 伪装。
- M3/M4 的执行感知预测、多日模型、正式 Benchmark、许可证和分支治理按路线图保留；许可证及仓库治理必须由仓库所有者选择。

## 当前验证

```text
Ruff / Pyright                     通过
决策测试                           59 passed
后端完整测试                       188 passed；coverage 90.10%
React 组件                         10 passed
TypeScript / 生产构建              通过
完整 make verify                   lint/audit/test/build/demo/benchmark 通过；本机页面 Chromium SIGTRAP
GitHub Actions 32742927633         python-compat / fieldflow 全部 success
```
