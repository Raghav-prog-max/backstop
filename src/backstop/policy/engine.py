"""The policy engine. The model proposes; this disposes.

Every evaluation is recorded whether or not it allowed anything — that is what makes
the "why not" view free rather than a feature someone has to remember to build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..domain.case import Case
from ..domain.types import Disposition
from ..planner.actions import Action
from .config import PolicyConfig
from .rules import RULES, RuleContext, RuleResult

# Dispositions ordered by how much they close down. The strictest result wins.
SEVERITY: dict[Disposition, int] = {
    Disposition.ALLOW: 0,
    Disposition.DEFER: 1,
    Disposition.DENY: 2,
    Disposition.SUPPRESS: 3,
    Disposition.HARD_STOP: 4,
}


@dataclass(slots=True)
class PolicyDecision:
    disposition: Disposition
    rule_ids: tuple[str, ...]
    reason: str
    # The rule that actually decided the outcome — not merely the first one to fire.
    # Audit trails that name the wrong rule are worse than no audit trail.
    deciding_rule: str = ""
    defer_until: datetime | None = None
    config_version: str = ""
    fired: list[RuleResult] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.disposition is Disposition.ALLOW


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def evaluate(self, case: Case, action: Action, ctx: RuleContext) -> PolicyDecision:
        fired = [r for rule in RULES if (r := rule(case, action, ctx)) is not None]

        if not fired:
            return PolicyDecision(
                Disposition.ALLOW,
                (f"cfg:{self.config.version}",),
                "no rule objected",
                deciding_rule="",
                config_version=self.config.version,
            )

        worst = max(fired, key=lambda r: SEVERITY[r.disposition])
        # Every rule that fired is recorded, not just the deciding one.
        rule_ids = tuple(r.rule_id for r in fired) + (f"cfg:{self.config.version}",)
        return PolicyDecision(
            disposition=worst.disposition,
            rule_ids=rule_ids,
            reason=f"{worst.rule_id}: {worst.reason}",
            deciding_rule=worst.rule_id,
            defer_until=worst.defer_until,
            config_version=self.config.version,
            fired=fired,
        )
