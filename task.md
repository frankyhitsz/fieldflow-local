# FieldFlow Local 任务记录

更新时间：2026-08-25

## 本轮目标

按再次变更的 `pro-plan.md` 完成 v0.5.8 Correctness Freeze。四项 P0 和当前架构内能够闭合的正确性问题直接修复；任务书已明确排到 v0.6 以后且需要新运行时或新业务实体的项目，逐项记录理由，不增加占位接口。

## 第一轮：确认缺陷与修复核心不变量

- [x] P0-01：响应者选择不再复制候选并模拟整条未来路线。新增 `EmergencyDecisionInformationSet`，政策改为 `MYOPIC_EARLIEST_EMERGENCY_FINISH`；选择后只对选中响应者模拟一次未来。
- [x] P0-02：紧急接单复用 PlanApplicability reducer，在同一事务中保留路线、标记覆盖不完整及规划/指标过期。
- [x] P0-03：新增 `WorkOrderCreate` / `EmergencyWorkOrderCreate`，公共创建和更新均不接受执行状态；服务端只创建 pending。
- [x] P0-04：容量成本加入来源规则和 `CostLedger`，删除 `FIXED_ONLY` 事后减法；剩余范围、付费班次和未使用候选均保持非负并对账。
- [x] 新增未来服务波动、未来 no-show、未来返程延误不改变响应者，以及已观察行程延误可以改变响应者的回归测试。
- [x] 新增无紧急事件 null 语义、决策证据、服务/行程检查点和成本组合回归。

## 第二轮：迁移、并发、统计与完整性

- [x] 新建 `PlanDependencyIndex` 和原子 reducer；覆盖差集每次重算，失效 assignment 累积，未分配需求和未使用技师不会误伤路线。
- [x] 数据编辑事务同时 CAS 数据 D 和活动 V；并发发布新 V 时拒绝旧适用性投影。
- [x] Schema v21 回填 v19/v20 旧覆盖语义，PlanApplicability 增加复合外键、状态 CHECK 和 JSON array CHECK；旧 `coverage_status` 改为派生投影。
- [x] `/api/v2` 的 WorkOrder、Technician、Lock、Reset 写操作强制 `If-Match`，缺失返回 428；v1 路径标记 deprecated。
- [x] 风险条件指标增加事件计数，零样本返回 null；配对紧急指标按事件 trial 计算并保留无条件影响，摘要携带 conditioning event、有效样本和配对 bootstrap 区间；事件样本少于 20 时明确返回 `INSUFFICIENT_EVENT_TRIALS`，不输出伪精确区间。
- [x] 风险比较在创建子 A 前预检 scope、as-of 与 ScenarioSet identity；增加紧急事件增量迟到、加班、未服务和受影响工单。
- [x] 重新认证使用 Plan 引用范围指纹，允许新增未参与路线的技师；历史求解来源改为结构化、可区分 `LEGACY_UNATTESTED`。
- [x] DecisionRuntimeManifest 与 ReleaseManifest 分离；前端锁文件和完整发布 SHA 不再阻止后端决策精确 retry。
- [x] 执行事件保存关系型身份、来源 assignment 和内容哈希；列表、完成命令和前序事件消费共用完整性门禁。孤立 STARTED/COMPLETED 状态进入完整性问题清单。
- [x] 修复状态机测试自身的 ID 重用缺陷，并重新运行受影响回归。

## 第三轮：前端契约与模型化测试

- [x] 前端区分普通新增需求与紧急未覆盖需求；失效 assignment 显示原因并禁用“开始服务”；零紧急样本显示“不适用”。
- [x] OpenAPI 生成 TypeScript component types，核心 WorkOrder、Technician、ExecutionEvent 直接引用生成类型；lint 检查生成文件漂移。
- [x] 增加 PlanApplicability `RuleBasedStateMachine`。
- [x] 增加定向 mutation smoke；覆盖差集、失效集合累积、零事件语义和固定成本工资四个 mutant 均被测试杀死。
- [x] Python 依赖改为 `pyproject.toml` 单一来源，安装与审计不再读取第二份清单。
- [x] 独立审计修复事件后仍读取服务中未来时长、v20 坏 JSON 迁移、编辑请求携带只读字段、Demo 突发单旧 payload、新增容量误标陈旧、执行事件消费门禁和依赖审计范围。
- [x] 完整后端 243 项通过，覆盖率 89.75%；React 13 项、构建、Demo、Benchmark、静态检查、OpenAPI/生成类型、依赖审计和 4/4 mutation smoke 通过。
- [ ] Playwright 页面流程与最终 `make verify`：本机 macOS 浏览器进程在进入页面测试前被环境以 SIGTRAP/SIGABRT 终止；API lifecycle 已通过，待 GitHub Linux CI 完成页面断言。
- [x] 完成 v0.5.8 对 P0/P1/P2 的逐项裁决文档；未把新领域模型、持久任务或仓库治理写成已完成。
- [x] 三轮独立审计记录已写入本地 `review-stage`；该目录按项目约定不提交。
- [ ] 提交、推送并确认 GitHub Actions 全绿。

## 明确边界

- Job Queue、Outbox、求解子进程硬取消、迁移维护 CLI、Artifact retention 属于任务书 M1/v0.6；当前没有持久 worker，不能用空路由冒充。
- 正式 Booking、工单收件箱、技师端、缺件/失败/再次上门、资产和库存属于 M2/v0.7。
- 时变旅行、真实多日需求预测、风险历史校准和组合容量属于 M3/v0.8。
- LICENSE 需要仓库所有者选择 MIT 或 Apache-2.0；GitHub 分支保护属于额外仓库治理操作，本轮不擅自变更。

逐项证据与理由见 `docs/pro-plan-v0.5.8-assessment.md`。
