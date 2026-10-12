import json
import sys
import tempfile
import unittest
from pathlib import Path

from localagent.core import AgentSpec, Workflow
from localagent.factory import CommandCI, Factory, StaticMetrics
from localagent.factory.gates import AutoApprover
from localagent.improvement import Task, create_strategy
from localagent.improvement.mass import (CommandTrainer, StructuralGuardrails, bradley_terry, sample_mix, select,
                                         tournament)
from localagent.improvement.mass.sft import RankedTask
from localagent.providers import ProviderRouter

from .fakes import FakeModel


class TestRanking(unittest.TestCase):
    def test_tournament_is_connected_with_degree(self):
        ids = [f"t{i}" for i in range(16)]
        pairs = tournament(ids, opponents=6, seed=3)
        deg = {i: 0 for i in ids}
        adj = {i: set() for i in ids}
        for a, b in pairs:
            deg[a] += 1
            deg[b] += 1
            adj[a].add(b)
            adj[b].add(a)
        self.assertTrue(all(2 <= d <= 6 for d in deg.values()))
        seen, stack = set(), [ids[0]]
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack += adj[x]
        self.assertEqual(seen, set(ids))

    def test_bradley_terry_orders_by_strength(self):
        ids = ["a", "b", "c"]
        results = [("a", "b", "a")] * 3 + [("b", "c", "b")] * 3 + [("a", "c", "a")] * 3 + [("a", "b", "tie")]
        s = bradley_terry(ids, results)
        self.assertGreater(s["a"], s["b"])
        self.assertGreater(s["b"], s["c"])


class TestGuardrails(unittest.TestCase):
    def test_rejects_caps_missing_contracts_and_size(self):
        g = StructuralGuardrails(max_agents=2, required_agent_ids=["security"])
        wf = Workflow("x", AgentSpec("lead", "l", "Recall the coder at most 3 rounds."),
                      [AgentSpec("a", "r", "i"), AgentSpec("b", "r", "i", "c"), AgentSpec("c", "r", "i", "c")])
        errs = " | ".join(g.check(wf))
        for needle in ("numeric caps", "output contract", "at most 2", "security"):
            self.assertIn(needle, errs)


class TestSelection(unittest.TestCase):
    def test_test_split_never_enters_training(self):
        with self.assertRaises(ValueError):
            select([RankedTask(Task("x", "coder", "p", split="test"), [])], 15)


class TestMassCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.templates = Path(self.tmp.name) / "tpl"
        self.templates.mkdir()
        (self.templates / "README.md").write_text("task workspace\n")

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, **mass_cfg):
        model = FakeModel()
        cfg = {**dict(iterations=2, search_tasks=2, candidates=4, opponents=3, train_ranks=2, val_ranks=1), **mass_cfg}
        fac = Factory(self.repo, ProviderRouter(model), ci=CommandCI({"ok": f"{sys.executable} -c pass"}),
                      approver=AutoApprover(), metrics=StaticMetrics({}), improvement=create_strategy("mass", **cfg))
        for i in range(3):
            fac.tasks.add(Task(f"t{i}", "coder", f"Build deliverable {i}", template_dir=str(self.templates)))
        fac.tasks.add(Task("held-out", "coder", "Test task", split="test", template_dir=str(self.templates)))
        return fac, model

    def test_cycle_retains_team_workflow_and_writes_sft_data(self):
        fac, model = self.factory()
        rep = fac.improve(["coder"])
        st = rep.stages["coder"]
        self.assertEqual(st["history"][0]["against"], "reference")
        self.assertIsNotNone(st["retained"])
        current = fac.workflow("coder")
        self.assertTrue(current.is_multi_agent)  # the solo v0 lost; a proposed team was retained
        self.assertEqual(current.version, st["retained"])
        self.assertEqual(st["selected"], {"train": 6, "val": 3})  # 3 train tasks x (2 train + 1 val)
        rows = [json.loads(x) for x in Path(rep.artifacts["train"]).read_text().splitlines()]
        self.assertTrue(all(r["metadata"]["task_id"] != "held-out" for r in rows))
        orch = [r for r in rows if r["metadata"]["role"] == "orchestrator"]
        self.assertTrue(orch)
        self.assertNotIn("Team:", orch[0]["messages"][0]["content"])  # workflow stripped
        self.assertTrue(all(m.get("weight") == 1 for r in rows for m in r["messages"] if m["role"] == "assistant"))
        self.assertTrue(all("weight" not in m for r in rows for m in r["messages"] if m["role"] != "assistant"))
        self.assertGreaterEqual(model.optimizer_calls, 2)
        self.assertIsNone(rep.new_model)  # export-only trainer
        mix = sample_mix(Path(rep.artifacts["train"]), 30, seed=1)
        self.assertEqual(len(mix), 30)

    def test_best_mode_and_trainer_produces_next_generation(self):
        fac, _ = self.factory(comparison="best")
        fac.improvement.trainer = CommandTrainer(f"{sys.executable} -c \"print('trained')\"",
                                                 "mock:student-L{generation}")
        rep = fac.improve(["coder"])
        self.assertEqual(rep.new_model, "mock:student-L1")
        self.assertEqual(fac.router.for_role("coder").spec, "mock:student-L1")
        gens = json.loads((fac.state / "improvement" / "generations.json").read_text())
        self.assertEqual(gens[-1]["model"], "mock:student-L1")
        # a fresh factory on the same state resumes from the newest generation
        again = Factory(self.repo, ProviderRouter("mock:base"), metrics=StaticMetrics({}))
        self.assertEqual(again.router.for_role("coder").spec, "mock:student-L1")

    def test_repo_as_template_skips_state_dir(self):
        fac, _ = self.factory(iterations=0, candidates=0)
        (self.repo / "app.py").write_text("x = 1\n")
        ex = fac.improvement_context().executor("*")
        task = Task("r", "coder", "p", template_dir=str(self.repo))
        ex.execute(Workflow.single("coder", "solo"), task)  # creates workspaces under repo/.localagent
        path = ex.execute(Workflow.single("coder", "solo"), task).workspace
        self.assertTrue((path / "app.py").exists())
        self.assertFalse((path / ".localagent").exists())

    def test_noop_slot(self):
        fac, _ = self.factory()
        fac.improvement = create_strategy("noop")
        self.assertEqual(fac.improve().stages, {})


if __name__ == "__main__":
    unittest.main()
