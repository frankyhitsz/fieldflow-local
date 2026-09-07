# 历史数据库固件

这两个库由未修改的历史提交运行 API 生成，不是把当前库的 `user_version` 改小。

| 固件 | 生成提交 | schema | V / A / Artifact / 风险比较 / E |
| --- | --- | --- | --- |
| v0510.sqlite.gz | `4d75b5c` | 23 | 2 / 6 / 14 / 1 / 3 |
| v0511.sqlite.gz | `35d3d6b41e3d193819a93937f8ea133d3310b27d` | 27 | 2 / 6 / 14 / 1 / 3 |

两者包含成本、容量、Risk SUMMARY、Risk FULL（非空 50-trial 明细）、配对风险比较，以及一张已完成和一张服务中的工单。生成脚本在旧版本中先检查分析及比较为 VERIFIED，再关闭应用并压缩数据库。

生成方式：建立对应提交的临时 worktree，使用当前测试依赖运行仓库中的 `scripts/generate_legacy_fixture.py`，把 `PYTHONPATH` 指向历史 worktree。脚本输出实际加载的 `backend/main.py` 路径。时间与 ID 是生成时的真实值，不声称重新生成的文件逐字节相同。

SHA-256（压缩文件）：

```text
ecd404a4417ce21d01ec525705446c52db0e454cdad0b1ecf6c20ca070bea177  v0510.sqlite.gz
31c9519a6223c80b96d73b9abd6f1e608a79f83f9c3bcde81d8bcc0554a49268  v0511.sqlite.gz
```

迁移测试逐条比较原 Artifact hash、原执行事件 payload、依赖比较的有效性及执行身份；schema 27 的 Blob 引用先解码再与升级结果比较。
