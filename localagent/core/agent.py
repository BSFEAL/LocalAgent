"""A single LLM agent: instructions + tools + a provider, run as a tool loop."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..providers.base import Message, Provider, Usage
from .tools import Toolbox
from .trajectory import Conversation, Trajectory


@dataclass
class AgentResult:
    output: str
    steps: int
    usage: Usage
    finished: bool  # False when the step budget ran out


@dataclass
class Agent:
    """Stateful agent. ``send`` continues the same conversation, which lets an
    orchestrator *recall* a worker with a narrower follow-up (a MASS design
    lever) instead of starting from scratch."""

    id: str
    instructions: str
    provider: Provider
    tools: Toolbox = field(default_factory=Toolbox)
    max_steps: int = 40
    role: str = "single"
    temperature: float | None = None
    seed: int | None = None
    trajectory: Trajectory | None = None
    conversation: Conversation = field(init=False)

    def __post_init__(self) -> None:
        self.conversation = Conversation(self.id, self.role, [Message("system", self.instructions)],
                                         self.tools.specs(), model=self.provider.spec)
        if self.trajectory is not None:
            self.trajectory.add(self.conversation)

    @property
    def messages(self) -> list[Message]:
        return self.conversation.messages

    def send(self, text: str) -> AgentResult:
        self.messages.append(Message("user", text))
        usage = Usage()
        specs = self.tools.specs() or None
        for step in range(1, self.max_steps + 1):
            comp = self.provider.complete(self.messages, specs, temperature=self.temperature, seed=self.seed)
            usage = usage + comp.usage
            self.conversation.usage = self.conversation.usage + comp.usage
            msg = comp.message
            self.messages.append(msg)
            if not msg.tool_calls:
                return AgentResult(msg.content, step, usage, True)
            for call in msg.tool_calls:
                result = self.tools.call(call.name, call.arguments)
                self.messages.append(Message("tool", result, tool_call_id=call.id, name=call.name))
        self.messages.append(Message("user", "Step budget exhausted. Reply with your final answer now, no tools."))
        comp = self.provider.complete(self.messages, None, temperature=self.temperature, seed=self.seed)
        usage = usage + comp.usage
        self.conversation.usage = self.conversation.usage + comp.usage
        self.messages.append(comp.message)
        return AgentResult(comp.message.content, self.max_steps, usage, False)

    run = send
