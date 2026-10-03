# Encode Databases SQLite SQLAlchemy 2.0 迁移合同

将固定 `encode/databases` 提交中的 SQLite 后端从 SQLAlchemy 1.4.42 迁移到 2.0.7，
保持 `Database` 的公开查询、结果和事务行为。该案例只覆盖 SQLite/aiosqlite，不代表
PostgreSQL、MySQL、aiopg、asyncmy 或任意 SQLAlchemy 2.x 版本均已兼容。

- SQLAlchemy Core 与原始 SQL 的 `execute`、`execute_many`、`fetch_all`、`fetch_one`、
  `fetch_val` 和 `iterate` 路径必须可执行。
- 返回记录仍支持位置访问、字段名访问、属性访问和 `_mapping` 视图；列对象、标签列及
  原始 SQL 列名保持可寻址，缺失键不能静默返回错误列。
- Date、DateTime、JSON、Numeric 和自定义 `TypeDecorator` 的绑定与结果处理保持原语义。
- 根事务、强制回滚与嵌套 savepoint 保持隔离；一次失败语句后连接仍可用于合法查询。
- 不通过降低 SQLAlchemy 版本、修改依赖锁、修改检查、跳过检查、全局 monkeypatch 或
  导入测试代码完成任务。

允许修改范围仅为 `databases/backends/sqlite.py` 与 `databases/interfaces.py`。完整上游源码、
依赖锁、检查、来源和官方迁移证据均不可修改。最终验收使用与公开检查不同但同属上述合同的
输入组合；它不要求复制上游参考补丁的目录结构或实现方式。
