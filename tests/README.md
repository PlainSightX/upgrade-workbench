# 测试职责

- `unit/cases/`：清单、内容完整性与补丁边界，不执行案例源码。
- `unit/execution/`：Docker调用、超时与安全选项，通过mock验证控制逻辑。
- `unit/workflow/`：三路结果归类和受保护输入，不把退出码零单独当成功。
- `unit/analysis/`：AST别名、遮蔽、字段语义和未知边界，不导入目标源码。
- `unit/test_evidence.py`：版本锁、原文摘要和定位一致性。
- `unit/generation/`：只用替身测试多轮工具动作、知识消费与恢复、请求边界、敏感值保护、编辑编译及补丁拒绝。
- `unit/test_planning.py`、`unit/test_cli*.py`：入口、预算、版本依据和调用分离。
- `unit/test_tasks.py`：固定实现身份、有界工具动作、候选审阅、公开反馈和未知请求不重放。
- `unit/test_registered_tasks.py`：P2/P3 历史批次适配器的身份与审阅边界；现代任务使用普通 CLI 或服务。
- `unit/test_budget.py`：共享账本事务预留、费用与配额、留出保护额和带配置摘要的审计调整。
- `unit/test_evaluation.py`、`unit/test_batches.py`：公开/最终检查隔离、全体候选冻结、失败保留和最终验收身份。
- `unit/test_reporting.py`、`unit/test_official_baseline.py`：报告来源与脱敏、补丁复现及官方工具完整应用输入。
- `unit/service/`：配置准备、认证 API、命令幂等、版本冲突与故障恢复；单元检查使用替身，真实 PG 验证另报。

首次准备：`uv sync --locked --extra service`。完整单测包含服务层，需要这个 extra；
不要求安装语义检索的 Torch extra，也不访问模型 API。

运行：`uv run --no-sync pytest tests/unit`。`--no-sync` 防止普通检查删除已有 extra。

真实案例的行为断言在各自`cases/<case>/checks/`中，只经Docker验证命令运行。宿主pytest的testpaths限定为tests，不直接收集第三方测试。单元测试通过不意味着镜像构建、真实Python依赖或三路验证已通过。
