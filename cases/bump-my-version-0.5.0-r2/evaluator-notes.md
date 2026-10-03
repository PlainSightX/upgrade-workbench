# r2 评价者说明

本修订是已经看过迁移候选之后的回顾性开发检查，不是盲测或独立项目。原案例及其 24 项结果保留；本修订的通过结果必须重新运行取得。

## 原始依据与覆盖

| 覆盖 | 旧源码依据 | 新检查与预期 |
| --- | --- | --- |
| 文件/版本段可选字段 | `config.py` 的 `FileConfig`、`VersionPartConfig` 注释与旧模型声明；`get_all_part_configs` 无参数构造版本段配置 | 每种模型分别检查省略、显式 None、合法值；原值保留、默认 independent=False |
| 主配置可选字段 | `Config` 的四个 Optional 字段；`DEFAULTS`；旧环境实际模型合同 | 省略/None 均得到 None；显式值保留。当前版本缺省模型可构造不等于整个发布应用能省略当前版本 |
| 序列化列表 | `Config.serialize = Field(min_items=1)`；`VersionConfig` 依赖格式列表顺序 | 缺失/None/空列表拒绝；一项和多项格式按序保留 |
| glob 展开及配置拷贝 | `files.get_glob_files` 对每个匹配项执行 `file_cfg.copy()` 后只修改副本 filename | 返回各自路径，保持调用方所有规则字段，不要求不存在业务需求的深拷贝语义 |
| 实际递归 glob 发布 | `resolve_file_config`、`modify_files`、`do_bump` 和 `update_config_file` 的真实调用 | 两个不同目录的 txt 定向更新；其他软件包样式文本、md 和 TOML 其他内容不变；dry run 全部不写 |
| 空 glob | `glob.glob` 无结果时的空循环；有 glob 时优先于 filename | 无结果、不修改原规则、不处理 fallback filename |
| glob 写前拒绝 | `modify_files` 先整组检查再逐个写入；`contains_version` 的原始版本回退 | 某个匹配文件完全缺少目标与原始版本时，全部文件与配置不写；不假设中途 I/O 回滚 |

新检查共 19 项，原 24 项原路径原字节保留，合计预登记 43 项。`evaluation.json` 登记两个互斥分组和精确 pytest nodeid。默认运行整个 `checks/`；公开反馈只运行其中登记的两个原文件。分组登记本身不等于执行器已经实现反馈隔离，调用端必须真正限制可见文件和日志。

## 技术说明与信息边界

原业务合同同时解释 BaseSettings、Optional 默认、Field 约束及 forward refs 迁移，不能作为无原文/无 AST 实验的共同输入。r2 改用中性行为要求；本文件、`evaluation.json`、检查源码、诊断补丁及结果禁止送入模型请求。原版本文档保留原字节，但是否投影由实验臂明确决定。

旧源码、SOURCE、许可证、上游材料与依赖锁均来自父清单的逐字节核验。`revision.json` 将父指纹与保留文件映射纳入本修订哈希清单；新 fingerprint 不能替代父 fingerprint。原来的模型与官方工具补丁可用于新检查下的回归，但属于旧产物复测，不是新增自主生成样本。

## 合成错误诊断

`diagnostics/drop-serialize-constraint.patch` 删除旧源码中唯一的列表最小长度限制；`diagnostics/alias-glob-rule.patch` 把逐文件复制错误改为共享调用者的规则。二者均是人工合成错误补丁，以本案例冻结原源码为基底，使用 **旧依赖环境** 运行，目的是单独确认检查能拒绝行为缺陷。它们不是迁移补丁、Agent 候选、真实缺陷样本或用户贡献。

别名错误预期仅在 `test_glob_resolution_preserves_caller_configuration` 被检出：解析调用正常返回，路径也可正确，但调用者规则的 filename 被最后一个匹配污染。长度错误预期仅在 `test_serialize_rejects_missing_null_and_empty[empty]` 被检出：空列表被接受。不得把导入/收集失败当作这两项诊断有效。

旧环境完整通过、精确检查身份一致、上述指定断言实际失败后，才可声明新增检查有效。这里记录的是预期；Docker 结果由运行报告负责。

## 制作与重现

`tools/dev/prepare_bump_revision.py` 先验证父案例，再读取本目录人工审阅的新检查、业务要求和本文件，并复制父清单中允许复用的已核验字节；它生成 lineage、分组、诊断补丁及清单。已封存目录禁止覆盖。重建到新空目录时，从已封存 r2 读取新资产并再次核验；无需联网，也不导入或执行目标源码。
