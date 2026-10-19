# 升级任务结果

案例：pydantic-cli-pydantic-2；状态：budget_exhausted。
初始策略：none；候选版本数：4。
独立验收：candidate_verified。公开反馈通过不代替独立验收。

| 阶段 | 状态 | 通过 / 收集 |
| --- | --- | --- |
| old_original | passed | 17 / 17 |
| new_original | failed | 0 / 0 |
| new_candidate | passed | 17 / 17 |

模型请求24次；报告输入/输出token：1468584/308106；未知用量0次。
仅本任务估算/预留费用：0.810312 USD，不是供应商账单。
官方静态转换不是自研算法；模型修补通过不等于用户独立实现。范围仅限登记行为，不能外推完整应用。

复现已审补丁，无需模型API。沿用已准备的项目环境；新机器按 docs/public-install.md 安装所需 extras，再运行：

```powershell
uv run --no-sync upgrade-workbench verify 'cases/pydantic-cli-pydantic-2/manifest.json' --candidate '<导出包>/candidate.patch' --candidate-origin agent_candidate --check-group all --base-image 'python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b' --prepare-timeout 600 --test-timeout 240
```

退出声明：call_limit。
诊断登记7次、完成7次、失败0次、未知0次、历史状态0次；探针3份。探针不计入独立验收。
模型正常交付、部分候选、无需源码改动声明和最终验收结果分别记录。
