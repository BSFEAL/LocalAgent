import json
import sys
import tempfile
import unittest
from pathlib import Path

from localagent.factory import AutoApprover, CallbackApprover, CommandCI, Factory, Outcome, StaticMetrics
from localagent.factory.gates import Approval
from localagent.providers import ProviderRouter

from .fakes import FakeModel

PY = sys.executable
TEST_CMD = f'{PY} -c "import feature; assert feature.feature() == 42"'


def make_factory(repo, model, metrics=None, **kw):
    return Factory(repo, ProviderRouter(model), ci=CommandCI({"test": TEST_CMD}),
                   approver=kw.pop("approver", AutoApprover(True)),
                   metrics=StaticMetrics(metrics if metrics is not None else {"error_rate": 0.0}), **kw)


class TestFactory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "README.md").write_text("# demo\nfeature returns 42\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_low_risk_ships_without_human(self):
        asked = []
        fac = make_factory(self.repo, FakeModel(),
                           approver=CallbackApprover(lambda *a: asked.append(1) or Approval(False, "x")))
        rep = fac.build(Outcome("Add feature()", "return 42", ["feature() == 42"]))
        self.assertEqual(rep.status, "deployed", rep.summary())
        self.assertEqual(rep.review.risk, "low")
        self.assertEqual(asked, [])
        self.assertEqual(fac.flags.get("feature-flag"), 100)
        self.assertTrue(Path(rep.deploy.dashboard).exists())
        stages = {t.stage for t in fac.trajectories.load()}
        self.assertTrue({"coder", "review", "deploy"} <= stages)
        self.assertEqual(fac.tasks.get("coder")[0].prompt.splitlines()[0], "# Outcome: Add feature()")

    def test_ci_failure_loops_back_to_coder(self):
        model = FakeModel(first_content="def feature():\n    return 0\n")
        fac = make_factory(self.repo, model)
        rep = fac.build(Outcome("Add feature()"))
        self.assertEqual(rep.status, "deployed")
        self.assertEqual(rep.iterations, 2)
        self.assertEqual([e.kind for e in fac.bus.of("ci.")], ["ci.failed", "ci.passed"])

    def test_blocking_review_loops_back(self):
        fac = make_factory(self.repo, FakeModel(blocking_once=True))
        rep = fac.build(Outcome("Add feature()"))
        self.assertEqual(rep.iterations, 2)
        self.assertEqual(rep.status, "deployed")

    def test_high_risk_requires_human(self):
        fac = make_factory(self.repo, FakeModel(file="db/migrations/001.py"), approver=AutoApprover(False))
        fac.ci = CommandCI({"noop": f"{PY} -c pass"})
        rep = fac.build(Outcome("Add migration"))
        self.assertEqual(rep.review.risk, "high")
        self.assertEqual(rep.status, "rejected")
        self.assertTrue(fac.bus.of("review.human_requested"))

    def test_rollout_rolls_back_on_breach(self):
        fac = make_factory(self.repo, FakeModel(), metrics={"error_rate": 0.5})
        rep = fac.build(Outcome("Add feature()"))
        self.assertEqual(rep.status, "rolled_back")
        self.assertEqual(fac.flags.get("feature-flag"), 0)

    def test_monitor_routes_to_perf_factory_and_sevbot(self):
        values = {"error_rate": 0.0, "p95_ms": 10.0}
        fac = make_factory(self.repo, FakeModel(), metrics=lambda: dict(values), auto_mitigate=True)
        self.assertEqual(fac.build(Outcome("Add feature()")).status, "deployed")
        self.assertFalse(fac.monitor_once().proposals)  # establishes the baseline
        values.update(p95_ms=30.0, error_rate=0.2)
        rep = fac.monitor_once()
        self.assertEqual([o.title for o in rep.proposals], ["Cache feature()"])
        self.assertEqual(rep.proposals[0].origin, "perf_factory")
        self.assertTrue(rep.incidents and rep.incidents[0].mitigated)
        self.assertEqual(fac.flags.get("feature-flag"), 0)
        self.assertFalse(fac.monitor_once().proposals)  # duplicate alert suppressed
        self.assertTrue(fac.bus.of("perf.duplicate"))

    def test_perf_budget_in_ci(self):
        model = FakeModel()
        fac = make_factory(self.repo, model, max_iterations=2)
        fac.ci = CommandCI({"test": TEST_CMD}, perf_command=f"{PY} -c \"print('{{\\\"p95_ms\\\": 99}}')\"",
                           perf_budgets={"p95_ms": 50})
        rep = fac.build(Outcome("Add feature()"))
        self.assertEqual(rep.status, "failed")
        self.assertIn("p95_ms", rep.ci.feedback())


class TestConfig(unittest.TestCase):
    def test_build_factory_from_toml(self):
        import tomllib
        from localagent.config import SAMPLE, build_factory
        cfg = tomllib.loads(SAMPLE)
        cfg["providers"]["default"] = "mock:x"
        with tempfile.TemporaryDirectory() as d:
            fac = build_factory(cfg, d, echo=False)
            self.assertEqual(fac.improvement.name, "mass")
            self.assertEqual(fac.router.for_role("coder").spec, "mock:x")
            self.assertEqual(json.loads(json.dumps(fac.improvement.config))["comparison"], "reference")


if __name__ == "__main__":
    unittest.main()
