import asyncio
import base64
import imaplib
import json
import re
import smtplib
import ssl
from collections import OrderedDict
from datetime import datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy_default
from email.utils import formataddr, getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any

from astrbot.api import logger
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
from astrbot.api.star import StarTools
from astrbot.core.platform.astr_message_event import MessageSesion

from .email_event import EmailMessageEvent

_REGEX_META = re.compile(r"[\\^$*?{}\[\]|()]")
"""Characters that mark a block_from entry as a regular expression."""

EMAIL_DEFAULT_CONFIG = {
    "imap_host": "",
    "imap_port": 993,
    "imap_security": "ssl",
    "imap_user": "",
    "imap_pass": "",
    "imap_send_id": False,
    "imap_timeout": 30,
    "mailbox": "INBOX",
    "poll_interval": 30,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",
    "smtp_auth": True,
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_timeout": 30,
    "tls_verify": True,
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
        "hint": "收信服务器地址。例：imap.gmail.com、outlook.office365.com、imap.qq.com、imap.163.com，自建服务填自己的域名。",
    },
    "imap_port": {
        "description": "IMAP 端口",
        "type": "int",
        "hint": "SSL 常用 993，STARTTLS/明文常用 143。请与加密方式保持一致。",
    },
    "imap_security": {
        "description": "IMAP 加密方式",
        "type": "string",
        "options": ["ssl", "starttls", "plain"],
        "labels": ["SSL (隐式 TLS)", "STARTTLS", "明文"],
        "hint": "SSL 直连 993；STARTTLS 先连 143 再升级；明文仅用于本地/内网自建服务。",
    },
    "imap_user": {
        "description": "IMAP 用户名",
        "type": "string",
        "hint": "通常为邮箱地址。多个实例可共享同一个邮箱账户。",
    },
    "imap_pass": {
        "description": "IMAP 密码 / 授权码",
        "type": "string",
        "secret": True,
        "show_key": True,
        "hint": "邮箱授权码或应用专用密码（Gmail/Outlook 已禁用登录密码），不是网页登录密码。",
    },
    "imap_send_id": {
        "description": "发送 IMAP ID",
        "type": "bool",
        "hint": "部分服务商（如 163、Outlook）要求客户端先发送 ID 命令才允许收信，遇到无法登录时可开启。",
    },
    "imap_timeout": {
        "description": "IMAP 超时(秒)",
        "type": "int",
        "hint": "连接与读写超时，默认 30。",
    },
    "mailbox": {
        "description": "邮件文件夹",
        "type": "string",
        "hint": "本实例只轮询该文件夹，默认 INBOX。可直接填可读名（中文等会自动编码）；支持自建文件夹的服务商（如 QQ 邮箱）可用不同文件夹隔离实例。若服务器上不存在该文件夹，启动时会在日志中列出可用文件夹。",
    },
    "poll_interval": {
        "description": "轮询间隔(秒)",
        "type": "int",
        "hint": "默认 30 秒。",
    },
    "smtp_host": {
        "description": "SMTP 服务器",
        "type": "string",
        "hint": "发信服务器地址。例：smtp.gmail.com、smtp.office365.com、smtp.qq.com、smtp.163.com，也可填自建服务或事务邮件服务的 SMTP。",
    },
    "smtp_port": {
        "description": "SMTP 端口",
        "type": "int",
        "hint": "SSL 常用 465，STARTTLS 常用 587，明文常用 25。请与加密方式保持一致。",
    },
    "smtp_security": {
        "description": "SMTP 加密方式",
        "type": "string",
        "options": ["starttls", "ssl", "plain"],
        "labels": ["STARTTLS", "SSL (隐式 TLS)", "明文"],
        "hint": "STARTTLS 先连 587 再升级（多数服务器默认）；SSL 直连 465；明文仅用于本地 relay。",
    },
    "smtp_auth": {
        "description": "SMTP 需要认证",
        "type": "bool",
        "hint": "关闭后不发送认证信息，适用于本地/内网无需认证的 SMTP relay。默认开启。",
    },
    "smtp_user": {
        "description": "SMTP 用户名",
        "type": "string",
        "hint": "通常为邮箱地址；部分事务邮件服务使用固定的用户名。留空且关闭认证时不登录。",
    },
    "smtp_pass": {
        "description": "SMTP 密码 / API Key",
        "type": "string",
        "secret": True,
        "show_key": True,
        "hint": "邮箱授权码、应用专用密码或服务商 API Key。多个实例可共享。",
    },
    "smtp_timeout": {
        "description": "SMTP 超时(秒)",
        "type": "int",
        "hint": "连接与发送超时，默认 30。",
    },
    "tls_verify": {
        "description": "校验 TLS 证书",
        "type": "bool",
        "hint": "默认开启。仅在自建服务使用自签名证书时关闭，存在中间人风险。",
    },
    "from_addr": {
        "description": "发件地址",
        "type": "string",
        "hint": "本实例的 From 地址，例如 bot1@example.com。部分服务商要求发件地址属于已验证域名。",
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
        "hint": "留空表示允许所有收件人；填写后仅处理命中地址的邮件。支持多个地址，是多实例按地址路由的推荐方式。",
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

EMAIL_I18N_RESOURCES = {
    "zh-CN": {
        "imap_host": {
            "description": "IMAP 服务器",
            "hint": "收信服务器地址。例：imap.gmail.com、outlook.office365.com、imap.qq.com、imap.163.com，自建服务填自己的域名。",
        },
        "imap_port": {
            "description": "IMAP 端口",
            "hint": "SSL 常用 993，STARTTLS/明文常用 143。请与加密方式保持一致。",
        },
        "imap_security": {
            "description": "IMAP 加密方式",
            "labels": ["SSL (隐式 TLS)", "STARTTLS", "明文"],
            "hint": "SSL 直连 993；STARTTLS 先连 143 再升级；明文仅用于本地/内网自建服务。",
        },
        "imap_user": {
            "description": "IMAP 用户名",
            "hint": "通常为邮箱地址。多个实例可共享同一个邮箱账户。",
        },
        "imap_pass": {
            "description": "IMAP 密码 / 授权码",
            "hint": "邮箱授权码或应用专用密码（Gmail/Outlook 已禁用登录密码），不是网页登录密码。",
        },
        "imap_send_id": {
            "description": "发送 IMAP ID",
            "hint": "部分服务商（如 163、Outlook）要求客户端先发送 ID 命令才允许收信，遇到无法登录时可开启。",
        },
        "imap_timeout": {
            "description": "IMAP 超时(秒)",
            "hint": "连接与读写超时，默认 30。",
        },
        "mailbox": {
            "description": "邮件文件夹",
            "hint": "本实例只轮询该文件夹，默认 INBOX。可直接填可读名（中文等会自动编码）；支持自建文件夹的服务商（如 QQ 邮箱）可用不同文件夹隔离实例。若服务器上不存在该文件夹，启动时会在日志中列出可用文件夹。",
        },
        "poll_interval": {
            "description": "轮询间隔(秒)",
            "hint": "默认 30 秒。",
        },
        "smtp_host": {
            "description": "SMTP 服务器",
            "hint": "发信服务器地址。例：smtp.gmail.com、smtp.office365.com、smtp.qq.com、smtp.163.com，也可填自建服务或事务邮件服务的 SMTP。",
        },
        "smtp_port": {
            "description": "SMTP 端口",
            "hint": "SSL 常用 465，STARTTLS 常用 587，明文常用 25。请与加密方式保持一致。",
        },
        "smtp_security": {
            "description": "SMTP 加密方式",
            "labels": ["STARTTLS", "SSL (隐式 TLS)", "明文"],
            "hint": "STARTTLS 先连 587 再升级（多数服务器默认）；SSL 直连 465；明文仅用于本地 relay。",
        },
        "smtp_auth": {
            "description": "SMTP 需要认证",
            "hint": "关闭后不发送认证信息，适用于本地/内网无需认证的 SMTP relay。默认开启。",
        },
        "smtp_user": {
            "description": "SMTP 用户名",
            "hint": "通常为邮箱地址；部分事务邮件服务使用固定的用户名。留空且关闭认证时不登录。",
        },
        "smtp_pass": {
            "description": "SMTP 密码 / API Key",
            "hint": "邮箱授权码、应用专用密码或服务商 API Key。多个实例可共享。",
        },
        "smtp_timeout": {
            "description": "SMTP 超时(秒)",
            "hint": "连接与发送超时，默认 30。",
        },
        "tls_verify": {
            "description": "校验 TLS 证书",
            "hint": "默认开启。仅在自建服务使用自签名证书时关闭，存在中间人风险。",
        },
        "from_addr": {
            "description": "发件地址",
            "hint": "本实例的 From 地址，例如 bot1@example.com。部分服务商要求发件地址属于已验证域名。",
        },
        "from_name": {
            "description": "发件人显示名",
            "hint": "可选。",
        },
        "allowed_to": {
            "description": "允许的收件人",
            "hint": "留空表示允许所有收件人；填写后仅处理命中地址的邮件。支持多个地址，是多实例按地址路由的推荐方式。",
        },
        "block_from": {
            "description": "拒收发件人黑名单",
            "hint": "命中这些发件人的邮件直接拒收。普通地址精确匹配；含正则元字符（如 * ? [] () | ^ $ \\）的条目按正则整串匹配，大小写不敏感。留空则不过滤。",
        },
        "daily_limit": {
            "description": "每日单发件人上限",
            "hint": "每个发件人每天最多回复的邮件数，超出后静默丢弃。默认 3。",
        },
        "daily_total_limit": {
            "description": "每日总发信上限",
            "hint": "每个实例每天发送邮件总数上限（所有收件人合计），超出后静默丢弃。0 表示不限制。",
        },
        "silent_on_llm_error": {
            "description": "LLM 失败时静默",
            "hint": "启用后，当默认 LLM 请求失败时不回复错误信息（只记日志）。默认开启。",
        },
        "reply_subject_prefix": {
            "description": "回复主题前缀",
            "hint": "默认 'Re: '。留空则直接使用原主题。",
        },
    },
    "en-US": {
        "imap_host": {
            "description": "IMAP server",
            "hint": "Incoming mail server, e.g. imap.gmail.com, outlook.office365.com, imap.qq.com, imap.163.com, or your own host.",
        },
        "imap_port": {
            "description": "IMAP port",
            "hint": "Usually 993 for SSL, 143 for STARTTLS/plain. Keep it consistent with the security mode.",
        },
        "imap_security": {
            "description": "IMAP security",
            "labels": ["SSL (implicit TLS)", "STARTTLS", "Plain"],
            "hint": "SSL connects on 993; STARTTLS upgrades from 143; plain is only for local/self-hosted servers.",
        },
        "imap_user": {
            "description": "IMAP username",
            "hint": "Usually the mailbox address. Multiple instances may share one account.",
        },
        "imap_pass": {
            "description": "IMAP password / app password",
            "hint": "Authorization code or app-specific password (Gmail/Outlook no longer accept the login password).",
        },
        "imap_send_id": {
            "description": "Send IMAP ID",
            "hint": "Some providers (e.g. 163, Outlook) require the IMAP ID command before delivering mail. Enable if login fails.",
        },
        "imap_timeout": {
            "description": "IMAP timeout (s)",
            "hint": "Connect and read timeout. Default 30.",
        },
        "mailbox": {
            "description": "Mailbox folder",
            "hint": "This instance only polls this folder. Default INBOX. Human readable names are accepted (non-ASCII names are encoded automatically). Providers supporting custom folders (e.g. QQ Mail) can isolate instances by folder. If the folder is missing, the available folders are logged at startup.",
        },
        "poll_interval": {
            "description": "Poll interval (s)",
            "hint": "Default 30 seconds.",
        },
        "smtp_host": {
            "description": "SMTP server",
            "hint": "Outgoing mail server, e.g. smtp.gmail.com, smtp.office365.com, smtp.qq.com, smtp.163.com, a self-hosted server or a transactional email SMTP.",
        },
        "smtp_port": {
            "description": "SMTP port",
            "hint": "Usually 465 for SSL, 587 for STARTTLS, 25 for plain. Keep it consistent with the security mode.",
        },
        "smtp_security": {
            "description": "SMTP security",
            "labels": ["STARTTLS", "SSL (implicit TLS)", "Plain"],
            "hint": "STARTTLS upgrades from 587 (common default); SSL connects on 465; plain is only for a local relay.",
        },
        "smtp_auth": {
            "description": "SMTP requires auth",
            "hint": "When disabled, no credentials are sent. Use for a local/intranet SMTP relay without authentication. Default on.",
        },
        "smtp_user": {
            "description": "SMTP username",
            "hint": "Usually the mailbox address; some transactional providers use a fixed username. Leave empty with auth disabled to skip login.",
        },
        "smtp_pass": {
            "description": "SMTP password / API key",
            "hint": "Authorization code, app-specific password or provider API key. Multiple instances may share it.",
        },
        "smtp_timeout": {
            "description": "SMTP timeout (s)",
            "hint": "Connect and send timeout. Default 30.",
        },
        "tls_verify": {
            "description": "Verify TLS certificate",
            "hint": "Enabled by default. Only disable it for self-signed certificates on self-hosted servers; it weakens security.",
        },
        "from_addr": {
            "description": "From address",
            "hint": "The From address of this instance, e.g. bot1@example.com. Some providers require a verified domain.",
        },
        "from_name": {
            "description": "From display name",
            "hint": "Optional.",
        },
        "allowed_to": {
            "description": "Allowed recipients",
            "hint": "Empty means all recipients are allowed; otherwise only matching mails are processed. Multiple addresses are supported and this is the recommended way to route multiple instances.",
        },
        "block_from": {
            "description": "Blocked senders",
            "hint": "Mails from these senders are rejected. Plain addresses match exactly; entries containing regex metacharacters (e.g. * ? [] () | ^ $ \\) match the whole address as a case-insensitive regex. Empty disables filtering.",
        },
        "daily_limit": {
            "description": "Daily per-sender limit",
            "hint": "Max replies per sender per day; further replies are dropped silently. Default 3.",
        },
        "daily_total_limit": {
            "description": "Daily total limit",
            "hint": "Max mails sent per instance per day (all recipients combined); further replies are dropped silently. 0 means unlimited.",
        },
        "silent_on_llm_error": {
            "description": "Stay silent on LLM failure",
            "hint": "When enabled, a failed default LLM request is not answered (logged only). Default on.",
        },
        "reply_subject_prefix": {
            "description": "Reply subject prefix",
            "hint": "Default 'Re: '. Empty reuses the original subject.",
        },
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


def _imap_mutf7_encode(value: str) -> str:
    """Encode a Unicode mailbox name into IMAP modified UTF-7 (RFC 3501).

    Printable ASCII except ``&`` is kept as-is, ``&`` becomes ``&-`` and any
    other character is UTF-16BE + base64 encoded, wrapped in ``&...-`` with
    ``/`` replaced by ``,``.

    Args:
        value: Human readable mailbox name.

    Returns:
        The ASCII modified UTF-7 representation used on the wire.
    """
    result: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode("ascii")
        result.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
        pending.clear()

    for char in value:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(result)


def _imap_mutf7_decode(value: str) -> str:
    """Decode an IMAP modified UTF-7 mailbox name back to Unicode.

    Args:
        value: Raw mailbox name as returned by ``LIST`` (modified UTF-7).

    Returns:
        The human readable mailbox name. Invalid sequences are kept verbatim.
    """
    result: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char != "&":
            result.append(char)
            index += 1
            continue
        end = value.find("-", index + 1)
        if end == -1:
            result.append(char)
            index += 1
            continue
        if end == index + 1:
            result.append("&")
            index = end + 1
            continue
        payload = value[index + 1 : end].replace(",", "/")
        padding = "=" * (-len(payload) % 4)
        try:
            decoded = base64.b64decode(payload + padding).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            result.append(value[index : end + 1])
        else:
            result.append(decoded)
        index = end + 1
    return "".join(result)


_LIST_LINE_RE = re.compile(
    r"^\*?\s*(?:LIST\s+)?\((?P<flags>[^)]*)\)\s+"
    r'(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$',
    re.IGNORECASE,
)


def _parse_list_line(line: str) -> tuple[str, str, str] | None:
    """Parse one ``LIST`` response line.

    Args:
        line: A ``LIST`` response such as ``(\\HasNoChildren) "/" "INBOX"``.

    Returns:
        A ``(raw_name, flags, delimiter)`` tuple, or ``None`` if unparseable.
    """
    match = _LIST_LINE_RE.match(line.strip())
    if not match:
        return None
    flags = match.group("flags")
    delim_token = match.group("delim")
    delimiter = "" if delim_token.upper() == "NIL" else delim_token[1:-1]
    name_token = match.group("name").strip()
    if len(name_token) >= 2 and name_token.startswith('"') and name_token.endswith('"'):
        raw = name_token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    else:
        raw = name_token
    return raw, flags, delimiter


@register_platform_adapter(
    "email",
    "Email 适配器 (IMAP 收信 / SMTP 发信)",
    default_config_tmpl=EMAIL_DEFAULT_CONFIG,
    config_metadata=EMAIL_CONFIG_METADATA,
    i18n_resources=EMAIL_I18N_RESOURCES,
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

        self.imap_host = str(platform_config.get("imap_host") or "")
        self.imap_port = int(platform_config.get("imap_port") or 993)
        self.imap_security = (
            str(platform_config.get("imap_security") or "ssl").strip().lower()
        )
        if self.imap_security not in ("ssl", "starttls", "plain"):
            logger.warning(
                "[email:%s] 未知的 imap_security=%r，回退为 ssl。",
                self.instance_id,
                self.imap_security,
            )
            self.imap_security = "ssl"
        self.imap_user = str(platform_config.get("imap_user") or "")
        self.imap_pass = str(platform_config.get("imap_pass") or "")
        self.imap_send_id = bool(platform_config.get("imap_send_id", False))
        self.imap_timeout = max(1, int(platform_config.get("imap_timeout") or 30))
        self.mailbox = str(platform_config.get("mailbox") or "INBOX")
        # Wire form used by SELECT; non-ASCII names are modified UTF-7 encoded
        # later during the startup folder check when the server list is known.
        self._wire_mailbox = (
            self.mailbox if self.mailbox.isascii() else _imap_mutf7_encode(self.mailbox)
        )
        self._mailbox_warned = False
        self.poll_interval = max(1, int(platform_config.get("poll_interval") or 30))

        self.smtp_host = str(platform_config.get("smtp_host") or "")
        self.smtp_port = int(platform_config.get("smtp_port") or 587)
        self.smtp_security = (
            str(platform_config.get("smtp_security") or "starttls").strip().lower()
        )
        if self.smtp_security not in ("ssl", "starttls", "plain"):
            logger.warning(
                "[email:%s] 未知的 smtp_security=%r，回退为 starttls。",
                self.instance_id,
                self.smtp_security,
            )
            self.smtp_security = "starttls"
        self.smtp_auth = bool(platform_config.get("smtp_auth", True))
        self.smtp_user = str(platform_config.get("smtp_user") or "")
        self.smtp_pass = str(platform_config.get("smtp_pass") or "")
        self.smtp_timeout = max(1, int(platform_config.get("smtp_timeout") or 30))
        self.tls_verify = bool(platform_config.get("tls_verify", True))
        self._ssl_context = ssl.create_default_context()
        if not self.tls_verify:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE

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

        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._seen_limit = 10000
        self._quota_date = ""
        self._quota_counts: dict[str, int] = {}
        self._quota_total = 0
        self._quota_lock = asyncio.Lock()
        safe_id = re.sub(r"[^0-9A-Za-z._-]", "_", self.instance_id) or "email"
        self._limit_file = (
            StarTools.get_data_dir("astrbot_plugin_email_adapter")
            / f"email_limit_{safe_id}.json"
        )
        self._load_quota()

        self._shutdown_event = asyncio.Event()

        if not self.imap_host or not self.imap_user or not self.imap_pass:
            raise ValueError(
                f"[email:{self.instance_id}] imap_host / imap_user / imap_pass 未配置。",
            )
        if not self.smtp_host:
            raise ValueError(
                f"[email:{self.instance_id}] smtp_host 未配置。",
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

        await self._check_mailbox()

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
        if (
            recipient
            and text.strip()
            and not await self.is_over_quota(recipient)
            and await self.send_mail(recipient, self.build_reply_subject(""), text)
        ):
            await self.try_consume(recipient)
        await super().send_by_session(session, message_chain)

    async def send_mail(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> bool:
        """Send one email through SMTP in a worker thread.

        Args:
            to: Recipient address.
            subject: Full subject line.
            body: Plain text body.
            in_reply_to: Original Message-ID used for the In-Reply-To header.
            references: References header used to keep the reply in the thread.

        Returns:
            True when the mail was accepted by the SMTP server, False otherwise.
        """
        try:
            await asyncio.to_thread(
                self._smtp_send,
                to,
                subject,
                body,
                in_reply_to,
                references,
            )
            logger.info(
                "[email:%s] 已向 %s 发送邮件: %s",
                self.instance_id,
                to,
                subject,
            )
            return True
        except Exception as e:
            logger.error(
                "[email:%s] 向 %s 发送邮件失败: %s: %s",
                self.instance_id,
                to,
                type(e).__name__,
                e,
                exc_info=True,
            )
            return False

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
            self._seen_ids[message_id] = None
            if len(self._seen_ids) > self._seen_limit:
                self._seen_ids.popitem(last=False)

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
            event.set_extra("references", parsed["references"])
            event.set_extra("bot_id", self.instance_id)
            self.commit_event(event)

            logger.info(
                "[email:%s] 收到来自 %s 的邮件: %r",
                self.instance_id,
                sender,
                parsed["subject"],
            )

    def _open_imap_connection(self) -> imaplib.IMAP4:
        """Open a logged-in IMAP connection according to the configured security.

        Honors ``imap_security`` (SSL / STARTTLS / plaintext), ``imap_timeout``,
        ``tls_verify`` and ``imap_send_id``.

        Returns:
            A ready-to-use IMAP connection.

        Raises:
            Exception: Propagated from imaplib when connecting or logging in fails.
        """
        if self.imap_security == "ssl":
            connection = imaplib.IMAP4_SSL(
                self.imap_host,
                self.imap_port,
                timeout=self.imap_timeout,
                ssl_context=self._ssl_context,
            )
        else:
            connection = imaplib.IMAP4(
                self.imap_host,
                self.imap_port,
                timeout=self.imap_timeout,
            )
            if self.imap_security == "starttls":
                connection.starttls(ssl_context=self._ssl_context)
        try:
            connection.login(self.imap_user, self.imap_pass)
            if self.imap_send_id:
                try:
                    connection._simple_command(
                        "ID",
                        '("name" "AstrBot" "version" "1.0")',
                    )
                except Exception as e:
                    logger.debug(
                        "[email:%s] 发送 IMAP ID 失败: %s",
                        self.instance_id,
                        e,
                    )
        except Exception:
            try:
                connection.logout()
            except Exception:
                pass
            raise
        return connection

    def _list_mailboxes(self) -> list[dict[str, Any]]:
        """List the mailboxes available on the server.

        Returns:
            A list of dicts carrying the decoded ``name``, the ``raw`` wire
            name, the ``delimiter`` and a ``selectable`` flag, sorted by decoded
            name.

        Raises:
            Exception: Propagated from imaplib when listing fails.
        """
        connection = self._open_imap_connection()
        try:
            status, data = connection.list()
        finally:
            try:
                connection.logout()
            except Exception:
                pass
        if status != "OK" or not data:
            return []
        folders: dict[str, dict[str, Any]] = {}
        for item in data:
            if isinstance(item, bytes):
                line = item.decode("ascii", "replace")
            elif isinstance(item, str):
                line = item
            else:
                continue
            parsed = _parse_list_line(line)
            if not parsed:
                continue
            raw, flags, delimiter = parsed
            folders[raw] = {
                "name": _imap_mutf7_decode(raw),
                "raw": raw,
                "delimiter": delimiter,
                "selectable": "\\Noselect" not in flags,
            }
        return sorted(folders.values(), key=lambda folder: folder["name"].lower())

    async def _check_mailbox(self) -> None:
        """Resolve the configured mailbox against the server once at startup.

        Matching uses the decoded (human readable) name, the raw wire name and
        the decoded form of the configured value, so plain and already-encoded
        names both work. When nothing matches, a warning is logged together with
        the available folders. Any failure only logs and never aborts the
        adapter.
        """
        try:
            folders = await asyncio.to_thread(self._list_mailboxes)
        except Exception as e:
            self._mailbox_warned = True
            logger.warning(
                "[email:%s] 无法列出服务器文件夹（%s: %s），继续使用配置值 %r。",
                self.instance_id,
                type(e).__name__,
                e,
                self.mailbox,
            )
            return
        if not folders:
            logger.warning(
                "[email:%s] 服务器未返回任何可用文件夹，继续使用配置值 %r。",
                self.instance_id,
                self.mailbox,
            )
            return

        target = self.mailbox.strip().lower()
        decoded_target = _imap_mutf7_decode(self.mailbox).strip().lower()
        ordered = [folder for folder in folders if folder["selectable"]] + [
            folder for folder in folders if not folder["selectable"]
        ]
        for folder in ordered:
            name = str(folder["name"]).strip().lower()
            raw = str(folder["raw"]).strip()
            if target in (name, raw.lower()) or decoded_target in (name, raw.lower()):
                self._wire_mailbox = folder["raw"]
                logger.info(
                    "[email:%s] 使用邮件文件夹 %r（raw: %r）。",
                    self.instance_id,
                    folder["name"],
                    folder["raw"],
                )
                return

        self._mailbox_warned = True
        selectable = [folder for folder in folders if folder["selectable"]] or folders
        preview = selectable[:30]
        listing = "\n".join(
            f"    - {folder['name']}  (raw: {folder['raw']})" for folder in preview
        )
        if len(selectable) > len(preview):
            listing += f"\n    ... 其余 {len(selectable) - len(preview)} 个"
        logger.warning(
            "[email:%s] 找不到邮件文件夹 %r。服务器可用文件夹(%d)：\n%s\n"
            "    提示：可直接填写上面的可读名（中文会自动编码）。",
            self.instance_id,
            self.mailbox,
            len(selectable),
            listing,
        )

    def _fetch_unseen(self) -> list[tuple[bytes, str]]:
        """Fetch all UNSEEN messages using a fresh IMAP connection.

        The connection is built according to ``imap_security`` (implicit SSL,
        STARTTLS or plaintext), honors ``imap_timeout`` / ``tls_verify`` and is
        always closed before returning, so concurrent instances never share
        state.

        Returns:
            A list of ``(raw_bytes, uid)`` tuples.
        """
        connection = self._open_imap_connection()
        results: list[tuple[bytes, str]] = []
        try:
            status, _ = connection.select(self._wire_mailbox, readonly=False)
            if status != "OK":
                if not self._mailbox_warned:
                    self._mailbox_warned = True
                    logger.warning(
                        "[email:%s] 无法选择邮箱文件夹 %r（raw: %r），请检查配置。",
                        self.instance_id,
                        self.mailbox,
                        self._wire_mailbox,
                    )
                else:
                    logger.debug(
                        "[email:%s] 邮箱文件夹 %r 仍不可用，跳过本轮。",
                        self.instance_id,
                        self.mailbox,
                    )
                return []

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
            "references": str(message.get("References", "")).strip(),
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

    def _smtp_send(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> None:
        message = EmailMessage()
        message["From"] = (
            formataddr((self.from_name, self.from_addr))
            if self.from_name
            else self.from_addr
        )
        message["To"] = to
        message["Subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        message.set_content(body)

        if self.smtp_security == "ssl":
            server_ctx = smtplib.SMTP_SSL(
                self.smtp_host,
                self.smtp_port,
                timeout=self.smtp_timeout,
                context=self._ssl_context,
            )
        else:
            server_ctx = smtplib.SMTP(
                self.smtp_host,
                self.smtp_port,
                timeout=self.smtp_timeout,
            )
        with server_ctx as server:
            if self.smtp_security == "starttls":
                server.ehlo()
                server.starttls(context=self._ssl_context)
                server.ehlo()
            if self.smtp_auth and self.smtp_user:
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
