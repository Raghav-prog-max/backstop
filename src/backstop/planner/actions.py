"""The action space. Every action is bounded by at least one rule except `wait`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..domain.types import Channel, MessageClass


class ActionKind(str, Enum):
    WAIT = "wait"
    RETRY_PAYMENT = "retry_payment"
    SWITCH_INSTRUMENT = "switch_instrument"
    REQUEST_REAUTH_LINK = "request_reauth_link"
    SEND_MESSAGE = "send_message"
    VOICE_CALL = "voice_call"
    OFFER_INSTALLMENT = "offer_installment"
    # B2B: ask the buyer to commit to a date. A promise puts the case on PR-08 hold;
    # a kept promise is a recovery, a broken one is information the planner acts on.
    REQUEST_PROMISE_TO_PAY = "request_promise_to_pay"
    ESCALATE_HUMAN = "escalate_human"
    CLOSE_CASE = "close_case"


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    fire_at: datetime
    channel: Channel | None = None
    template: str | None = None
    reason: str | None = None
    # Which TCCCPR class this contact falls under. Defaults to the stricter of the
    # two, so a caller that forgets to set it gets the safer treatment.
    message_class: MessageClass = MessageClass.PROMOTIONAL

    def idem_key(self, case_id: str, attempt: int) -> str:
        return f"{case_id}:{self.kind.value}:{attempt}:{self.fire_at.isoformat()}"

    def __str__(self) -> str:
        bits = [self.kind.value]
        if self.channel:
            bits.append(self.channel.value)
        if self.template:
            bits.append(self.template)
        return "/".join(bits)
