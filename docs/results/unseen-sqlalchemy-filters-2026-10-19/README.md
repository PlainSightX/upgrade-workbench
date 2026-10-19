# 未见仓库迁移：sqlalchemy-filters 0.13.0 → SQLAlchemy 2.0

日期：2026-10-19（UTC）。状态：**Agent 候选通过独立验收（`candidate_verified`）**。

这是第二批未见仓库中的一个。合同和检查在任何模型请求之前冻结，然后走一次普通任务流程。
结果：旧环境 18/18，直接升级 15/18，Agent 候选 18/18。共 13 次 deepseek-flash 请求，
估算 0.314502 美元。这是**单个案例的一次运行**，不能用来推断成功率或跨仓库泛化。
四个未见仓库的汇总见 [unseen-summary](../unseen-summary-2026-10-19/README.md)。

## 仓库与固定版本

- 上游：[juliotrigo/sqlalchemy-filters](https://github.com/juliotrigo/sqlalchemy-filters)，
  标签 v0.13.0，提交 `e182378a7a53f05b8c11f9889787bd5b12579a5c`（2023-04-13），Apache-2.0。
- 归档 SHA-256：`45844a5500b1e0aafc4e2b7a40a555cc15caa88a532dc05b3af8b9af311f01d3`。
  库源码 7 个 Python 模块，约 790 行；上游测试原样放在案例的 `upstream/` 下，只作来源留存，不当检查运行。
- 依赖：Python 3.9 固定镜像，`SQLAlchemy 1.4.54 → 2.0.38`。`six 1.16.0`、`pytest 7.4.4`
  及其余带哈希锁定的依赖两边一致。
- 案例：[`cases/sqlalchemy-filters-sqlalchemy-2`](../../../cases/sqlalchemy-filters-sqlalchemy-2/business-contract.md)，
  指纹 `e2fff6009e9cc94742e6ab25a46197314fdb4703fc518f80ee7275ce41f453c1`。

## 为什么选它

- **此前没出现过。** `cases/`、`docs/`、`tools/`、`src/` 里都没有这个仓库的名字。
  “未见”只指本工作台开发时没用过，**不能排除模型预训练见过它**。
- **迁移需求是真实的。** v0.13.0 的 CI 明确写着尚不支持 SQLAlchemy 2.0，PyPI 上至今没有
  支持 2.0 的版本。因此这里不存在上游官方修复可读；网上可能有分支或第三方补丁，
  编写合同和检查时没有查找或阅读，也没有提供给模型。预训练中见过类似补丁的风险仍然存在。
- **规模小、失败有意义。** 直接升级时，3 项检查失败：混合属性（`hybrid_property` /
  `hybrid_method`）在 2.0 中的 `extension_type` 变成枚举，库按旧 `symbol` 比较，报
  `FieldNotFound`；无列查询的模型推断依赖已删除的私有属性 `Query._legacy_setup_joins`。

## 预先登记了什么

顺序记录在 [preregistration.json](preregistration.json) 中，时间均为 UTC：

| 步骤 | 时间 | 模型请求数 |
| --- | --- | --- |
| 合同、条目和两组检查计算哈希 | 02:52:21 | 0 |
| Docker 准入（旧 / 直接升级） | 02:52:27 | 0 |
| 封存清单后创建任务 | 02:55:53 | 0 |
| 准备第 1 次模型请求 | 02:56:51 | — |
| 任务终态后独立验收 | 03:25:32 | 13 |

- [业务合同](../../../cases/sqlalchemy-filters-sqlalchemy-2/business-contract.md)和 7 条结构化条目，
  依据见 [oracle-sources](../../../cases/sqlalchemy-filters-sqlalchemy-2/oracle-sources.md)。
  依据来自固定版本的 README、docstring 和上游测试描述的行为；检查是新写的，没有照抄上游测试。
- 公开反馈 6 项，修复过程中可见。独立验收 12 项，模型看不到，只在终态后运行。
  覆盖自动连接（过滤、排序、限制加载）、关闭自动连接、嵌套布尔函数、字段/聚合/`select_from`
  查询、操作符、空值排序、`apply_loads` 实际不选取的列、分页边界和错误类型。
- 封存清单里的检查字节，与 02:52:21 记录的哈希逐项相同。第一次模型请求之后，检查没有改过。
- 冻结之前做过两次 Docker 编写期试运行（无模型请求）。第一次旧环境 16/18，是检查本身写错：
  一处把“已显式连接的多模型查询仍可省略 model”当成了文档行为，另一处“无模型查询抛 `BadQuery`”
  在开启自动连接时会先在 `auto_join` 里失败。两处都按文档改成显式 model 和 `do_auto_join=False`，
  第二次试运行旧环境 18/18，其输入字节与正式准入完全相同。
- 允许修改 `sqlalchemy_filters/` 下的 7 个 `.py` 模块；README 和许可不在修改范围内。
- 版本依据复用此前案例中已核验哈希的 SQLAlchemy `migration_20.rst`（提交 `23f5f33`），
  登记与 sqlalchemy-mixins 相同的 5 个 ORM 章节，没有按已知失败挑选，也没有写入修法提示。

## 模型、调用与费用

配置见 [run-config.json](run-config.json)，与 sqlalchemy-mixins 逐字节相同（P6）：`deepseek-flash`、
thinking 开启、每任务最多 24 次请求、受限 Contract Auditor、无官方种子、BM25 默认检索。
任务冻结的实现身份摘要也与该次相同。没有使用 Pro 或 reasoner 模型。

| 项目 | 数值 |
| --- | --- |
| 模型请求 | 13 次（Solver 12，Contract Auditor 1），未知用量 0 |
| 输入 / 输出 token | 467,497 / 145,205 |
| 估算费用 | **0.314502 USD**（按 0.30 / 1.20 美元每百万 token 的标价，不是账单；共享账本结算与此一致） |
| 供应商请求累计 | 598.0 秒 |
| 创建至独立验收 | 约 30 分钟，包含审阅等待和 Docker 时间，不是无人值守时延 |
| 只读依赖查询 | 8 次 |
| 受审诊断 | 1 次：原始源码公开检查（新环境 5/6）；另有候选批准后的公开检查 1 次 |

## 过程与结果

| 修订 | 内容 | 公开检查 |
| --- | --- | --- |
| 原始源码 | — | 5/6 |
| r1 `1dbeb0c3` | 混合属性判断同时接受 1.4 的 `symbol` 和 2.0 的枚举名/值；模型推断与自动连接对私有属性改用 `getattr` 兜底；排序优先使用 `nulls_first()` / `nulls_last()` | 6/6 |

Agent 先查了 8 次依赖事实，请求了一次原始源码的公开检查，然后一次提交候选。Contract Auditor
没有提出具体疑问，但指出 `apply_loads`、错误类型和整体迁移条目在公开反馈中没有观察。
Agent 在退出声明里把这三条如实标为 `unobserved`，以 `candidate_ready` 提交。独立验收结果如下：

| 环境 | 结果 | 通过 / 收集 |
| --- | --- | --- |
| 旧依赖 + 原始源码 | passed | 18 / 18 |
| 新依赖 + 原始源码 | failed | 15 / 18 |
| 新依赖 + Agent 候选 r1 | **passed** | **18 / 18** |

最终补丁 [candidate.patch](candidate.patch)，SHA-256 `46fb5258…cba5`，只改了 `models.py` 和
`sorting.py`，+30 / −7 行。完整报告见 [task-report.md](task-report.md)。

## 人工参与

维护者负责选仓库、编写合同和检查、Docker 准入、执行安全审阅、运行独立验收和撰写本文。
审阅内容：批准 1 个候选，接受 1 次诊断（原始源码公开检查），没有拒绝。没有手改候选，
没有向模型提示根因或修法，也没有反馈独立验收结果。人工审阅时间没有测量。

## 限制

- 只有一个仓库、一次运行、没有对照臂。
- 检查是维护者写的，不是上游测试，也没有第三方复核。冻结前按旧环境结果修正过两处检查，修正理由见上文。
- 补丁对私有属性用 `getattr` 兜底，在本案例覆盖的查询形态下通过，但没有证明私有接口在所有查询形态下等价；
  PostgreSQL 专用的 `any` / `not_any`、MySQL、异步和 2.0 风格的 `select()` 都没有验证。
- 不知道模型是否在预训练中见过这个仓库或其第三方补丁。

## 复核

无需模型 API，Docker 运行时从仓库根目录执行：

```powershell
uv run --no-sync upgrade-workbench inspect cases/sqlalchemy-filters-sqlalchemy-2
uv run --no-sync upgrade-workbench --work-root .local/unseen-sqlf-replay verify cases/sqlalchemy-filters-sqlalchemy-2 --candidate docs/results/unseen-sqlalchemy-filters-2026-10-19/candidate.patch --candidate-origin agent_candidate --check-group all --prepare-timeout 600 --test-timeout 240
```

案例的获取、冻结、准入和封存脚本是 `tools/dev/acquire_unseen_batch_cases.py`。
原始任务、请求轨迹和准入日志没有公开，不在本目录。
