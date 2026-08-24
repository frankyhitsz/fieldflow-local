# `pro-plan.md` M0–M3 复核

## 复核口径

本轮以审查文件锁定的 `ea98e33` 为基线。每项结论都沿实际 API、SQLite 事务、调度器、Verifier 和 React 操作流核查；仅凭网页端静态推断、但与当前实现不符的结论不会直接照单修改。

## M0 缺陷

| 项目 | 核查结论 | 处理 |
| --- | --- | --- |
| 完成一次后第二、第三次重排失败 | 成立 | completed 通过事件和历史 V 追溯，不再要求存在于 future source；加入连续两次重排回归。 |
| 非首项 started 沿用旧序号，未来队列不连续 | 成立 | future sequence 从 1 重排；source sequence/hash 单独保存。 |
| 前端把重排 cutoff 当实际执行时间 | 成立 | 新增实际执行对话框与独立默认时间；事件保存计划/实际偏差。 |
| 早开工和零时长完成缺少契约 | 成立 | 早开工要求授权原因；完成必须严格晚于 start event。 |
| 业务字段变化后旧 pending 分配仍可开工 | 成立 | 分配保存 planning fingerprint；开工时对当前工单、技师、锁定和 travel fingerprint 复核。备注、姓名等元数据不参与。 |
| 完成后下一段仍从 depot 校验 | 成立 | 从前序实际完成位置和时间计算；删除前序返回结构化冲突。 |
| started 超过计划完成时被当作已可用 | 成立 | 统一保守投影至少 15 分钟剩余服务，调度、验证和执行门禁共用。 |
| drop penalty 被二次缩放 | 成立 | SolverPolicy V2 保存 original/effective 值，effective 直接取真实求解输入。 |
| baseline 冒充有 routing time limit | 成立 | 非 routing policy 的 time limit 为 null；Run/Result/Policy 在发布事务中交叉核对。 |
| 应用重启后 RUNNING 命令永久占用 | 成立 | 启动对账为 `FAILED_RETRYABLE`，已有发布记录时对账为完成。 |
| execution sequence 只靠应用层唯一 | 成立 | Schema v9 新增数据库唯一索引。 |
| 无变化保存仍增加 D | 成立 | 比较规范化后的完整场景，相同则直接返回当前 D。 |

## 需要修正的静态推断

- “跨快照 comparison 会被当作优化收益”不成立。API 返回 `comparable=false`，界面明确只展示原始值并列出快照差异。
- “当前完全没有执行来源身份”表述过度。旧实现已有 plan version 和 assignment 关联；缺口是稳定 Booking 身份及来源序号分离，本轮已补到事件和重排上下文，尚未拆成独立 Booking 表。
- “把所有模块级对象立即改成 App Factory”对单应用本地部署不是 P0。Store 可通过 `create_app(store_override=...)` 隔离；实验 executor 仍按进程单例管理，完整依赖注入留待异步 worker 改造。
- “只要列出 Crew、库存、跨日 benchmark 就算支持”不接受。Benchmark 对未进入领域模型的能力明确标记 `unsupported`，不会用空目录或固定返回值冒充。

## M3 实现范围

- 成本：技师成本从浮点单位迁移为整数分，当前和历史快照都做 v10 保值转换；公开 V 可输出六项成本及总额。
- 容量：同一冻结快照、同一确定性贪心评估器比较六类方案，输出边际成本与业务 KPI 变化。
- 风险：固定 seed 模拟旅行、服务时长、缺勤、突发单和客户不在场；输出 SLA、迟到分位、加班、未服务和失效概率。
- Benchmark：可运行 smoke 覆盖现有可表达场景，并把 M0 状态流关联到确定性回归。Crew、跨日和零件短缺仍是明确边界。

经营分析不会发布 Candidate，不消耗 V，也不回写业务数据。它用于比较假设，不替代财务结算或正式排程。
