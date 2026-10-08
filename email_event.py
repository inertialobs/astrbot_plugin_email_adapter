from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata

if TYPE_CHECKING:
    from .email_adapter import EmailPlatformAdapter


class EmailMessageEvent(AstrMessageEvent):
    """AstrBot message event backed by a received email."""

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter: EmailPlatformAdapter,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.adapter = adapter

    async def send(self, message: MessageChain) -> None:
        """Send the reply through SMTP, respecting the per-recipient daily quota.

        The quota is consumed once per ``send`` call. When the quota is already
        exhausted the message is dropped silently and only a debug log is kept.

        Args:
            message: Message chain produced by the pipeline.
        """
        recipient = self.get_sender_id()
        text = message.get_plain_text()
        if not recipient or not text.strip():
            return

        if self.adapter.silent_on_llm_error and self._is_llm_failure():
            logger.debug(
                "[email:%s] LLM 请求失败，保持静默，不发送回复给 %s。",
                self.adapter.instance_id,
                recipient,
            )
            return

        if await self.adapter.is_over_quota(recipient):
            logger.debug(
                "[email:%s] Daily quota reached, silently dropping reply to %s.",
                self.adapter.instance_id,
                recipient,
            )
            return

        subject = self.adapter.build_reply_subject(
            str(self.get_extra("subject", "") or ""),
        )
        message_id = str(self.get_extra("message_id", "") or "")
        references = str(self.get_extra("references", "") or "")
        thread_refs = f"{references} {message_id}".strip()
        if await self.adapter.send_mail(
            recipient,
            subject,
            text,
            in_reply_to=message_id,
            references=thread_refs,
        ):
            await self.adapter.try_consume(recipient)
        await super().send(message)

    def _is_llm_failure(self) -> bool:
        """Heuristically detect that the current reply is an LLM error.

        AstrBot does not expose a stable "LLM failed" flag, so this combines the
        error extras set by the core with a request/response lifecycle tracked by
        the plugin hooks: if a default LLM request started but never produced a
        non-error response, the outgoing message is treated as a failure.

        Returns:
            True when the pending message should be suppressed.
        """
        if self.get_extra("_llm_error_message"):
            return True
        if self.get_extra("_third_party_runner_error"):
            return True
        return bool(self.get_extra("_email_llm_pending")) and not self.get_extra(
            "_email_llm_ok",
        )
