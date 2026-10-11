"""Tools agents can call. All filesystem/shell tools are confined to a workspace."""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..providers.base import ToolSpec


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)

    def __call__(self, **kwargs: Any) -> str:
        try:
            return str(self.fn(**kwargs))
        except Exception as e:  # tool errors go back to the model, not up the stack
            return f"ERROR: {type(e).__name__}: {e}"


def schema(required: list[str] | None = None, **props: Any) -> dict[str, Any]:
    """Shorthand: schema(path="string", limit=("integer", "max lines"))."""
    properties = {}
    for k, v in props.items():
        t, desc = (v, "") if isinstance(v, str) else v
        properties[k] = {"type": t, **({"description": desc} if desc else {})}
    return {"type": "object", "properties": properties, "required": required or []}


@dataclass
class Toolbox:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, *tools: Tool) -> "Toolbox":
        for t in tools:
            self.tools[t.name] = t
        return self

    def subset(self, names: list[str] | None) -> "Toolbox":
        if names is None:
            return Toolbox(dict(self.tools))
        return Toolbox({n: self.tools[n] for n in names if n in self.tools})

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.tools:
            return f"ERROR: unknown tool {name!r}. Available: {', '.join(self.tools)}"
        return self.tools[name](**args)

    def __contains__(self, name: str) -> bool:
        return name in self.tools


class Workspace:
    """A directory agents may read/write. Paths outside it are rejected."""

    IGNORE = {".git", ".localagent", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache"}

    def __init__(self, root: str | Path, *, allow_shell: bool = True, shell_timeout: int = 600,
                 shell_allowlist: list[str] | None = None, max_read_chars: int = 60_000):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allow_shell = allow_shell
        self.shell_timeout = shell_timeout
        self.shell_allowlist = shell_allowlist
        self.max_read_chars = max_read_chars

    def resolve(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"path escapes workspace: {path}")
        return p

    def files(self, pattern: str = "*") -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in self.IGNORE)
            for f in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, f), self.root)
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f, pattern):
                    out.append(rel)
        return out

    # --- tool implementations -------------------------------------------------
    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> str:
        text = self.resolve(path).read_text(errors="replace")
        if offset or limit:
            lines = text.splitlines()
            text = "\n".join(lines[offset: offset + limit if limit else None])
        if len(text) > self.max_read_chars:
            text = text[: self.max_read_chars] + f"\n...[truncated, {len(text)} chars total]"
        return text

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {path}"

    def edit_file(self, path: str, old: str, new: str) -> str:
        p = self.resolve(path)
        text = p.read_text()
        n = text.count(old)
        if n != 1:
            return f"ERROR: expected exactly one match for `old`, found {n}"
        p.write_text(text.replace(old, new, 1))
        return f"edited {path}"

    def list_files(self, pattern: str = "*") -> str:
        files = self.files(pattern)
        return "\n".join(files[:500]) + (f"\n...({len(files)} files)" if len(files) > 500 else "")

    def search(self, regex: str, pattern: str = "*") -> str:
        rx = re.compile(regex)
        hits = []
        for rel in self.files(pattern):
            try:
                for i, line in enumerate((self.root / rel).read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
            except OSError:
                continue
            if len(hits) > 300:
                break
        return "\n".join(hits) or "no matches"

    def run(self, command: str, timeout: int | None = None) -> tuple[int, str]:
        if not self.allow_shell:
            return 126, "shell disabled for this workspace"
        if self.shell_allowlist is not None:
            exe = shlex.split(command)[0] if command.strip() else ""
            if exe not in self.shell_allowlist:
                return 126, f"command {exe!r} not in allowlist {self.shell_allowlist}"
        try:
            r = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True,
                               timeout=timeout or self.shell_timeout)
            return r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired as e:
            return 124, f"timeout after {e.timeout}s\n{e.stdout or ''}{e.stderr or ''}"

    def run_command(self, command: str) -> str:
        code, out = self.run(command)
        if len(out) > 20_000:
            out = out[:5_000] + "\n...[snip]...\n" + out[-15_000:]
        return f"exit={code}\n{out}"

    def toolbox(self, read_only: bool = False) -> Toolbox:
        tb = Toolbox().add(
            Tool("read_file", "Read a text file in the workspace (optionally a line range).",
                 schema(["path"], path="string", offset="integer", limit="integer"), self.read_file),
            Tool("list_files", "List workspace files matching a glob pattern.",
                 schema([], pattern="string"), self.list_files),
            Tool("search", "Regex search over workspace files; returns file:line: text.",
                 schema(["regex"], regex="string", pattern=("string", "glob filter")), self.search),
        )
        if not read_only:
            tb.add(
                Tool("write_file", "Create or overwrite a file with the given content.",
                     schema(["path", "content"], path="string", content="string"), self.write_file),
                Tool("edit_file", "Replace exactly one occurrence of `old` with `new` in a file.",
                     schema(["path", "old", "new"], path="string", old="string", new="string"), self.edit_file),
            )
            if self.allow_shell:
                tb.add(Tool("run_command", "Run a shell command in the workspace root; returns exit code and output.",
                            schema(["command"], command="string"), self.run_command))
        return tb

    def snapshot(self, max_chars: int = 40_000, max_file_chars: int = 6_000) -> str:
        """Text rendering of the workspace, used as the 'deliverable' for judging."""
        parts, total = [], 0
        files = self.files()
        parts.append("FILES:\n" + "\n".join(files[:300]))
        for rel in files:
            try:
                text = (self.root / rel).read_text()
            except (UnicodeDecodeError, OSError):
                continue
            chunk = f"\n=== {rel} ===\n{text[:max_file_chars]}"
            if total + len(chunk) > max_chars:
                parts.append("\n...[snapshot truncated]")
                break
            parts.append(chunk)
            total += len(chunk)
        return "".join(parts)
