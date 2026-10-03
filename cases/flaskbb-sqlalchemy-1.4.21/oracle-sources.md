# 判据来源与限制

源码来自 `flaskbb/flaskbb` 固定 commit，归档和逐文件哈希见 `SOURCE.json`。反馈组从该
commit 的 `tests/unit/test_forum_models.py` 中选取并保持核心断言，覆盖 topic/post 保存、
last-post 更新和隐藏内容计数。独立验收组在模型任务登记前编写，额外检查 Session 刷新后的
真实持久状态、显式隐藏查询和关系设置中的 autoflush 边界。

上游 PR #593 证明这是实际发生过的迁移，但其补丁与修改后的测试不属于模型输入。旧环境是
行为参照，不是完整产品规范；合同不覆盖视图、插件、搜索索引、并发、性能、PostgreSQL/MySQL、
完整安装流程或 FlaskBB 的全部测试。SQLite 的通过与失败只支持本合同范围内的结论。

检查设计者已经看过上游迁移范围，因此本案例标记为 development，而不是未见留出集。
直接升级失败只能证明存在迁移工作，候选通过仍需人工审查，不能外推为通用迁移成功率。
