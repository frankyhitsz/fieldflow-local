# 2026-09-07 审查处理记录

起点：`35d3d6b41e3d193819a93937f8ea133d3310b27d`，v0.5.11 / schema 27。

实现终点：`2fe12586ad353aebd5811eb94a4edf5e8ca052bf`，v0.5.12 / schema 28。后端/存储提交 `c78ddec`，前端提交 `60a4189`，发行与性能提交 `2fe1258`。

本记录区分静态判断、隔离机制实验和仓库实测。只有完成回归并取得输出的条目才标为关闭。迁移使用历史提交生成的临时数据库，不修改仓库根目录的用户数据库。

目标：v0.5.12 / schema 28。下面列的是当前实现与已有证据；最终门禁和远端提交状态记录在文末。

## 逐项判断

| 项目 | 裁决及处理 | 回归证据 |
| --- | --- | --- |
| F01 | 成立。V6 风险证据采用原哈希投影，排除后来新增的默认字段；没有重签旧证据。 | `test_real_legacy_artifacts_and_comparison_survive_upgrade`，真实 v0.5.10/v0.5.11 库 |
| F02 | 成立，危险前提是在线替换或直接复制主库。API、协调器、Worker、CLI 共用每库文件锁；恢复也用 SQLite backup 读取 WAL。 | `test_restore_preserves_committed_wal_rows`、跨进程维护拒绝测试 |
| F03 | 成立。分别验证 SQLite、schema 与领域证明；备份名加入微秒和随机后缀；替换前后 fsync，并留下包含候选 hash、旧 hash 和备份位置的日志。 | `test_domain_verify_detects_corruption_with_valid_sqlite`、同秒备份唯一性、独立安装恢复流程 |
| F04 | 成立。实验命令键与语义指纹分开；同一键重放原结果，不同命令可以再次运行相同输入。 | `test_identical_experiment_can_run_twice_and_cancelled_can_rerun` |
| F05 | 成立。API 只持久入队；Dispatcher 获得工作槽后交接给 Worker，确认 Outbox 失败不重复释放。 | `test_submit_success_ack_failure_releases_slot_exactly_once`、`test_spawn_start_failure_finishes_job_and_next_job_runs`、实验队列容量测试 |
| F06 | 成立。一个 Receipt 可以有多个重排命令；首次消费关系保留，后续命令各自保存准确 Plan/Job 结果。 | `test_failed_emergency_replan_can_use_new_command_and_replay_after_note` |
| F07 | 成立。成功命令先从可信终态资源重放，不重新拿当前备注/状态与接收快照做完整 hash 比较。 | 上述备注重放测试、`test_emergency_command_replays_after_emergency_start`、旧执行命令重放回归 |
| F08 | 成立。发布返回准确的 PlanVersion，后续命令、Receipt 和 Job 不再重查活动指针来决定本次结果。 | `test_other_plan_published_before_ack_cannot_change_command_result`，受控交错中第一请求仍返回 V001，第二请求为 V002 |
| F09 | 成立，作为发布阻断处理。正式发布与 Job 完成在同一写事务决定；取消胜出时不能发布。 | `test_cancel_and_publish_have_one_transaction_winner` 的提交前/提交后两种 Event 交错 |
| F10 | 成立。新增持续 Dispatcher、到期租约回收、重派上限和数据库入队上限；短任务保留工作槽，等待 30 秒的任务提升优先级。 | 持久重启恢复、代际回收、容量测试、`test_waiting_long_job_is_promoted_without_losing_short_job_priority` |
| F11 | 条件成立。普通 Store 打开不恢复任务；启动恢复必须获得每库协调器锁，第二服务不能中断现存所有者。 | `test_child_store_does_not_run_application_restart_recovery`、协调器/维护锁回归 |
| F12 | 成立。owner、generation、有效租约与取消状态共同控制心跳、A/Run/Candidate 写入、实验保存和 Plan 提交。 | `test_same_owner_old_generation_cannot_heartbeat_finish_or_commit` |
| F13 | 成立。A 预留与 JobAttempt 绑定同事务；进程启动也位于异常收束范围；停止采用 terminate→有界 join→kill。 | `test_analysis_binding_survives_crash_before_ipc_and_cancel_interrupts_owner`、真实 spawn 启动失败测试 |
| F14 | 成立。跟随者等待同一 A 的终态，不把 RUNNING 当成失败，也不取得原计算的中断权限。 | `test_follower_job_waits_for_running_analysis_without_interrupting_owner` |
| F15 | 成立。实验取消入口适配 Job 的同一取消事务；实验终态与 Job 终态同时提交，普通 save 不能改写终态。 | 真实风险子进程取消、实验排队取消、提交取消竞态 |
| F16 | 成立。新增 LOADING/READY/STALE/FAILED 状态；最多三次一致快照核对，失败后只读、可重试，不显示“无风险”。 | React 失联后只读、同 D 不同 V 拒绝、未知地图不画可执行路线测试 |
| F17 | 成立，但后端 Complete 规则原已正确。前端改读可信 ActiveBookingView，移除当前 assignment 依赖。 | `can complete a trusted active booking without any active plan assignment` |
| F18 | 成立。产品 Plan 请求改用 v2，显式发送活动 V；基线/优化/重排还发送 D 的 If-Match，其余变更请求携带 expected_revision。 | `sends explicit D and V preconditions to the v2 plan endpoint`、现有 v2/428/CAS 回归 |
| F19 | 成立。新增任务中心，离页中止本地轮询但不取消任务；从服务端续接。精确 retry / 当前重跑加入异步入口。 | JobCenter 重挂载与取消/准确结果测试、AbortSignal 与 StrictMode 回归 |
| F20 | 成立。默认比较使用 baseline 自己的 Scenario；公开比较还检查范围、截点、水位及指标政策，不可比时不展示改善百分比。首页差值直接读取后端 Comparison DTO。 | 跨快照/截点比较回归；React 用相同客户端元数据、不同后端 comparable 结果验证不得自行推断改善 |
| F21 | 成立。运行指纹只包含运行依赖，不包含开发依赖或工具设置；加入 normalization、execution 和实际分析执行函数，并检查决策模块的本地依赖闭包。 | `test_development_configuration_does_not_change_runtime_dependency_hash`、三个实际源码变更测试 |
| F22 | 条件成立。欧氏参数和矩阵均序列化到子进程，保留数值类型及指纹；不再按 Provider 类别退回不可取消的同步计算。紧急重排也保存入队配置，不支持的 Provider 在入队前返回 422。 | 两类 Provider roundtrip、真实矩阵首段开工/重排、`test_emergency_queue_freezes_provider_and_rejects_changes_before_execution` |
| F23 | 成立。GC 的筛选与删除在同一 IMMEDIATE 事务，并用数据库触发器保护 Blob 引用。 | `test_blob_prune_keeps_referenced_rows`、`test_prune_racing_new_reference_keeps_blob`：独立连接持有未提交引用，GC 在写锁处等待，提交后保留 Blob |
| F24 | 成立。写入复核与读取共用有界解码；先检查长度、流结束与尾随数据，再解析 JSON；增加深度/结构上限。 | `test_store_and_read_share_blob_limits` |
| F25 | 成立。Store open 去掉全库工作；历史核验按数据库代际缓存且外部变更使其失效；三个列表分页。 | checkpoint 外部篡改、worker open 测试；10/1000/10000 修订基准 |
| F26 | 成立。只有明确旧 schema 迁移才能回填缺失命令证明；更新/恢复先验证原记录，当前库缺证明不自动洗成有效。 | `test_current_schema_missing_command_manifest_is_not_backfilled`、命令 quarantine 回归 |
| F27 | 读校验缺口成立，不能称为普通接口可篡改：已有终态触发器会阻止常规 UPDATE。补齐语义字段对照和重启失败清单。 | `test_result_manifest_must_match_run_semantics`：先验证 UPDATE 被拒绝，再移除触发器模拟损坏库并验证加载失败 |
| F28 | 成立。每个 mutant 先跑原版 baseline；仅执行了测试且存在断言失败时计为 KILLED，导入错误、无测试和超时均为 HARNESS_ERROR。 | 8/8 mutant 附 baseline/JUnit/日志；判定器异常类别测试 |
| F29 | 成立的是验收缺口，不是“所有旧迁移测试均无用”。补入两个真实历史提交生成的完整库。 | 见 `tests/fixtures/README.md`，原 Artifact hash 与 E payload 均保持 |
| F30 | 条件成立。采用任务书允许的“正式拒绝多应用并存”方案，不声称已有多租户隔离。第二 app 在修改共享状态前被拒绝。 | `test_second_app_rejected_before_touching_first_jobs` |

## 独立迭代中发现的问题

第一轮定向检查确认 V6 风险 hash 的真实迁移反例，并修复安全维护与 mutation 判定。第二轮全量回归暴露矩阵整数坐标转浮点后改变指纹、人工改派终态与恢复阶段冲突、重启 Run 留下旧结果清单，均补回归后修正。

第三轮增加真实进程、损坏库、前端卸载和独立安装验证。发现策略实验轮询在连续 RUNNING 状态时停止，以及 StrictMode effect 重放提前中止轮询控制器；两项均已修复。错误透传也统一保留完整性问题的 409，而不是被后台任务转成 500。

完整门禁还发现新 Blob 引用触发器会阻碍 schema 1–15 的旧表重建。迁移现先在备份后移除该组触发器，完成表迁移再重建；24 项历史/维护回归和随后 348 项全量回归通过。收尾检查继续补齐紧急重排冻结模型、KPI 使用服务端比较结果和构建脚本精确选择本次产物，避免目录中的旧包干扰验收。

## 任务包与取舍

FF-1901–FF-1908 对应上表的历史兼容、维护、可信测试、实验、Receipt、取消和任务运行时。故障交错使用测试中对事务入口、进程启动器和确认入口的 Event/替换注入，不给正式接口添加调试断点。可原子提交的 Plan/command/Receipt/Job 在同一事务完成，因此没有再引入一套重复的 COMMITTED_PENDING_ACK 状态。

FF-1909–FF-1911 覆盖前端、比较和证明边界。保留已验证的后端 Complete 规则，修复前端遗漏；对 F27、F29 的风险描述作了限定，而不是把所有静态怀疑都称为已发生的安全漏洞。短事务历史操作没有影子 Job；已有同步分析兼容入口及配对比较 Saga 未被描述为全部异步化。

FF-1912 已落实缓存失效、列表分页、多档实测和保留政策。缓存命中不是增量验证所有后续写入：其他连接写库后，首次读取仍需重新核验历史链。当前只对 warm dispatch 的 SQL 数不随历史增长设硬门禁；本机毫秒数不是跨平台时延门槛。Outbox 采用保留政策，未提供自动归档/删除机制，也没有以 Blob GC 冒充完整的数据生命周期管理。

FF-1913 的独立发行包、构建锁、SBOM、恢复演练和本机边界已验证。分支保护、required checks 的仓库设置、Release 发布和 LICENSE 选择没有代替所有者操作。FF-1914 按任务书列为后续产品路线，不计作本维护版缺陷修复。

## 迁移与运行边界

- 真实 schema 23/27 固件各有 2 个 V、6 个 A、14 个 Artifact、1 个配对比较和 3 个 E；含一单完成、一单服务中。通过 CLI 完成 dry-run、apply、verify、恢复迁移前备份、再次 apply/verify；最终保留原 Artifact hash、原 E payload 和有效依赖关系，两次迁移产生不同备份。
- 恢复演练在 WAL 尚有已提交数据时执行，恢复库保留该行；另一个进程持有应用访问锁时，维护 CLI 返回 `DATABASE_IN_USE`。用户的根目录 `fieldflow.db` 没有参与测试或被替换。
- 每库一个协调器、每进程一个应用；不支持多个 Uvicorn worker，也不把本机单用户服务当成已具备局域网身份认证。
- warm dispatch 的 p95 从 10 条历史的 0.992 ms 到 10,000 条的 1.201 ms，均为 5 条被跟踪的业务 SQL。首次整链核验分别约 2.4/100.6/1041.7 ms；这些数字是本机测量，不是所有设备的性能保证。
- 正式业务/执行/分析/命令/Job 历史保留，不自动清除；Blob GC 只清除无引用对象。Outbox 已派发记录也保留，当前没有自动归档或删历史操作，避免把空间治理变成丢审计记录。见规格中的保留政策。
- FF-1913 已用 wheel 与 sdist 在独立安装目录验证 UI、CLI、清单及完整服务链；构建工具链独立锁定。未创建 GitHub Release、改变分支保护或代选 LICENSE，这些需要仓库所有者单独决定。
- FF-1914 按任务书明确不并入维护版。多次上门、缺件、库存和资产域不能用占位字段冒充闭环。

## 验证记录

首次完整 `make verify` 已通过：348 项后端、覆盖率 87.14%、8/8 mutation、24 项 React、7 项 Playwright，以及锁文件、静态检查、生成契约、依赖审计、构建、Demo、历史基准、wheel/sdist 独立安装服务链。此前的失败回归保留为修复过程，不用“定向测试通过”替代完整门禁。

收尾补充验证：22 项运行时、15 项存储、15 项旧库/维护回归通过；26 项 React 连续五轮通过，7 项 Playwright 再次通过。原有执行时间测试改为等待快照核验后按钮可用，未绕过页面门禁。静态检查、构建、Demo、性能及发行包门禁均复验通过。

冻结源码的 `make test`：352 passed，覆盖率 87.36%，退出码 0，耗时 423.54 秒。最后扩充的旧库 CLI 恢复再升级流程另跑 15 项，全部通过。

最终验证命令的退出码均为 0：`make lock-check`、`make lint`、`make audit`、`make test`、`make mutation-test`、`make test-frontend`、`make build`、`make demo-check`、`make benchmark`、`make history-benchmark`、`make distribution-check`、`make test-e2e`。本地日志分别在 `/tmp/fieldflow-sept7-final-352.log`、`/tmp/fieldflow-sept7-final-gates.log`、`/tmp/fieldflow-sept7-react-repeat.log` 和 `/tmp/fieldflow-sept7-restore-cycle.log`；提交后的 CI 为干净检出提供复验。

远端验证见 [实现提交的 GitHub Actions #72](https://github.com/frankyhitsz/fieldflow-local/actions/runs/34087295579)，覆盖 Ubuntu Python 3.11、Ubuntu Python 3.12 完整流程和 macOS Python 3.12。结果与失败输出以该运行记录为准，本地验证不代替远端检查。后续文档提交的检查可在 [Actions 列表](https://github.com/frankyhitsz/fieldflow-local/actions/workflows/ci.yml) 查看。

构建依赖锁另经 `pip-audit -r requirements-build.lock` 检查，退出码 0，未发现已知漏洞。
