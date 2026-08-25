# `pro-plan.md` 复核记录（v0.5.10）

复核对象是基于 `7489526` 的更新版审查稿。结论来自实际调用路径、SQLite 迁移、API/前端契约和本地回归，不把静态审查中的推测直接当成事实。

## 发布门禁

| 项目 | 裁决 | 处理结果 |
| --- | --- | --- |
| P0-01 来源 Plan 与活动 Plan 未原子绑定 | 成立，已修复 | 新增持久 `PlanningReservation`。一次 `BEGIN IMMEDIATE` 冻结并验证 Scenario、活动 Plan、来源 Plan、执行水位/上下文，同时创建 ScheduleRun。Run 与 Candidate 绑定 reservation ID/hash；发布前再次核对当前 D、活动/来源 V、manifest 和执行上下文。activate、restore、reattest、实验发布和人工改派也携带活动 V 前置条件。使用 `Event` 精确控制的慢重排/人工改派竞态证明陈旧结果不能发布且不占 V。 |
| P0-02 当前 Scenario 未锚定修订链头 | 成立，已修复 | Schema v23 为 `scenarios` 增加当前快照 hash、最新 D 编号/hash 和 proof origin。所有业务读取通过同一链头门禁；payload-only 修改会阻止 GET、编辑、baseline、replan、发布和分析，不能被下一次编辑“洗入”历史。 |
| P0-03 失效 assignment 仍算当前已排 | 成立，已修复 | 新增 `/operational-view`，后端为每张工单派生唯一 disposition。KPI、队列、地图、时间轴、详情和开工按钮均消费同一结果；失效分配单列、优先显示并禁止开工，已完成工单不进入规划风险队列。 |
| P0-04 发布冲突退化成字符串 | 成立，已修复 | `PublicationConflict` 统一转换为结构化 409；补充 `ErrorDetail`、API/前端测试和 AST 规则，阻止相关异常处理器重新出现 `HTTPException(409, str(error))`。前端 `ApiError` 保留 code 与 expected/current 诊断。 |

## P1 逐项裁决

| ID | 裁决 | 当前结论 |
| --- | --- | --- |
| P1-01 | 成立，已修复 | 可信事件加载器在校验业务内容、关系身份和来源 Plan 后强制重写 self/source/effective integrity，payload 修改这些标签不会影响 API 输出。 |
| P1-02 | 成立，已修复 | 人工改派终态重放只从命令记录读取资源引用，再加载当前 Scenario 和已验证 Plan 重建结果；不再保存或信任完整结果副本。失败终态也重放同一结构化 detail。 |
| P1-03 | 成立，已修复 | 原生 revision 写入只验证场景链头和最新节点，为 O(1)。全链校验移到启动完整性扫描。 |
| P1-04 | 成立，已修复 | D000 是唯一根，编号必须连续。根错误、缺口和损坏后代分别标记 `ROOT_INVALID`、`GAP_DETECTED`、`ANCESTOR_INVALID`，后代不会成为第二个可信根。 |
| P1-05 | 成立，已修复 | revision 与 scenario head 保存 `NATIVE_ATTESTED`、`MIGRATION_BACKFILLED` 或 `LEGACY_UNATTESTED`。迁移回填不再冒充原生写时证明。 |
| P1-06 | 成立，已修复 | 风险请求显式选择 `ACTIVE_DEMAND_LOCATIONS`、`ALL_FROZEN_LOCATIONS_AS_SPATIAL_PROXY` 或 `UNIFORM_SERVICE_AREA`；策略、候选位置 ID 和结果进入 ScenarioSet、输入 hash、Artifact、OpenAPI、界面和指标文档。未配置经验分布时拒绝该选项。 |
| P1-07 | 成立，已修复 | V6 分开输出 published-work、all-demand 和 emergency late 指标。旧 late 字段保留为 all-demand 兼容 alias，并在指标文档写明预计 v0.7 移除。 |
| P1-08 | 成立，已修复 | 试验 outcome 区分 disposition、迟到分钟与技师；分别统计 disposition 改变、新增未服务、新增迟到、迟到加重及去重并集。 |
| P1-09 | 成立，未伪装完成 | 5000-trial 风险仍是同步 CPU 任务，35 秒客户端超时与不可续跑问题需要持久 Job Queue 和子进程。该项属于审查稿自己的 v0.6 范围；本轮没有用不可恢复的后台线程冒充持久任务。 |
| P1-10 | 成立，部分收敛 | 默认 `SUMMARY_ONLY` 不再为每个 trial 保存工单明细；显式 `FULL_TRIAL_DETAIL` 上限 1000，先控制数据库增长。内容寻址压缩 Blob、导出和 prune 需要独立存储迁移及维护 CLI，保留到 v0.6。 |
| P1-11 | 成立，已修复当前误伤 | 拆分 runtime/dev lock。DecisionRuntimeManifest V2 只绑定 runtime lock，并保存锁中每个生产 distribution 的实际安装版本、Python、SQLite、OR-Tools、Pydantic、OS 和架构；Ruff、Pytest、Pyright 等工具升级不再改变精确 retry 身份。wheel hash 属于 P1-13 的剩余供应链工作。 |
| P1-12 | 成立，已修复实际误伤 | 没有先大拆 `models.py`；决策 build SHA 通过 AST 计算决策文件直接导入并递归引用的模型定义闭包。测试证明 RiskSimulationResult/PlanVersion 被包含，而无关 PlanVersionPatch 不改变闭包。模块物理拆分仍与 P1-20 一起渐进进行。 |
| P1-13 | 成立，部分完成 | 选择明确支持 Linux/macOS：`uvloop` 使用平台 marker，包元数据写明平台，CI 新增 macOS Python 3.12 后端全量测试。lock 仍没有 wheel hash；在未建立分平台解析和 hash 更新流程前不生成看似安全但不可维护的伪 lock，该部分列入 v0.6。 |
| P1-14 | 成立，持续收敛 | OpenAPI snapshot 与生成 TypeScript 继续由 CI 防漂移；Point、Technician、WorkOrder、ExecutionEvent、ManualReassignmentResult 和 Operational View 直接引用生成 schema，展示层只覆盖运行时已标准化的必填性。Schedule/Plan/分析大对象仍有兼容 view model，全面 adapter 化列入模块拆分。 |
| P1-15 | 成立，需独立迁移工具 | v23 保持升级前时间戳备份、约束修复和 foreign-key check，并修复了 v20 坏 JSON 必须先清洗再计算 proof 的顺序。临时数据库重建、dry-run、原子文件替换和 restore CLI 属于 v0.6；普通启动迁移尚不能宣称进程崩溃原子。 |
| P1-16 | 成立，需持久 Saga | 风险比较的子分析仍各自可审计，但没有可恢复的 Comparison Job。它依赖 P1-09 的持久任务运行时，不能靠新增空状态表解决。 |
| P1-17 | 成立，已修复 | PlanApplicability 保存 evaluated D、场景快照 hash、reducer policy 与 projection hash；active projection 必须与 Scenario 链头一致，否则 Operational View 和 Plan 使用失败关闭。 |
| P1-18 | 成立，文档已校正 | 继续使用 `benchmark-smoke` 和 `mutation-smoke` 名称；README、spec 和任务记录不把它们描述成性能趋势系统或系统性 mutation score。 |
| P1-19 | 成立，文档已校正 | 统一使用“内容一致性证明和篡改迹象检测”。同库 hash 不宣称抵抗能够同步重算内容和证明的恶意写入者。 |
| P1-20 | 成立，暂不做大搬迁 | 四个核心模块仍偏大，也确实增加误伤概率。本轮先关闭事务/信任缺口并缩小源码指纹；在没有稳定 application/infrastructure 边界前大规模搬文件会放大回归面。模块拆分与 v0.6 Job/存储边界共同实施。 |

P1-09、P1-15、P1-16 和 P1-20 不是“不合理”，但都需要新的持久运行时或迁移架构。P1-10、P1-13、P1-14 已先关闭当前可独立验证的风险，剩余部分没有写成已完成。

## 产品与领域建议

| 方向 | 结论 |
| --- | --- |
| Booking / Visit 正式实体 | 合理。当前 booking ID 和冻结身份只建立执行证明，不支持一单多次上门；完整聚合会改变排程变量、事件模型和迁移，按审查稿列入 v0.7。 |
| 技师执行端与离线同步 | 业务上合理，但原项目范围明确排除手机端；若未来启用局域网/移动端，幂等事件同步和身份认证必须一起实现。 |
| 工单分诊 | 合理，适合在 Visit 之前增加；本轮不能用更多 WorkOrder status 绕过现有执行事件状态机。 |
| Customer / Site / Asset / Contract | 合理，需要独立实体和引用迁移；当前重复文本 fixture 不应被描述为客户主数据。 |
| 周期维护 | 合理，但依赖多日日历和未来需求生成，属于 v0.7/v0.8。 |
| 零件和库存 | 合理，现阶段仍明确 unsupported；不以 note 或 skill 字符串模拟库存承诺。 |
| 外部供应商确认 | 合理。在 Provider 承诺建立前，外包继续只返回 `EXTERNAL_CONDITIONAL`，不会升级为正式可执行结果。 |
| 真实旅行模型 | 合理。当前 provider 是静态欧氏/矩阵模型，departure minute 仅为接口兼容；界面和报告不声称实时路况。 |
| 历史风险校准 | 合理。当前概率是显式输入假设，不声称来自实际历史估计。 |
| 多日滚动规划 | 合理。当前剩余计划严格限制为一次性日内范围，多日成本不冒充不同日期需求预测。 |
| 实际经营核算 | 合理，但当前数值只是决策假设比较，不是财务结算。 |
| 身份、角色和操作者审计 | 多人部署前必须完成；当前系统保持本机单用户边界。 |

## 未随本次代码提交执行的仓库治理

LICENSE 需要仓库所有者选择 MIT 或 Apache-2.0；分支保护、required checks、签名 tag/Release 和可见性设置属于 GitHub 仓库治理，不由一次代码提交授权自动决定。CI、依赖审计和生成契约门禁继续保留。

## v0.5.10 验收映射

- FF-1701：PlanningReservation、Candidate/Run 绑定、发布复核、活动 V 前置条件及确定性竞态测试。
- FF-1702：Schema v23 Scenario 链头、统一读取门禁、篡改隔离与不可洗入测试。
- FF-1703：Operational View 与六类 disposition 驱动全部主界面区域。
- FF-1704：结构化冲突 DTO、统一异常转换、AST 防回归和前端 details 测试。
- FF-1705：执行 trust label 重算、人工改派资源重放和终态一致性。
- FF-1706：D000/连续链、proof origin、invalid descendant 和 O(1) 写入。
- FF-1707：Risk V6 位置政策、迟到拆分、工单 outcome、条件样本和 Artifact 详情策略。

完整验收首先由 [GitHub Actions #50](https://github.com/frankyhitsz/fieldflow-local/actions/runs/32873627146) 完成：Ubuntu Python 3.11、Ubuntu Python 3.12 全流程和 macOS Python 3.12 三个任务全部通过。后端 271 项、覆盖率 88.45%，8/8 mutation smoke、React 15 项、Playwright 5/5，静态检查、runtime/dev/npm 依赖审计、构建、Demo 和 Benchmark 同步通过。实现提交为 `f9b8c40`。

文档提交后的 Actions #51 暴露一个真实的时序问题：成本和风险请求虽然并发发起，界面仍等到较慢的风险模拟结束才显示已完成的成本结果。现已改为每项完成即独立更新，并新增受控延迟风险请求的 React 回归测试；当前前端测试为 16 项。
