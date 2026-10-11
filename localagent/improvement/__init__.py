"""The paper slot: pluggable self-improvement strategies."""
from . import mass  # noqa: F401  registers "mass"
from .base import (Execution, ImprovementContext, ImprovementReport, ImprovementStrategy, NoopStrategy, Task,
                   TaskBank, TaskExecutor, WorkspaceExecutor, available_strategies, create_strategy,
                   register_strategy)

__all__ = ["Execution", "ImprovementContext", "ImprovementReport", "ImprovementStrategy", "NoopStrategy", "Task",
           "TaskBank", "TaskExecutor", "WorkspaceExecutor", "available_strategies", "create_strategy",
           "register_strategy"]
