# Encode Databases SQLAlchemy 2.0 Case

这是固定 `encode/databases` 迁移前提交的 SQLite 后端行为切片。案例仅评价公开
`Database` API 在 SQLAlchemy 2.0 下的查询编译、结果映射、类型处理和事务行为，不把
其他数据库后端或整个上游迁移记为本案例成果。

上游参考迁移只用于选择和最终人工复核，不进入 Solver、Contract Auditor 或冻结检查。
