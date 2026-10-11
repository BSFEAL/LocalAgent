import tempfile
import unittest
from pathlib import Path

from localagent.core import Agent, AgentSpec, Trajectory, Workflow, WorkflowRunner, WorkflowStore, Workspace
from localagent.providers import Message, ProviderRouter, ScriptedProvider, ToolCall

from .fakes import FakeModel


class TestWorkspace(unittest.TestCase):
    def test_confinement_and_tools(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(d)
            tb = ws.toolbox()
            self.assertIn("wrote", tb.call("write_file", {"path": "a/b.txt", "content": "x = 1\n"}))
            self.assertEqual(tb.call("read_file", {"path": "a/b.txt"}), "x = 1\n")
            self.assertIn("ERROR", tb.call("read_file", {"path": "../../etc/passwd"}))
            self.assertIn("a/b.txt:1", tb.call("search", {"regex": "x ="}))
            self.assertIn("exit=0", tb.call("run_command", {"command": "echo hi"}))
            self.assertNotIn("write_file", ws.toolbox(read_only=True))


class TestAgent(unittest.TestCase):
    def test_tool_loop_and_trajectory(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(d)
            p = ScriptedProvider(responses=[{"tool": "write_file", "args": {"path": "f.txt", "content": "hi"}}, "done"])
            traj = Trajectory("t", "coder")
            res = Agent("a", "sys", p, ws.toolbox(), trajectory=traj).run("go")
            self.assertEqual(res.output, "done")
            self.assertEqual((Path(d) / "f.txt").read_text(), "hi")
            self.assertEqual([m.role for m in traj.conversations[0].messages],
                             ["system", "user", "assistant", "tool", "assistant"])

    def test_step_budget(self):
        p = ScriptedProvider(responder=lambda m, t: Message("assistant", "", tool_calls=[ToolCall("nope", {})])
                             if t else Message("assistant", "forced"))
        res = Agent("a", "sys", p, ScriptedToolbox(), max_steps=3).run("go")
        self.assertFalse(res.finished)
        self.assertEqual(res.output, "forced")


def ScriptedToolbox():
    from localagent.core import Tool, Toolbox, schema
    return Toolbox().add(Tool("nope", "noop", schema(), lambda: "ok"))


class TestWorkflow(unittest.TestCase):
    def test_multi_agent_parallel_delegation(self):
        with tempfile.TemporaryDirectory() as d:
            model = FakeModel()
            wf = Workflow("coder", AgentSpec("lead", "lead", "coordinate"),
                          [AgentSpec("builder", "b", "build", "files"), AgentSpec("checker", "c", "check", "ok")])
            traj = Trajectory("t", "coder")
            out = WorkflowRunner(ProviderRouter(model)).run(wf, "task", Workspace(d).toolbox(), stage="coder",
                                                            trajectory=traj)
            self.assertIn("final", out)
            roles = sorted(c.role for c in traj.conversations)
            self.assertEqual(roles, ["orchestrator", "worker", "worker"])

    def test_recall_continues_worker_conversation(self):
        def respond(msgs, tools):
            if "orchestrator" in msgs[0].content:
                n = len([m for m in msgs if m.role == "tool"])
                if n < 2:
                    return Message("assistant", "", tool_calls=[ToolCall("delegate", {
                        "agent_id": "w", "task": f"round {n}"})])
                return "final"
            return "worker reply"
        with tempfile.TemporaryDirectory() as d:
            traj = Trajectory("t", "s")
            wf = Workflow("s", AgentSpec("lead", "l", "orchestrator"), [AgentSpec("w", "w", "work", "text")])
            WorkflowRunner(ProviderRouter(ScriptedProvider(responder=respond))).run(
                wf, "task", Workspace(d).toolbox(), stage="s", trajectory=traj)
            worker = next(c for c in traj.conversations if c.role == "worker")
            self.assertEqual([m.content for m in worker.messages if m.role == "user"], ["round 0", "round 1"])

    def test_store_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            store = WorkflowStore(Path(d))
            wf = Workflow.single("coder", "do it")
            self.assertIs(store.current_or("coder", wf), wf)
            wf2 = Workflow.from_dict({**wf.to_dict(), "version": 1})
            store.save("coder", wf2, make_current=True)
            self.assertEqual(store.load("coder").version, 1)
            bad = Workflow("x", AgentSpec("a", "r", "i"), [AgentSpec("a", "r", "")])
            self.assertTrue(bad.validate())


if __name__ == "__main__":
    unittest.main()
