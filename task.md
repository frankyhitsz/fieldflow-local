# FieldFlow Local 任务记录

更新时间：2026-08-24

## 本轮目标

完成 `pro-plan.md` 的 v0.5.3 决策正确性工作包 FF-1001～FF-1010。修复必须覆盖模型、迁移、接口、界面和故障注入；经过三轮独立审计与整套验证后再提交。

## 第一轮：数字口径与反事实证据

- [x] 拆分正常人工、加班基础工资和加班溢价，修正 `PAID_SHIFT` 少算加班基础工资的问题。
- [x] 对 `OCCUPIED_MINUTES`、`PAID_SHIFT` 和随机整数政策验证现金成本恒等式；`SALARIED_ALLOCATION` 继续稳定拒绝。
- [x] 容量成本增加单位类型、每日单位数和受影响实体；修正 `PER_SHIFT` 未乘技师数量的问题，拒绝无定义单位的 cadence。
- [x] 不可执行容量方案的正式 KPI、周期影响和抵消天数改为 null，仅保留诊断指标和违规。
- [x] 合成技师使用确定性保留 ID 并检查冲突；反事实场景重新走 Pydantic 聚合校验。
- [x] 突发事件发生与实际致损分开统计；共同随机量按 seed、trial、事件和实体派生并保存场景集哈希。

## 第二轮：A-run、冻结计划和 Saga

- [x] A-run 请求改为 COST/CAPACITY/RISK 判别联合，重复或无关参数返回 422，不再静默覆盖。
- [x] 新分析返回 201、运行中复用返回 202、终态复用返回 200。
- [x] 失败或中断 A 可显式 retry；新 attempt 保存 logical ID、来源 A、attempt 序号和原始请求，原记录不改写。
- [x] 容量 A 的每个选项保存规范化路线、完整校验报告、路线差异和输入变化 Artifact，并提供列表和详情接口。
- [x] 新 V 保存发布排程哈希、校验政策和报告哈希；分析、报告和历史激活共用冻结计划完整性检查。
- [x] 人工改派锁定后数据变化进入不可覆盖的 `FAILED_CONTEXT_CHANGED`；同 key 稳定重放，新 key 可重新发起。
- [x] Schema 升至 v16，移除阻止 retry 的输入唯一约束并增加反事实 Artifact 表；旧库迁移前保留时间戳备份。

## 第三轮：界面、边界与交付审计

- [x] 运营复盘分列正常人工、加班基础工资和溢价；突发事件与实际致损使用不同标签。
- [x] 不可执行容量行用 `—` 隐藏正式数字，可展开全部违规；“回本”改为“经济影响抵消天数”。
- [x] 失败和中断 A 在页面提供显式重试按钮，并说明原记录会保留。
- [x] 补充成本、容量单位、无效方案、共同随机场景、冻结损坏、状态码、retry、Artifact 和 Saga 终态测试。
- [x] 更新 OpenAPI 快照并完成 Ruff、Pyright、TypeScript、后端、React、构建、Demo、Benchmark 和依赖审计；Playwright API 流程通过，本机 Chromium 其余用例受系统 SIGTRAP 阻止，等待 Linux CI 补证。
- [ ] 提交、推送并确认 GitHub Actions 最新提交全部通过。

## 后续版本范围

- [ ] v0.6.0：持久 Job Queue、Outbox、进度、取消、启动对账和显式迁移 CLI。
- [ ] v0.7.0：Booking、调度收件箱、技师端流程、资产、周期维护、库存和 Crew。
- [ ] v0.8.0：incurred/remaining/combined 分析、组合容量、敏感性、风险校准和多日模型。
- [ ] v1.0.0：正式 Benchmark、依赖锁、许可证选择和 GitHub 分支治理。

## 当前验证

```text
make lint                         通过
Python 依赖审计                  无已知漏洞
npm production audit             0 vulnerabilities
后端、接口与属性测试              179 passed；coverage 89.31%
React 组件                        10 passed
生产构建                          通过
Demo check                       通过
Benchmark smoke                  通过（严格完整性检查暴露的无效延迟 Fixture 已修正）
Playwright API 生命周期           1 passed
Playwright 浏览器页面             本机 Chromium 启动后被系统 SIGTRAP 终止；非断言失败，等待 Linux CI
```
