# 未见仓库迁移：Flask-Pydantic 0.11.0 → Pydantic 2

日期：2026-10-19（UTC）。状态：**Agent 候选通过独立验收（`candidate_verified`）**。

这是第二批未见仓库中的一个。合同和检查在任何模型请求之前冻结，然后走一次普通任务流程。
结果：旧环境 17/17，直接升级 12/17，Agent 候选 17/17。共 18 次 deepseek-flash 请求，
估算 0.498157 美元。这是**单个案例的一次运行**，不能用来推断成功率或跨仓库泛化。
四个未见仓库的汇总见 [unseen-summary](../unseen-summary-2026-10-19/README.md)。

## 仓库与固定版本

- 上游：[pallets-eco/flask-pydantic](https://github.com/pallets-eco/flask-pydantic)，
  标签 v0.11.0，提交 `ebb08fed208bcc478e286cbd195328cc6993bd28`（2022-09-25），MIT。
- 归档 SHA-256：`a8407463c6e9aec163baf35a14a528234cc57c9983bb9f2f332394bf726d67fe`。
  库源码 5 个 Python 模块，约 380 行；上游测试原样放在案例的 `upstream/` 下，只作来源留存，不当检查运行。
- 依赖：Python 3.9 固定镜像，`pydantic 1.10.15 → 2.6.4`。Flask 2.3.3、Werkzeug 2.3.8、
  `pytest 7.4.4`、`typing-extensions 4.12.2` 及其余带哈希锁定的依赖两边一致。
- 案例：[`cases/flask-pydantic-pydantic-2`](../../../cases/flask-pydantic-pydantic-2/business-contract.md)，
  指纹 `84e89541ad829e775de264feb9c5d4a23f8030af316ee683f41202d7a98af1e1`。

## 为什么选它

- **此前没出现过。** `cases/`、`docs/`、`tools/`、`src/` 里都没有这个仓库的名字。
  “未见”只指本工作台开发时没用过，**不能排除模型预训练见过它**。
- **迁移需求是真实的。** v0.11.0 声明 `pydantic>=1.7`，实际只能在 1.x 上运行；上游后来在 v0.12.0
  （2024-01-08）才改为 `pydantic>=2.0`。那次发布及其提交、issue 在编写合同和检查时没有读过，
  也没有提供给模型。它发布于 2024 年初、项目知名度较高，**模型在预训练中见过该修复的风险较高**。
- **规模小、失败有意义。** 直接升级时，5 项检查失败，查询参数路径都报
  `AttributeError: 'FieldInfo' object has no attribute 'is_complex'`（返回 500），自定义校验器的错误
  还会因 2.0 错误对象里的 `ctx` 含异常实例而无法序列化为 JSON。

## 预先登记了什么

顺序记录在 [preregistration.json](preregistration.json) 中，时间均为 UTC：

| 步骤 | 时间 | 模型请求数 |
| --- | --- | --- |
| 合同、条目和两组检查计算哈希 | 02:52:21 | 0 |
| Docker 准入（旧 / 直接升级） | 02:52:27 | 0 |
| 封存清单后创建任务 | 02:55:52 | 0 |
| 准备第 1 次模型请求 | 02:56:50 | — |
| 任务终态后独立验收 | 04:07:20 | 18 |

- [业务合同](../../../cases/flask-pydantic-pydantic-2/business-contract.md)和 5 条结构化条目，
  依据见 [oracle-sources](../../../cases/flask-pydantic-pydantic-2/oracle-sources.md)。
  依据来自固定版本的 README、`validate` docstring 和上游测试描述的行为；检查是新写的。
- 公开反馈 6 项，修复过程中可见。独立验收 11 项，模型看不到，只在终态后运行。
  覆盖多值查询参数、多模型请求体与响应、`exclude_none`、别名序列化、元组返回的状态码和响应头、
  错误状态码与抛错配置、自定义校验器消息、路径参数错误、`get_json_params` 透传和非模型返回值。
- 错误项只核对外层结构、`loc` 列表和自定义消息是否出现，不固定 Pydantic 版本相关的 `msg` / `type` 文本。
- 封存清单里的检查字节，与 02:52:21 记录的哈希逐项相同。第一次模型请求之后，检查没有改过。
- 冻结之前做过两次 Docker 编写期试运行（无模型请求）。第一次旧环境 8/17，原因是检查文件用了
  `from __future__ import annotations`，库按视图函数的 `__annotations__` 取模型类，拿到的是字符串。
  这是检查写法问题，不是迁移行为；删掉该导入后第二次试运行旧环境 17/17，输入字节与正式准入完全相同。
  在第一次试运行之前，还把一项 415 响应检查换成了 `get_json_params` 透传检查：415 只出现在上游
  单元测试的模拟请求里，合同条目没有登记它，没有为它做环境核实。
- 允许修改 `flask_pydantic/` 下的 5 个 `.py` 模块；README 和许可不在修改范围内。
- 版本依据复用此前案例中已核验哈希的 Pydantic `docs/migration.md`（提交 `5f033e4`），按标题登记
  BaseModel、Field、config、validators、可空字段、移动与删除 7 个通用章节，没有写入修法提示。

## 模型、调用与费用

配置见 [run-config.json](run-config.json)，与 sqlalchemy-mixins 逐字节相同（P6），实现身份摘要也相同。
没有使用 Pro 或 reasoner 模型。

| 项目 | 数值 |
| --- | --- |
| 模型请求 | 18 次（Solver 16，Contract Auditor 2），未知用量 0 |
| 输入 / 输出 token | 840,535 / 204,992 |
| 估算费用 | **0.498157 USD**（按 0.30 / 1.20 美元每百万 token 的标价，不是账单；共享账本结算与此一致） |
| 供应商请求累计 | 838.1 秒 |
| 创建至独立验收 | 约 71 分钟，包含审阅等待和 Docker 时间，不是无人值守时延 |
| 只读依赖查询 | 0 次 |
| 受审诊断 | 8 次：新探针 3 份，在 r2、r3 上重跑；另有每个候选批准后的公开检查 3 次 |

## 过程与结果

| 修订 | 内容 | 公开检查 |
| --- | --- | --- |
| 原始源码 | — | 3/6 |
| r1 `0791f581` | 查询参数按字段注解判断是否多值；`.json()` 改为 `model_dump_json()`；`parse_obj_as` 改为 `TypeAdapter`；`__root__` 改为 `RootModel`；错误项统一整理为 `loc` / `msg` / `type` | 6/6 |
| r2 `b1455f42` | `request_body_many` 对非数组请求体（含空对象）直接给出 `root` 错误；`response_many` 拒绝字符串、字节和字典 | 6/6 |
| r3 `a2199fa2` | `response_many` 额外拒绝单个模型实例 | 6/6 |

r1 公开检查全过之后，Agent 写了 3 份探针，分别测输入（多值查询、多模型请求体、`get_json_params`）、
响应序列化和错误结构。三份在 r1 新环境都测得 0 个违反。随后 Contract Auditor 提出两处具体疑问：
空 JSON 对象会被当成空数组接受、空字符串或空字典会被 `response_many` 当成空模型列表序列化。
Agent 据此修出 r2。第二次审计又指出空模型实例同样可迭代，Agent 修出 r3。
其中一份错误结构探针在旧环境执行失败（使用了 2.0 才有的 `field_validator`），只能在新环境读数。
探针不参与验收。Agent 以 `candidate_ready` 提交，`migration.pydantic2` 条目如实标为 `unobserved`。

| 环境 | 结果 | 通过 / 收集 |
| --- | --- | --- |
| 旧依赖 + 原始源码 | passed | 17 / 17 |
| 新依赖 + 原始源码 | failed | 12 / 17 |
| 新依赖 + Agent 候选 r3 | **passed** | **17 / 17** |

最终补丁 [candidate.patch](candidate.patch)，SHA-256 `461ba40f…946f`，只改了 `converters.py` 和
`core.py`，+88 / −24 行。完整报告见 [task-report.md](task-report.md)。

**事后对照（不属于本轮成绩）：** 终态冻结之后，用同一组 17 项检查回放 r1 和 r2，两者都是 17/17。
也就是说，审计疑问带来的 r2、r3 修改处理的是合同允许范围内、但本组检查没有覆盖的边界输入；
独立验收无法区分 r1 和 r3。

## 人工参与

维护者负责选仓库、编写合同和检查、Docker 准入、执行安全审阅、运行独立验收和撰写本文。
审阅内容：批准 3 个候选，接受 8 次诊断（3 份新探针及其在新 revision 上的重跑，探针只用
Flask 测试客户端在内存中请求），没有拒绝。没有手改候选，没有向模型提示根因或修法，
也没有反馈独立验收结果。人工审阅时间没有测量。

## 限制

- 只有一个仓库、一次运行、没有对照臂。
- 上游修复早已公开，模型可能在预训练中见过；本次结果不能区分“推理得出”和“回忆得出”。
- 检查是维护者写的，冻结前按旧环境结果修正过检查写法，修正理由见上文。
- 只覆盖合同列出的行为和固定的 Flask / Werkzeug 版本；`flask_restful` 集成、表单根模型、
  错误文本的逐字一致都没有验证。

## 复核

无需模型 API，Docker 运行时从仓库根目录执行：

```powershell
uv run --no-sync upgrade-workbench inspect cases/flask-pydantic-pydantic-2
uv run --no-sync upgrade-workbench --work-root .local/unseen-fpyd-replay verify cases/flask-pydantic-pydantic-2 --candidate docs/results/unseen-flask-pydantic-2026-10-19/candidate.patch --candidate-origin agent_candidate --check-group all --prepare-timeout 600 --test-timeout 240
```

案例的获取、冻结、准入和封存脚本是 `tools/dev/acquire_unseen_batch_cases.py`。
原始任务、请求轨迹和准入日志没有公开，不在本目录。
