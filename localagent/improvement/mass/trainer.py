"""Post-training backends. Provider-agnostic: the framework only needs a new model spec back."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class TrainResult:
    model_spec: str | None  # e.g. "vllm:my-org/qwen-L1"; None = data exported only
    detail: str = ""


class Trainer(Protocol):
    def train(self, train_path: Path, val_path: Path, base_model: str, out_dir: Path, generation: int
              ) -> TrainResult: ...


class ExportOnlyTrainer:
    """Writes datasets and stops. Workflow improvements still apply; closed API
    models that cannot be fine-tuned use this."""

    def train(self, train_path, val_path, base_model, out_dir, generation):
        return TrainResult(None, f"datasets exported: {train_path}, {val_path}")


class CommandTrainer:
    """Shell out to any trainer (LoRA via axolotl/unsloth/torchtune/TRL, a cloud
    fine-tuning CLI, ...). Placeholders: {train} {val} {base_model} {base_name}
    {out_dir} {generation}. ``model_spec`` is formatted the same way and becomes
    the next generation's provider spec - e.g. a vLLM server you (re)start on the
    exported weights."""

    def __init__(self, command: str, model_spec: str, *, serve_command: str | None = None, timeout: int = 7 * 86400):
        self.command, self.model_spec, self.serve_command, self.timeout = command, model_spec, serve_command, timeout

    def train(self, train_path, val_path, base_model, out_dir, generation):
        fields = {"train": train_path, "val": val_path, "base_model": base_model,
                  "base_name": base_model.partition(":")[2], "out_dir": out_dir, "generation": generation}
        q = {k: shlex.quote(str(v)) for k, v in fields.items()}
        r = subprocess.run(self.command.format(**q), shell=True, capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0:
            raise RuntimeError(f"trainer failed ({r.returncode}): {r.stderr[-2000:]}")
        if self.serve_command:
            subprocess.run(self.serve_command.format(**q), shell=True, check=True, timeout=3600)
        return TrainResult(self.model_spec.format(**{k: str(v) for k, v in fields.items()}), r.stdout[-2000:])
