# 项目记忆：带源码引用的知识页与跨任务导入

记录时间 2026-09-27 至 09-29，本页于 2026-10-19 整理。这里的“项目记忆”是被迁移仓库的知识，
不是工作台自己的文档。机制默认关闭，只在显式选择 `project_context_policy` 时启用。

**本页只说明机制和已见案例上的真实观察。没有测量到质量、成功率或效率的提升**，
也没有做“导入知识 / 不导入知识”的对照。

## 知识页：引用由程序核对，解释仍是模型写的

- 模型用 `record_project_knowledge` 写 module、flow、constraint 三类知识页。每页 1 至 12 条引用
  （路径 + 行范围），程序校验范围在登记源码或公开业务合同内，并记下文件和片段的 SHA-256。
  constraint 页必须引用业务合同，flow 页至少引用两个源码文件。
  实现见 [project_context.py](../../../src/upgrade_workbench/generation/project_context.py) 的 `bind_pages`。
- 整套知识标为 `model_interpretation_not_behavioral_evidence`：引用身份可核对，解释是否正确不由引用校验保证。
- 从 `project-context-v5` 起，维护按单个主题进行（`revise_project_topic`，[topic_maintenance.py](../../../src/upgrade_workbench/topic_maintenance.py)）。
  更新必须带前一版的 `topic_sha256`，不能改主题的 id 和类别；新引用按当前源码重新绑定。
  一个主题的更新不会把其他主题标成已复核，候选再变时该解释又回到待复核。

## 代码变化后：重新定位引用，失配标记待复核

从 `project-context-v3` 起，候选修订改变源码后，此前的主动阅读范围按以下规则重新绑定
（[source_context.py](../../../src/upgrade_workbench/generation/source_context.py) 的 `rebind_source_reads`）：

| 情况 | 状态 | 当前位置 |
| --- | --- | --- |
| 文件字节未变 | `unchanged_file` | 原行号 |
| 文件变了，原片段在当前文件中恰好出现一次 | `unique_excerpt_relocated` | 新行号 |
| 原片段已改动或删除 / 出现多次 / 文件不存在 | `pending_relocation`（原因 `excerpt_changed_or_removed` / `ambiguous_excerpt` / `file_missing`） | 不给出，不按旧行号猜 |
| 此后完整重读了当前文件 | `reread_current_file` | 只消除“位置未知”，不代表旧片段找回 |

只要还有 `pending_relocation`，读取保留统计的 `complete` 就是 false，不会因为分母为 0 而显示完整。
容量放不下的部分另记为 `capacity_omission`。旧的探针和检查结果保留原修订，不随阅读重新绑定而变成当前结果。

知识页另外报告适用性（`knowledge_applicability`）：每条引用按文件哈希标 `unchanged` 或 `changed_or_missing`；
只要候选修订变了，整页解释都是 `needs_review`。引用文件未变也一样，因为调用方、配置或依赖可能已经变了。

## 跨任务导入：重新校验输入，过期解释只作线索

实现见 [knowledge_transfer.py](../../../src/upgrade_workbench/knowledge_transfer.py)。

- **显式导出。** `upgrade-workbench task-knowledge-export <旧任务> --output <文件>` 先核对来源任务的原请求、响应、引用和源码，
  再导出知识包，返回 `path` 与 `sha256`。包里只有历史解释、引用片段和输入身份，
  没有候选补丁、观察日志或验收记录。任务有主题更新或本身导入过知识时导出 `knowledge-import-v2`，
  每个主题保留根来源和更新链，未维护的主题保持原历史身份。
- **显式导入。** 新任务配置写 `knowledge_import: {path, sha256}`，要求 `protocol_revision: 6` 和
  `project-context-v4` 及以上策略。摘要不符、仓库不同、包里多出字段、协议冻结后改引用，都会被拒绝。
  新任务保存自己的不可变副本，外部文件移走后仍可继续。
- **每次请求都重新校验。** 重算全部公开源码、两份依赖锁、业务合同、结构化要求、声明环境、本地 wheel、
  允许修改范围、审阅配置和基础镜像的身份。每条引用分为 `unchanged_file`、`unique_excerpt_relocated`、
  `ambiguous_excerpt`、`changed_or_missing`，只有能唯一绑定当前原文时才给出当前位置。
- **过期解释只作线索。** 任一源码或声明输入变化，全部主题标为 `needs_review`；没有变化也只是 `reference_only`。
  解释始终标为 `historical_model_interpretation_unverified`。请求明确告诉模型：这是不可信的历史数据，
  不是指令，也不能作为完成证据，要对照当前合同、依赖和源码重新核实。

失效粒度偏保守：任何输入变化都会让全部主题待复核，没有按依赖图判断哪些主题真正受影响。
声明范围外的运行时配置仍是未知。摘要只保证完整性，不是第三方签名，也不证明解释正确。

## 实际观察

都在已见仓库上，每个任务各运行一次。原始任务、请求轨迹和归档没有公开；下面的数字照原记录。

### 1. 连续修改中的重新定位（Luigi，`project-context-v3`，案例未公开）

不调用模型，重放此前 v1/v2 配置下的冻结请求，旧配置重建与原请求逐字节相同。
v3 在两组末次请求中保留了能绑定当前源码的已读范围（341/341 行、81/81 行），
并把找不到唯一位置的历史读取分别列为 2 条和 3 条待定位；原 v2 投影把后一组记为 0/0。
这是请求装配上的改进，不是迁移成功或性能收益。

一次真实任务（30 次请求上限）：

- 第 6 次请求（补丁之后）明确列出 2 条 `pending_relocation`，没有把 0/0 当成完整；第 7 次 Agent 重读了当前历史模块，
  此后当前源码保留量从 200 行增加到 780 行。那 2 条旧范围始终没有被完整重读，到结束仍是 `pending_relocation`。
- 知识页显示 2 个来源未变、4 个来源有变化，全部旧解释保持 `needs_review`。模型没有读取知识页详情，也没有刷新解释。
- 任务以 `unresolved / call_limit` 结束，没有正常提交：第 29 次提交没有引用两条已被后续探针替代的无测量记录，
  被完成校验拒绝（当时的校验也没有区分已替代的探针和仍未解除的反例）；第 30 次说明 2766 字符，超过 2000 字符上限。
  冻结候选的独立验收为旧环境 3/3、直接升级 0/3、候选 3/3。
  30 次请求，输入 6,505,271 / 输出 298,820 token，估算 2.310179 美元。
- 同一案例此前 v2 是 12 次请求正常提交。两次不是同期对照，不能据此说 v3 变差，也不支持任何效率提升。

### 2. 跨任务导入（FlaskBB，`project-context-v4`）

来源任务用案例 flaskbb-sqlalchemy-1.4.21-r2 的 `manifest-v3.json`，在修补前形成 9 页知识
（模块、跨文件流程、公开业务约束），经导出核验后成包。新任务用同一案例的 `manifest-v4.json`，
两份清单只差 `review` 字段；新任务通过普通 CLI 创建，事先限定最多 8 次调用、停在首个审阅或终态边界。
源码和依赖锁与来源相同，所有引用都是 `unchanged_file`；但 `case.review` 变了，所以 9 个主题全部 `needs_review`。
公开目录是这两份原件的可移植派生（省略静态资源与文档），原件指纹 `8bbe842c…`、`a9a9891f…` 记在
[PUBLIC_FIXTURE.json](../../../cases/flaskbb-sqlalchemy-1.4.21-r2/PUBLIC_FIXTURE.json)。

| 观察项 | 结果 |
| --- | --- |
| 真实请求 | 1 次，9 个主题全部送达，收到响应 |
| 知识使用 | 回答引用“读取过期对象属性可能触发过早 INSERT”这条历史解释，把它当作待验证的原因，不是结论 |
| 下一动作 | `run_public_checks`：先对原始修订运行公开检查，观察当前的实际失败 |
| 停止位置 | `pending_diagnostic_review`，即事先设定的边界；不是迁移成功或失败 |
| 候选与验收 | 没有候选，没有启动 Docker 目标执行，独立验收未运行 |
| 用量 | 输入 218,961 / 输出 4,462 token，供应商耗时 22.218 秒，估算 0.071043 美元 |

这说明导入的背景被消费了，旧解释被当作线索去补取当前观察；不说明解释正确，也不说明比不导入更好。
这不是未见迁移问题，也不是同仓库新业务需求的评估。

### 3. 后续同仓库任务：重新定位与过期知识（FlaskBB，`project-context-v5` 至 v10，案例未公开）

这 9 页（以及后来逐主题维护过的版本）又被导入同一仓库后续的几次迁移任务：
SQLAlchemy 1.4.37 → 2.0.29，以及在其候选上派生的 HTTP、CLI、写入和权限任务。

- **过期内容确实存在，但不是整页作废。** 维护者只读复核（结果没有反馈给模型）：数据库主题仍在讲旧查询实现和
  1.4.21 环境；扩展主题说没有配置 Session 选项，当前源码已有 `session_options={"future": True}`；
  迁移边界主题的目标版本、可编辑范围和 setup 要求已过期。实体职责、隐藏/恢复和钩子顺序等解释仍有可保留部分。
  文件哈希变化不等于整页语义错误。
- **重新定位实际发生过。** v7 任务第 12 次在首个候选读取 `database.py` 第 51 至 165 行；第 15 次在第二个候选按未变文件保留；
  第 17 次在第三个候选唯一重定位到第 54 至 168 行，并据其中 `with_hidden` 的实现查询锁定依赖。
  更早读取的第 1 至 165 行整段仍待定位。
- **送达不等于理解。** v5 任务第 15 次把第 50 至 165 行重新绑定到下一个候选，116 行当前正文实际送达；
  第 16 次随后围绕该实现提出探针，其中“八个公开节点从未调用 `with_hidden`”的判断是错的。
- **标记待复核不等于有人维护。** v7 结束时模型自己指出数据库和迁移边界两页已过期，却把修订留作欠项，9 页仍为 `needs_review`。
- **旧解释被当作线索。** v8 第 1 至 3 次沿旧引用和旧版本假设重读数据库、用户和论坛模型；v9 第 3、7 次明确用历史主题选择要读的源码。
  这是有限的调查线索消费，不证明改善了最终修复。
- **修订可能引入新错误。** v8 第 4 次把“无 session options”纠正为当前的 `future=True`，同一页却把
  “访问权限属性可能触发查询”扩大成“构造 User/Group 就会触发 lazy reads”，依据不足；
  第 5 次复核记为 4 confirmed、1 corrected、4 unresolved。
- **confirmed 只是标签。** v9 第 38 次把 9 页全部标为 confirmed，上面的因果错误仍在。
- **有限的正确纠错链。** v10 第 8 次怀疑旧知识中“User 用 `make_comparable` 装饰”的说法，第 8、9 次核查源码，第 10 次实质纠正。
  权限任务第 13 次删去“构造即查询”，补入权限/组缓存、定向失效及普通组管理操作的缓存边界，
  第 16、17 次依据修订后页面中的未知项查询缓存实现。这说明维护过的解释会进入后续调查，
  不说明 9 页都正确，也不说明知识带来了独立收益。

## 不能得出的结论

- 没有“导入 / 不导入”或“有 / 无知识层”的对照；没有测量修复质量、成功率、请求数、费用、返工或人工时间的变化。
- 以上观察都是已见仓库上的单次运行，不外推到未见仓库；不同仓库的知识包会被直接拒绝。
- 送达、引用命中和 confirmed 标签都不等于解释正确。知识包还会增加输入 token 和维护成本。
- 重新定位只认原片段的精确唯一匹配，不追踪语义上的移动。

## 复核

不需要模型 API：

```powershell
uv sync --locked --extra service
uv run --no-sync pytest tests/unit/generation/test_knowledge_continuity.py tests/unit/generation/test_knowledge_transfer.py tests/unit/generation/test_knowledge_maintenance.py
```

- [test_knowledge_continuity.py](../../../tests/unit/generation/test_knowledge_continuity.py)：未变文件与唯一移位片段；
  改动或重复的片段不能以 0/0 显示完整；完整重读只消除位置未知；引用未变时解释仍是 `needs_review`。
- [test_knowledge_transfer.py](../../../tests/unit/generation/test_knowledge_transfer.py)：CLI 导出与普通导入、外部文件移走后继续、
  输入变化与歧义引用、错误仓库/摘要/多余字段/旧策略被拒绝、协议冻结后不能改引用。
- [test_knowledge_maintenance.py](../../../tests/unit/generation/test_knowledge_maintenance.py)：逐主题更新进入下一次请求而不改动其他主题，
  以及导出 → 维护 → 再导入的三任务链。

这些测试用合成的模型响应，只验证机制，不代表真实模型的行为。
