# FieldFlow Local 任务记录

更新时间：2026-08-24

## 本轮目标

复核新版 `pro-plan.md`，完成 v0.5.2 的经营分析可信性工作包，记录需要后续领域模型或仓库治理授权的项目；经过三轮独立审计、整套本地验证和 Linux CI 后提交。

## 第一轮：分析语义与反事实正确性

- [x] 以 `EX_ANTE_FROZEN_PLAN` 替代含糊的 `FULL_DAY_PLAN`；执行后必须显式选择，并持久化水位、时点和执行上下文。
- [x] 为未实现的 actual、remaining forecast 和 combined 范围返回稳定错误。
- [x] 完整哈希权威 Schedule；校验场景、排程快照、政策、旅行模型、服务时长、SLA 和重算 KPI。
- [x] 保存算法版本和 build SHA，CI 使用提交 SHA，本地使用后端源码哈希。
- [x] 容量尾部追加计算真实 Depot 返程，并用独立 Verifier 复核完整反事实。
- [x] 修复不适用容量选项仍计固定投入的边界错误。

## 第二轮：恢复、成本与页面流程

- [x] 人工改派增加 `LOCK_COMMITTED`、`REPLAN_CREATED`、`PLAN_PUBLISHED` 阶段和稳定 Run ID。
- [x] 在三个持久阶段注入进程终止；重启后 Lock、D、Run、V 不重复，不同请求复用键返回 409。
- [x] A 运行增加 `RUNNING / COMPLETED / FAILED / INTERRUPTED`，失败结果可审计并按完整输入去重。
- [x] 新增分析周期、成本频率、人工模式、一次性投入、日运营变化、周期影响和盈亏平衡字段。
- [x] 新技师改为显式或保守 archetype；补技能绑定可解锁的未服务需求；尾部追加能力在接口和页面明确命名。
- [x] 风险指标拆分 Monte Carlo 均值抽样区间、全日总迟到分位和五类扰动概率。
- [x] 运营复盘首屏只 GET 已有 A；显式按钮才创建，成本与风险部分成功不互相遮蔽。
- [x] 三个同步分析接口标记 deprecated；正式 UI 仅使用 A-run。

## 第三轮：属性、兼容和交付审计

- [x] 增加 KPI/旅行/证据/build SHA 哈希、损坏方案、成本周期、付费班次和风险标签测试。
- [x] 增加随机加班容量、完整可行性与一次性成本周期属性测试。
- [x] Schema 升至 v14，v1–v13 迁移矩阵和备份路径断言同步更新。
- [x] OpenAPI 快照、前端类型、README、架构、指标、逐项复核和 Changelog 更新到 v0.5.2。
- [x] Benchmark smoke 改用完整排程一致性校验，并检查容量违规和风险新字段；仍明确不称正式性能 Benchmark。
- [ ] 整套本地 `make verify` 通过。
- [ ] 提交并推送；确认最新提交的 `python-compat` 与 `fieldflow` CI 均通过。

## 明确保留的后续范围

- [ ] 实际已发生成本、剩余预测和 actual-plus-forecast 需要正式执行投影。
- [ ] A/Run 的独立 worker、取消、进度和 Outbox 属于 v0.6.0 运行时。
- [ ] 单一 D 拆分、稳定 Location ID、显式迁移 CLI、PlanVersion 兼容字段清理和生成式 OpenAPI 客户端需要版本化迁移。
- [ ] 固定承诺间隙插入、组合容量、敏感性、风险校准、Booking、收件箱、资产、周期维护、库存和 Crew 需要新领域数据与验收。
- [ ] Python 传递依赖统一锁文件需要选定跨平台锁定工具；不以本机 `pip freeze` 代替。
- [ ] LICENSE 和 GitHub 分支保护等待所有者选择与额外治理授权。

## 当前验证

```text
make lint                         通过
React 组件                        8 passed
生产构建                          通过
后端与接口                        156 passed，coverage 88.92%
决策/属性/并发改派最终专项         35 passed
人工改派三阶段故障注入            3 passed
Playwright API 生命周期           1 passed
Demo check                       通过
Benchmark smoke                  通过
npm production audit             0 vulnerabilities
pip-audit                        本机两次连接 PyPI 均被对端重置，等待 Linux CI 在线复核
Playwright 页面流程              本机 Chromium 启动受沙箱 SIGTRAP 阻止，等待 Linux CI 复核
```
