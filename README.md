# Upgrade Workbench

[![CI](https://github.com/PlainSightX/upgrade-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/PlainSightX/upgrade-workbench/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**依赖升级后测试通过，不等于行为没变。** 这是一个面向 Python 依赖迁移的多角色 Agent 工作台：模型调查受影响代码、查询对应版本的官方迁移说明并生成补丁；程序负责工具隔离、候选身份、费用和中断恢复；最后在 Docker 中对「旧版 / 直接升级 / 修复候选」执行同一组行为检查，独立判定是否通过，结果不回传给模型继续修补——判定权和生成权严格分离。

## 项目摘要

**主要工作**：设计并实现判定权与生成权分离的多角色迁移 Agent（Solver / Investigator / Contract Auditor / Repository Reader），覆盖受影响代码的 AST 静态分析、版本文档混合检索、Docker 三环境独立行为验收的完整链路；在 4 个开发期从未用过的真实开源仓库上做未见仓库验收，并用两组历史对照回答"比简单工具 Agent 是否更划算"。

**成果**：未见仓库验收 4/4 全部提交或预算截停，核心案例的检查组曾被模型中间版本绕过、靠追问促成了修复；语义风险自查机制上线后发现候选给出的解释本身是错的（真实修复对，诊断理由错），如实保留为可选项；两组历史对照方向相反（A2 更贵、A5 更省），拒绝归因为单一机制。

**架构**：案例快照 → AST 影响分析 → BM25+向量混合版本文档检索 → 多角色决策 → 候选冻结 → 三环境独立验收（Docker 隔离）→ 补丁与行为差异报告；候选是否通过由独立于全部角色的执行层判断，结果不回传。

**关键技术**：LangGraph（interrupt/resume + PostgresSaver 检查点）、AST 影响分析、BM25 + BGE-M3 向量混合检索、Docker 隔离执行、Hypothesis 探针输入生成、SHA-256 指纹化的案例/依赖锁/检查文件绑定。

已验证范围主要是 Pydantic v1→v2 与 SQLAlchemy 1.x→2.0 的已登记案例。

## 一个例子：测试全绿，SQL 却被改坏

csvkit 的 `csvsql` 升级到 SQLAlchemy 2.0 后，字符串执行接口失效。把查询改成 `text(query)` 能通过原有的 4 项检查，却会把 SQL 中的 `:name` 冒号文本当成绑定参数——测试显示全绿，语义已经错了。补充跨连接可见性与冒号字面量检查后，正确修复是改用 `exec_driver_sql()`。

| 环境 | 同一组 6 项检查 |
| --- | --- |
| 原源码 + 旧依赖 | 6/6 |
| 原源码 + 新依赖（直接升级） | 0/6 |
| 修复候选 + 新依赖 | 6/6 |

不需要模型密钥即可复验：[csvsql 案例](docs/examples/csvsql/README.md)。同一案例里也保留了一次真实模型新任务的失败（4/6，遗漏两项冒号检查），没有重跑到成功——行为检查本身不穷尽所有业务行为，这条记录留作证据。

## 未见仓库验收：4 个开发期未用过的开源仓库，各跑一次

| 仓库 | 迁移 | 旧环境 | 直接升级 | Agent 候选 | 请求 | 估算费用 | 任务终态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sqlalchemy-mixins 1.5.3 | SQLAlchemy 1.4.54→2.0.38 | 19/19 | 9/19 | **19/19** | 24 | $0.844074 | `submitted` |
| pydantic-cli 4.3.0 | Pydantic 1.10.15→2.6.4 | 17/17 | 0/17（导入失败） | **17/17** | 24 | $0.810312 | `budget_exhausted`（调用上限截停，非主动收尾） |
| Flask-Pydantic 0.11.0 | Pydantic 1.10.15→2.6.4 | 17/17 | 12/17 | **17/17** | 18 | $0.498157 | `submitted` |
| sqlalchemy-filters 0.13.0 | SQLAlchemy 1.4.54→2.0.38 | 18/18 | 15/18 | **18/18** | 13 | $0.314502 | `submitted` |
| **合计** | | | | 4 / 4 | 79 | **$2.467045** | |

案例选择标准：工作台开发期间从未出现过；许可宽松；源码约 400–1,100 行；固定在上游自己迁移之前的标签；直接升级有真实的接口断裂。维护者只审阅执行边界（修改范围、无文件/网络/进程越权），不提示根因或修法，不反馈独立验收结果。

值得主动说的三件事：① sqlalchemy-mixins 的检查组是唯一真正挡住中间修订的——事后回放显示 r2 候选公开检查 6/6 全过，但完整检查只有 17/19，是 Auditor 继续追问促成了 r3 才达到 19/19；② pydantic-cli 没有主动结束，用完 24 次调用被截停，且改了合同未登记的原始行为（把 `--help` 的非零退出码改成了 0）；③ 另外 3 个案例的较早修订版本事后回放也能通过同组检查，说明检查组本身的区分力有限，不是每次都像 sqlalchemy-mixins 那样被真正挡住。完整披露见 [未见仓库汇总](docs/results/unseen-summary-2026-10-19/README.md)。

## 是否比简单工具 Agent 更划算：两个方向相反的真实结果

用同模型的"简单工具 Agent"（可读源码、跑工具、改候选，非裸模型）与本工作台做两轮历史比较，任务/修改范围/验收/停止条件预先冻结：

| 历史比较 | 简单 Agent | 工作台 | 结论 |
| --- | --- | --- | --- |
| A2：三项历史迁移，各两次 | 6/6 验收，47 请求，$1.19 | 6/6 验收，41 请求，$1.75 | 同样通过，工作台估算费用**反而高 46.7%** |
| A5：Continuum 审计历史迁移，各两次 | 2/2 验收，58 请求，$1.71 | 2/2 验收，16 请求，$0.93 | 同样通过，工作台请求少 72.4%、估算费用低 **45.4%** |

两个结果方向相反——A5 省钱明显，A2 反而更贵，而且 A5 里工作台初始拿到约 150 KB 完整目标源码，比对照组多出一截，所以这不能归因为某个单一机制（BM25、AST 或某个角色）带来的普遍优势，只能说"能力与费用依赖任务，不因一个成功案例切换通用默认配置"。详情见 [历史对照](docs/examples/comparison/README.md)。

## 语义风险自查：三轮消融，发现模型自己给的解释是错的

起因是一次 csvsql 任务里，模型的结束说明混淆了 `Connection.execute(str)` 与 `Session.execute(str)`，用"插入后行数正确"错误推断全部 SQL 语义保持。于是设计了一个可选的语义风险自查机制，让 Solver 在收尾前做输入分区和差分观测。

第一轮冻结比较：开启后 csvsql 候选验收从 4/6 升到 6/6，但调用从 4 次升到 7 次（+75%），估算费用从 $0.088 升到 $0.224（+154%）。更关键的是——**改进臂自己提出的冒号探针，被独立复核证明是错的**：它测的 `ready:ok` 和 `10:30:00` 这两个值在 `text()` 里根本不形成绑定，和驱动执行结果相同；真正能形成区分的是 `':marker'`。也就是说，候选确实改对了，但模型给出的解释依据是错的，而且这个错误没有被反馈纠正，原样留在了最终报告里。

转移到另一个任务后，两臂验收结果完全相同，但调用和估费都在涨，没有新增质量收益。最终决定：**保留为可选项，不升级为默认配置**。这组实验的价值不在于机制本身有没有做出来，而在于项目把"候选变好不能证明机制有效"这种怀疑态度，用到了自己的诊断工具上。完整的三轮记录（含第三轮 bump-my-version 上的定向/穷举对照）见 [语义风险消融完整记录](docs/semantic-risk-comparison.md)。

## 其他实验

- **版本文档检索**（16 条开发查询）：BM25 单独 11/16 进前五，BM25+BGE-M3+RRF 混合提升到 **15/16**；中文查询 BM25 前五 0/4，混合后 3/4。重排能补中文查询但会把其他题的正确首位结果往后挤，所以没有设为默认，普通任务仍用 BM25。详情见 [检索结果](docs/results/retrieval-pydantic-2026-09-16/README.md)。
- **跨任务知识复用**：模型把调查到的模块职责、流程和约束写成带源码引用的"知识页"；代码变化后按"未变/唯一重定位/待复核/重读"四态规则重新绑定旧引用，任何输入变化会让全部主题标为待复核，默认保守失效。已验证机制可用，但在已见案例上没有测得质量或效率提升，如实记录空结论。详情见 [项目记忆机制](docs/results/project-memory-2026-10-19/README.md)。

## 快速开始

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)；行为验证另需 Docker。

```powershell
uv sync --locked --extra service
uv run --no-sync pytest tests/unit          # 单测不调用模型、不运行第三方代码
uv run --no-sync upgrade-workbench inspect cases/csvkit-csvsql-sqlalchemy-2-a3
```

复验 csvsql 固定候选（无需模型密钥，需 Docker）：

```powershell
uv run --no-sync upgrade-workbench --work-root .local/csvsql-replay verify cases/csvkit-csvsql-sqlalchemy-2-a3 --candidate docs/examples/csvsql/candidate.patch --candidate-origin agent_candidate --check-group all --prepare-timeout 600 --test-timeout 240
```

配置模型、预算账本和服务模式见 [安装与首次使用](docs/public-install.md)。

## 架构

```
已审查的案例快照（源码 + 旧/新依赖锁 + 行为检查）
  → 影响分析（AST 导入/别名/再导出/继承索引，定位受影响调用点）
  → 版本文档检索（BM25 + 向量混合，按依赖版本过滤）
  → 多角色决策（见下）
  → 候选冻结
  → 三环境独立验收（旧版 / 直接升级 / 候选，Docker 隔离）
  → 补丁 + 行为差异报告
```

**四个角色，权限严格分离**：

| 角色 | 能写候选 / 定终态 | 职责 |
| --- | --- | --- |
| Solver | 是 | 唯一能读代码、查文档、跑诊断、提交补丁或结束的角色 |
| Investigator（可选） | 否 | 只读来源核查，输出"已观察事实 + 待判别问题"，不接受补丁或终态动作 |
| Contract Auditor（可选） | 否 | 只读合同审阅，对照业务约定提出质疑，不代答、不编辑候选 |
| Repository Reader | 否 | 阶段性只读预读，交接给 Solver 后退出 |

角色身份本身不授予任务状态权限：候选最终是否通过，由独立于全部角色的三环境执行层判断，结果不回传给任何角色继续修补。案例、依赖锁与检查文件均 SHA-256 指纹化，结果绑定到确切的输入与实现身份。

## 技术栈

Python 3.12、LangGraph（服务模式的 interrupt/resume + PostgresSaver 检查点）、FastAPI、PostgreSQL + SQLAlchemy 2.0、BGE-M3（向量检索）、Hypothesis（探针输入生成）、Docker（第三方代码隔离执行）、GitHub Actions（1,892 项单元测试，6 分片并行）。

## 局限

公开材料是精选投影，不分发完整转移输入、原始模型轨迹或准入日志。

- 对照结果来自已登记的开发案例，每臂 2 次；未见仓库结果只有 4 个案例、各一次运行，不是成功率，也没有统计显著性。
- "未见"只指工作台开发期间没有用过，不排除模型预训练已经见过对应的上游修复——4 个案例里 3 个的上游修复早已公开。
- 源码分析不是完整调用图，动态导入、包装和复杂绑定存在未知边界。
- 费用是按登记单价的估算，不是供应商账单；未测量人工节省时间，无生产用户。

## 文档

- [安装与首次使用](docs/public-install.md)
- [语义风险消融完整记录](docs/semantic-risk-comparison.md)
- [项目记忆机制](docs/results/project-memory-2026-10-19/README.md)
- [全部未见仓库案例](docs/results/unseen-summary-2026-10-19/README.md)
