"""已复核源码快照的隔离执行边界。"""

from .case_dependency_query import run_case_dependency_query
from .dependency_query import (
    DependencyEnvironmentIdentity,
    VerifiedDependencyRoot,
    bind_dependency_root,
    execute_dependency_query,
)
from .docker import DockerExecutor, DockerUnavailable
from .environment import CaseEnvironment, parse_case_environment

__all__ = [
    "CaseEnvironment",
    "DependencyEnvironmentIdentity",
    "DockerExecutor",
    "DockerUnavailable",
    "VerifiedDependencyRoot",
    "bind_dependency_root",
    "execute_dependency_query",
    "parse_case_environment",
    "run_case_dependency_query",
]
