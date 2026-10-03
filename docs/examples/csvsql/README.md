# csvsql：测试通过之后，SQL 字面量仍可能被改坏

csvkit 1.1.1 使用 SQLAlchemy 1.4 的字符串执行接口，数据库流程已有显式事务。
直接安装 SQLAlchemy 2.0 后，字符串执行入口失效；简单改成 `text(query)`
虽然能通过原四项检查，却会把 SQL 中的冒号文本解释成绑定参数。
固定候选改用 `exec_driver_sql()`，并适配结果列名入口。

这一案例展示了“API 替换完成”与“行为保持”之间的区别。检查覆盖 SQLite 的查询、
两份 CSV 联结、插入前后操作的跨连接可见性，以及查询和插入中的冒号字面量。
它不证明所有数据库方言都兼容。

## 无模型复验

从仓库根目录，在 Docker 已运行的情况下执行：

```powershell
uv run --no-sync upgrade-workbench inspect cases/csvkit-csvsql-sqlalchemy-2-a3
uv run --no-sync upgrade-workbench --work-root .local/csvsql-replay verify cases/csvkit-csvsql-sqlalchemy-2-a3 --candidate docs/examples/csvsql/candidate.patch --candidate-origin agent_candidate --check-group all --prepare-timeout 600 --test-timeout 240
```

案例固定 Python 3.9、镜像摘要及带哈希的旧/新依赖锁；不要覆盖成通用 Python 3.12 镜像。
运行产物落在指定工作根，第三方源码只在隔离容器中运行。

| 环境 | 同一组冻结检查的预期 |
| --- | --- |
| 原源码、旧依赖 | 6/6 |
| 原源码、新依赖 | 0/6 |
| 固定修复候选、新依赖 | 6/6 |

最终检查共六项：公开反馈两项、独立验收四项。历史结构化探针为 `inconclusive/invalid_measurement`，
不能把探针视为另一份全绿成绩。当前复验见 [result.json](result.json)。

## 新实现的真实模型运行

另一次无种子新任务的[精选结果](new-run.json)为：3 次模型请求后正常提交，旧版 6/6、
直接升级 0/6、模型候选 4/6，最终 `candidate_not_accepted`。
它先通过两项公开反馈，随后自主提出插入前后效果的跨连接探针并通过，最终仍遗漏两项
冒号字面量验收。该失败候选与上面的正确固定候选是不同对象。
本次只确认新实现能调查、生成、审阅、提交并接受独立验收；不主张自主修复成功。
没有把隐藏失败反馈给模型调优，也没有重新运行直到成功。

后续一次固定的[语义自查比较](../../joint-r2-agent.md)观察到当前配置4/6、可选机制6/6，
但增加调用与费用，模型自选探针仍有区分度错误；另一个转移任务两臂同为4/4。
这些新任务与上面的固定复验、首次新任务各自保留身份，不覆盖原负结果。

## 沿代码复核这个困难

原入口位于 `cases/csvkit-csvsql-sqlalchemy-2-a3/source/csvkit/utilities/csvsql.py`：
`_failsafe_main()` 的插入前、插入后和查询路径都调用 `Connection.execute(query)`，
已有 `begin()` / `commit()`；结果列名访问旧的私有入口 `rows._metadata.keys`。
官方依据位于同案例 `evidence/migration.rst` 的 960–964 行：旧 `Connection.execute(str)`
交给驱动执行，而 `Session.execute(str)` 会转换为 `text()`。两个接收者不能混为一谈。

难点是选择保持输入解释方式的替换，而非仅消除 API 异常。普通查询或提交后行数正确，
都无法证明 SQL 字面量未被重新解释。固定补丁可在本目录逐行查看；上述六项检查分别
验证可执行性、CSV 结果及外部可见效果。额外的模型自选探针是调查证据，不拥有验收权。

程序的链路是 `tasks.py` 准备/推进与审阅 → `diagnostics.py` 绑定测量到具体修订 →
`workflow.py` / `execution` 执行三环境比较 → `reporting.py` 导出结果。
模型负责选补丁与测量；程序限制改动范围、隔离执行并保存身份，最终结果独立判断。
公开复验不会重现历史请求中断；请求未知时禁止盲重发的边界见首页说明。

## 来源与限定

这是对历史 Agent 生成候选的复验，不发送模型请求，不重现历史调查过程或费用。
案例已经用于开发与纠错；归档中 `split: holdout` 是原字段，不能据此称它为本轮未见任务。
原输入字节保留，说明放在案例外，不修改冻结 manifest。

- csvkit 源码 revision：`fa9e0db1cdd30757835912e48467d6ab2be02c14`，MIT，见案例 `source/COPYING`。
- SQLAlchemy 迁移依据 revision：`23f5f3350974d9452f3b844617c31eb5b41474ae`，MIT，见案例 `evidence/LICENSE`。
- 案例指纹：`353eb4b92795313b2db2326bcf5a62ee001b620ba3d1d4008492dcfd7fe1dd24`。
- 候选 SHA256：`d3d5311cb51e33b18b63a9fb027c22bd2d9de8c76e8f78a60074a88d9679d16e`。

目录只携带复验所需输入、候选和精选结果，不包含模型原始回复、账本或大量容器日志。
