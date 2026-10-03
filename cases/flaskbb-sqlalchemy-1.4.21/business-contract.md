# FlaskBB SQLAlchemy 1.4 迁移行为合同

把固定 FlaskBB 基线从 SQLAlchemy 1.3.24 升级到 1.4.21，同时保持选定的论坛数据模型行为。
本案例是公开真实应用的有界迁移，不代表 FlaskBB 全仓库、所有插件或所有数据库后端均已兼容。

创建 topic 和 post 后，作者身份、首帖、末帖、论坛末帖以及 topic/post 计数必须真正持久化；
刷新 Session 后仍应得到相同结果。追加帖子应更新 topic/forum 的末帖与计数而不改变首帖。
隐藏的 topic/post 默认查询不可见，显式 `with_hidden()` 查询可以取回。建立 Forum、Topic、
Post、User 和 Group 关系时，ORM 查询不得把字段尚未填完的对象提前 autoflush 到数据库。

旧环境与直接升级执行同一份检查。公开反馈来自上游既有论坛模型测试的选定行为；最终验收
由本项目独立编写，检查重新加载后的数据库状态和迁移边界。禁止修改检查、依赖锁或环境合同，
禁止降低 SQLAlchemy 版本。允许修改范围仅为 manifest 所列六个 Python 文件。

执行使用固定 Python 3.9 镜像、wheel-only 哈希锁、无外网临时 SQLite。参考 PR 只用于案例
选择和最终人工复核，不进入求解上下文，也不计为 Agent 或用户实现成果。
