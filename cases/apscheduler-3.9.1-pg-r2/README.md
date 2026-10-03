# APScheduler PostgreSQL Persistence

固定 APScheduler 3.9.1 完整 38 模块原字节；允许迁移的组件仅为 SQLAlchemy
job store。真实 Job、日期触发器、暂停调度器及 PG 持久化参与行为验证。
包中其他可选后端不在本次运行范围，不代表 38 个模块全部得到测试。

SQLAlchemy 1.4.54 -> 2.0.38 是固定历史迁移版本，不声称是最新发行版。
以原始迁移指南和上游 job-store 测试行为为参考；新增 PG 跨连接可见性、
错误后状态、连接池释放等独立检查。公开反馈与最终检查文件分别冻结。

SOURCE.json 保存下载归档与逐文件身份；metadata-only dist-info 仅提供版本发现，
是显式工程适配，不是上游源文件。不安装或运行上游 setup.py。目标只在 Docker
中运行，每条验证独立 PG/数据/网络命名空间，无宿主端口或持久卷。

本案例是已见开发材料，不是独立留出；正式产品比较另行登记。
