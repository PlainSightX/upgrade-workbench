# 开发与验证

使用 Python 3.12、uv 和仓库内原锁文件：

```powershell
uv sync --locked --extra service
uv run --no-sync ruff check src tests tools/release
uv run --no-sync pytest tests/unit
uv run --no-sync python tools/release/check_public_tree.py
```

单测不运行第三方目标、不调用真实模型；Docker 行为验证和模型新任务分别显式启动，
见 [首次安装](docs/public-install.md)。第一次联网安装依赖不等于离线安装。

按职责修改模块和测试。中文注释解释状态、对象身份、副作用、失败边界与非显然的取舍。
不为缩短文件而增加通用框架，不修改候选自己的验收条件。

源码和锁文件构成任务实现身份。源码变化后创建新任务；需要恢复旧任务时使用其原实现，
不要改写旧 task 的身份字段使它适配新代码。

`advance_task` 的关键顺序是：准备请求 → 预留费用 → 记录 attempt → 持久化
`calling_model` → 一次发送 → 核销费用 → 持久化响应 → 处理动作。
远端结果未知时停止，不通过自动重试掩盖不确定副作用。

报告写清验收范围与未测内容，保留失败和完整比较分母。测试通过不能代替业务验收、
组件收益、真实用户量或生产可用性。公开材料只保留必要输入与精选结果，第三方归因见
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
