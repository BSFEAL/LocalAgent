"""Production signals: metric sources, guardrail signals, dashboards."""
from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


class MetricsSource(Protocol):
    def read(self) -> dict[str, float]: ...


class StaticMetrics:
    """Values from a dict or a zero-arg callable (tests, simulations, custom code)."""

    def __init__(self, values: dict[str, float] | Callable[[], dict[str, float]]):
        self.values = values

    def read(self) -> dict[str, float]:
        return dict(self.values() if callable(self.values) else self.values)


class JsonFileMetrics:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict[str, float]:
        return {k: float(v) for k, v in json.loads(self.path.read_text()).items()} if self.path.exists() else {}


class CommandMetrics:
    """Any CLI that prints a JSON object of metrics (datadog/gcloud/kubectl wrappers...)."""

    def __init__(self, command: str, timeout: int = 60):
        self.command, self.timeout = command, timeout

    def read(self) -> dict[str, float]:
        out = subprocess.run(self.command, shell=True, capture_output=True, text=True, timeout=self.timeout).stdout
        return {k: float(v) for k, v in json.loads(out).items()}


class PrometheusMetrics:
    """Instant PromQL queries: ``{"p95_ms": 'histogram_quantile(0.95, ...)*1000'}``."""

    def __init__(self, base_url: str, queries: dict[str, str], timeout: int = 30):
        self.base_url, self.queries, self.timeout = base_url.rstrip("/"), queries, timeout

    def read(self) -> dict[str, float]:
        out = {}
        for name, q in self.queries.items():
            url = f"{self.base_url}/api/v1/query?" + urllib.parse.urlencode({"query": q})
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                res = json.loads(r.read())["data"]["result"]
            if res:
                out[name] = float(res[0]["value"][1])
        return out


@dataclass
class Signal:
    metric: str
    max: float | None = None
    min: float | None = None
    description: str = ""
    kind: str = "error"  # "latency" -> Perf Factory, "error"/"availability" -> Sevbot

    def breached(self, values: dict[str, float]) -> str | None:
        v = values.get(self.metric)
        if v is None:
            return None
        if self.max is not None and v > self.max:
            return f"{self.metric}={v:.4g} > max {self.max:.4g}"
        if self.min is not None and v < self.min:
            return f"{self.metric}={v:.4g} < min {self.min:.4g}"
        return None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        return cls(str(d["metric"]), d.get("max"), d.get("min"), d.get("description", ""),
                   d.get("kind") or ("latency" if any(t in str(d["metric"]) for t in ("latency", "p9", "_ms"))
                                     else "error"))


def check(signals: list[Signal], values: dict[str, float]) -> list[tuple[Signal, str]]:
    return [(s, msg) for s in signals if (msg := s.breached(values))]


def write_dashboard(path: Path, title: str, signals: list[Signal], flag: str | None = None) -> Path:
    """A vendor-neutral dashboard spec; adapters can translate it to Grafana/Datadog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": title, "flag": flag,
                                "panels": [{"title": s.description or s.metric, "metric": s.metric,
                                            "thresholds": {"max": s.max, "min": s.min}, "kind": s.kind}
                                           for s in signals]}, indent=2))
    return path


def signals_to_dicts(signals: list[Signal]) -> list[dict[str, Any]]:
    return [asdict(s) for s in signals]
