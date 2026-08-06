"""One-time automatic greeting for new or inactive buyer dialogs."""

from __future__ import annotations

from datetime import datetime
import logging

from .funpay import FunPayError, FunPayMessage, FunPayReplyClient, RealOperationsDisabled
from .repositories import AutoReplyRepository, ReplyTarget, StoredFunPayMessage, TaskStateRepository


AUTO_REPLY_TEXT = "Привет"


class AutoReplyService:
    """Sends the exact greeting once, never during the initial historical sync."""

    def __init__(
        self, replies: FunPayReplyClient, repository: AutoReplyRepository, states: TaskStateRepository,
        logger: logging.Logger, *, default_enabled: bool, inactive_after_seconds: int,
    ) -> None:
        self._replies = replies
        self._repository = repository
        self._states = states
        self._logger = logger
        self._default_enabled = default_enabled
        self._inactive_after_seconds = inactive_after_seconds

    def is_initialized(self) -> bool:
        return self._states.load("funpay_auto_reply_bootstrap") is not None

    def mark_initialized(self) -> None:
        self._states.save("funpay_auto_reply_bootstrap", "ready")

    def is_enabled(self) -> bool:
        state = self._states.load("funpay_auto_reply")
        if state is None:
            return self._default_enabled
        return state[0] == "enabled"

    def maybe_reply(self, message: FunPayMessage, stored: StoredFunPayMessage, local_dialog_id: int) -> None:
        if message.direction != "incoming" or not self.is_enabled() or not message.buyer_nickname or not message.sent_at:
            return
        previous_sent_at = self._repository.previous_message_time(local_dialog_id, stored.local_id)
        if not _starts_new_or_inactive_dialog(previous_sent_at, message.sent_at, self._inactive_after_seconds):
            return
        attempt = self._repository.claim(
            stored.local_id, ReplyTarget(local_dialog_id, message.dialog_id, message.buyer_nickname), message.sent_at
        )
        if attempt is None:
            return
        try:
            self._replies.send_reply(
                attempt.target.external_dialog_id, attempt.target.buyer_nickname, AUTO_REPLY_TEXT, attempt.idempotency_key
            )
        except (FunPayError, RealOperationsDisabled, ValueError):
            self._repository.mark(attempt.attempt_id, "failed")
            self._logger.warning("Automatic greeting was not sent")
            return
        self._repository.mark(attempt.attempt_id, "sent")


def _starts_new_or_inactive_dialog(previous_sent_at: str | None, current_sent_at: str,
                                   inactive_after_seconds: int) -> bool:
    if previous_sent_at is None:
        return True
    try:
        previous = datetime.fromisoformat(previous_sent_at.replace("Z", "+00:00"))
        current = datetime.fromisoformat(current_sent_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (current - previous).total_seconds() >= inactive_after_seconds
