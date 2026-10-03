# Copier r2 检查修订依据

本修订属于开发集的检查有效性修正，不是新的独立项目，也不是模型修复成功。原 `copier-6.2.0` 封存案例及其失败报告不得覆盖。

## 原失败原因

原旧环境收集 8 项，7 项通过，`test_explicit_answers_drive_typed_rendering` 失败。该检查把 `data={"count": "7", "enabled": "yes"}` 交给 `run_copy(defaults=True)`，却期望字符串已转换为整数和布尔值。模板中的 `count + 1` 因字符串与整数相加而报错。

上游 `source/copier/main.py` 的 `Worker.answers` 先把 `self.data` 放进 `AnswersMap.init`，遇到已有显式答案的键就跳过 `Question` 的构建。因此显式数据不走问题转换。这也与上游 `upstream/tests/test_complex_questions.py::test_api` 直接传入原生整数、布尔值的做法一致。

`source/copier/user_data.py` 则明确提供另一条路径：`Question.get_default` 转换问题默认值；`filter_answer` 转换问题回答；`_check_type` 在未声明类型时根据默认值推断；`validate_answer` 对不能转换的数值返回 `False`。本次修订分别检查这些边界，不要求旧应用实现原本没有的隐式行为。

## 修订范围

- 保留全部原源码、来源、上游测试、许可证、版本证据及依赖锁字节。
- 原公开检查仅把错误测试中的显式参数改为 `7` 和 `True`，另外增加显式字符串类型保持检查。
- 增加问题过滤器的整数和布尔转换检查；增加默认值转换、默认类型推断和无效数值拒绝检查。
- 原最终 4 项文件输出检查保持原字节；新检查不改变已知输入接受范围。
- 默认检查总数为 13，其中公开 6 项，最终 7 项。检查分组按真实职责冻结，数量不是覆盖充分性的证明。

`revision.json` 记录父指纹、原失败报告与日志摘要。`historical-baseline/` 保留这些失败原件的原字节副本；它们与本说明不进入生成者上下文，不执行其中的文件。

本修订必须先在旧环境通过，才有资格继续迁移评价。通过后仍不代表远端模板、更新合并、TUI、CLI 全部选项或完整 Copier 行为已验收。
