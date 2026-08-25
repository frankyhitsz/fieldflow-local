# FieldFlow Local 任务记录

更新时间：2026-08-25

## 本轮目标

逐条复核 `pro-plan.md` 的 P0/P1/P2 和 FF-1201～FF-1283。v0.5.5 必须完成决策证据与语义正确性的 M0 门禁；能在现有本地单机契约内安全完成的高优先级项一并修复。需要新 worker、业务实体、外部数据、许可证或仓库治理决策的项目留下具体边界，不创建占位实现。

## 第一轮：证明链和失败关闭

- [x] Schema v18 为 Plan、A、Artifact 增加不可降级的关系型证明要求；旧数据显式迁移为 Legacy。
- [x] POST 重放、GET、列表、同步兼容接口、retry、rerun 和风险比较统一使用校验读取路径。
- [x] 新增 input/result/failure/runtime manifest，绑定请求、政策、上下文、快照、排程、旅行模型、依赖环境和错误。
- [x] 损坏或缺证据的 required 记录进入 `FAILED`，不再向调用方返回 result。
- [x] 修复旧唯一约束迁移丢失 Decision Artifact 的路径，并增加真实 v14 Artifact 保全样本。
- [x] 损坏 JSON 使用 `json_valid` 隔离、quarantine 和稳定 409，不再静默成为可信记录。

## 第二轮：重排、风险和命令恢复

- [x] 普通方案统一使用完整冻结范围；重排成本、容量、风险统一使用发布时剩余范围和同一排程签名。
- [x] 容量尾部追加在空未来路线下仍使用 route entry 的位置、可用时间和返回点。
- [x] 风险事件先独立生成再选择承接技师；空闲技师计算出发、服务、返程和加班。
- [x] 缺勤/突发“发生”与“造成损害”分开，保存 trial 证据；配对比较持久化差值区间和 win/tie/loss。
- [x] exact retry 与 current rerun 都使用持久幂等命令协议；修复命令已预留但 A 未绑定时重启卡死。
- [x] 人工改派锁使用引用计数回收；启动只对账所属分析命名空间。

## 第三轮：数据语义、界面和工程门禁

- [x] 公共实验评分保存完整政策快照；公平 KPI 增加归一化最小/最大负载。
- [x] Booking ID、客户窗口迟到和相对计划偏差分别保存；旧字段保留读取兼容。
- [x] 方案名称迁入 `plan_metadata`；`plan_applicability` 成为当前方案权威并同步场景缓存。
- [x] 跨业务快照比较不再返回误导性 delta；哈希规范增加版本和 Unicode NFC。
- [x] 外包证据明确容量、起止时间和 SLA 未验证，界面不把测算假设显示为承诺。
- [x] 运营复盘增加 Plan/A 完整性、RuntimeManifest、发布上下文、场景集和下载入口。
- [x] 加入 ESLint/React Hooks、局部 strict Pyright、85% 覆盖门槛、Python 依赖一致性检查和固定 SHA 的 GitHub Actions。
- [x] `docs/pro-plan-v0.5.4-assessment.md` 逐项记录完成、部分完成、反驳和所有者决策。

## 必须保持为后续范围

- FF-1220～FF-1224、FF-1228：持久 Job Queue、Outbox、独立求解子进程、硬取消、完整依赖注入和运维 CLI 需要运行架构升级。
- FF-1240～FF-1250：收件箱、完整 Booking、技师端、资产、周期维护、库存、通知和导入导出需要新的业务实体与数据来源。
- FF-1260～FF-1266：incurred/实时 remaining、多日、组合容量和校准风险模型需要执行历史或模型版本升级。
- FF-1280～FF-1283：正式 Benchmark、许可证、治理、单一锁工具和发布材料分别需要独立验证或仓库所有者决定。

## 验证状态

- [x] P0/P1 定向故障注入和迁移测试：通过；迁移触发器与 WAL 初始化回归已发现并修复。
- [x] React 组件 10 项、ESLint、TypeScript 和生产构建：通过。
- [x] 后端完整测试：203 passed；coverage 89.84%，85% 门槛通过。
- [x] Ruff、Pyright、ESLint、React Hooks、TypeScript、OpenAPI、依赖一致性与 Python/npm 审计：通过。
- [x] React 10 项、生产构建、Demo、Benchmark 和 Playwright API 主流程：通过。
- [x] Playwright：本机 API lifecycle 通过；本机页面浏览器受 SIGTRAP/SIGABRT 限制，GitHub Linux CI #36 补证 5/5 通过。
- [x] GitHub Actions #36：`python-compat` 与完整 `fieldflow` 均成功。
