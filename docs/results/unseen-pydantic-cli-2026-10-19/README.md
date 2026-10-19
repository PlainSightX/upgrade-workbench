# 未见仓库迁移：pydantic-cli 4.3.0 → Pydantic 2

日期：2026-10-19（UTC）。状态：**Agent 候选通过独立验收（`candidate_verified`），但任务以调用上限结束（`budget_exhausted`）**。

这是第二批未见仓库中的一个。合同和检查在任何模型请求之前冻结，然后走一次普通任务流程。
结果：旧环境 17/17，直接升级 0/17（两组检查都在导入时出错），Agent 候选 17/17。
共 24 次 deepseek-flash 请求，正好用完上限，估算 0.810312 美元。Agent 没有给出退出声明，
是调用上限让任务停下的。这是**单个案例的一次运行**，不能用来推断成功率或跨仓库泛化。
四个未见仓库的汇总见 [unseen-summary](../unseen-summary-2026-10-19/README.md)。

## 仓库与固定版本

- 上游：[mpkocher/pydantic-cli](https://github.com/mpkocher/pydantic-cli)，
  标签 v4.3.0，提交 `50d1edaa239a9b1d16e4f089f5151bd0930d1329`（2022-03-03），MIT。
- 归档 SHA-256：`99d9ca068bd0820399abe99caf50da5bb7cb65d193ff73fe8b5e8d875dda1ce9`。
  库源码 6 个 Python 模块，约 1,100 行；上游示例和测试原样放在案例的 `upstream/` 下，只作来源留存，不当检查运行。
- 依赖：Python 3.9 固定镜像，`pydantic 1.10.15 → 2.6.4`。`pytest 7.4.4`、`typing-extensions 4.12.2`
  及其余带哈希锁定的依赖两边一致。
- 案例：[`cases/pydantic-cli-pydantic-2`](../../../cases/pydantic-cli-pydantic-2/business-contract.md)，
  指纹 `3843c1fac0bc86b61ccb59faf894320b5baf6957e32b3d50ab335ab710b67fd6`。

## 为什么选它

- **此前没出现过。** `cases/`、`docs/`、`tools/`、`src/` 里都没有这个仓库的名字。
  “未见”只指本工作台开发时没用过，**不能排除模型预训练见过它**。
- **迁移需求是真实的。** v4.3.0 声明 `pydantic>=1.5,<2`；上游在 PyPI 上下一个版本是 v7.0.0
  （2024-08-23）才支持 Pydantic 2，中间没有发布 5.x 或 6.x。那次发布及其提交、issue 在编写合同和检查时
  没有读过，也没有提供给模型。修复发布于 2024 年中，预训练见过的可能性存在，但项目较小众。
- **失败有意义。** 直接升级时导入就失败：`pydantic.fields.ModelField` 在 2.0 中已删除。
  修复不只是改导入名，还要把字段形状、可空、必填、`cli=` 额外参数和用户 `class Config` 里的
  `CLI_*` 设置都换到 2.0 的 `FieldInfo` / `model_fields` 上，同时保留 README 定义的布尔开关语义。

## 预先登记了什么

顺序记录在 [preregistration.json](preregistration.json) 中，时间均为 UTC：

| 步骤 | 时间 | 模型请求数 |
| --- | --- | --- |
| 合同、条目和两组检查计算哈希 | 02:52:20 | 0 |
| Docker 准入（旧 / 直接升级） | 02:52:27 | 0 |
| 封存清单后创建任务 | 02:55:51 | 0 |
| 准备第 1 次模型请求 | 02:56:51 | — |
| 任务终态后独立验收 | 04:16:33 | 24 |

- [业务合同](../../../cases/pydantic-cli-pydantic-2/business-contract.md)和 7 条结构化条目，
  依据见 [oracle-sources](../../../cases/pydantic-cli-pydantic-2/oracle-sources.md)。
  依据来自固定版本的 README、模块 docstring 和上游示例与测试描述的行为；检查是新写的。
- 公开反馈 6 项，修复过程中可见。独立验收 11 项，模型看不到，只在终态后运行。
  覆盖必填和可空布尔、默认为 True 的反向开关、自定义前缀、多值与枚举、校验失败、
  退出码与异常处理、prologue / epilogue、`--help` / `--version`、JSON 预设的环境变量与自定义键。
- 检查里的可空字段一律显式写 `= None`，因为 Pydantic 2 改变了裸 `Optional[...]` 的默认值；
  这样检查测的是库，而不是用户模型自身的语义变化。
- 封存清单里的检查字节，与 02:52:20 记录的哈希逐项相同。第一次模型请求之后，检查没有改过。
- 冻结之前做过两次 Docker 编写期试运行（无模型请求）。第一次旧环境 16/17，原因是检查直接调用
  `pydantic_class_to_parser(...)` 时没有传 `default_value_override`，旧代码的默认值 `...` 不能当字典用。
  改为显式传 `{}` 后第二次试运行旧环境 17/17，输入字节与正式准入完全相同。
- 允许修改 `pydantic_cli/` 下的 6 个 `.py` 模块；README 和许可不在修改范围内。
- 版本依据与 Flask-Pydantic 案例相同：已核验哈希的 Pydantic `docs/migration.md`，7 个通用章节。

## 模型、调用与费用

配置见 [run-config.json](run-config.json)，与 sqlalchemy-mixins 逐字节相同（P6），实现身份摘要也相同。
没有使用 Pro 或 reasoner 模型。

| 项目 | 数值 |
| --- | --- |
| 模型请求 | 24 次（Solver 23，Contract Auditor 1），未知用量 0 |
| 输入 / 输出 token | 1,468,584 / 308,106 |
| 估算费用 | **0.810312 USD**（按 0.30 / 1.20 美元每百万 token 的标价，不是账单；共享账本结算与此一致） |
| 供应商请求累计 | 1287.5 秒 |
| 创建至独立验收 | 约 81 分钟，包含审阅等待和 Docker 时间，不是无人值守时延 |
| 只读依赖查询 | 6 次 |
| 受审诊断 | 7 次：新探针 3 份，在后续 revision 上重跑 4 次；另有每个候选批准后的公开检查 4 次 |

## 过程与结果

| 修订 | 内容 | 公开检查 |
| --- | --- | --- |
| 原始源码 | — | 0/6（导入失败） |
| r1 `f4f06594` | 字段处理改用 `FieldInfo` / `model_fields`；`cli=` 从 `json_schema_extra` 读取；可空、多值和类型从注解推导；读取用户 `class Config` 的 `CLI_*` 设置；`CliConfig` 改用 `ConfigDict` | 6/6 |
| r2 `d9b1e42e` | 子命令的 JSON 预设读取改用复制后的配置 | 6/6 |
| r3 `5564947c` | 默认值覆盖参数省略时按“未提供”处理 | 6/6 |
| r4 `f4977ea7` | 子命令解析器的 `--help` 改为与单命令相同的提前终止动作，使其返回 0 | 6/6 |

r2 公开检查全过之后，Agent 先后写了 3 份探针：校验与钩子、多值/枚举与帮助文本、`--help` / `--version`。多值探针在 r2 上三个环境都执行失败，没有有效读数；Agent 修出 r3（默认值覆盖参数省略时的处理）
后同一探针测得 0。`--help` 探针在 r3 上**旧环境和新环境都测得 1 个违反**：子命令解析器的
`--help` 在原始 v4.3.0 上本来就返回非零。Agent 把它当成缺陷修成 r4，之后重跑测得 0。
随后 Agent 在 r4 上重跑探针，Contract Auditor 指出布尔开关和多值字段两条只有很少的公开断言；
第 24 次请求时达到上限，任务以 `budget_exhausted` 结束，没有退出声明。
按普通流程，终态且候选已审阅即可做独立验收：

| 环境 | 结果 | 通过 / 收集 |
| --- | --- | --- |
| 旧依赖 + 原始源码 | passed | 17 / 17 |
| 新依赖 + 原始源码 | failed | 0 / 0（2 个导入错误） |
| 新依赖 + Agent 候选 r4 | **passed** | **17 / 17** |

最终补丁 [candidate.patch](candidate.patch)，SHA-256 `89fd4477…2488`，只改了 `__init__.py` 和
`core.py`，+134 / −53 行。完整报告见 [task-report.md](task-report.md)。

**事后对照（不属于本轮成绩）：** 终态冻结之后，用同一组 17 项检查回放 r1、r2、r3，三者都是 17/17。
本组检查在 r1 时就已全部满足。后续三次修订处理的是检查没有覆盖的路径；其中 r4 改变了原始版本
子命令 `--help` 的退出码，这超出了“保持旧行为”的要求，合同没有登记该行为，检查也没有覆盖。

## 人工参与

维护者负责选仓库、编写合同和检查、Docker 准入、执行安全审阅、运行独立验收和撰写本文。
审阅内容：批准 4 个候选，接受 7 次诊断（探针只在内存中构造模型并调用 `to_runner`），没有拒绝。
没有手改候选，没有向模型提示根因或修法，也没有反馈独立验收结果。人工审阅时间没有测量。

## 限制

- 只有一个仓库、一次运行、没有对照臂。
- 任务没有由 Agent 主动结束，而是用完 24 次调用被截停；调用上限更低时，候选仍是 r1，事后回放显示
  它在本组检查上同样通过。这说明本组检查对后三次修订没有区分力，也说明 24 次中有相当部分用于检查覆盖之外的调查。
- 检查是维护者写的，冻结前按旧环境结果修正过一处检查调用，修正理由见上文。
- `shtab` 补全、`CLI_EXTRA_OPTIONS`、`run_and_exit` 的进程退出路径、Pydantic 2 的弃用告警都没有验证。
- 不知道模型是否在预训练中见过上游 v7.0.0 的修复。

## 复核

无需模型 API，Docker 运行时从仓库根目录执行：

```powershell
uv run --no-sync upgrade-workbench inspect cases/pydantic-cli-pydantic-2
uv run --no-sync upgrade-workbench --work-root .local/unseen-pcli-replay verify cases/pydantic-cli-pydantic-2 --candidate docs/results/unseen-pydantic-cli-2026-10-19/candidate.patch --candidate-origin agent_candidate --check-group all --prepare-timeout 600 --test-timeout 240
```

案例的获取、冻结、准入和封存脚本是 `tools/dev/acquire_unseen_batch_cases.py`。
原始任务、请求轨迹和准入日志没有公开，不在本目录。
