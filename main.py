from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest


class Main(star.Star):
    """Email platform adapter plugin entry point.

    Importing the adapter module triggers ``register_platform_adapter`` so the
    ``email`` platform type becomes available while this plugin is enabled.
    """

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        from . import email_adapter  # noqa: F401

    @filter.on_llm_request()
    async def mark_llm_pending(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Track that a default LLM request started for an email event.

        Args:
            event: Message event being processed.
            req: The provider request (unused).
        """
        if event.get_platform_name() != "email":
            return
        event.set_extra("_email_llm_pending", True)
        event.set_extra("_email_llm_ok", False)

    @filter.on_llm_response()
    async def mark_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """Mark a successful LLM response so the reply is not suppressed.

        Args:
            event: Message event being processed.
            response: The LLM response; a non-error role means success.
        """
        if event.get_platform_name() != "email":
            return
        if response is not None and getattr(response, "role", None) != "err":
            event.set_extra("_email_llm_ok", True)
