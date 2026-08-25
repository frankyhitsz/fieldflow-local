# `pro-plan.md` 复核记录（v0.5.9）

复核对象是 2026-08-25 19:49 更新的审查稿，基线提交为 `5de982f`。结论来自当前代码、SQLite 关系数据、API/浏览器流程和可复现测试，不沿用审查稿对运行环境的假设。

## P0 发布门禁

| 项目 | 裁决 | 处理 |
| --- | --- | --- |
| P0-01 执行事件信任闭包 | 成立，已修复 | 重排、发布复核、列表、完成/前序消费和幂等重放统一调用可信事件加载器。加载器核对关系列、内容哈希、来源 Plan、来源 assignment、连续水位和 start/complete Booking 链。命令重放只信资源 ID，不信缓存结果副本。 |
| P0-02 等待客户后的应急返回行程 | 成立，已修复 | 候选投影从应急位置重新计算到下一客户的行程。零随机回归逐 trial 核对候选终点与选中响应者实际终点。 |
| P0-03 同 D 下旧求解覆盖新 V | 成立，已修复 | ScheduleRun 和 Candidate 冻结求解开始时的活动 Plan ID；发布事务同时 CAS D 与活动 V。并发候选只有一个可发布，冲突不分配 V。 |
| P0-04 新需求从风险工作区消失 | 成立，已修复 | 网页按当前 Scenario 与冻结 Plan 的 disposition 差集计算未覆盖需求。风险队列、位置图、当前待排数和当前需求已排率都包含新需求；冻结 SLA 明确标注口径。 |
| P0-05 风险重复计数和零样本 | 成立，已修复 | 受影响工单改为逐工单迟到/未服务 disposition 差异的集合计数；零紧急样本的条件失败概率返回 null；单 Plan 均值区间改为固定 seed 的 percentile bootstrap。 |

上述五项均有定向回归。执行事件内容、关系列、命令副本、资源缺失、应用重启重放、活动 V 并发和修订源被改写时都采用失败关闭。

## P1 逐项裁决

| ID | 裁决 | 当前结论 |
| --- | --- | --- |
| P1-01 | 成立，已修复 | ScenarioRevision 增加 snapshot/previous/revision hash，读取时核对关系身份和整链；reset 不再读取未验证的第一行，启动扫描会登记损坏证明。Schema 升至 v22。 |
| P1-02 | 成立，已修复 | 同 P0-01；`command_keys.payload` 不再是执行重放来源。 |
| P1-03 | 成立，已修复 | 启动扫描逐条调用可信事件加载器，不再只检查 action 行是否存在。 |
| P1-04 | 成立，已修复 | 比较及回滚预览覆盖 disposition、技师、顺序、arrival/start/finish、travel、locked、source sequence/hash，并返回 schedule/source schedule identity。 |
| P1-05 | 成立，已修复 | 原始 objective 只在场景快照和 SolverPolicy fingerprint 都一致时返回；两个名称同为 `custom` 不构成可比依据。 |
| P1-06 | 成立，已修复 | 无效或条件选项的正式 KPI、成本和经济建议为空；结果只留 `diagnostic_*`，未验证外包只放在 `conditional_upper_bound_kpis`。 |
| P1-07 | 成立，已修复 | 删除 `EMPTY_CANDIDATE` 这一错误捷径。覆盖完整、每单均有未分配原因的零路线方案可以作为有效诊断候选；缺失 disposition 仍由 `MISSING_WORK_ORDER` 拒绝。 |
| P1-08 | 成立，已收敛信任源 | 多轴 PlanApplicability 是读取时唯一事实；`coverage_status` 列暂留作旧 SQL 客户端的派生缓存，不再参与可信判定。移除列要重建表，收益不足以抵消本地数据库迁移风险。 |
| P1-09 | 成立，已修复 | 采用审查稿的“安全前缀继续执行”：只禁用 `invalid_assignment_ids`，文案不再声称整份 Plan 都不可执行。 |
| P1-10 | 成立，未伪装完成 | SQLite 关闭外键并重建关系表需要事务边界；当前每次升级先做不可覆盖的时间戳备份，重建脚本自身使用 `BEGIN IMMEDIATE`。完整维护模式、dry-run 和崩溃注入属于独立迁移 CLI，不能在本轮用删除 `commit` 冒充原子迁移。 |
| P1-11 | 与已确认产品决策一致，不作为缺陷 | v1 历史没有可恢复业务快照。项目此前明确选择“备份后清空旧方案编号”，迁移同时保存原数据库和 orphan 行；改为保留不可验证的正式历史会违背已确认默认项。 |
| P1-12 | 成立，已修复 | 新增解析后完整 Python 传递依赖锁；setup/CI 先安装 lock，再 `--no-deps` 安装项目。GitHub 的最低版本门禁进一步发现并修正了 Python 3.12+ 的 NumPy 锁定值，当前锁可由 Python 3.11–3.13 共用。RuntimeManifest 绑定 lock 内容。 |
| P1-13 | 成立，已修复 | Decision build SHA 只覆盖 decision、scheduler、verification、travel、planning、hashing、models、timeutils 和 provenance；API 文案或报告改动不阻断精确 retry。 |
| P1-14 | 部分成立 | OpenAPI snapshot 和生成类型继续由 CI 防漂移，WorkOrder、Technician、ExecutionEvent 等核心契约已直接引用生成类型。把所有 UI view model 一次改成生成结构会把展示状态与线格式耦合，当前不为追求“零手写类型”进行高风险机械重写。 |
| P1-15 | 成立，已修复 | 新建 WorkOrder/Technician 的外部 code 改为 URL 安全 ASCII：首字符字母或数字，后续允许字母、数字、`.`、`_`、`~`、`-`；存量领域模型继续读取旧 ID，避免升级后丢失本地数据。 |
| P1-16 | 成立，需持久运行时 | 风险比较目前会先完成 A 再保存比较清单，一侧失败会留下可审计的独立 A。要实现原子父 Saga，需要先有持久 Job Queue/租约；本轮不增加无 worker 的状态壳。 |
| P1-17 | 成立，需持久运行时 | 同步求解和进程内线程池只适合当前本地单用户范围。跨进程 worker、租约和资源配额列入 v0.6，不在请求线程外再套一层不可恢复线程。 |
| P1-18 | 成立，需 Outbox | 紧急接单已有可恢复 command/Saga 状态，但没有通用 Outbox。Outbox 必须与持久 worker 一起落地。 |
| P1-19 | 成立，是 OR-Tools 进程内限制 | 取消会阻止继续发布，但不能可靠中断正在运行的 native solve。硬停止需要子进程隔离。 |
| P1-20 | 成立，需保留策略 | 当前 Artifact 对复现是必要证据；直接删快照会破坏清单。内容寻址去重和 retention 需要维护命令及可预览策略。 |
| P1-21 | 成立，已明确威胁模型 | 文档统一称“内容一致性与篡改迹象检测”。同库 hash 不声称抵抗能重算整链的恶意写入者；外部 HMAC/签名不属于离线 MVP。 |
| P1-22 | 事实成立，不是当前缺陷 | 项目书明确使用静态离线二维距离并排除生产交通。接口保留 departure time 是为未来模型兼容，当前报告和 UI 均不声称实时交通。 |
| P1-23 | 成立，已补期限 | v1 CAS 写接口返回 `Deprecation: true`、`Sunset: 2027-03-31` 和 v2 successor Link；v2 继续强制 If-Match。 |
| P1-24 | 成立，已改名实质 | RuleBasedStateMachine 继续覆盖 PlanApplicability；定向 mutation gate 从 4 个扩展为 8 个，新增活动 V CAS、修订证明、应急返回和条件零样本。它仍明确叫 mutation smoke，不冒充全量 mutation testing。 |

P1-10、P1-16 至 P1-20 的共同前置条件是持久运行时或维护工具。审查稿本身把它们排入 M1/v0.6；本轮保留真实边界，没有增加无法工作的占位 API。

## P2 逐项裁决

| ID | 结论 |
| --- | --- |
| P2-01 | 巨型模块属实，模块拆分应随 M1 worker/storage 边界进行；本轮先收窄决策代码指纹，避免无行为收益的大搬迁。 |
| P2-02–P2-04 | Booking/Visit、多次上门和更完整执行状态都合理，但会改变工单聚合、排程变量和历史兼容，属于 v0.7 现场服务模型。 |
| P2-05 | 技师移动端被原项目书明确排除；本地调度台不扩展手机端。 |
| P2-06–P2-08 | 站点资产、周期维护、库存和供应商确认是合理业务扩展，需新实体和流程，不用字符串字段冒充。 |
| P2-09–P2-10 | 历史校准和真正多日滚动预测合理，当前静态 fixture 与重复日成本必须继续明确标注，不宣称预测能力。 |
| P2-11–P2-12 | 登录、角色、隐私和备份生命周期对多人部署必要；当前产品定位严格限于本机单用户，文档需维持此边界。 |
| P2-13 | LICENSE 需要仓库所有者在 MIT/Apache-2.0 中作出授权选择；分支保护、签名 Release 是额外 GitHub 治理操作，本次“提交代码”授权不等同于替所有者选择许可证或修改保护规则。依赖审计已在 CI。 |
| P2-14 | 当前有确定性 Demo check 和 benchmark smoke，但尚不是带硬件基线、历史趋势和业务阈值的正式 Benchmark；该建议成立。 |

## v0.5.9 验收映射

- FF-1601：可信事件加载、事件关系链、资源重放、启动扫描和重启重放已覆盖。
- FF-1602：应急状态投影与零随机实际时间线一致。
- FF-1603：活动 Plan CAS 已进入 Candidate/Run/发布事务和并发测试。
- FF-1604：当前需求 disposition 已进入队列、地图和 KPI。
- FF-1605：唯一受影响工单、null 条件指标和 bootstrap 区间已落地。
- FF-1606：ScenarioRevision v22 证明链已落地。
- FF-1607：容量正式/诊断字段已拆分。
- FF-1608：比较和回滚差异字段已补齐。
- FF-1609：零路线完整诊断不再被空候选规则误杀。
- FF-1610：状态机测试保留，定向 mutation smoke 扩展到 8 个关键不变量。

最终测试数量和 GitHub Actions 链接在 `task.md` 通过后更新；未通过的门禁不会写成完成。
