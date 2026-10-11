"""Initial (v0) workflows for every agentic stage.

These are only starting points: they live in the WorkflowStore once written,
and the self-improvement slot evolves them per stage.
"""
from __future__ import annotations

from ..core.workflow import AgentSpec, Workflow

FINDINGS_CONTRACT = ('JSON: {"risk": "low|medium|high", "approve": true|false, "findings": '
                     '[{"severity": "blocker|major|minor|nit", "file": "...", "line": 0, "message": "..."}]}')


def coder_workflow() -> Workflow:
    return Workflow(
        "coder",
        AgentSpec("codex", "software engineer",
                  "Implement the requested outcome in the repository. Read the relevant code first, "
                  "make minimal focused changes that match the surrounding style, add or update tests, "
                  "and run the project's checks with run_command before finishing. Keep docs in the code "
                  "up to date. Finish with a short summary of what changed and why."),
    )


def coder_team_workflow() -> Workflow:
    """A multi-agent alternative; MASS may discover something like this on its own."""
    return Workflow(
        "coder",
        AgentSpec("lead", "tech lead",
                  "Decompose the outcome, delegate implementation and testing, review results against "
                  "the acceptance criteria, recall agents with narrower follow-ups until everything passes."),
        [AgentSpec("implementer", "software engineer", "Implement code changes as assigned.",
                   "List of files changed, rationale, open questions."),
         AgentSpec("tester", "test engineer", "Write/extend tests for the assigned behaviour and run them.",
                   "Tests added, command run, pass/fail with failing output excerpt."),
         AgentSpec("critic", "code reviewer", "Critically review the current diff for bugs and missed criteria.",
                   "Numbered list of concrete defects with file:line, or 'LGTM'.",
                   tools=["read_file", "list_files", "search", "run_command"])],
        protocol="Implementer and tester may run in parallel. Critic reviews before finishing; "
                 "any defect goes back to the implementer via recall.",
    )


def review_workflow() -> Workflow:
    ro = ["read_file", "list_files", "search"]
    return Workflow(
        "review",
        AgentSpec("risk_classifier", "release risk classifier",
                  "Merge the specialist reviews into one verdict. Classify the change risk: low = isolated, "
                  "well-tested, no data/infra/security surface; medium = touches shared code paths or config; "
                  "high = data migrations, auth, secrets, infra, public API or wide blast radius. "
                  "approve=false if any blocker finding stands.", FINDINGS_CONTRACT, tools=ro),
        [AgentSpec("data", "data specialist",
                   "Review the diff for schema/migration safety, data correctness, PII handling, query cost.",
                   FINDINGS_CONTRACT, tools=ro),
         AgentSpec("infra", "infrastructure specialist",
                   "Review for performance, resource usage, concurrency, config and deploy safety, rollback.",
                   FINDINGS_CONTRACT, tools=ro),
         AgentSpec("cloud", "cloud specialist",
                   "Review for cloud resource/IAM changes, cost, quotas, regional/availability concerns.",
                   FINDINGS_CONTRACT, tools=ro),
         AgentSpec("security", "security specialist",
                   "Review for injection, authz/authn, secrets, unsafe deserialization, SSRF, dependency risk.",
                   FINDINGS_CONTRACT, tools=ro)],
        protocol="Specialists review in parallel; the risk classifier aggregates.",
    )


def sevbot_workflow() -> Workflow:
    return Workflow(
        "sevbot",
        AgentSpec("sevbot", "incident commander",
                  "Investigate the incident using metrics, recent deploys and code history. Identify the most "
                  "likely cause, propose ranked mitigations (rollback/flag-off first when a recent change "
                  "correlates), and answer responders' questions concisely with evidence.",
                  'JSON: {"summary": "...", "suspected_cause": "...", "confidence": 0.0, '
                  '"mitigations": ["..."], "rollback": true|false, "flag": "name or null"}'),
    )


def perf_workflow() -> Workflow:
    return Workflow(
        "perf",
        AgentSpec("perf_engineer", "performance engineer",
                  "Given a latency regression, the change history and the code, find the likely cause and "
                  "describe a concrete fix as an outcome for the coding agent.",
                  'JSON: {"title": "...", "description": "...", "acceptance_criteria": ["..."]}',
                  tools=["read_file", "list_files", "search", "search_context"]),
    )


def deploy_workflow() -> Workflow:
    return Workflow(
        "deploy",
        AgentSpec("deploy_agent", "release engineer",
                  "Given a change, decide which production signals to watch during rollout and their "
                  "thresholds, and name the feature flag guarding it.",
                  'JSON: {"flag": "...", "signals": [{"metric": "...", "max": 0.0, "min": null, '
                  '"description": "..."}]}', tools=["read_file", "list_files", "search"]),
    )


DEFAULT_WORKFLOWS = {
    "coder": coder_workflow, "review": review_workflow, "sevbot": sevbot_workflow,
    "perf": perf_workflow, "deploy": deploy_workflow,
}
