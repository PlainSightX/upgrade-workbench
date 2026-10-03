# 第三方来源与归因

自有代码拟采用 [MIT 草案](LICENSE.candidate)，最终发布前由项目所有者确认。
这个草案不替代第三方许可。各案例保留原版权、许可证和来源 revision；依赖的安装与使用
仍受各自条款约束。`docs/third-party-assets.json` 是按实际候选文件生成的资产索引。

| 公开资产 | 来源 | 保留的许可位置 |
| --- | --- | --- |
| bump-my-version 源码 | [callowayproject/bump-my-version](https://github.com/callowayproject/bump-my-version)，0.5.0 快照 | 案例 `upstream/LICENSE`，MIT |
| Copier 源码 | [copier-org/copier](https://github.com/copier-org/copier)，6.2.0 快照 | 案例 `upstream/LICENSE`，MIT |
| fastapi-jwt-auth 源码 | [IndominusByte/fastapi-jwt-auth](https://github.com/IndominusByte/fastapi-jwt-auth) | 案例 `evidence/LICENSE`，MIT |
| FlaskBB 源码/模板/原生测试 | [flaskbb/flaskbb](https://github.com/flaskbb/flaskbb) | 案例 `source/LICENSE`，BSD-3-Clause；`source/NOTICE` 的改编代码 MIT 通知 |
| Encode databases 源码 | [encode/databases](https://github.com/encode/databases) | 案例 `source/LICENSE.md`，BSD-3-Clause |
| APScheduler 源码 | [agronholm/apscheduler](https://github.com/agronholm/apscheduler)，3.9.1 | 案例 `upstream/LICENSE.txt`，MIT |
| Optuna 源码 | [optuna/optuna](https://github.com/optuna/optuna)，3.1.0 | 案例 `upstream/LICENSE`，保留完整 MIT 与附加第三方通知 |
| OpenAPI Python client 源码 | [openapi-generators/openapi-python-client](https://github.com/openapi-generators/openapi-python-client)，0.10.0 | 案例 `upstream/LICENSE`，MIT；嵌套 schema 的 Kuimono `source/openapi_python_client/schema/openapi_schema_pydantic/LICENSE` 另行保留 |
| csvkit 源码 | [wireservice/csvkit](https://github.com/wireservice/csvkit)，1.1.1 | 案例 `source/COPYING`，MIT |
| SQLAlchemy 迁移原文 | [sqlalchemy/sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) | 案例 `evidence/LICENSE`，MIT；bundle 记录 revision 与 upstream_path |
| Pydantic 迁移原文 | [pydantic/pydantic](https://github.com/pydantic/pydantic) | 案例 `evidence/LICENSE`，JWT 中为 `evidence/pydantic-LICENSE`，MIT |
| Werkzeug 原文 | [pallets/werkzeug](https://github.com/pallets/werkzeug) | 案例 `evidence/LICENSE.rst`，BSD |

四文件 FlaskBB 导航夹具来自经审查历史候选，保留原字节及 BSD/NOTICE；其来源 JSON 明确
不是 pristine upstream，也不是可运行 HTTP 案例。公开派生案例的原 manifest 与成员哈希
保存在 `PUBLIC_FIXTURE.json`；不因删掉非必要资产而抹掉来源或冒充原冻结案例。

## FlaskBB 依赖 wheel

固定环境随案例分发旧依赖 wheel，用于在不同环境中重建同一输入；wheel 不被重打包。
资产索引记录完整 wheel SHA256、内嵌许可成员、许可文本 SHA256 和外置补充条款。
blinker、celery、flask-allows、flask-mail、flask-whooshee、future、limits、olefile、
speaklater 的完整许可在 wheel 内；future 的附加上游声明保留原样。
click-didyoumean 与 Flask-Themes2 的缺失条款由版本来源中的原许可作为外置文件保留。

## 不随候选分发

FlaskBB 的 docs/static/aurora 前端资产、字体、source map 与未核明的前端许可集合不分发。
原生 Python、模板、测试与必要主题 `info.json` 保留。这些公开 fixture 不证明完整站点
静态资源加载；代表可执行迁移使用 csvsql。BGE 权重不随 Git/wheel 发布，按固定 revision
另行下载并核哈希；开发工具不执行远端模型 Python。历史 Continuum 比较只分发自有结果
投影，不分发其源码或原始模型轨迹。
