# FieldFlow Local 可靠性规格

## 目标

FieldFlow 是离线现场服务调度台。系统必须把业务数据、现场执行事实、求解候选、正式方案和经营分析分开保存；失败、重复请求或进程中断不能改写最后一份可执行方案，也不能让公开编号无故跳号。

本轮以 2026-08-29 更新的 `pro-plan.md` 为审查输入。每条建议都以当前代码、SQLite 关系和可运行流程复核；合理项直接实现并验证，过度推断则记录反证。任务书中的持久运行时、现场业务闭环和决策科学扩展也进入本轮渐进交付范围，不因跨版本而自动延期，但未完成的能力不能以占位类型或文档声明冒充完成。

当前发布目标为 v0.5.11 Correctness Freeze：Start 与 Complete 使用各自的权威事实；紧急接收有独立 Receipt；调度读取绑定同一 D/V/E；Run、Candidate、恢复变换、命令和发布都具有可验证清单；报告明确区分冻结与当前；Risk V7 和 Capacity Artifact 能自包含地解释结果。

## 当前范围

- FastAPI、SQLite、React，单机离线运行。
- 每个场景独立维护 `D` 数据修订、`V` 正式方案和 `A` 经营分析记录。
- baseline、optimize、replan、历史激活、业务回滚和实验发布都经过 Candidate 校验及事务内复核。
- 现场状态只能通过“开始服务”和“完成服务”命令变更，不能通过通用工单编辑绕过正式分配。
- 策略实验使用冻结快照；候选不占用 V，人工选择后才发布。
- 行程、指标、解释、发布验证和经营分析使用同一 `TravelTimeProvider` 指纹。

## 公开编号

- 工单、技师、锁定和执行事实变化产生下一条 `D`，不会占用 V。
- 成功发布正式方案才在事务内分配下一条 `V`；内部基线、候选、失败和取消均不占号。
- 经营分析开始前分配下一条 `A`，并记录 `RUNNING`、`COMPLETED`、`FAILED` 或 `INTERRUPTED`。相同 V、分析类型和完整输入指纹在运行中返回 202、完成后返回 200。精确 retry 复用冻结输入并在事务内分配唯一 attempt；运行环境变化时拒绝。按当前上下文重跑会创建新的 logical analysis。
- `PlanVersion` 的冻结 payload 不保存运行时 `active`、`coverage_status` 或名称变化；Schema v21 的 `plan_applicability` 独立投影路线可执行、覆盖完整、规划/指标/商业数据是否当前、再优化机会和无效 assignment，名称、备注和标签由 `plan_metadata` 覆盖。`coverage_status` 只由多轴状态投影，不再作为第二套事实。
- PlanningReservation 在一个 `BEGIN IMMEDIATE` 中冻结已校验的 Scenario、活动 Plan、来源 Plan、执行水位和上下文；ScheduleRun 与 Candidate 绑定 reservation ID/hash。发布事务在分配 V 之前复核 reservation、候选 D、活动/来源 V 及执行上下文；任一变化都拒绝陈旧候选，失败请求不占版本号。
- baseline、optimize、replan、activate、restore、reattest、人工改派和实验发布使用同一活动 V 前置条件。冲突返回结构化 code 和 expected/current ID，不把 Store 异常退化成字符串。
- `D000` 是每个场景唯一根，后续编号必须连续。原生写入只验证关系行保存的链头和最新节点，保持 O(1)；启动扫描负责整链验证。损坏节点之后的修订标记为 `ANCESTOR_INVALID`，不会被重新解释为新根；迁移回填证明标记为 `MIGRATION_BACKFILLED`。
- `scenarios.payload`、关系 ID、当前快照 hash、最新 D 编号/hash 和最新修订快照必须完全一致。失败时 GET、编辑、求解、发布和分析均拒绝，不能通过下一次正常编辑把未留痕修改洗入历史。
- PlanApplicability 保存被评估的 D、场景快照 hash、reducer 版本和 projection hash。Operational View 只使用与当前链头完全匹配的投影，并为每张工单返回唯一 disposition，统一驱动 KPI、队列、地图、时间轴、详情和开工门禁。

## 正式方案与硬承诺

- 锁定工单必须分配给指定技师；锁定工单缺失或未分配时，整个候选不可发布。
- 服务中的工单必须保留来源 Booking 的技师、计划时间、来源序号和分配指纹。未来路线重新从 1 连续编号，来源序号单独保存。
- 已完成工单保留来源和执行事件，但不进入未来排程；完成后可以继续进行第二次、第三次重排。
- ScheduleRun 创建时冻结基础与目标快照、Reservation、来源/活动 V 前置条件和求解输入清单；SQLite 触发器禁止改写输入身份和终态。Candidate 插入后不可修改，并保存排程、验证报告与关系身份清单。发布 Artifact 同时绑定 RunInput、RunResult 和 Candidate Manifest。
- Restore 不再改写 Run 的基础 D。`RestoreTransformManifest` 证明目标快照等于“当前基础快照 + 来源 Plan 冻结快照 + 允许的场景 ID/下一 D 变换”；发布事务重新计算该变换。
- 每个新 V 保存完整发布验证 Artifact；replan V 另存每位技师的发布时路线入口、可用时间、返回点、执行水位和冻结分配身份。经营分析不能用当前执行事实改写这份历史上下文。
- Plan Manifest V2 绑定关系型身份、不可变头、完整血缘、Candidate、PlanningContext、VerificationArtifact 和全部 Plan Artifact。关系型 ID、场景、编号和创建时间必须与 JSON payload 一致。
- Plan 的 `self_integrity` 与 `effective_integrity` 分开返回。除审计查看外，执行、分析、重放、克隆、恢复、比较和报告都必须通过同一使用门禁。Legacy 方案只能查看；需要重新使用时，系统从冻结快照创建带完整证明的新 V，不能原地升级旧记录。重新认证分严格快照与规划等价两种模式，并分别保存路线完整性、原求解来源、继承政策和 replay 校验政策。缺少发布上下文的旧 replan、已经开始执行的场景均在入口拒绝。
- 历史激活保存操作谱系和原始稳定性基准两条关系。激活旧 replan 时不能把刚激活的版本误当成原始重排基准。

## 现场执行

- 开始服务要求当前有正式方案、工单有有效分配、技师匹配、D 修订匹配，并携带独立的实际发生时间和幂等键。完成服务以已验证 Start Event 和 Booking 身份为权威；后续技师资料变化、活动 Plan 切换或撤销不能阻止已经开始的服务登记完成。
- 公共创建命令不接受 `status`；普通和紧急新工单都由服务端建立为 `PENDING`。`STARTED` 与 `COMPLETED` 只能来自不可变执行事件。
- 早于客户允许时间开始时必须填写授权原因；完成时间必须严格晚于实际开始时间。
- 同一技师不能并行服务，且必须按正式路线顺序执行。前序完成后，下一段从实际完工位置和时间校验。
- 开始服务时可记录预计剩余服务分钟；未提供时使用 `active_service_default_remaining_minutes`，调度器、Verifier 和执行门禁共享同一政策。
- pending 分配保存 assignment-feasibility fingerprint；技能、位置、时长、时间窗、班次、锁定或行程模型变化后，失效 assignment 进入累积集合并禁止开工，其他有效承诺仍可执行。优先级、VIP、drop penalty 等目标字段会让方案提示指标过期，但不伪装成分配约束；未分配工单和未参与路线的技师不会误伤现有路线。覆盖完整性每次按“当前需求减 Plan disposition”重算，删除一张新工单不会掩盖其他未覆盖需求。
- 网页使用 `/api/v2` 写入 WorkOrder、Technician、Lock 和 Reset，并强制携带 `If-Match: Dnnn`；缺失返回 428，过期返回结构化 409。旧 v1 写路径标记 deprecated，保留兼容。数据事务还对活动 V 做 CAS，不能把基于旧 V 的适用性投影写到刚发布的新 V。
- 执行事件同时保存关系型身份列、来源 Plan/assignment、Booking 字符串身份和内容哈希；列表读取、完成命令和依赖前序完成事件的开始命令都先通过同一校验门禁。旧数据中没有 start/complete 事件支撑的状态会进入完整性问题清单。
- 重排上下文、发布事务复核和执行命令重放也使用同一事件读取门禁。事件序列必须从 1 连续，complete 必须引用更早且 Booking/assignment 一致的 start；命令缓存只保存资源引用，不是可信事件副本。
- 执行事件 API 明确返回 self/source/effective integrity；这些标签由可信加载器重算，不接受 payload 自行声称。这里的 SHA-256 是内容一致性和篡改迹象检测；hash 与 payload 同处可写 SQLite，不能对抗能够重算整条证明链的恶意写入者。
- “锁定并改派”是分阶段持久 Saga：`RESERVED → LOCK_COMMITTED → REPLAN_CREATED → PLAN_PUBLISHED → COMPLETED`。Run 标识在锁定提交时固定；应用重启后恢复同一 Run，不重复锁定、D、Run 或 V。求解失败进入 `FAILED_AFTER_LOCK`；锁定后业务上下文改变进入 `FAILED_CONTEXT_CHANGED`。两者都是不可覆盖的终态，新请求必须使用新 key。

## 时间、行程与求解

- 最早服务时间为 `max(window_start, reported_at)`。
- 加班按末单完成后返回技师出发点的时间计算，KPI、证据和界面使用同一口径。
- 行程记录同时保存模型名称和配置或矩阵内容指纹；同名但内容不同的模型不能通过复核。
- OR-Tools 只有明确返回超时状态时才标记 `TIME_LIMIT_*`；可行但未证明最优的结果不得显示为最优。
- 求解政策快照保存 original/effective penalty，避免权重倍率被重复应用。

## 幂等与恢复

- 紧急工单接收保存独立 `EmergencyIntakeReceipt`，绑定工单内容、提交 D、请求指纹和 ACTIVE/CONSUMED/CANCELLED 状态。命令重放必须重新加载 Receipt 和工单；删除使用显式取消事务。
- 需要后台执行的任务与 Transactional Outbox 在同一事务提交。紧急接单推进 D 时一并写入 REPLAN Job；应用在求解前退出时，启动恢复器会领取同一任务并继续，不要求用户重新提交。
- Runtime Job 保存输入清单、幂等键、租约、心跳、attempt、进度和终态资源。成本、容量、风险和策略候选在受限子进程运行；取消、墙钟超时或进程异常会终止子进程，已经预留的 A 明确进入 `INTERRUPTED`，不遗留伪运行状态。
- `command_keys` 保存关系字段、载荷哈希和不可变 Command Manifest。坏载荷进入 quarantine；关系清单仍可信的终态命令只从资源 ID 重建，关系也损坏时失败关闭。
- 相同键和相同请求返回同一结果；相同键对应不同请求返回 409。
- 同一幂等求解命令在计算前原子抢占；并发重复请求不能重复运行求解器。
- 启动时对账 `RUNNING`、`REPLAN_RUNNING` 和带发布键的 `INTAKE_COMMITTED`：已有发布结果则完成，否则转为 `FAILED_RETRYABLE`。纯接收命令没有发布键时不被误判。
- 失败候选不新增 V，不替换最后正式方案。

## 经营分析

- 经营分析只接受冻结 V，并把运行保存为不可变 `DecisionAnalysisRun`。请求按 COST、CAPACITY、RISK 判别，只允许一个 `request` 参数对象，不接受会被静默覆盖的并列参数。记录包含执行范围与水位、分析时点、执行上下文、快照和完整排程哈希、行程与政策指纹、语义版本、算法版本、DecisionRuntimeManifest、ReleaseManifest、输入哈希、原始请求、attempt 谱系、终态、结果或结构化错误。精确 retry 只绑定后端决策代码和 Python 依赖，前端或文档发布 SHA 不改变决策身份。
- 普通计划使用 `FROZEN_FULL_PLAN`；带发布上下文的重排使用 `PUBLICATION_REMAINING_PLAN`，从冻结 route entry 开始并排除发布时已开始或完成的服务。`EX_ANTE_FROZEN_PLAN` 仅作为旧客户端兼容别名，由接口映射到上述实际范围。成本、容量和风险必须使用同一工作视图及排程签名。
- 成本只使用整数分，并拆分正常人工、加班基础工资和加班溢价。每个分项写入 `CostLedger`，包含来源、范围和金额；`FIXED_ONLY` 候选从源头关闭工资分项，不再从完成结果中倒减。所有金额非负，现金运营成本严格等于人工、加班、行程和外包分项之和。
- `PUBLICATION_REMAINING_PLAN` 是发布时点之后的一次性日内范围，强制 `horizon.days=1`。`PAID_SHIFT` 同时列出完整日已承诺人工和剩余增量人工；没有剩余任务的技师不再计入增量成本。容量分析复用相同口径。
- 容量默认采用 `SELECTED_PLAN_DELTA + TAIL_APPEND_ONLY`：当前 V 的 assignment 固定，只在路线尾部安置原未服务需求。新增技师使用显式或保守 archetype，补技能以未服务需求为目标。可选受控重算时，参照和方案使用相同确定性政策。没有供应商容量承诺时，外包只能返回 `EXTERNAL_CONDITIONAL` 和条件上界，正式可执行性、KPI 与经济建议保持为空。
- 每个容量反事实都检查工单唯一性与覆盖、技能、客户窗口、旅行连续性、服务时长、锁定、固定 assignment、真实返程和加班上限。外包选项保存显式 `ExternalAssignment`、逐工单 disposition、外部 SLA 假设和统一 KPI。新增技师与外包成本分别选择工资/固定费和成本政策/容量政策来源，不能重复计费。`option_applicable` 与 `schedule_feasible` 分开返回；违规方案的正式 KPI 和经济建议为 null，只保留诊断指标。每个带哈希 Artifact 自包含 decision status、正式结果可用性、结构校验、商业验证、条件假设和条件上界。
- “调整出发点”只表示将一名高行程技师的出发点移至需求中心，不声称新增了完整服务站点。
- 风险默认遵循正式方案开始时刻。replan 路线从发布时 route entry 开始。随机量按 `(seed, trial, event_type, entity_id)` 派生；计划无关的场景集保存突发事件时间、位置、时长、技能和 SLA。请求显式选择紧急位置来自活动需求、完整冻结地点代理或均匀服务区；尚未配置经验分布时拒绝该选项。`BETWEEN_VISITS_ONLY` 不在行驶途中改道、不抢占服务。响应策略为 `MYOPIC_EARLIEST_EMERGENCY_FINISH`：先保存事件信息集、候选、排除理由和确定性预计完成时间，选中响应者后才模拟一次未来结果；未来 no-show、服务时长和返程延误不能反向改变选择。
- `published_commitment_sla_rate` 只度量已发布承诺，`all_demand_sla_rate` 把应急需求纳入分母。迟到分钟也分别输出 published/all-demand p50、p90、p95，并单列紧急工单迟到分布。旧 late alias 只作兼容映射。条件紧急指标返回事件和完成样本数；零事件时完成率、准时率、未服务率和严重度均为 null，界面显示“不适用”。
- 紧急影响分别统计 disposition 改变、新增未服务、新增迟到、迟到加重及其去重并集。Risk V7 还保存工单级窗口违反、技师级加班突破与分钟数；紧急窗口/加班归因使用集合差和恶化集合，不再依赖全局布尔翻转。`SUMMARY_ONLY` Artifact 只保存紧凑 `PairedTrialVector`，选人证据只保留实际发生紧急事件的 trial；显式 FULL_TRIAL_DETAIL 最多 1000 次。单方案 SLA Monte Carlo 均值区间使用固定 seed 的 percentile bootstrap。
- 配对比较在创建子 A 前核对范围、分析时点和共同 ScenarioSet 身份。紧急完成与准时只对发生事件的 trial 计算，同时保留无条件影响；每个摘要带 conditioning event 和有效样本数，区间使用固定 seed 的配对 bootstrap。条件事件样本少于 20 时标记 `INSUFFICIENT_EVENT_TRIALS` 并不给出数值估计或区间。它绑定两个 A 的清单以及 trial、scenario-set Artifact 哈希；任一证明失效时整个业务 `result` 及兼容数值投影均为空。
- Risk Comparison 使用持久 Saga 保存 RESERVED、两个子 A 完成、比较完成和失败阶段；同一幂等键会复用已经完成且证明有效的子 A，从失败阶段继续。
- A 的状态、开始/结束时间和 reservation manifest 同时保存在关系型列；终态触发器禁止回退。A 终态保存 input/result/failure manifest、Artifact 哈希清单和总清单哈希。完成事务会重新加载父 Plan；计算期间父证明变化时保存失败 A，不保存结果。所有读取和重放路径都会重算；父 Plan、父 A 或依赖 Artifact 失败会向下传播 `effective_integrity=FAILED`，业务 result 被移除。Schema v20 前的记录由关系列显式标记 `LEGACY_MIGRATED`，不能仅靠删除 JSON 字段降级。
- 运营复盘初次进入只读取已有 A；用户显式点击才创建。成本与风险并行使用部分成功语义，任一失败不隐藏另一项结果。同步直接分析接口保留兼容但在 OpenAPI 标记 deprecated。

## 兼容与迁移

- Schema v10–v23 保留原有成本、方案、分析、适用性、修订链头和 PlanningReservation 演进；v24 增加 Emergency Intake Receipt；v25 增加 Run/Candidate/Command/Restore 清单；v26 增加 Runtime Job、Outbox 和 Risk Comparison Saga；v27 增加内容寻址 Artifact Blob。
- 应用启动只初始化空数据库，不隐式升级已有 schema。`fieldflow migrate dry-run/apply/verify/restore` 在临时副本验证、备份并原子替换。大 Artifact 使用内容哈希和 zlib 压缩去重；维护命令只能清理超过保留期且没有关系引用的 Blob。
- Python 生产与开发依赖分别使用带 SHA-256 的 `requirements-runtime.lock` 和 `requirements-dev.lock`，安装强制 `--require-hashes`。决策运行时清单 V2 只绑定生产 lock，并保存实际 distribution inventory。当前支持 Python 3.11–3.13、Linux/macOS；CI 覆盖 Ubuntu 3.11/3.12 与 macOS 3.12。`docs/sbom-runtime.cdx.json` 由锁文件确定性生成并在验证门禁检查。
- v1 旧方案历史缺少完整业务快照，按已确认的产品决定先生成时间戳备份再重建，当前业务数据保留。
- 重建前，旧 schedules、plans、artifacts、experiments 和 publication keys 会逐行复制到 quarantine ledger；列表读取遇到损坏记录时跳过坏行并登记，场景、Plan、A、Artifact、ScheduleRun、Candidate、Experiment 和执行事件的关键单项读取返回稳定完整性错误。`GET /api/integrity-issues` 只暴露记录来源和原因，不返回原始业务 payload。
- 旧 `/schedules` 和同步经营分析接口继续兼容；同步分析接口内部也创建或复用持久 A，不再绕过审计。

## 非目标

- 在线地图、云服务、手机端技师应用、财务结算。
- Crew 同时到场、零件库存、跨规划日排班和完整多站点库存模型。
- 执行水位下的 incurred/remaining/actual-plus-forecast 分析；支持前必须保持拒绝门禁。
- 云端多租户、在线地图和远程 SaaS 控制面。
- 未经仓库所有者选择，擅自添加许可证或修改 GitHub 治理规则。

## 验收

- 连续成功发布严格得到 V001、V002、V003；失败、候选和 A 记录不占 V。
- 锁定、技能、时间窗、班次、返程、执行中冻结和覆盖完整性由发布验证器检查。
- `reported_at` 在贪心和 OR-Tools 路径都生效；固定 seed 返回确定结果。
- 决策分析绑定选中 V 的发布上下文、完整排程、行程、政策、算法和构建来源；显式事前分析允许在执行后复核，当前执行事件变化不能改变历史输入。
- 容量中所有 `feasible` 结果通过完整反事实验证；成本周期逐项对账；风险遵循发布时间并使用准确统计标签。
- 紧急命令中断恢复、人工改派三个持久阶段崩溃、PlanVersion payload 不变和历史稳定性谱系有回归测试。
- Schema v1–v26 均可通过显式 CLI 迁移到 v27，并通过链头、完整性、Artifact Blob 保全、关系型终态、适用性和外键检查。
- Legacy Plan 不能执行或创建 A；重新认证只生成新的 V2 方案且不修改旧 V。Plan、A、Artifact、RiskComparison 任一父依赖篡改都必须传播失败。
- Ruff、Pyright、ESLint、React Hooks、TypeScript、依赖审计、OpenAPI 快照及生成类型、RuleBasedStateMachine、安全 mutation score、后端与 React 测试、生产构建、Demo、性能趋势和 Playwright 主流程通过后才能交付。
