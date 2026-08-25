# FieldFlow Local 任务记录

更新时间：2026-08-25

## 本轮目标

按 2026-08-25 19:49 更新的 `pro-plan.md` 完成 v0.5.9 Trust Closure。发布门禁是执行事实只能经过可信读取路径、求解结果同时绑定 D 与活动 V、紧急投影与实际时间线一致、当前未覆盖需求不能被冻结 KPI 隐藏，并让数据修订历史成为可校验的恢复源。

## v0.5.9 实施记录

### 第一轮：信任闭包

- [x] `_execution_source_context`、重排、发布复核和幂等重放统一从关系行重新加载并校验执行事件；事件水位必须连续，开始/完成事件必须属于同一 Booking 和来源 assignment。
- [x] 执行命令重放不再读取 `command_keys.payload` 中的结果副本；事件缺失、内容被改写或来源 Plan 失效时失败关闭。
- [x] ScheduleRun/Candidate 保存求解开始时的活动 V，发布事务同时 CAS 数据 D 和活动 V；并发旧候选不能覆盖新方案或消耗版本号。
- [x] ScenarioRevision 增加快照哈希、前序哈希和修订哈希；reset、列表和启动扫描验证关系身份与整条证明链。

### 第二轮：投影、指标和界面

- [x] 修复等待客户时接应急单后漏算返回客户行程；零随机测试核对候选终点与选中响应者实际终点。
- [x] 风险受影响工单按工单级 disposition 差异去重；零事件条件概率返回 null；单方案 Monte Carlo 区间改为确定性 percentile bootstrap。
- [x] 风险队列、位置图和 KPI 使用当前需求 disposition；新增未覆盖需求明确标记，不再显示为冻结方案的 100% 当前覆盖。
- [x] `route_executable=false` 的界面文案与后端安全前缀语义一致：只阻止失效 assignment，其他已验证承诺仍可执行。

### 第三轮：P1 收敛与验证

- [x] 比较/回滚差异覆盖 disposition、技师、顺序、到达、开始、完成、行程、锁定和来源 assignment 身份；原始 objective 仅在完整求解政策一致时比较。
- [x] 容量正式 KPI/成本与诊断结果分栏；条件上界不再写入正式反事实字段；完整未分配 disposition 的零路线方案不再触发 `EMPTY_CANDIDATE`。
- [x] PlanApplicability 多轴字段成为读取时唯一事实，`coverage_status` 只保留为兼容 SQL 投影；新建工单和技师 code 限制为 URL 安全字符，存量 ID 保持可读。
- [x] 增加完整 Python 传递依赖锁，决策代码指纹缩小到实际决策模块，旧 v1 CAS 写接口声明 2027-03-31 Sunset。
- [x] GitHub 的 Python 3.11 门禁发现 `numpy==2.5.2` 只支持 Python 3.12+；锁文件收敛到 3.11–3.13 共用的 `numpy==2.4.6`，并通过目标平台解析、全新 Python 3.13 环境安装、`pip check` 和 254 项后端测试。
- [x] Mutation smoke 从 4 个扩展为 8 个定向 mutant，新增执行事件、活动 V CAS、修订证明、零随机投影、唯一计数和当前需求界面回归。
- [ ] 完成最终 lint、类型检查、依赖审计、全量后端/前端/E2E、Demo、Benchmark 和 GitHub Actions 验证。

逐项裁决见 `docs/pro-plan-v0.5.9-assessment.md`。

## v0.5.8 历史记录

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
- [x] GitHub Actions #42 暴露干净 editable install 的 setuptools 顶层包误发现；`pyproject.toml` 现明确只打包 `backend`，并用全新虚拟环境验证安装元数据。
- [x] 本机 macOS 浏览器进程在进入页面测试前被环境以 SIGTRAP/SIGABRT 终止；GitHub Actions #45 在 Linux 完成同一门禁，Playwright 5/5 通过，完整 `fieldflow` job 全绿。
- [x] 完成 v0.5.8 对 P0/P1/P2 的逐项裁决文档；未把新领域模型、持久任务或仓库治理写成已完成。
- [x] 三轮独立审计记录已写入本地 `review-stage`；该目录按项目约定不提交。
- [x] 功能提交 `7b2ee6d`、干净安装修复 `d7a6bf8` 已推送；GitHub Actions #45 的 Python 3.11 与完整 `fieldflow` job 均通过。

## 明确边界

- Job Queue、Outbox、求解子进程硬取消、迁移维护 CLI、Artifact retention 属于任务书 M1/v0.6；当前没有持久 worker，不能用空路由冒充。
- 正式 Booking、工单收件箱、技师端、缺件/失败/再次上门、资产和库存属于 M2/v0.7。
- 时变旅行、真实多日需求预测、风险历史校准和组合容量属于 M3/v0.8。
- LICENSE 需要仓库所有者选择 MIT 或 Apache-2.0；GitHub 分支保护属于额外仓库治理操作，本轮不擅自变更。

逐项证据与理由见 `docs/pro-plan-v0.5.8-assessment.md`。
