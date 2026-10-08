import asyncio
import imaplib
import json
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy_default
from email.utils import formataddr, getaddresses, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .email_event import EmailMessageEvent

_REGEX_META = re.compile(r"[\\^$*?{}\[\]|()]")
"""Characters that mark a block_from entry as a regular expression."""

EMAIL_DEFAULT_CONFIG = {
    "imap_host": "imap.qq.com",
    "imap_port": 993,
    "imap_user": "",
    "imap_pass": "",
    "mailbox": "Bot1",
    "poll_interval": 30,
    "smtp_host": "smtp.resend.com",
    "smtp_port": 587,
    "smtp_user": "resend",
    "smtp_pass": "",
    "from_addr": "",
    "from_name": "",
    "allowed_to": [],
    "block_from": [],
    "daily_limit": 3,
    "daily_total_limit": 0,
    "silent_on_llm_error": True,
    "reply_subject_prefix": "Re: ",
}

EMAIL_CONFIG_METADATA = {
    "imap_host": {
        "description": "IMAP 服务器",
        "type": "string",
        "hint": "收信服务器地址，例如 imap.qq.com。",
    },
    "imap_port": {
        "description": "IMAP 端口",
        "type": "int",
        "hint": "SSL 端口，通常为 993。",
    },
    "imap_user": {
        "description": "IMAP 用户名",
        "type": "string",
        "hint": "多个实例可共享同一个邮箱账户。",
    },
    "imap_pass": {
        "description": "IMAP 授权码",
        "type": "string",
        "secret": True,
        "show_key": True,
        "hint": "邮箱授权码，不是登录密码。",
    },
    "mailbox": {
        "description": "邮件文件夹",
        "type": "string",
        "hint": "本实例只轮询该文件夹，例如 Bot1。不同实例请使用不同文件夹。",
    },
    "poll_interval": {
        "description": "轮询间隔(秒)",
        "type": "int",
        "hint": "默认 30 秒。",
    },
    "smtp_host": {
        "description": "SMTP 服务器",
        "type": "string",
        "hint": "发信服务器地址，例如 smtp.resend.com。",
    },
    "smtp_port": {
        "description": "SMTP 端口",
        "type": "int",
        "hint": "STARTTLS 端口，通常为 587。",
    },
    "smtp_user": {
        "description": "SMTP 用户名",
        "type": "string",
        "hint": "Resend 固定为 resend。",
    },
    "smtp_pass": {
        "description": "SMTP 密码 / API Key",
        "type": "string",
        "secret": True,
        "show_key": True,
        "hint": "Resend API Key。多个实例可共享。",
    },
    "from_addr": {
        "description": "发件地址",
        "type": "string",
        "hint": "本实例的 From 地址，例如 bot1@example.com。",
    },
    "from_name": {
        "description": "发件人显示名",
        "type": "string",
        "hint": "可选。",
    },
    "allowed_to": {
        "description": "允许的收件人",
        "type": "list",
        "items": {"type": "string"},
        "hint": "留空表示允许所有收件人；填写后仅处理命中地址的邮件。支持多个地址。",
    },
    "block_from": {
        "description": "拒收发件人黑名单",
        "type": "list",
        "items": {"type": "string"},
        "hint": "命中这些发件人的邮件直接拒收。普通地址精确匹配；含正则元字符（如 * ? [] () | ^ $ \\）的条目按正则整串匹配，大小写不敏感。例：noreply@example.com、.*@spam\\.com。留空则不过滤。",
    },
    "daily_limit": {
        "description": "每日单发件人上限",
        "type": "int",
        "hint": "每个发件人每天最多回复的邮件数，超出后静默丢弃。默认 3。",
    },
    "daily_total_limit": {
        "description": "每日总发信上限",
        "type": "int",
        "hint": "每个实例每天发送邮件总数上限（所有收件人合计），超出后静默丢弃。0 表示不限制。",
    },
    "silent_on_llm_error": {
        "description": "LLM 失败时静默",
        "type": "bool",
        "hint": "启用后，当默认 LLM 请求失败时不回复错误信息（只记日志）。默认开启。",
    },
    "reply_subject_prefix": {
        "description": "回复主题前缀",
        "type": "string",
        "hint": "默认 'Re: '。留空则直接使用原主题。",
    },
}


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML → plain text extractor built on the standard library."""

    _BLOCK_TAGS = (
        "br",
        "p",
        "div",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _html_to_text(html_content: str) -> str:
    """Convert an HTML body into a readable plain text string.

    Args:
        html_content: Raw HTML source.

    Returns:
        Plain text with tags removed and whitespace normalized. Falls back to a
        regex-based tag strip if parsing fails.
    """
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html_content)
        parser.close()
        text = parser.get_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_content)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


@register_platform_adapter(
    "email",
    "Email 适配器 (IMAP 收信 / SMTP 发信)",
    default_config_tmpl=EMAIL_DEFAULT_CONFIG,
    config_metadata=EMAIL_CONFIG_METADATA,
    logo_path="logo.png",
    support_streaming_message=False,
)
class EmailPlatformAdapter(Platform):
    """A lightweight Email platform adapter supporting multiple bot instances.

    Each instance polls its own IMAP mailbox and sends through SMTP with an
    independent sender address, deduplication set and daily quota file. All
    blocking network IO is executed in worker threads so the event loop is
    never blocked.
    """

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.instance_id = str(platform_config.get("id") or "email")

        self.imap_host = str(platform_config.get("imap_host") or "imap.qq.com")
        self.imap_port = int(platform_config.get("imap_port") or 993)
        self.imap_user = str(platform_config.get("imap_user") or "")
        self.imap_pass = str(platform_config.get("imap_pass") or "")
        self.mailbox = str(platform_config.get("mailbox") or "INBOX")
        self.poll_interval = max(1, int(platform_config.get("poll_interval") or 30))

        self.smtp_host = str(platform_config.get("smtp_host") or "smtp.resend.com")
        self.smtp_port = int(platform_config.get("smtp_port") or 587)
        self.smtp_user = str(platform_config.get("smtp_user") or "resend")
        self.smtp_pass = str(platform_config.get("smtp_pass") or "")
        self.from_addr = str(platform_config.get("from_addr") or self.imap_user)
        self.from_name = str(platform_config.get("from_name") or "")

        raw_prefix = platform_config.get("reply_subject_prefix")
        self.reply_subject_prefix = "Re: " if raw_prefix is None else str(raw_prefix)

        allowed = platform_config.get("allowed_to") or []
        if isinstance(allowed, str):
            allowed = [allowed]
        self.allowed_to = {
            str(addr).strip().lower() for addr in allowed if str(addr).strip()
        }

        blocked = platform_config.get("block_from") or []
        if isinstance(blocked, str):
            blocked = [blocked]
        self._block_patterns: list[re.Pattern[str]] = []
        for entry in blocked:
            pattern = str(entry).strip()
            if not pattern:
                continue
            if _REGEX_META.search(pattern):
                try:
                    self._block_patterns.append(re.compile(pattern, re.IGNORECASE))
                    continue
                except re.error as e:
                    logger.warning(
                        "[email:%s] block_from 非法正则 %r，将按字面量处理: %s",
                        self.instance_id,
                        pattern,
                        e,
                    )
            self._block_patterns.append(
                re.compile(re.escape(pattern), re.IGNORECASE),
            )

        self.daily_limit = max(0, int(platform_config.get("daily_limit") or 3))
        raw_total_limit = platform_config.get("daily_total_limit")
        self.daily_total_limit = (
            0 if raw_total_limit is None else max(0, int(raw_total_limit))
        )
        self.silent_on_llm_error = bool(
            platform_config.get("silent_on_llm_error", True),
        )

        self._seen_ids: set[str] = set()
        self._quota_date = ""
        self._quota_counts: dict[str, int] = {}
        self._quota_total = 0
        self._quota_lock = asyncio.Lock()
        safe_id = re.sub(r"[^0-9A-Za-z._-]", "_", self.instance_id) or "email"
        self._limit_file = Path(get_astrbot_data_path()) / f"email_limit_{safe_id}.json"
        self._load_quota()

        self._shutdown_event = asyncio.Event()

        if not self.imap_user or not self.imap_pass:
            raise ValueError(
                f"[email:{self.instance_id}] imap_user / imap_pass 未配置。",
            )

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="email",
            description="Email 适配器 (IMAP 收信 / SMTP 发信)",
            id=self.instance_id,
            support_streaming_message=False,
        )

    async def run(self) -> None:
        """Poll the configured mailbox until the adapter is terminated.

        Any exception raised during a polling cycle is logged and swallowed so
        that a single instance failure never affects other instances.
        """
        if self.allowed_to:
            logger.info(
                "[email:%s] 仅处理收件人命中 %s 的邮件。",
                self.instance_id,
                sorted(self.allowed_to),
            )
        else:
            logger.info(
                "[email:%s] allowed_to 为空，允许所有收件人。",
                self.instance_id,
            )
        logger.info(
            "[email:%s] Email 适配器启动，mailbox=%s，间隔=%ss，单发件人上限=%s，总上限=%s。",
            self.instance_id,
            self.mailbox,
            self.poll_interval,
            self.daily_limit,
            self.daily_total_limit or "不限",
        )

        while not self._shutdown_event.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "[email:%s] 轮询失败: %s: %s",
                    self.instance_id,
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.poll_interval,
                )
            except asyncio.TimeoutError:
                continue

        logger.info("[email:%s] Email 适配器已停止。", self.instance_id)

    async def terminate(self) -> None:
        self._shutdown_event.set()

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        recipient = session.session_id
        text = message_chain.get_plain_text()
        if recipient and text.strip() and await self.try_consume(recipient):
            await self.send_mail(recipient, self.build_reply_subject(""), text)
        await super().send_by_session(session, message_chain)

    async def send_mail(self, to: str, subject: str, body: str) -> None:
        """Send one email through SMTP in a worker thread.

        Args:
            to: Recipient address.
            subject: Full subject line.
            body: Plain text body.
        """
        try:
            await asyncio.to_thread(self._smtp_send, to, subject, body)
            logger.info(
                "[email:%s] 已向 %s 发送邮件: %s",
                self.instance_id,
                to,
                subject,
            )
        except Exception as e:
            logger.error(
                "[email:%s] 向 %s 发送邮件失败: %s: %s",
                self.instance_id,
                to,
                type(e).__name__,
                e,
                exc_info=True,
            )

    def build_reply_subject(self, original_subject: str) -> str:
        """Build the reply subject applying the configured prefix.

        Args:
            original_subject: Subject of the received email.

        Returns:
            The reply subject without duplicating an existing prefix.
        """
        original = (original_subject or "").strip()
        prefix = self.reply_subject_prefix
        if not original:
            return prefix or "AstrBot"
        if prefix and not original.lower().startswith(prefix.strip().lower()):
            return f"{prefix}{original}"
        return original

    async def try_consume(self, recipient: str) -> bool:
        """Consume one daily quota slot for a recipient.

        Enforces both the per-recipient limit and the instance-wide daily total
        limit. Nothing is counted unless the send is actually allowed.

        Args:
            recipient: Email address that would receive the reply.

        Returns:
            True when a slot is available, False when a daily limit is met.
        """
        async with self._quota_lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._quota_date:
                self._quota_date = today
                self._quota_counts = {}
                self._quota_total = 0
            if (
                self.daily_total_limit > 0
                and self._quota_total >= self.daily_total_limit
            ):
                return False
            key = recipient.strip().lower()
            count = self._quota_counts.get(key, 0)
            if count >= self.daily_limit:
                return False
            self._quota_counts[key] = count + 1
            self._quota_total += 1
            await asyncio.to_thread(self._save_quota)
            return True

    async def is_over_quota(self, recipient: str) -> bool:
        """Check whether the daily quota is already exhausted for a recipient.

        Read-only counterpart of :meth:`try_consume` used to drop over-quota
        emails before they enter the pipeline, so the LLM is never invoked for
        them. Handles the day rollover so counters are reset at midnight.

        Args:
            recipient: Sender address about to be processed.

        Returns:
            True when either the per-recipient or the instance-wide daily limit
            has already been reached.
        """
        async with self._quota_lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._quota_date:
                self._quota_date = today
                self._quota_counts = {}
                self._quota_total = 0
            if (
                self.daily_total_limit > 0
                and self._quota_total >= self.daily_total_limit
            ):
                return True
            key = recipient.strip().lower()
            return self._quota_counts.get(key, 0) >= self.daily_limit

    async def _poll_once(self) -> None:
        mails = await asyncio.to_thread(self._fetch_unseen)
        for raw, uid in mails:
            try:
                parsed = self._parse_mail(raw)
            except Exception as e:
                logger.warning(
                    "[email:%s] 解析邮件失败 (%s): %s",
                    self.instance_id,
                    uid,
                    e,
                )
                continue

            message_id = parsed["message_id"] or f"uid:{uid}"
            if message_id in self._seen_ids:
                continue
            self._seen_ids.add(message_id)

            if self._is_blocked_sender(parsed):
                logger.debug(
                    "[email:%s] 已拒收发件人 %s 的邮件。",
                    self.instance_id,
                    parsed["from_addr"],
                )
                continue

            if not self._is_allowed_recipient(parsed):
                logger.debug(
                    "[email:%s] 忽略非允许收件人的邮件: to=%s",
                    self.instance_id,
                    parsed["to"],
                )
                continue

            sender = parsed["from_addr"]
            if not sender:
                continue

            if await self.is_over_quota(sender):
                logger.debug(
                    "[email:%s] 发件人 %s 已达每日限额，跳过处理（不调用 LLM）。",
                    self.instance_id,
                    sender,
                )
                continue

            abm = AstrBotMessage()
            abm.type = MessageType.FRIEND_MESSAGE
            abm.self_id = self.from_addr or self.instance_id
            abm.session_id = sender
            abm.message_id = message_id
            abm.sender = MessageMember(sender, parsed["from_name"] or sender)
            abm.message_str = parsed["body"]
            abm.message = [Plain(parsed["body"])] if parsed["body"] else []
            abm.raw_message = parsed

            event = EmailMessageEvent(
                message_str=abm.message_str,
                message_obj=abm,
                platform_meta=self.meta(),
                session_id=abm.session_id,
                adapter=self,
            )
            event.set_extra("subject", parsed["subject"])
            event.set_extra("to", parsed["to"])
            event.set_extra("message_id", message_id)
            event.set_extra("bot_id", self.instance_id)
            self.commit_event(event)

            logger.info(
                "[email:%s] 收到来自 %s 的邮件: %r",
                self.instance_id,
                sender,
                parsed["subject"],
            )

    def _fetch_unseen(self) -> list[tuple[bytes, str]]:
        """Fetch all UNSEEN messages using a fresh IMAP connection.

        Returns:
            A list of ``(raw_bytes, uid)`` tuples. The connection is always
            closed before returning, so concurrent instances never share state.
        """
        connection = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        results: list[tuple[bytes, str]] = []
        try:
            connection.login(self.imap_user, self.imap_pass)
            status, _ = connection.select(self.mailbox, readonly=False)
            if status != "OK":
                raise RuntimeError(f"无法选择邮箱文件夹 {self.mailbox!r}")

            status, data = connection.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return []

            for uid in data[0].split()[:100]:
                status, msg_data = connection.fetch(uid, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = next(
                    (
                        part[1]
                        for part in msg_data
                        if isinstance(part, tuple) and part[1]
                    ),
                    None,
                )
                if raw is None:
                    continue
                results.append((raw, uid.decode("ascii", "ignore")))
                connection.store(uid, "+FLAGS", "\\Seen")
            return results
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    def _parse_mail(self, raw: bytes) -> dict[str, Any]:
        message = BytesParser(policy=email_policy_default).parsebytes(raw)
        from_name, from_addr = parseaddr(str(message.get("From", "")))
        return {
            "from_addr": from_addr.strip().lower(),
            "from_name": from_name.strip(),
            "to": str(message.get("To", "")),
            "delivered_to": str(message.get("Delivered-To", "")),
            "x_original_to": str(message.get("X-Original-To", "")),
            "subject": str(message.get("Subject", "")),
            "message_id": str(message.get("Message-ID", "")).strip(),
            "body": self._extract_body(message),
        }

    @staticmethod
    def _extract_body(message: EmailMessage) -> str:
        try:
            part = message.get_body(preferencelist=("plain", "html"))
        except (AttributeError, KeyError, TypeError):
            part = None
        if part is None:
            return ""
        content = part.get_content()
        if isinstance(content, bytes):
            content = content.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_subtype() == "html":
            return _html_to_text(str(content))
        return str(content).strip()

    def _is_blocked_sender(self, parsed: dict[str, Any]) -> bool:
        if not self._block_patterns:
            return False
        sender = (parsed.get("from_addr") or "").strip()
        if not sender:
            return False
        return any(pattern.fullmatch(sender) for pattern in self._block_patterns)

    def _is_allowed_recipient(self, parsed: dict[str, Any]) -> bool:
        if not self.allowed_to:
            return True
        candidates: set[str] = set()
        for field in ("to", "delivered_to", "x_original_to"):
            value = parsed.get(field) or ""
            for _, addr in getaddresses([value]):
                addr = addr.strip().lower()
                if addr:
                    candidates.add(addr)
        return bool(candidates & self.allowed_to)

    def _smtp_send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = (
            formataddr((self.from_name, self.from_addr))
            if self.from_name
            else self.from_addr
        )
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if self.smtp_user:
                server.login(self.smtp_user, self.smtp_pass)
            server.send_message(message)

    def _load_quota(self) -> None:
        self._quota_date = datetime.now().strftime("%Y-%m-%d")
        self._quota_counts = {}
        self._quota_total = 0
        if not self._limit_file.exists():
            return
        try:
            data = json.loads(self._limit_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(
                "[email:%s] 读取限额文件失败: %s",
                self.instance_id,
                e,
            )
            return
        if not isinstance(data, dict) or data.get("date") != self._quota_date:
            return
        counts = data.get("counts")
        if isinstance(counts, dict):
            self._quota_counts = {
                str(key): int(value)
                for key, value in counts.items()
                if isinstance(value, (int, float))
            }
        total = data.get("total")
        if isinstance(total, (int, float)):
            self._quota_total = int(total)

    def _save_quota(self) -> None:
        payload = {
            "date": self._quota_date,
            "counts": self._quota_counts,
            "total": self._quota_total,
        }
        try:
            self._limit_file.parent.mkdir(parents=True, exist_ok=True)
            self._limit_file.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(
                "[email:%s] 写入限额文件失败: %s",
                self.instance_id,
                e,
            )
