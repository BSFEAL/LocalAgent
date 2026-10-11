"""Resolve ``"<provider>:<model>"`` specs to providers, and route roles to models.

Built-in provider names::

    openai:gpt-5.5                      OpenAI (OPENAI_API_KEY)
    anthropic:claude-opus-5-5           Anthropic (ANTHROPIC_API_KEY)
    gemini:gemini-3-pro                 Google (GEMINI_API_KEY)
    vllm:Qwen/Qwen3.6-27B               local vLLM   (VLLM_BASE_URL, default :8000)
    ollama:qwen3:32b                    local Ollama (OLLAMA_BASE_URL, default :11434)
    lmstudio:<model>                    local LM Studio (:1234)
    openrouter:<vendor>/<model>         OpenRouter (OPENROUTER_API_KEY)
    compat:<model>                      any OpenAI-compatible server (COMPAT_BASE_URL)
    mock:<anything>                     offline scripted provider

Register your own with :func:`register_provider`.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .anthropic import AnthropicProvider
from .base import Provider
from .gemini import GeminiProvider
from .mock import EchoProvider, ScriptedProvider
from .openai_compat import OpenAICompatProvider

ProviderFactory = Callable[..., Provider]
_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    _REGISTRY[name] = factory


def _compat(name: str, base_env: str, default_base: str, key_env: str | None = None) -> ProviderFactory:
    def make(model: str, **kw: Any) -> Provider:
        kw.setdefault("base_url", os.environ.get(base_env, default_base))
        kw.setdefault("api_key_env", key_env or f"{name.upper()}_API_KEY")
        p = OpenAICompatProvider(model, **kw)
        p.name = name
        return p
    return make


register_provider("openai", OpenAICompatProvider)
register_provider("anthropic", AnthropicProvider)
register_provider("gemini", GeminiProvider)
register_provider("vllm", _compat("vllm", "VLLM_BASE_URL", "http://127.0.0.1:8000/v1"))
register_provider("ollama", _compat("ollama", "OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"))
register_provider("lmstudio", _compat("lmstudio", "LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1"))
register_provider("openrouter", _compat("openrouter", "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
register_provider("compat", _compat("compat", "COMPAT_BASE_URL", "http://127.0.0.1:8000/v1"))
register_provider("mock", ScriptedProvider)
register_provider("echo", EchoProvider)


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def create_provider(spec: str | Provider, **options: Any) -> Provider:
    """Build a provider from ``"name:model"`` (the model part may itself contain colons)."""
    if isinstance(spec, Provider):
        return spec
    name, sep, model = spec.partition(":")
    if not sep:
        raise ValueError(f"provider spec must look like 'provider:model', got {spec!r}")
    if name not in _REGISTRY:
        raise ValueError(f"unknown provider {name!r}; available: {', '.join(available_providers())}")
    return _REGISTRY[name](model, **options)


class ProviderRouter:
    """Maps factory roles (``coder``, ``review.security``, ``sevbot``, ``mass.judge``...)
    to providers. Lookup falls back along dotted prefixes, then to ``default``.

    This is what makes mixing vendors trivial: run the coder on a local model,
    the security reviewer on a hosted frontier model, and the judge on a third.
    """

    def __init__(self, default: str | Provider, routes: dict[str, str | Provider] | None = None,
                 options: dict[str, dict[str, Any]] | None = None):
        self.options = options or {}
        self._specs: dict[str, str | Provider] = {"default": default, **(routes or {})}
        self._cache: dict[str, Provider] = {}

    def set(self, role: str, spec: str | Provider) -> None:
        self._specs[role] = spec
        self._cache.pop(role, None)
        if role == "default":
            self._cache.clear()

    def spec_for(self, role: str) -> str | Provider:
        parts = role.split(".")
        for i in range(len(parts), 0, -1):
            key = ".".join(parts[:i])
            if key in self._specs:
                return self._specs[key]
        return self._specs["default"]

    def for_role(self, role: str = "default") -> Provider:
        spec = self.spec_for(role)
        if isinstance(spec, Provider):
            return spec
        if spec not in self._cache:
            name = spec.partition(":")[0]
            self._cache[spec] = create_provider(spec, **self.options.get(name, {}))
        return self._cache[spec]

    __call__ = for_role

    def routes(self) -> dict[str, str]:
        return {k: (v.spec if isinstance(v, Provider) else v) for k, v in self._specs.items()}
