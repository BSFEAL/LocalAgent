"""A role-aware fake model so the whole factory and MASS run offline."""
import json

from localagent.providers import Message, ScriptedProvider, ToolCall


def _tool_results(msgs):
    return [m for m in msgs if m.role == "tool"]


class FakeModel(ScriptedProvider):
    """Plays coder, reviewers, deploy agent, perf engineer, sevbot, MASS optimizer and judge.

    Knobs let tests steer behaviour: risk level, CI-breaking first attempt, etc.
    """

    def __init__(self, *, risk="low", file="feature.py", content="def feature():\n    return 42\n",
                 first_content=None, blocking_once=False):
        super().__init__("fake", responder=self.respond)
        self.risk, self.file, self.content = risk, file, content
        self.first_content, self.blocking_once = first_content, blocking_once
        self.coder_runs = 0
        self.optimizer_calls = 0

    def respond(self, msgs, tools):
        system = msgs[0].content
        last_user = next(m.content for m in reversed(msgs) if m.role == "user")
        if "designs multi-agent workflows" in system:
            self.optimizer_calls += 1
            return json.dumps({"design": {
                "orchestrator": {"id": "lead", "role": "lead", "instruction": "Coordinate the team. TEAM-MARK"},
                "agents": [{"id": "builder", "role": "builder", "instruction": "Build it.",
                            "output_contract": "files changed"},
                           {"id": "checker", "role": "checker", "instruction": "Check it.",
                            "output_contract": "defects or LGTM"}],
                "protocol": "builder then checker; recall builder on defects"},
                "changes_from_previous": ["added checker with recall loop"]})
        if "strict expert evaluator" in system:
            a, b = last_user.split("# Deliverable B")
            a_team, b_team = "TEAM-MARK" in a, "TEAM-MARK" in b
            winner = "A" if a_team and not b_team else "B" if b_team and not a_team else "tie"
            return json.dumps({"winner": winner, "rationale": "team output is more thorough"})
        if "specialist" in system or "risk classifier" in system:
            findings = []
            if self.blocking_once and "security" in system:
                self.blocking_once = False
                findings = [{"severity": "blocker", "file": self.file, "line": 1, "message": "unsafe"}]
            return json.dumps({"risk": self.risk, "approve": not findings, "findings": findings})
        if "release engineer" in system:
            return json.dumps({"flag": "feature-flag", "signals": [{"metric": "error_rate", "max": 0.05}]})
        if "performance engineer" in system:
            return json.dumps({"title": "Cache feature()", "description": "memoize", "acceptance_criteria": ["p95 ok"]})
        if "incident commander" in system:
            return json.dumps({"summary": "errors after deploy", "suspected_cause": "feature-flag",
                               "confidence": 0.8, "mitigations": ["turn flag off"], "rollback": True,
                               "flag": "feature-flag"})
        if "orchestrator of a multi-agent team" in system:
            if not _tool_results(msgs):
                return Message("assistant", "", tool_calls=[ToolCall("delegate_parallel", {"assignments": [
                    {"agent_id": "builder", "task": "build"}, {"agent_id": "checker", "task": "check"}]})])
            return "TEAM-MARK final: done"
        # coder / solo / worker agents: write a file once, then answer
        if not _tool_results(msgs):
            self.coder_runs += 1
            body = self.first_content if (self.first_content and self.coder_runs == 1) else self.content
            return Message("assistant", "", tool_calls=[ToolCall("write_file", {"path": self.file, "content": body})])
        marker = " TEAM-MARK" if "TEAM-MARK" in system else ""
        return f"implemented {self.file}{marker}"
