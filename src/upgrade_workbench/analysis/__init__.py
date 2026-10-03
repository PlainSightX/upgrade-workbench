"""只读影响定位；静态候选不会代替隔离执行的行为证据。"""

from upgrade_workbench.evidence import migration_package


def analyze_case(case, *, candidate_reference=None):
    """按核验后的版本合同路由，不能因文件名或代码词语切换迁移家族。"""
    package = migration_package(case)
    if package == "sqlalchemy":
        from .sqlalchemy import analyze_case as analyze
    elif package == "werkzeug":
        from .generic import analyze_case as analyze
    else:
        from .pydantic import analyze_case as analyze
    return analyze(case, candidate_reference=candidate_reference)

__all__ = ["analyze_case"]
