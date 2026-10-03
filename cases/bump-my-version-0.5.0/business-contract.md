# 版本发布工具的升级行为合同

状态：2026-09-16 已完成源码与检查审阅，冻结下述已有行为用于迁移比较。执行结果另存于运行报告，本文不作为成功声明。

## 这个应用做什么

`bump-my-version` 是实际的开源命令行发布工具。使用者给出当前版本、要提升的版本段和一组文件规则，应用解析配置，计算下一版本，检查文件，再同步更新各文件及配置里的当前版本。它还能提交 Git/Hg 和打标签，本批不调用这些功能。

本批固定 0.5.0 的提交 `fc491fc50fa85b2b23c1eaf4980a30a17637b4f6`，保留全部 14 个应用 Python 模块。MIT 许可证、README、原 pyproject、选中的上游测试和夹具保留原字节；来源及哈希见 [SOURCE.json](SOURCE.json)。这是历史真实应用的限定流程验证，不是我们自建的业务样例，也不等同于整个 CLI 通过迁移验收。

## 为什么适合检验迁移

主要调用链为 `get_configuration → VersionConfig / Version → do_bump → resolve_file_config / modify_files → update_config_file`。Pydantic 配置模型连接了多个业务模块，不只是孤立模型构造。

旧实现从 `pydantic` 导入 `BaseSettings`，多个 `Optional` 字段没有默认值，使用 `Field(min_items=1)` 和带本地类型参数的 `update_forward_refs`。升级到 Pydantic 2 后，至少需要正确处理设置类迁移、可省略字段以及前向引用。静态预期首先发生 `BaseSettings` 导入错误；它是否正是本环境的首个失败，必须由 Docker 实测确认，不能把预计失败写成结果。

## 冻结的已有行为

| 流程/输入 | 必须保留的结果 | 原始依据 |
|---|---|---|
| INI/TOML 配置读取 | 与原上游 JSON 夹具完全一致 | `tests/test_config.py` 的两个读取测试 |
| 多份配置同时存在 | 采用原搜索顺序中第一个有配置内容的文件 | `test_multiple_config_files` |
| 配置文件更新 | 仅更新当前版本，其他行、格式不改变 | `test_update_config_file`，三种文件名 |
| `0.9` 提升 patch/minor/major | 分别是 `0.9.1`、`0.10`、`1` | `test_serialize_three_part` |
| 非数字发布段及自定义初值 | 保留原枚举顺序、可选段和重置初值 | `test_bump_non_numeric_parts`、`test_part_first_value` |
| 独立 build 段 | 提升 major 时不重置独立 build | `test_build_number_configuration` |
| 同文件的点分/横线两种格式 | 同时由 `0.10.2` / `0-10-2` 更新为 `0.10.3` / `0-10-3` | `test_single_file_processed_twice` |
| 四文件发布再 patch | 完整版本、major、README、含构建信息的版本各按自身序列化规则更新 | `test_multi_file_configuration` |
| 搜索值与原始版本字符串均不存在 | 抛出 `VersionNotFoundError`，文件字节不变 | `test_non_matching_search_does_not_modify_file` |
| UTF-8 文件更新 | 只改变版本，保留非 ASCII 字符 | `test_simple_replacement_in_utf8_file` |
| 未知版本段/非法正则 | 保留原拒绝行为，不默认猜测版本 | `tests/test_version_part.py` 的对应负例 |

`checks/test_upstream_contract.py` 调用未改写的上游测试函数，以显式参数选择 21 个检查实例。包装层只设置容器临时工作目录、提供夹具路径和列明参数；未运行整个上游测试套件，也不宣称全部既有行为已覆盖。

另有 3 个项目补充检查，放在 `checks/test_application_pipeline.py`，并明确不是上游已有测试：

1. 调用真实 `do_bump`，将 `1.2.3` 的 minor 提升到 `1.3.0`，同时更新 VERSION、requirements 中的 MyProject 和 TOML 配置；相同版本号的 OtherPackage 不变。依据是 `bump.py` 的流程顺序、`files.py` 的定向替换以及上游定向替换测试。
2. `dry_run=True` 时三个文件字节均不变。依据是 `do_bump` 向文件及配置写入函数传递 `dry_run`，两个函数各自跳过写入。
3. 第二个文件搜索值及原始版本字符串均不存在时，第一个文件和配置也不改变。依据是 `modify_files` 先执行整组 `_check_files_contain_version` 再进入写入循环；这不意味着运行中磁盘错误可自动回滚。

三项仅把现有实现的可观察行为合并为端到端断言，不新增发布策略。

原实现还有必须保留区分的行为：`ConfiguredFile.contains_version` 在搜索式等于该文件的 `version_config.search` 时，允许用文件内存在 `version.original` 作为回退匹配。构造器会把文件自定义搜索式也传入这个版本配置，所以不能声称这里检查了软件包身份。例如文件仅含 `OtherPackage==1.2.3` 和 `MyProject==0.9.0`，当前版本为 `1.2.3` 时，这个预检查仍可能通过。正向定向替换检查只证明写入不会修改 OtherPackage，不证明预检查能够识别包身份。

首次旧环境运行 `9856606cd4d2451fa5037d084776d493` 因我们新增负例的预条件忽略上述回退而得到 23/24；原报告保留。经工程负责人审阅，仅将该负例中的 OtherPackage 值改成 `0.8.0`，使第二文件完全不含原始版本字符串；`raises` 和所有文件字节不变的断言保持原样，上游代码/断言未改。修正后需要重新封存并完整重跑旧/新基线，不能沿用首次失败结果宣称通过。

## 行为取舍与需要用户参与的边界

本批采用“保留上述已有行为”，不修改业务需求。保持上游明确允许省略的文件/版本段参数，不可因 Pydantic 2 的默认变化要求用户把每个旧可选字段都补上。

以下问题没有在本批擅自决定：

- 环境变量、配置文件、显式参数发生冲突时是否改优先级。旧 `get_configuration` 会向设置类传入完整默认项，不能按“更合理”的猜测重写该行为。
- 是否采用更严格的数字到字符串转换、未知字段处理或类型校验。若修复需要改变旧输入的接受范围，应列出具体输入及旧/新结果，再共同决定。
- CLI 参数、显示输出、Git/Hg 提交、签名标签的兼容范围。本批排除，不得由所选检查通过推导它们已经可用。
- 全组预检查之后发生 I/O 错误是否要求回滚。本批只保留写前验证顺序，不新增事务文件系统承诺。

这些边界不阻塞先保留既有断言并测量原始升级的失败。若后续候选确实触及未定行为，再提供具体差异供用户决定。

## 依赖与执行边界

旧/新环境固定 Python 3.12、相同 click / rich-click / rich / tomlkit / pytest / typing-extensions。旧版固定 Pydantic 1.10.24；新版固定 Pydantic 2.11.7 和设置包 pydantic-settings 2.10.1。`pydantic-settings` 及其新增传递依赖属于迁移所需变化，不伪称只改变了一个包。其余共同包版本必须一致。

上游元数据包含 Python 3.12 支持分类，但这只是支持声明；本批锁解析不代表应用已运行。依赖采用 wheel-only 解析与哈希锁，禁止运行上游 setup/build hooks。两个 `.in` 是本项目为可重复评测建立的环境，不是上游当年的原锁文件。

执行仅限工程已有的隔离 Docker 路径；容器无网络、无宿主目录、无宿主环境变量或密钥、无 Docker socket。应用读写只接触容器临时夹具。`get_scm_info` 会尝试探测 Git/Hg；临时目录无仓库，未安装的工具也会被应用捕获并返回无 SCM。未调用提交、标签或任意外部命令功能。上游 `tests/conftest.py` 的 Git/Hg fixture 只是保留源文件，没有被选中或请求。

候选只能更改经最终清单允许的应用文件；检查、上游测试、夹具、依赖和合同均不可由候选修改。验收目标是原生 Pydantic 2 迁移，单纯改用 `pydantic.v1` 不能算完成目标；功能断言之外还需要审阅这一范围。

## 本批不包含的结论

本合同本身不提供模型调用、Agent 自主修复、人工参考修复或官方工具对比结果，也没有当前生产流量和完整 CLI/SCM 兼容声明。运行报告必须分别记录实际执行结果；不得从合同与文件存在推导这些工作已完成。
