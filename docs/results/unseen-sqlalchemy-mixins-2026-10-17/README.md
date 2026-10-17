# 未见仓库迁移：sqlalchemy-mixins 1.5.3 → SQLAlchemy 2.0

日期：2026-10-17（UTC）。状态：**Agent 候选通过独立验收（`candidate_verified`）**。

我们选了一个本工作台开发期间从没用过的公开仓库，在任何模型请求之前冻结了合同和检查，
再走一次普通任务流程。结果是：旧环境 19/19，直接升级 9/19，Agent 候选 19/19。
共 24 次 deepseek-flash 请求，估算 0.844074 美元。这是**单个案例的一次运行**，
不能用来推断成功率、跨仓库泛化，也不能说明它比其他 Agent 或人工更好。

## 仓库与固定版本

- 上游：[absent1706/sqlalchemy-mixins](https://github.com/absent1706/sqlalchemy-mixins)，
  标签 v1.5.3，提交 `ce4badbc51f8049783fa3909615ccfc7c1198d98`（2022-05-03），MIT。
- 归档 SHA-256：`6410a63ab5312e8aa78dad7997e9cb4da3c73d3d224b51718db70e85f47a3d7b`。
  库源码 10 个 Python 模块，约 910 行；上游测试原样放在案例的 `upstream/` 下，只作来源留存，不当检查运行。
- 依赖：Python 3.9 固定镜像，`SQLAlchemy 1.4.54 → 2.0.38`。`six 1.16.0`、`pytest 7.4.4`
  及其余带哈希锁定的依赖两边一致。
- 案例：[`cases/sqlalchemy-mixins-sqlalchemy-2`](../../../cases/sqlalchemy-mixins-sqlalchemy-2/business-contract.md)，
  指纹 `862caaa6d0b448b7a1b553841a34a60f09c56461155383251dd144e5fed940de`。

## 为什么选它

- **此前没出现过。** `cases/`、`docs/`、`tools/`、`src/` 里都没有这个仓库的名字。
  “未见”只是说本工作台开发时没用过它，**并不能排除模型预训练见过它**，包括它后来公开发布的修复。
- **迁移需求是真实的。** v1.5.3 只支持到 SQLAlchemy 1.4；上游后来在 v2.0.0（提交 `2c78dfd`，
  2023-03-21）才加上 2.0 支持。这个修复以及相关 issue 和 PR，在编写合同和检查时没有读过，也没有提供给模型。
- **规模够小、失败有意义。** 直接升级时，10 项检查都在 `ArgumentError: Strings are not
  accepted for attribute names in loader options` 处失败。这是 2.0 的真实接口变化：
  预加载选项不再接受字符串关系名。单改一个导入名修不好，需要把字符串路径解析成映射属性，
  同时保持嵌套预加载策略和查询次数。

## 预先登记了什么

顺序记录在 [preregistration.json](preregistration.json) 中，时间均为 UTC：

| 步骤 | 时间 | 模型请求数 |
| --- | --- | --- |
| 合同、条目和两组检查计算哈希 | 13:25:34 | 0 |
| Docker 准入（旧 / 直接升级） | 13:25:42 | 0 |
| 封存清单后创建任务 | 13:29:26 | 0 |
| 准备第 1 次模型请求 | 13:29:33 | — |
| 任务终态后独立验收 | 14:06:41 | 24 |

- [业务合同](../../../cases/sqlalchemy-mixins-sqlalchemy-2/business-contract.md)和 8 条结构化条目，
  依据见 [oracle-sources](../../../cases/sqlalchemy-mixins-sqlalchemy-2/oracle-sources.md)。
  依据来自固定版本的 README、docstring 和上游测试中描述的行为；检查是新写的，没有照抄上游测试。
- 公开反馈 6 项，修复过程中可见。独立验收 13 项，模型看不到，只在终态后运行。
  覆盖嵌套预加载 schema、自动预加载、`lazy="dynamic"` 关系上的 `smart_query`、
  嵌套 `or_`/`and_`、别名过滤、序列化、检视属性、时间戳，以及“只 flush，提交权在调用方”。
- 封存清单里的检查字节，与 13:25:34 记录的哈希逐项相同。第一次模型请求之后，检查没有改过。
  冻结之前有一处编写期调整：把一个参数化测试拆成两个普通测试，因为评价分组用 AST 推导 nodeid。
  这次调整发生在准入之前。
- 先解决了一处冲突：README 推荐 `autocommit=True` 会话，而 2.0 已经删除了这个模式。
  合同不要求这个模式，检查使用调用方显式提交，和上游测试一致。
- 允许修改 `sqlalchemy_mixins/` 下的 10 个 `.py` 模块；`.pyi`、README 和许可不在修改范围内。
- 版本依据复用此前案例中已核验哈希的 SQLAlchemy `migration_20.rst`
  （提交 `23f5f33`），按标题重新登记 5 个 ORM 相关章节，没有写入修法提示。

## 模型、调用与费用

配置见 [run-config.json](run-config.json)，与 csvsql A3 使用的 P6 配置相同：
`deepseek-flash`、thinking 开启、每任务最多 24 次请求、
受限 Contract Auditor、无官方种子、BM25 默认检索。没有使用 Pro 或 reasoner 模型。

| 项目 | 数值 |
| --- | --- |
| 模型请求 | 24 次（Solver 22，Contract Auditor 2），未知用量 0 |
| 输入 / 输出 token | 1,407,680 / 351,466 |
| 估算费用 | **0.844074 USD**（按 0.30 / 1.20 美元每百万 token 的标价，不是账单；共享账本增量与此一致） |
| 供应商请求累计 | 1517.9 秒 |
| 创建至独立验收 | 约 37 分钟，包含审阅等待和 Docker 时间，不是无人值守时延 |
| 只读依赖查询 | 7 次 |
| 受审诊断 | 6 次：原始源码公开检查 1 次，新探针 3 份，在 r3 上重跑探针 2 次；另有每个候选批准后的公开检查 3 次 |

## 过程与结果

| 修订 | 内容 | 公开检查 |
| --- | --- | --- |
| 原始源码 | — | 3/6 |
| r1 `b1f7e435` | 把字符串和点号路径解析成类绑定属性，再逐级链式调用 `joinedload`/`subqueryload`；`contains_eager` 改为基于 `of_type(alias)` 的链；`AppenderQuery` 在 2.0 下转换为普通 Query | 6/6 |
| r2 `932ca1b3` | 只删掉上游遗留的 `print(flat_schema)` | 6/6 |
| r3 `ff00b9e9` | 每条最长 schema 路径只生成一条预加载链，避免父层 `subqueryload` 和子层 `joinedload` 的策略冲突 | 6/6 |

r2 公开检查全过之后，Agent 先自己写了一个探针，检查普通 `lazy="dynamic"` 关系上的
`smart_query`，三个环境都没有观察到反例。接着 Contract Auditor 先后提出两个具体疑问，
Agent 分别写了探针：

- dynamic 查询上的 limit 可能丢失：这个探针在**旧环境也报错**
  （`Query.filter()` 不能在已有 LIMIT 的查询上调用），设计无效，不算发现了缺陷。
- 嵌套 schema 的预加载策略可能冲突：旧环境测得，r2 在新环境执行失败。
  Agent 据此修出 r3，在 r3 上重跑，测得差值为 0。

探针不参与验收。Agent 以 `candidate_ready` 提交，第 24 次请求正好是调用上限。
独立验收结果如下：

| 环境 | 结果 | 通过 / 收集 |
| --- | --- | --- |
| 旧依赖 + 原始源码 | passed | 19 / 19 |
| 新依赖 + 原始源码 | failed | 9 / 19 |
| 新依赖 + Agent 候选 r3 | **passed** | **19 / 19** |

最终补丁 [candidate.patch](candidate.patch)，SHA-256 `7098ddcd…8cdbc`，只改了 `eagerload.py`
和 `smartquery.py`，+117 / −22 行。完整报告见 [task-report.md](task-report.md)。

**事后对照（不属于本轮成绩）：** 终态冻结之后，用同一组 19 项检查回放 r2，得到 17/19。
失败的是两项嵌套 schema 验收。也就是说，如果 Agent 在公开 6/6 时就停下，这一轮会是
`candidate_not_accepted`。这次运行中，审阅者疑问加上 Agent 自己的探针确实改变了结果，
但这只是一次观察，不能证明 Auditor 或探针机制稳定有效。

## 人工参与

维护者负责选仓库、编写合同和检查、Docker 准入、执行安全审阅，
以及运行独立验收和撰写本文。审阅内容：批准 3 个候选，接受 6 次诊断（公开检查和
无文件、网络、进程访问的内存 SQLite 探针），没有拒绝。没有手改候选，没有向模型提示根因或修法，
也没有反馈独立验收结果。模型负责定位、补丁、探针设计和退出判断。人工审阅时间没有测量。

## 限制

- 只有一个仓库、一次运行、没有对照臂。不能得出成功率、成本优势或“优于裸模型/其他 Agent”的结论。
- 检查是维护者写的，不是上游测试，也没有第三方独立复核。依据和冲突处理都已公开，但 oracle 的独立性仍然有限。
- 只覆盖 SQLite 和合同列出的行为。PostgreSQL/MySQL 方言、异步、`Query.get` 弃用告警、
  类型存根 `.pyi` 和 2.0 风格的 `select()` 接口都没有验证。
- 没有和上游 v2.0.0 的修复做对比。也不知道模型是否在预训练中见过那个修复。
- 第 24 次请求时提交，正好用完上限。调用上限更低的话，结果可能不同。

## 复核

无需模型 API，Docker 运行时从仓库根目录执行：

```powershell
uv run --no-sync upgrade-workbench inspect cases/sqlalchemy-mixins-sqlalchemy-2
uv run --no-sync upgrade-workbench --work-root .local/unseen-sam-replay verify cases/sqlalchemy-mixins-sqlalchemy-2 --candidate docs/results/unseen-sqlalchemy-mixins-2026-10-17/candidate.patch --candidate-origin agent_candidate --check-group all --prepare-timeout 600 --test-timeout 240
```

案例的获取、准入和封存脚本是 `tools/dev/acquire_sqlalchemy_mixins_case.py`。
原始任务、请求轨迹和准入日志没有公开，不在本目录。
