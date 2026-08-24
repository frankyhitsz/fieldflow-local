# FieldFlow Local 任务记录

更新时间：2026-08-24

## 本轮目标

逐项复核 `pro-plan.md` 对当前项目的判断，修复能被运行环境证实的问题，记录暂不成立或需要新业务模型的建议，并在三轮独立审计和 Linux CI 通过后提交。

## 第一轮：经营分析语义与恢复

- [x] 容量分析默认相对选中的 V 做增量测算，不再静默生成另一份贪心基线；另提供同政策受控重算模式。
- [x] 风险仿真默认遵循正式方案开始时刻，最早可行执行改为显式政策。
- [x] 成本、容量和风险严格绑定快照、排程、行程模型、求解政策和代码版本。
- [x] 当前仅允许无执行事实的全日分析；started/completed 方案明确返回执行上下文错误。
- [x] 成本拆分现金运营成本、服务失败经济损失和总经济影响；风险改为额外中断概率并列出原始与预计总未服务数。
- [x] 紧急重排命令保存独立 publication key；重启可对账 `INTAKE_COMMITTED`，且不会误处理仅接收工单的记录。

## 第二轮：运行时模型与业务命令

- [x] 新增持久 `DecisionAnalysisRun`，按场景分配 A001、A002；相同 V、类型和输入指纹去重。
- [x] Schema v12 将 `active` 和 `coverage_status` 移到 `plan_applicability` 投影；业务数据变化不会再改写历史方案 payload。
- [x] Schema v13 增加经营分析表；v1–v12 迁移矩阵覆盖完整性、外键和新表。
- [x] 历史重排分开保存操作来源和原始稳定性基准，激活不会改变稳定率参照。
- [x] “锁定并改派”合并成一个幂等命令；求解失败时锁定保留、最后方案保持可见并标记过期。
- [x] 开始服务可填写预计剩余分钟；保守默认改为求解政策配置，调度、验证和执行门禁共用。
- [x] “增加服务站点”更名为“调整一名技师出发点”，与实际模型能力一致。

## 第三轮：工程与边界审计

- [x] 增加 Pyright basic 门禁并清零错误。
- [x] Pydantic 改为直接依赖；升级受审计影响的 FastAPI、Starlette、OR-Tools、pytest 等依赖。
- [x] 增加 Hypothesis 属性测试、v1–v12 迁移矩阵、恢复故障测试和经营分析专项回归。
- [x] 增加 OpenAPI 快照检查、Python 3.11 兼容作业和 Python/npm 依赖审计。
- [x] 前端开发服务器只监听 `127.0.0.1`。
- [x] Benchmark smoke 增加选中 V 基准、风险计划时刻、旅行指纹、成本对账和执行态拒绝检查。
- [x] 复核 TypeScript 7 与当前 `typescript-eslint` peer 范围；上游尚不支持该版本，本轮不安装一套声明不兼容的 ESLint 依赖。
- [x] 边界复查修复外包方案漏计自定义固定投入、容量参照控件字号和改派 CAS 校验；OpenAPI 快照阻止了内部参数意外暴露成查询参数。
- [x] 本机 Playwright API 生命周期通过；页面进程在 macOS 沙箱中以 `SIGTRAP` 退出，等待 Linux CI 作最终页面证据。

## 明确保留的后续范围

- [ ] `INCURRED_ACTUAL`、`REMAINING_FORECAST` 和执行水位绑定分析需要正式执行投影；当前以拒绝门禁保证不输出错误数字。
- [ ] 独立持久 worker、OR-Tools 子进程硬取消和 Outbox 属于异步运行时改造。
- [ ] planning/metadata/execution 三套公开修订号会破坏现有 D API，需要专门迁移版本。
- [ ] Crew、跨日、库存、组合容量、敏感性和风险校准需要新领域数据，不能从现有单日 Fixture 推断。
- [ ] LICENSE 需要仓库所有者选择法律文本；分支保护属于 GitHub 治理操作，本轮不代替所有者决定。

## 当前验证

三轮“发现—修复—回归”已经完成，提交前的本地证据如下：

```text
make lint                         通过（Ruff、Pyright、OpenAPI、TypeScript）
后端与接口                        138 passed，coverage 88.96%
React 组件                        7 passed
生产构建                          通过
Demo check                       通过
Benchmark smoke                  通过
pip-audit                        0 known vulnerabilities
npm audit --offline              0 vulnerabilities（本机在线端点两次 ECONNRESET）
Playwright API 生命周期           1 passed
```

页面级 Playwright 在本机尚未进入应用断言，Chromium 启动时被 macOS 进程沙箱以 `SIGTRAP` 终止。GitHub Actions 运行 `32715170478` 已在 Linux 上完成在线依赖审计和全部 Playwright 流程；`python-compat` 与 `fieldflow` 两个作业均通过。
