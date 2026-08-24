# `pro-plan.md` 逐项复核

## 复核口径

审查文件基于网页端静态阅读，部分判断准确指出了契约缺口，也有一些建议需要新数据和新运行时。以下结论沿 FastAPI、SQLite、调度器、Verifier、React 和可运行测试逐项核查。“修复”表示当前实现和回归测试均已覆盖；“门禁”表示先进模型尚未实现，但系统已经停止输出语义错误的结果；“延期”不计作已完成。

## P0 与 P1

| 编号 | 合理性 | 当前处理 |
| --- | --- | --- |
| P0-01 容量分析没有以选中 V 为基准 | 成立 | 默认 `SELECTED_PLAN_DELTA` 固定选中 V 的已有安排，只放置原未服务工单；可选受控重算时，参照和全部选项使用同一确定性政策。响应保存选中、参照和选项排程签名。 |
| P0-02 风险不遵循正式开始时刻 | 成立 | 默认 `FOLLOW_PUBLISHED_SCHEDULE`，服务与行程波动从已发布开始时刻传播；最早可行执行改成显式策略。固定 seed 回归验证两种策略不同。 |
| P0-03 分析忽略现场执行上下文 | 成立 | 已加严格门禁：快照含 started 或 completed 时返回 `EXECUTION_ANALYSIS_CONTEXT_REQUIRED`。完整的 actual-plus-forecast 尚未实现，不再用全日数字掩盖缺口。 |
| P0-04 `INTAKE_COMMITTED` 可永久卡住 | 成立 | 紧急命令保存准确 publication key；启动时对账 `RUNNING`、`REPLAN_RUNNING` 和带发布键的 `INTAKE_COMMITTED`。已有发布则完成，否则变成可重试；没有发布键的纯接收记录不误伤。 |
| P1-01 决策分析没有冻结旅行模型 | 成立 | 三类分析都要求当前 provider 指纹与 V 中冻结指纹一致，重算返程和新增路径也使用同一 provider；不一致返回结构化 409。 |
| P1-02 “增加服务站点”名不副实 | 成立 | 采用审查建议中的即时修正：改为“将一名高行程技师的出发点移至需求中心”。完整站点、库存和多站选择不在当前模型中。 |
| P1-03 成本混合现金和损失 | 成立 | 分为 `cash_operating_cost_cents`、`service_failure_loss_cents` 和 `total_economic_impact_cents`；旧 `total_cost_cents` 仅保留兼容别名。 |
| P1-04 “计划失效概率”误导 | 成立 | 主指标改为 `additional_disruption_probability`，另列 `baseline_unserved_orders` 与 `expected_total_unserved_orders`；旧字段仅为兼容别名。 |
| P1-05 分析结果不持久 | 成立 | 新增 `DecisionAnalysisRun` 和 A 编号。记录 V、快照、排程、执行水位、行程、政策、代码、输入哈希和结果；相同输入原子去重。 |
| P1-06 没有区分全日、已发生和剩余 | 成立 | 当前明确只支持 `FULL_DAY_PLAN`，存在任何执行事实便拒绝。incurred/remaining/actual-plus-forecast 需要执行投影，列为后续范围。 |
| P1-07 超时固定追加 15 分钟 | 成立 | 开始服务可填 `estimated_remaining_minutes`；缺省值移入 `SolverConfig.active_service_default_remaining_minutes`，调度、验证和开工门禁共享。 |
| P1-08 历史 replan 激活改变稳定性语义 | 成立 | 分离 `lineage_source_version_id` 与 `stability_baseline_version_id`；激活保持原始重排基准并按该基准重新规范化。 |
| P1-09 锁定并改派是两个请求 | 成立 | 新增单一幂等命令 `/manual-reassignment`。锁定是持久业务决定；重排失败返回部分成功结果并保留最后正式方案为过期可见状态。 |
| P1-10 PlanVersion 混合冻结历史与适用状态 | 成立 | `active` 与 `coverage_status` 迁到 `plan_applicability`。数据编辑只改投影，历史 payload 字节保持不变。显式名称编辑仍是允许的展示元数据更新。 |
| P1-11 单一 D 混合多种修订 | 方向合理但属于破坏性架构变更 | 执行事件已有独立单调水位，A/V 也已独立；公开 D 继续兼容聚合业务修订。拆成 planning/metadata/execution 三套编号需专门迁移和客户端版本，不在补丁版本中强改。 |
| P1-12 Benchmark smoke 太弱 | 成立 | smoke 已增加选中 V 签名、同政策基准、发布时间语义、旅行指纹拒绝、执行态拒绝、成本对账和元数据检查；它仍明确叫 smoke，不包装成正式性能 benchmark。 |

## 工程、依赖与仓库治理

| 项目 | 结论与处理 |
| --- | --- |
| P2-01 静态门禁与属性测试不足 | Pyright basic 已进 `make lint` 和 CI；增加 Hypothesis 属性测试、v1–v12 迁移矩阵、恢复故障测试和 OpenAPI 快照。前端 ESLint 建议合理，但当前 TypeScript 7 超出已发布 `typescript-eslint` peer 范围；本轮保留严格 `tsc`，不安装声明不兼容的工具链。 |
| P2-02 Pydantic 是传递依赖 | 成立；已在 `requirements.txt` 和 `pyproject.toml` 直接声明。 |
| P2-03 开发服务器监听 `0.0.0.0` | 成立；Vite host 改为 `127.0.0.1`，FastAPI 启动保持本机地址。 |
| P2-04 缺少 LICENSE 与分支保护 | 事实成立，但不是可自行决定的代码缺陷。许可证需要所有者选择法律文本；分支保护是额外 GitHub 治理操作，未在仅提交代码的授权下代替所有者修改。 |
| 依赖和兼容矩阵 | 增加 Python 3.11 作业、`pip-audit` 和 npm production audit。修复审计发现的 FastAPI/Starlette、pytest 和 protobuf/OR-Tools 问题，并处理新版 OR-Tools Python 绑定不再接受车辆列表的兼容变化。 |

## 里程碑判断

| 里程碑 | 已完成 | 仍需后续项目 |
| --- | --- | --- |
| M0 v0.5.1 经营分析正确性 | 选中 V 基准、风险计划时刻、旅行指纹、成本与风险命名、执行态门禁、紧急恢复、选项文案和专项测试均完成。 | 执行水位下的实际加预测分析。 |
| M1 v0.6.0 持久运行时 | `DecisionAnalysisRun`、PlanApplicability、OpenAPI/类型/属性/迁移/安全门禁已提前完成。 | 独立 worker、正式 Booking 表、三类修订号、矩阵旅行模型和更彻底依赖注入。 |
| M2 v0.7.0 运营闭环 | 已有执行事件、人工改派命令、策略实验和计划/实际基础字段。 | 收件箱、完整 Booking 生命周期、执行模拟器、资产周期维护、库存和 Crew。 |
| M3 v0.8.0 决策科学 | 已有可审计成本、容量和固定 seed 风险分析。 | 风险校准、组合容量、敏感性/Pareto 和有多 seed、p50/p95、硬件说明的正式 benchmark。 |
| M4 v1.0.0 开源发布 | README、贡献、安全与行为准则已存在。 | 所有者选择许可证、仓库治理和可复现 Release。 |

## 对静态推断的修正

- 审查提出的 `DecisionAnalysisRun` 并不要求引入云服务或在线队列；在当前 SQLite 事务内可以严谨实现，因此本轮已完成。
- “增加站点”不能仅靠新名称变成真实站点模型，本轮明确降级为出发点假设，没有添加虚假的 Depot 实体。
- 正式 Benchmark 不能靠扩大 smoke 用例数量完成。当前脚本只作确定性回归，性能、可行率和统计分布仍不对外宣称。
- 完整执行感知分析若缺少实际成本、位置和水位，继续计算比返回 409 更危险；因此当前门禁是刻意的正确行为，不是悄悄忽略需求。
