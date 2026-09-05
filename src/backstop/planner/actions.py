"""The action space. Every action is bounded by at least one rule except `wait`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..domain.types import Channel


class ActionKind(str, Enum):
    WAIT = "wait"
    RETRY_PAYMENT = "retry_payment"
    SWITCH_INSTRUMENT = "switch_instrument"
    REQUEST_REAUTH_LINK = "request_reauth_link"
    SEND_MESSAGE = "send_message"
    VOICE_CALL = "voice_call"
    OFFER_INSTALLMENT = "offer_installment"
    ESCALATE_HUMAN = "escalate_human"
    CLOSE_CASE = "close_case"


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    fire_at: datetime
    channel: Channel | None = None
    template: str | None = None
    reason: str | None = None

    def idem_key(self, case_id: str, attempt: int) -> str:
        return f"{case_id}:{self.kind.value}:{attempt}:{self.fire_at.isoformat()}"

    def __str__(self) -> str:
        bits = [self.kind.value]
        if self.channel:
            bits.append(self.channel.value)
        if self.template:
            bits.append(self.template)
        return "/".join(bits)
