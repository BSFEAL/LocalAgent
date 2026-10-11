"""Run the whole factory + one MASS cycle offline with a scripted model.

    python examples/offline_demo.py

Swap ``ProviderRouter(FakeModel())`` for e.g. ``ProviderRouter("ollama:qwen3:32b")``
or ``ProviderRouter("anthropic:claude-opus-5-5", {"mass": "vllm:Qwen/Qwen3.6-27B"})``
to run it for real.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localagent.factory import AutoApprover, CommandCI, Factory, Outcome, Signal, StaticMetrics  # noqa: E402
from localagent.improvement import Task, create_strategy  # noqa: E402
from localagent.providers import ProviderRouter  # noqa: E402
from tests.fakes import FakeModel  # noqa: E402


def main() -> None:
    repo = Path(tempfile.mkdtemp(prefix="localagent-demo-"))
    (repo / "README.md").write_text("# demo service\n`feature()` must return 42.\n")
    values = {"error_rate": 0.0, "p95_ms": 12.0}
    factory = Factory(
        repo, ProviderRouter(FakeModel()),
        ci=CommandCI({"test": f'{sys.executable} -c "import feature; assert feature.feature() == 42"'}),
        approver=AutoApprover(),  # sandbox only; use CLIApprover / CallbackApprover for real
        metrics=StaticMetrics(lambda: dict(values)),
        guardrails=[Signal("error_rate", max=0.05)],
        improvement=create_strategy("mass", iterations=2, search_tasks=2, candidates=4, opponents=3,
                                    train_ranks=2),
        auto_mitigate=True, echo=True)

    print("\n== 1. builder -> coder -> CI -> review -> deploy ==")
    print(factory.build(Outcome("Add feature()", "return 42", ["feature() == 42"])).summary())

    print("\n== 2. production: latency regression + outage ==")
    factory.monitor_once()  # baseline
    values.update(p95_ms=40.0, error_rate=0.2)
    rep = factory.monitor_once()
    print("perf proposals:", [o.title for o in rep.proposals])
    print("incidents:", [(i.summary, i.mitigations, i.mitigated) for i in rep.incidents])

    print("\n== 3. the paper slot: one MASS cycle on the coder stage ==")
    for i in range(3):
        factory.tasks.add(Task(f"synthetic-{i}", "coder", f"Write deliverable {i}", template_dir=str(repo)))
    report = factory.improve(["coder"])
    print("retained workflow:", factory.workflow("coder").render())
    print("SFT data:", report.artifacts.get("train"))
    print(f"\nstate lives in {factory.state}")


if __name__ == "__main__":
    main()
