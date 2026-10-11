"""Human-in-the-loop gate for changes that are not low-risk."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .changes import ChangeSet
from .outcome import Outcome
from .review import ReviewResult


@dataclass
class Approval:
    approved: bool
    reviewer: str
    comment: str = ""


class Approver(Protocol):
    def request(self, outcome: Outcome, changes: ChangeSet, review: ReviewResult) -> Approval: ...


class AutoApprover:
    """For tests/sandboxes only. Never use in production."""

    def __init__(self, approve: bool = True):
        self.approve = approve

    def request(self, outcome, changes, review) -> Approval:
        return Approval(self.approve, "auto", "auto-approved" if self.approve else "auto-rejected")


class CallbackApprover:
    """Bridge to anything: a Slack button, a GitHub PR review, a web form."""

    def __init__(self, fn: Callable[[Outcome, ChangeSet, ReviewResult], Approval]):
        self.fn = fn

    def request(self, outcome, changes, review) -> Approval:
        return self.fn(outcome, changes, review)


class CLIApprover:
    def request(self, outcome, changes, review) -> Approval:
        print(f"\n=== Human review required ({review.risk} risk) ===\n{outcome.title}\n{changes.summary()}")
        print("Reasons: " + "; ".join(review.reasons))
        for f in review.findings:
            print(f"  [{f.reviewer}/{f.severity}] {f.file}:{f.line} {f.message}")
        if input("Show diff? [y/N] ").strip().lower() == "y":
            print(changes.diff)
        ok = input("Approve deploy? [y/N] ").strip().lower() == "y"
        return Approval(ok, "cli", input("Comment (optional): ").strip())
