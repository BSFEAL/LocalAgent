"""Context sources: where agents 'get context from' (the box top-right in the factory).

Built-ins cover the repo itself (source + docs-in-code), git history, a skills
directory, and folders of exported notes (Slack/Notion dumps, runbooks).
Live integrations (Slack, Notion, Datadog, Databricks...) implement the same
two-method protocol.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .tools import Tool, schema


@dataclass
class ContextItem:
    source: str
    title: str
    text: str
    score: float = 0.0


class ContextSource(Protocol):
    name: str

    def search(self, query: str, limit: int = 5) -> list[ContextItem]: ...


def _terms(q: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", q.lower())]


def _score(text: str, terms: list[str]) -> float:
    low = text.lower()
    return sum(low.count(t) for t in terms) / (1 + len(low) / 5000)


class DirectorySource:
    """Keyword retrieval over text files in a directory (docs, notes, skills, runbooks)."""

    def __init__(self, root: str | Path, name: str = "docs",
                 patterns: tuple[str, ...] = ("*.md", "*.txt", "*.rst")):
        self.root, self.name, self.patterns = Path(root), name, patterns

    def search(self, query: str, limit: int = 5) -> list[ContextItem]:
        terms = _terms(query)
        items = []
        for pat in self.patterns:
            for p in self.root.rglob(pat):
                if any(part.startswith(".") for part in p.relative_to(self.root).parts):
                    continue
                try:
                    text = p.read_text(errors="ignore")
                except OSError:
                    continue
                s = _score(text, terms)
                if s > 0:
                    items.append(ContextItem(self.name, str(p.relative_to(self.root)), text[:4000], s))
        return sorted(items, key=lambda i: -i.score)[:limit]


class GitSource:
    name = "git"

    def __init__(self, repo: str | Path):
        self.repo = Path(repo)

    def search(self, query: str, limit: int = 5) -> list[ContextItem]:
        try:
            log = subprocess.run(["git", "log", "--oneline", "-n", "200"], cwd=self.repo,
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        terms = _terms(query)
        hits = [line for line in log.splitlines() if any(t in line.lower() for t in terms)]
        return [ContextItem("git", "recent commits", "\n".join(hits[:limit * 4] or log.splitlines()[:limit * 2]), 1.0)]


class ContextHub:
    def __init__(self, sources: list[ContextSource] | None = None):
        self.sources = list(sources or [])

    def add(self, source: ContextSource) -> None:
        self.sources.append(source)

    def gather(self, query: str, limit: int = 8, max_chars: int = 12_000) -> str:
        items: list[ContextItem] = []
        for s in self.sources:
            try:
                items += s.search(query, limit)
            except Exception as e:  # a broken integration must not stop the factory
                items.append(ContextItem(s.name, "error", str(e)))
        items.sort(key=lambda i: -i.score)
        out, total = [], 0
        for i in items[:limit]:
            chunk = f"[{i.source}] {i.title}\n{i.text}\n"
            if total + len(chunk) > max_chars:
                break
            out.append(chunk)
            total += len(chunk)
        return "\n".join(out)

    def tool(self) -> Tool:
        return Tool("search_context", "Search project knowledge (docs, history, notes, skills) for a query.",
                    schema(["query"], query="string"), lambda query: self.gather(query) or "nothing found")
