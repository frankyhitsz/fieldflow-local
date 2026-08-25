# FieldFlow Local 任务记录

更新时间：2026-08-25

## 本轮目标

复核变更后的 `pro-plan.md`。v0.5.6 完成 M0 的证据闭包、Plan 使用门禁和经营口径修复；P1/P2 中需要 worker、外部数据、新业务实体、许可证或仓库治理决定的项目说明边界，不以空接口冒充完成。

## 第一轮：信任闭包与使用门禁

- [x] Schema v19 为 A 增加关系型状态、开始/结束时间、lease 字段和 ReservationManifest；数据库触发器禁止终态回退。
- [x] Plan Manifest V2 绑定关系型身份、不可变头、完整血缘、PlanningContext、VerificationArtifact 和 Plan Artifact 清单。
- [x] Plan → A → Artifact → RiskComparison 统一返回 self/parent/effective trust；父依赖失败时业务结果不可继续使用。
- [x] 执行、分析、重放、人工改派、克隆、恢复、比较、报告和 `/schedules` 使用统一 Plan 门禁。
- [x] Legacy Plan 只读；新增 re-attestation 命令从冻结快照创建新 V，不修改旧记录，不消耗 D。
- [x] RiskComparison 绑定两端 A 与 trial/scenario-set Artifact；幂等预检在子分析之前，GET 和重放复核全部依赖。

## 第二轮：经营语义与字段影响

- [x] `PUBLICATION_REMAINING_PLAN` 强制单日范围；PAID_SHIFT 拆分完整日承诺和剩余增量，空闲技师不计增量。
- [x] 容量分析复用剩余成本口径；供应商容量未确认的外包改为条件状态，正式 KPI、可执行性和经济建议为空。
- [x] 风险统计拆分已发布承诺与全部需求 SLA；应急进入全部需求分母，并保存完成、准时和未服务概率。
- [x] 应急技师按 trial 随机进度选择，应急去程应用相同旅行扰动。
- [x] assignment-feasibility fingerprint 与目标偏好拆分；优先级、VIP、drop penalty 不再阻止既有分配开工。
- [x] 字段按 metadata、commercial/objective、assignment feasibility、execution 分类；元数据编辑不再清空当前方案。

## 第三轮：损坏隔离、界面与回归矩阵

- [x] v1 历史重建前逐行归档旧方案相关表；迁移仍先创建时间戳数据库备份。
- [x] 场景、Plan 和 A 列表隔离无法解析的记录；Plan 列表返回跳过数量，`/api/integrity-issues` 提供不含业务 payload 的隔离索引。
- [x] 前端不把 Legacy/FAILED 作为默认业务结果；禁用执行型操作并提供 Legacy 重新认证入口。
- [x] 前端展示有效信任、条件外包、完整日/剩余人工、已发布承诺/全部需求 SLA 和应急指标。
- [x] 新增 Legacy 门禁、re-attestation、父级篡改传播、Plan 血缘/Artifact 篡改、通用报告旁路、关系型终态、剩余 PAID_SHIFT、多日拒绝、条件外包、应急统计、风险比较依赖/幂等和损坏列表隔离测试。
- [x] 后端完整测试第一轮：213 passed。
- [x] React 组件 10 项、ESLint、TypeScript 和生产构建第一轮：通过。
- [x] 第二轮：`make lint`、89.53% 覆盖率、组件、构建、Demo、Benchmark 和依赖审计通过；修复 benchmark 仍构造旧式 Plan 的回归。
- [x] 第三轮：关系型 A 身份复核发现并补齐 started_at/Plan/type 交叉验证；Playwright API lifecycle 通过。本机 Chromium/Chrome 页面进程受 SIGTRAP/SIGABRT 限制，完整 5 项由 GitHub Linux CI 复核。
- [x] GitHub Actions #38：Python 3.11 与完整 fieldflow 作业通过；Linux Playwright 5/5 通过。

## 逐条裁决边界

- M0 FF-1301～FF-1309、FF-1311 已完成；FF-1310 完成备份、旧行归档和关键列表隔离，显式迁移 CLI 与全表健康扫描归入 v0.6.0。
- M1 FF-1320～FF-1328 需要持久 Job Queue、Outbox、独立求解进程、完整依赖注入、修订拆分、时变旅行或数据保留策略，不属于本次同步单机补丁。
- M2/M3 的正式 Booking、技师端、资产、库存、通知、导入导出、组合容量、风险校准和多日规划需要新的业务实体或真实数据，不创建不可用占位能力。
- LICENSE、分支保护和 required checks 会改变法律或仓库治理状态，保留给仓库所有者明确决定。

详细证据与反驳见 `docs/pro-plan-v0.5.6-assessment.md`。
