# `pro-plan.md` 核查记录

> 这是 0.2.0 时的历史记录，其中依赖和架构描述不代表当前版本。当前裁决见 [v0.5.9 复核记录](pro-plan-v0.5.9-assessment.md)。

核查日期：2026-08-24  
目标版本：0.2.0

本记录把审查意见分为三类：已采纳、部分采纳、暂不采纳。结论来自代码检查、失败注入、接口调用、SQLite 约束测试和前端构建，不把静态推测当成既成事实。

## P0

| 编号 | 结论 | 处理 |
| --- | --- | --- |
| P0-01 失败结果可能发布 | 已确认并修复 | 旧流程能把失败结果发布成新的当前版本。现增加 `ScheduleRun → ScheduleCandidate → verifier → PlanVersion` 四步。失败、无解、空结果、陈旧结果不发布，不消耗 V 号，原当前方案不变。优化和重排都有失败注入测试。 |
| P0-02 覆盖校验不完整 | 已确认并修复 | 新增独立 `backend/verification.py`。校验完整覆盖、重复、交集、未知引用、技能、时间窗、班次、行程、路线连续性、锁定、已开始工单、revision/hash、KPI、业务评分和发布状态。空 assignments 不再绕过校验；Store 在发布事务内重新校验，不能靠伪造一份已通过的报告绕过。 |
| P0-03 求解目标与页面目标混用 | 已确认并修复 | 保存 `solver_objective_value` 和 `business_score`，后者带 `FIELD_SERVICE_SCORE_V2`。公平目标改为按班次归一化的服务负载跨度；页面和报告明确区分两个值。跨策略只比较 KPI、Pareto 关系和统一业务评分。 |
| P0-04 时限和状态不准确 | 已确认并修复 | API 最小求解时限改为 1 秒；保存请求时限、实际配置、耗时、OR-Tools 状态码、终止原因和是否找到解。状态来自 `routing.status()`，不再靠耗时猜测。UI 不把 `FEASIBLE` 写成“最优”。 |

## P1

| 编号 | 结论 | 处理 |
| --- | --- | --- |
| P1-01 同址仍有 3 分钟行程 | 已确认并修复 | `EUCLIDEAN_GRID_V2` 对同一点返回 0。固定准备时间没有混进行程。 |
| P1-02 SLA 分母误导 | 已确认并修复 | 保留兼容字段，同时增加 `assigned_on_time_rate`、`committed_on_time_rate`、延迟总分钟和 p90。调度台、版本页、报告和实验页默认展示包含未分配工单的 SLA 履约率。 |
| P1-03 利用率和公平度不完整 | 已确认并修复 | 增加服务/占用利用率、行程/等待/加班比例、归一化负载及范围。公平策略和公共评分使用归一化负载范围。 |
| P1-04 重排稳定率过敏 | 已确认并修复 | “变化”按技师和前驱关系判断，不再因路线前方插单导致所有 sequence 变化。增加同技师率、邻接保留率、时间偏移中位数/p90、超过 15 分钟数量和需通知客户数量。重排惩罚与 `changed` 使用同一前驱定义。 |
| P1-05 解释不是真实反事实 | 已确认并修复 | 每个分配保存 `previous → job → next` 的路线插入净行程。其他合格技师只给“现有路线、未重新排时”的行程增量估算，并明确范围；不再使用“至少减少”“全局最优”等话术。 |
| P1-06 聚合校验不足 | 已确认并修复 | `ScheduleScenario` 统一检查日期、重复 ID、悬空/重复锁定、技能和已完成工单锁定。Fixture、API 保存和数据库反序列化都经过同一 Pydantic 聚合。 |
| P1-07 状态允许回退 | 核心问题已修；七状态扩展暂不采纳 | 已确认 `started → pending` 原来可通过，现改为 `pending → started → completed` 单向状态机；已开始/完成工单的业务字段不可改。审查建议的 dispatched/accepted/en-route/arrived/cancelled 需要新的现场动作、时间戳和 UI，属于 v0.4 产品扩展，不能只加枚举。 |
| P1-08 突发单幂等和计划时钟 | 已采纳 | 重排必须显式给 `planning_time`（兼容 `current_time`，两者同时出现必须相等）。突发单必须有 `reported_at`。相同幂等键和 payload 返回原结果；相同工单 ID 不同内容返回 409。前端“保存并重排”改为一个复合请求。后端先在内存中构造 D+1，求解和校验通过后才在同一事务中写入数据修订和 V 版本；失败时两者都不变。 |
| P1-09 血缘不严格 | 已采纳 | Run、Candidate 和 Plan 保存数据修订、场景 hash、源版本/hash、求解配置 hash、旅行/指标/求解器版本。源快照不一致时关系为 `fresh_after_data_change`，不再称 `optimized_from`。 |
| P1-10 实验指纹不完整 | 已采纳 | 指纹包括场景快照、完整 profile 快照、时限、求解器、旅行模型、评分政策和 seed。修改同一 profile 内容会得到不同实验；已有回归测试。 |
| P1-11 评分缺少规范化和版本 | 已采纳 | 公共评分先按班次、总服务量、总需求惩罚和工单数归一化，再加权；保存政策版本。实验页展示 Pareto 前沿、被支配关系和统一评分。 |
| P1-12 生命周期与全局状态 | 部分采纳 | 审查指出的 executor 生命周期问题成立。执行器现由 FastAPI lifespan 创建和关闭，导入模块不创建线程，关闭测试会确认资源释放。`Store` 仍是模块级单例；当前 SQLite 每次操作独立开关连接，测试通过环境变量和 reload 隔离。完整 app factory、Store/JobRunner 接口和多 app 同进程隔离保留为架构任务。 |
| P1-13 SQLite 约束和迁移 | 已采纳，并修正一处审查前提 | Schema v4 为主要父子关系增加真实外键，为 artifact 增加唯一父对象 CHECK，启用 WAL/busy timeout，并在迁移后执行外键检查。v2/v3 升级保留历史且先备份，无法挂到合法父对象的旧记录会进入 `migration_orphans`，不再被静默丢弃。v1 清空旧 schedule 并非“静默删除”：这是前一轮已确认的一次性旧历史重建要求，仍保留备份。兼容 `schedules` 投影暂不删除。 |
| P1-14 前端硬编码、陈旧显示和无障碍 | 大部分已采纳；轮询结论有误 | 地图显示每名技师的真实出发点，时间轴按数据范围生成，次日时间用“当日/次日 + 时间”编辑。锁定后立即进入 stale 状态，不再改写旧 schedule 假装有效。对话框和地图点补充 ARIA/键盘操作。原代码的实验轮询已经有 cancelled flag 和 timeout cleanup，因此“完全缺少取消”不成立；本轮又增加 AbortController，实际中止网络请求。 |
| P1-15 安全边界测试不足 | 已采纳 | 新增无解、超时、空结果、遗漏/重复/交集、伪造 KPI、陈旧候选、原子回滚、迁移、外键、状态回退、幂等、profile 变更、生命周期、跨日控件和 stale UI 测试。 |

## 工程与开源意见

| 意见 | 结论 |
| --- | --- |
| 版本号不一致 | 已修。`backend/_version.py` 是运行时版本源，FastAPI 和健康接口直接读取它；测试约束前端 package 与其一致。本轮核查时为 0.2.0，后续里程碑见 Changelog。公开方案的 V001/V002 与软件版本没有关系。 |
| `pyproject.toml` 缺依赖 | 已修。运行依赖和 dev extra 已列出，`requirements.txt` 继续作为精确安装清单。 |
| CI 缺静态门禁 | 已修主要部分。CI 运行 Ruff、TypeScript、后端 75% 覆盖率门槛（当前约 89%）、组件测试、构建、Demo 和 Playwright。Dependabot 检查 pip、npm 和 Actions；`npm install` 当前报告 0 个漏洞。没有为了凑清单同时引入 mypy/Pyright 和 ESLint。 |
| 开源文件不足 | 部分采纳。已增加 SECURITY、CONTRIBUTING、CODE_OF_CONDUCT、CHANGELOG、架构、指标、限制、数据格式、Benchmark 状态、威胁模型和 ADR。未添加 LICENSE：许可证是仓库所有者的法律选择，不能由实现者代选。Demo GIF 和正式 Benchmark 也不能用合成结果冒充。 |

## 建议架构与任务书

| 任务 | 状态 |
| --- | --- |
| FF-201 Run/Candidate/Published Plan | 已完成 |
| FF-202 独立 ScheduleVerifier | 已完成 |
| FF-203 optimize/replan 安全发布 | 已完成 |
| FF-204 状态和时间限制 | 已完成 |
| FF-205 求解目标与业务评分 | 已完成 |
| FF-206 KPI V2 | 已完成 |
| FF-207 TravelTimeProvider | 已完成；同时提供矩阵 provider 和非对称测试 |
| FF-208 分配解释 | 已完成路线插入证据；完整可行重求解反事实仍不是当前承诺 |
| FF-209 P0 回归和属性测试 | 已完成失败路径、约束、确定性和并发回归；未引入 Hypothesis 依赖 |
| FF-220 聚合与状态机 | 聚合已完成；七状态现场闭环延期 |
| FF-221 app factory/依赖注入 | lifespan 已完成；完整工厂和仓储接口延期 |
| FF-222 SQLite Schema | 已完成本版本所需外键、CHECK、备份、WAL 和迁移测试 |
| FF-223 血缘与幂等 | 已完成 |
| FF-224 策略实验室 | 已完成冻结快照、统一预算、失败隔离、Pareto 和人工发布 |
| FF-225 前端计划一致性 | 已完成 |
| FF-226 API 客户端可靠性 | 已完成错误解析、35 秒诊断超时、轮询 abort 和重复操作禁用；后台任务取消 API 延期 |
| FF-227 质量门禁 | 已完成当前栈的静态、覆盖、组件、构建、Demo、E2E 和 CI 门禁 |
| FF-240～FF-245 业务扩展 | 暂不采纳到本轮。导入、执行闭环、休息/区域/团队/前置约束、成本容量、Monte Carlo、库存和协作工单都需要单独产品需求和数据，不是现有缺陷修复。 |
| FF-250 Benchmark | 暂不发布性能结论。已写明基准所需场景、seed、硬件和统计口径。 |
| FF-251 开源治理 | 文档部分完成；LICENSE、真实 Demo 素材和正式 Benchmark 待所有者决定或提供。 |
| FF-252 打包发布 | 版本和依赖已统一；Git tag/GitHub Release 应在本轮验证和仓库所有者确认许可证后执行。 |

审查提出的后端目录大拆分、Repository/UoW 全量抽象和 v0.4 业务模块都具有长期价值，但当前代码量还不足以抵消迁移成本。本轮先把安全边界做实，并把剩余工作写成明确限制，不用空目录或占位类型伪装“架构完成”。

## 验证入口

```bash
make lint
make test
make test-frontend
make build
make demo-check
make test-e2e
make verify
```

关键测试在 `tests/test_safety_regressions.py`、`tests/test_versions_and_strategies.py`、`frontend/src/test/App.test.tsx` 和 `frontend/e2e/main-flow.spec.ts`。
