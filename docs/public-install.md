# 安装与第一次运行

本说明对应公开整改候选。Python 3.12 为工作台宿主版本；案例自己的 Python 和依赖
只在固定的 Docker 环境中执行。模型调用、固定补丁复验和服务建表是三条不同路径。

## 源码安装

在仓库根目录执行：

```powershell
uv sync --locked --extra service
uv run --no-sync upgrade-workbench --help
uv run --no-sync pytest tests/unit
uv run --no-sync ruff check src tests
```

完整单测需要 `service` extra。基础 CLI 可以只安装基础依赖；语义检索的 BGE/Torch
另用 `retrieval` 或 `retrieval-cuda`，两者不能同时安装。普通运行使用 `--no-sync`，
防止检查命令移除已安装的 extra。单测不会默认调用模型或运行第三方案例源码。

## 固定候选复验

不需要模型密钥或预算账本。先确认 Docker 引擎可用，再按
[csvsql 说明](examples/csvsql/README.md)执行。该案例包含六项冻结检查，
旧版、直接升级和候选执行同一组检查；历史候选复验不等于本次自主生成。

## 输入准备与人工节点

现有案例可直接选用；新增案例须由使用者提供可恢复的源码快照、旧/新依赖锁、
执行环境、可修改范围、版本资料和行为合同。源码与资料由维护者审查；合同先根据
原业务测试、旧环境行为和官方迁移要求确定，修复任务不能修改它自己的验收。
工作台检查身份与边界，不会自动证明合同穷尽，也不接收任意仓库就保证迁移成功。

普通使用选择一条路径：固定候选复验不调用模型；新CLI任务逐次推进；服务任务另需PG、
API与worker。高级检索、多角色和知识复用按相应配置启用，安装extra不等于自动使用。
模型提出源码读取、诊断、补丁与退出判断；使用者审阅完整补丁或探针的执行范围。
最终取得候选补丁、固定验证配置、结构化比较报告和状态；未通过也可以导出失败报告。
本轮没有测量新增案例的人工准备耗时。

## 新服务实例

前提是一个归本实例所有的 PostgreSQL 数据库/角色，以及已初始化的预算账本。
服务准备不依赖本机历史 admin-config 或 `.local/runtime-layout.json`，不自动创建
数据库、不启动 Docker、不调用模型。建表由之后的 `setup` 明确完成。

先为所用模型核对价格并初始化账本。示例规格的字段如下，价格与日期按供应商填写：

```json
{
  "mode": "user_managed",
  "limit_usd": null,
  "model": "your-model",
  "input_per_million": "0.30",
  "output_per_million": "1.20",
  "pricing_source": "https://your-provider.example/pricing",
  "pricing_checked_at": "YYYY-MM-DD"
}
```

这里的数字仅展示字段格式，不是现行价格或本项目的费用承诺。
将以下普通任务配置保存为 `profile.json`，模型名与账本一致，endpoint 按实际服务填写。
本例使用已有 P4 工具流程，不启用可选多角色；SQLAlchemy 案例使用 `seed_strategy: "none"`。

```json
{
  "protocol_revision": 4,
  "max_calls": 10,
  "seed_strategy": "none",
  "generation": {
    "model": "your-model",
    "endpoint": "https://your-provider.example/v1/chat/completions",
    "thinking_mode": "disabled",
    "max_output_tokens": 8192,
    "timeout_seconds": 120
  }
}
```

```powershell
uv run --no-sync upgrade-workbench budget-init .local/accounting.sqlite3 accounting.json
uv run --no-sync upgrade-service --config .local/service-new.json prepare --work-root <work-root> --budget .local/accounting.sqlite3 --case cases/csvkit-csvsql-sqlalchemy-2-a3/manifest.json --profile profile.json
uv run --no-sync upgrade-service --config .local/service-new.json setup
```

在两个独立终端分别运行以下持续进程，工作目录和配置路径相同：

```powershell
# 终端一：HTTP API
uv run --no-sync upgrade-service --config .local/service-new.json serve
# 终端二：任务 worker
uv run --no-sync upgrade-service --config .local/service-new.json worker
```

`UPGRADE_DATABASE_URL` 提供完整 PostgreSQL 连接串；也可以用 `--database-url-env`
指定已有环境变量名。当前 worker 固定从 `DEEPSEEK_API_KEY` 读取模型密钥，
使用兼容 endpoint 时也用此变量；它必须在 worker 进程中可见，勿写入 profile。
服务 API token 在新配置中自动生成，准备命令只显示状态和配置位置。
准备阶段共用实际任务的只读校验，缺字段、额度不合法、种子不适用于案例或执行范围
越界时拒绝写配置和工作根，不为校验创建任务或修改账本。P6 历史任务上下文的完整
来源核对仍在实际绑定时执行；本次新实例示例不使用该高级路线。

Windows 的 `<work-root>` 应是用户选择的短绝对路径；准备会实际检查嵌套路径与
Git 物化能力，不因系统允许长路径就跳过检查。Linux 同样使用明确的实例工作根。
所有运行输出均须位于冻结案例之外。

相同配置再次准备会复用原 token 和绑定；路径、案例、profile 或数据库连接改变会拒绝。
迁移既有实例需要另行核对，不能通过覆盖配置把旧任务变成新身份。
`tools/dev/setup_local_service.py` 是历史本机维护入口，不作为新用户的首次安装路径。

## 提交与推进服务任务

准备命令将 profile 注册为 `main`。客户端命令从配置读取 API token，不需手工复制 token。
在第三个终端运行：

```powershell
uv run --no-sync upgrade-service --config .local/service-new.json request GET /registry
uv run --no-sync upgrade-service --config .local/service-new.json request POST /jobs --idempotency-key csvsql-001 --json '{"case_id":"csvkit-csvsql-sqlalchemy-2-a3","profile":"main"}'
uv run --no-sync upgrade-service --config .local/service-new.json request GET /jobs/<job-id>
```

提交返回 job ID；worker 初始化后状态成为 `ready`。读取该任务的最新 `version`，
将以下内容保存为 `advance.json`，再提交一条命令：

```json
{"expected_version":"<latest-version>","reviewed_tool":false}
```

```powershell
uv run --no-sync upgrade-service --config .local/service-new.json request POST /jobs/<job-id>/advance --idempotency-key csvsql-step-001 --body-file advance.json
uv run --no-sync upgrade-service --config .local/service-new.json request GET /jobs/<job-id>/commands
uv run --no-sync upgrade-service --config .local/service-new.json request GET /jobs/<job-id>
```

每条新命令使用新 key 和最新 version；重试同一命令保留原 key 和原 body。
`202` 表示受理，执行结果由 worker 更新。候选待审时先读取完整 `/jobs/<job-id>/diff`，
核对改动范围，再向 `/jobs/<job-id>/review` 提交：

```json
{"expected_version":"<latest-version>","revision":"<candidate-revision>","sha256":"<candidate-sha256>","decision":"accept","reviewer":"your-name","note":"说明已检查的完整补丁和允许执行的范围"}
```

审阅只允许执行公开反馈，不代表补丁正确。待诊断审阅时先读 `/jobs/<job-id>/diagnostic`，
再向 `/jobs/<job-id>/diagnostic-review` 提交最新 version、`request_id`、decision、reviewer、note。
继续逐次 advance；终态后向 `/jobs/<job-id>/finalize` 提交仅含最新 `expected_version` 的 body，
最后读取 `/jobs/<job-id>/report`。独立验收结果不返回模型；失败报告也保留。
上述 POST 均通过 `request ... --body-file ... --idempotency-key ...` 发送。
完整字段与响应可用认证客户端 `request GET /openapi.json` 查看。

## 不使用服务的普通 CLI 任务

复制前面的 profile 为 `task-config.json`，删除 `seed_strategy` 字段，补上
`"execution": {"prepare_timeout": 600, "test_timeout": 240}`。CLI 的 seed 独立通过参数传入。
初始化账本后执行：

```powershell
uv run --no-sync upgrade-workbench --work-root <work-root> task-create cases/csvkit-csvsql-sqlalchemy-2-a3 --config task-config.json --budget .local/accounting.sqlite3 --seed none
uv run --no-sync upgrade-workbench task-status <task.json>
uv run --no-sync upgrade-workbench continue-task <task.json>
```

CLI 同样读取 `DEEPSEEK_API_KEY`。每次状态输出给出下一行动；待审候选先用 `task-diff`，
再用 `task-review --review-revision ... --review-sha256 ... --reviewer ... --review-note ...`。
诊断先用 `task-diagnostic` 查看完整输入，再以 `task-diagnostic-review` 绑定 request ID。
终态后执行 `task-finalize`，然后 `task-export --output <new-directory>` 导出报告与审阅补丁。
不要用新源码继续旧冻结任务；普通任务不需要创建实验批次。

`task-finalize` 只有已完成的独立验收成功才返回0；未通过、未知或未完成返回2。
`task-status` 和 `task-export` 的成功仅表示读取或导出完成，不能当作候选通过。
CLI状态输出的 `next_action` 可帮助选下一步；服务响应按 `status`、终态和待审诊断字段
选择上述路由，再读取最新version并使用 `--body-file`。人工审阅不会自动批准。
远端结果未知时先恢复原请求，不创建新key盲重发。

## wheel 的能力边界

```powershell
uv build --wheel
```

wheel 包含完整产品模块和构建时冻结的 `uv.lock`，用于任务实现身份校验。
它不包含全部案例、数据库状态或模型权重。安装 wheel 的用户仍需获取经审查的案例，
并显式指定 manifest、profile、账本和工作根。无需进入源码目录才能创建普通任务或准备服务。

本轮实际验证平台是 Windows 11 宿主、Linux Docker；GitHub Actions 的 Linux 安装
合同已修正，但本轮没有触发远端 Actions，不据此宣称所有平台已完整验证。
