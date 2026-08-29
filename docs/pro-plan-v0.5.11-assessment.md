# `pro-plan.md` 复核结论（v0.5.11）

复核基线为 `4d75b5c7e2f40895c8f3614de971e984eba28a03`。审查稿中的代码判断均用当前 SQLite 关系、接口和可执行流程复现；路线图建议则按是否具备业务输入、能否形成闭环来裁决。以下结论对应 2026-08-29 更新稿。

## P0

| 项目 | 结论 | 处理 |
| --- | --- | --- |
| P0-01 已开始工单可能无法完成 | 成立 | Start 与 Complete 改用不同权威来源。Complete 只依赖可信 Start Event、Booking、技师一致性和时间顺序；技师资料变化、活动 Plan 变化或不存在活动 Plan 均不再阻止完成。 |
| P0-02 紧急接单重放不验证工单资源 | 成立 | 新增 Emergency Intake Receipt，绑定工单哈希、提交 D、请求指纹和状态。重放重新读取工单；资源缺失、内容变化或取消均返回稳定冲突码。 |
| P0-03 同 D 混入不同活动 V | 成立 | 新增事务一致的 DispatchSnapshot，绑定 D/V/E、Scenario 链头、活动 Plan、适用性和 Operational View。前端按快照令牌原子更新，并在焦点恢复、可见性变化和 BroadcastChannel 通知后重取。 |

## P1

| 项目 | 结论 | 处理 |
| --- | --- | --- |
| P1-01 Restore 混用基础与目标快照 | 成立 | Run 保留命令基础 D；目标 D 和允许变换由 RestoreTransformManifest 单独证明，发布事务重新计算。 |
| P1-02 Run/Candidate 缺少清单与可信加载 | 成立 | 增加 RunInput、RunResult、Candidate Manifest、关系身份列和不可变触发器，所有读取经过统一校验。 |
| P1-03 Plan-changing 请求的活动 V 前置条件可选 | 成立 | `/api/v2` 的 baseline、optimize、replan、activate、restore、reattest 和实验发布要求显式传入活动 V（显式 `null` 表示期望无活动方案）；缺失为 428。旧接口继续兼容。 |
| P1-04 Reattest 指纹遗漏活动 V | 成立 | 统一命令指纹包含字段存在性、活动 V、D、E、来源和业务参数。 |
| P1-05 报告把冻结 KPI 当当前状态 | 成立 | 报告分为冻结方案和当前运营两种模式；两者都显示 D/V/E、指标新鲜度和证明状态。当前报告列出新增未覆盖、失效分配、服务中和已完成工单。 |
| P1-06 Operational View 未验证执行链 | 成立 | 读取时校验执行序号、Start/Complete、Booking、来源 assignment 和事件哈希；快照返回 E 水位、上下文哈希和完整性。 |
| P1-07 风险致损分项使用全局布尔 | 成立 | Risk V7 保存工单级窗口违反和技师级加班突破/分钟数，以集合差和恶化集合归因。 |
| P1-08 SUMMARY_ONLY 仍保存丰富 trial | 成立 | 摘要模式不再保存完整 RiskTrialMetric，只保存配对比较所需的紧凑向量；紧急选人证据只覆盖实际发生事件的 trial。 |
| P1-09 Capacity Artifact 成本不闭包 | 成立 | Artifact 保存正式/诊断成本、成本账本哈希、成本/容量政策哈希和参照成本哈希，独立读取时重新核对。 |
| P1-10 长计算占用同步 HTTP | 成立，兼容接口保留 | 新增持久 analysis job API；网页改为提交任务后轮询。成本、容量和风险在受 CPU/内存/墙钟限制的子进程运行，支持硬取消。旧同步接口标记为兼容路径。通用 baseline/optimize/replan 仍保留同步返回契约。 |
| P1-11 Risk Comparison 不是可恢复 Saga | 成立 | 增加 RESERVED、BEFORE_COMPLETED、AFTER_COMPLETED、COMPARISON_COMPLETED、FAILED 五阶段记录；同一键从已完成子 A 继续，故障注入验证不重复第一个子分析。 |
| P1-12 `command_keys` 缺少可信关系 | 成立 | 增加 Command Manifest、载荷哈希、关系身份校验和持久隔离。只有关系清单仍可信的终态记录可从资源引用恢复；关系被改写时拒绝。 |
| P1-13 启动时自动迁移 | 成立 | 提供 inspect、backup、dry-run、apply、verify、restore CLI。应用启动拒绝升级旧 schema；apply 在临时副本验证后原子替换并保留备份。 |
| P1-14 多处重复完整快照 | 部分成立 | 最大的 trial、决策和排程 Artifact 已改为内容寻址、zlib 压缩与去重 Blob，并提供导出、保留期 prune 和 vacuum。Plan/Run/Candidate 仍保留用于独立审计的自包含快照；在没有分块清单迁移方案前不删除这些证明。 |
| P1-15 实验线程无法硬停止 OR-Tools | 成立 | 每个实验候选在 spawn 子进程求解；取消和超时会 terminate。Linux 由父进程监控实际 RSS 并执行 2 GiB 硬上限，不用虚拟地址空间误判内存。 |
| P1-16 当前链头与完整历史未分开 | 成立 | DispatchSnapshot 分别返回当前链头可用性和完整历史链状态/问题数；历史祖先损坏不会伪装成当前头损坏。 |
| P1-17 无活动 Plan 时 Preview 使用最新历史 | 成立 | Preview、激活和恢复比较只使用真实活动 Plan；没有活动 Plan 时当前方案基准为无。 |
| P1-18 锁无安装哈希、Pyright 范围窄 | 成立 | runtime/dev 锁包含发行文件 SHA-256，安装强制 `--require-hashes`；生成 CycloneDX SBOM，声明 Python 3.11–3.13，strict 范围扩到 7 个核心模块。 |
| P1-19 OpenAPI 类型与手写 DTO 双轨 | 成立 | Scenario、Schedule、Plan、策略、成本、容量、风险、A、Artifact 和 Job 类型改为从生成 schema 派生；仅保留把服务端默认字段视作已填充的窄化层。 |
| P1-20 Benchmark/mutation 只有 smoke | 成立 | Benchmark 记录耗时并与提交基线做趋势门禁；安全关键 mutation 输出逐项结果和 100% 最低得分。旧 make 名称保留为别名。 |

## v0.6 任务书

- FF-1820、1821：已建立带租约、心跳、attempt、取消和终态的 SQLite Job Queue；Job 与 Outbox 同事务提交。紧急接单把重排任务与新 D 一起提交，故障注入验证重启后自动发布一个新 V。
- FF-1822：策略求解和经营分析均在受限子进程运行。取消后不会留下 `RUNNING` A；同步兼容接口仍在调用进程运行。
- FF-1823：Risk Comparison Saga 已实现并覆盖第二子分析故障后的续跑。
- FF-1824：Artifact Blob 支持内容哈希、压缩、去重、导出、无引用保留期清理和 vacuum。
- FF-1825、1826：显式迁移 CLI、带哈希锁、平台上限、SBOM 和 strict 类型门禁已实现。
- FF-1827：CLI、子进程 worker、适用性、执行、证明和报告已拆出；`main.py`、`storage.py` 仍偏大。继续机械搬目录不会改善领域边界，因此没有在本轮做一次高冲突的全仓改名。

## v0.7、v0.8 与 v1 建议

这些建议多数是合理产品方向，但不能作为 v0.5.11 已有实现的缺陷来判定。

- Visit 多次上门、技师离线端、分诊、客户/资产、周期维护、库存和供应商结算需要新的聚合、冲突合并和权限策略。当前只有 Start/Complete 两事件；添加一组空表会造成“已支持”的假象，还会破坏现有 WorkOrder 单次服务约束，因此本轮没有以占位模型冒充闭环。
- 分时道路、历史时长、no-show/缺勤/紧急率校准需要真实观测样本、版本和漂移阈值。仓库没有这些数据，凭 Fixture 拟合参数不构成校准。Pareto 候选、日内成本范围和固定 seed 配对分析已经保留；多日、跨日积压和实际毛利仍明确拒绝。
- 已增加 CODEOWNERS、SBOM、性能趋势、数据保留/隐私说明和现有威胁边界。LICENSE 会改变法律授权，MIT 与 Apache-2.0 不能由代码审查代选；分支保护、required checks、签名 tag/Release 会修改 GitHub 治理，也不包含在本次提交与推送授权中。

## 额外发现

实施后的独立回归又发现并修复：Command 状态更新漏写 Manifest payload；求解终态把 solver 字段反向写回不可变输入；API handler 参数调整破坏直接调用兼容；旧 Artifact 迁移产生嵌套 Blob 引用；恢复任务序列化把未传入的活动 V 误变成显式 `null`；重启恢复 Job 遇到已中断 A 时没有创建下一 attempt；压缩 Blob 解码缺少绝对大小上限；Demo 验证复用固定临时数据库而受旧 schema 污染；SQLite CLI 与测试辅助连接只提交但没有关闭。这些问题均有回归测试，不依赖审查稿结论。
