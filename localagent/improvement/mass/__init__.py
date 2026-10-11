"""MASS: Recursive Self-Improvement through Multi-Agent Self-Supervision (arXiv:2610.12176)."""
from .guardrails import StructuralGuardrails
from .judge import PairwiseJudge, Verdict
from .optimizer import HistoryRow, WorkflowOptimizer
from .ranking import bradley_terry, tournament
from .rhi import RecursiveHarnessImprovement, SearchResult
from .sft import collect, rank, sample_mix, select, write_dataset
from .strategy import MassStrategy
from .trainer import CommandTrainer, ExportOnlyTrainer, Trainer, TrainResult

__all__ = ["StructuralGuardrails", "PairwiseJudge", "Verdict", "HistoryRow", "WorkflowOptimizer",
           "bradley_terry", "tournament", "RecursiveHarnessImprovement", "SearchResult", "collect", "rank",
           "sample_mix", "select", "write_dataset", "MassStrategy", "CommandTrainer", "ExportOnlyTrainer",
           "Trainer", "TrainResult"]
