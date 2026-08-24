# FieldFlow Local 第二轮任务记录

更新时间：2026-08-24

## 当前状态

- [x] 完整读取新 `pro-plan.md`。
- [x] 检查工作区、分支和远端；当前仅 `pro-plan.md` 为用户提供的未跟踪文件。
- [x] 建立本轮规格、范围和验收标准。
- [x] 运行修改前基线：Ruff、TypeScript、37 项后端测试、4 项 React 测试、Vite 构建和 Demo 均通过；后端覆盖率 88.78%。
- [x] 建立 P0/P1 逐项证据矩阵。
- [x] 第一轮：业务事实、计划覆盖、标准化和 Run 原子性。
- [x] 第一轮完整回归。
- [x] 第二轮：恢复语义、幂等、应用生命周期、实验治理和安全边界。
- [x] 第二轮完整后端回归与边界故障注入。
- [x] 最终独立复审和逐项结论。

## 已确认发现

- P0-01 成立：复合紧急重排失败时，新工单没有写入当前场景；现有测试把消失当作正确结果。
- P0-02 风险成立，但旧行为来自上一轮明确选择的“完整快照恢复为新版本”。处理方式是拆分激活、克隆和显式回滚，而不是简单删除回滚能力。
- P0-03 部分成立：`replan_schedule` 会用计划时间推断在途并冻结，Verifier 只强制 started/completed，二者定义不一致。
- P0-04 成立：Verifier 重算 KPI 时仍直接信任 assignment 的 `changed`；`locked` 和解释也没有统一重建。
- P1-01 成立：Candidate 与 Run 使用两次独立提交。
- P1-02 成立：baseline、optimize 和历史恢复没有统一幂等键。
- P1-03 成立：标签和策略名称在 Pydantic 长度校验后才 `strip()`。
- P1-04 成立：TravelTimeProvider 存在，但主要路径仍走全局默认 provider。
- P1-05 是真实运行时缺口，但需要子进程 worker 和 API 过渡契约；本轮不用未使用的队列枚举假装完成。
- P1-06 成立：已增加 profile 去重/上限、4 槽有界队列、协作取消、部分成功和显式获胜者。
- P1-07 成立：当前比较接口不说明两个计划是否来自同一需求快照。
- P1-08 是产品扩展；当前三状态只适合 Demo。先停止把计划时间描述为真实在途，再评估完整事件闭环。
- P1-09 成立：导入 `backend.main` 会创建 Store 并初始化数据库。
- P1-10 是容量风险，不是当前数据损坏证据；迁移 v1 清历史是此前用户明确选择且有备份，不能写成静默丢失。
- P1-11 中动态时间轴、多出发点、跨日控件和 AbortController 已在上一轮实现；“仍全部缺失”的结论不成立。部分覆盖状态尚未实现。
- P1-12 部分成立：默认 loopback 和 CORS 已存在；TrustedHost、严格 Origin 和写接口令牌尚未实现。
- P1-13 成立：仓库没有 LICENSE；本轮不能替所有者选择法律条款。

## 验证记录

### 修改前基线

```text
make lint          通过
make test          37 passed，coverage 88.78%
make test-frontend 4 passed
make build         通过
make demo-check    通过
```

仅有 OR-Tools SWIG 类型缺少 `__module__` 的 3 条上游弃用提示。

### 第一轮

已完成：

- Schema v5 增加幂等命令记录，并约束一个 Run 只能对应一个 Candidate。
- 紧急工单先写 D 修订；失败重排保留工单、最后发布 V 和 `PARTIAL_NEW_DEMAND` 状态。
- 失败请求可用相同幂等键重放，返回原 Run，不重复求解或写入工单。
- `PlanningContext` 保存冻结项、来源和计划时间；只有 started/completed 自动冻结，计划时间推断只生成警告。
- 新增 `ScheduleNormalizer`，重算旅行、SLA、锁定、变化、解释、证据、KPI 和业务评分。
- Scheduler、Normalizer 和 Verifier 可显式接收同一 TravelTimeProvider。
- Candidate 和 Run 完成合并为一个事务；触发器失败注入证明两边同时回滚。
- 版本比较增加快照可比性和增删改工单摘要；前端对不同需求显示警告。
- 字符串改为去空白后再校验，颜色使用六位十六进制约束。

额外发现并修复：重排时点晚于技师最大可用时间时，OR-Tools 原先会因非法 `SetRange` 抛出 500；现在该技师会被视为无剩余容量，返回正常诊断。

```text
make lint          通过
make test          44 passed，coverage 88.22%
make test-frontend 4 passed
make build         通过
make demo-check    通过
```

第一轮留下的历史操作语义、baseline/optimize 幂等、应用启动副作用、策略实验治理和本地 Web 边界已转入第二轮处理。

### 第二轮

已完成：

- 历史页拆分“重新激活”、“克隆场景”和“回滚业务数据”，后者需差异预览、原因、确认令牌和 D 修订校验。
- baseline、optimize、replan 和历史动作使用按场景/动作隔离的幂等键；重试不新增 Run 或 V。
- 实验限制 profile 数量和排队容量，支持取消、部分成功和显式 winner；全部失败不再标记普通完成。
- 应用导入不再创建数据库或线程；Store 和 worker 由 lifespan 管理。
- 增加 TrustedHost、本地 Origin 检查和报告文件名清洗。
- Patch 区分 omitted/null/显式清空，空 Patch 不再浪费 D 修订。

本轮额外发现并修复：

- 策略页会把不可发布但分数较低的候选标为推荐。
- 对比页在基准目标为 0 时可能显示无穷百分比。
- 指纹序列化不会递归处理容器内的 Pydantic 对象。
- 突发工单已入库、但初始重排准备失败时，命令会停在 `INTAKE_COMMITTED`，重试又会重复进入准备。现在会记录结构化失败并稳定重放。
- 业务回滚成功后 D 会变化，原实现因先检查旧 D 而无法幂等重放。现在先按来源版本和原请求查找已完成发布。
- Store 事务内的最终发布校验仍使用默认行程模型，会拒绝使用注入矩阵产生的正常候选。现在 Store、API、求解、标准化和发布复核共用一个 provider。
- 技师 Patch 合并后的班次校验错误没有转成 422；现在与工单 Patch 保持一致。
- 小字号虽已不低于 11px，但时间轴、图例和次要标题仍有低于 4.5:1 的灰字。已换为统一的 `--ink-soft`，并取消被支配候选整行透明度。

```text
make test          54 passed，coverage 88.30%
make lint          通过
实验取消/部分失败/队列满载故障注入  通过
```

最终复审结果：

```text
make lint          通过
make test          54 passed，coverage 88.30%
make test-frontend 4 passed
make build         通过
make demo-check    通过
git diff --check   通过
CSS 业务文字下限  通过（显式最小值 11px）
```

`make test-e2e` 已尝试两种浏览器。本机 Google Chrome 在当前 macOS 沙箱中以 `SIGABRT` 退出，Playwright 缓存的 headless Chromium 以 `SIGTRAP` 退出；3 项测试均未进入页面或执行断言。这项不记为通过，需在允许启动浏览器的本机或 CI 环境补跑。
