"""LocalAgent: a provider-agnostic agentic software factory with a pluggable self-improvement slot."""
from .core import AgentSpec, ContextHub, EventBus, Workflow, WorkflowRunner, Workspace
from .factory import Factory, Outcome
from .improvement import ImprovementStrategy, available_strategies, create_strategy, register_strategy
from .providers import Provider, ProviderRouter, create_provider, register_provider

__version__ = "0.1.0"
__all__ = ["AgentSpec", "ContextHub", "EventBus", "Workflow", "WorkflowRunner", "Workspace", "Factory", "Outcome",
           "ImprovementStrategy", "available_strategies", "create_strategy", "register_strategy", "Provider",
           "ProviderRouter", "create_provider", "register_provider"]
