# astrbot_plugin_email_adapter

AstrBot 原生 Email 平台适配器（IMAP 收信 / SMTP 发信），支持多个 Bot 实例并行。
仅依赖 Python 标准库，不依赖 Node.js / OneBots。

## 安装

- 方式一（已就绪）：本目录已位于 `data/plugins/astrbot_plugin_email_adapter/`，重启
  AstrBot 后在「插件」页启用 `astrbot_plugin_email_adapter` 即可。
- 方式二（上传安装）：在「插件」页上传打包好的
  `astrbot_plugin_email_adapter.zip`。

启用后可在「平台」页看到 `email` 类型并新增实例。

> 注意：同一份插件不要同时保留在 `builtin_stars/` 和 `data/plugins/`，
> 否则平台类型 `email` 会重复注册、其中一个插件加载失败。

## 多实例配置

在「平台」页新增多个 `type: "email"` 的条目。多个实例共享同一个 IMAP 账户与
SMTP 凭据，但各自使用不同的文件夹和发件地址：

| 字段 | 说明 |
| --- | --- |
| `id` | 实例唯一标识，用于日志前缀和限额文件名，如 `email_bot1` |
| `imap_host` / `imap_port` | 收信服务器，QQ 邮箱为 `imap.qq.com` / `993` |
| `imap_user` / `imap_pass` | IMAP 账户与授权码（多实例可共享） |
| `mailbox` | 只轮询该文件夹，如 `Bot1`，不读取 INBOX 或其他实例的文件夹 |
| `poll_interval` | 轮询间隔秒数，默认 30 |
| `smtp_host` / `smtp_port` | 发信服务器，Resend 为 `smtp.resend.com` / `587` |
| `smtp_user` / `smtp_pass` | Resend 固定用户名 `resend`，密码为 API Key（多实例可共享） |
| `from_addr` / `from_name` | 本实例发件地址与显示名，如 `bot1@example.com` |
| `allowed_to` | 允许的收件人列表；**留空则允许所有收件人**，填写后仅处理命中地址的邮件 |
| `block_from` | 拒收发件人黑名单；普通地址精确匹配，含正则元字符的条目（如 `.*@spam\.com`）按正则整串匹配（大小写不敏感），命中即拒收。留空不过滤 |
| `daily_limit` | 每个发件人每天的回复上限，默认 3 |
| `daily_total_limit` | 本实例每天发送邮件总数上限（所有收件人合计），超出后静默丢弃；`0` 表示不限制 |
| `silent_on_llm_error` | LLM 请求失败时是否静默（不回错误信息，只记日志），默认 `true` |
| `reply_subject_prefix` | 回复主题前缀，默认 `Re: `，留空则不添加 |

示例（两个 Bot 共享邮箱和 Resend，各用不同文件夹/地址）：

```json
[
  {
    "id": "email_bot1",
    "type": "email",
    "enable": true,
    "imap_host": "imap.qq.com",
    "imap_port": 993,
    "imap_user": "shared@qq.com",
    "imap_pass": "<授权码>",
    "mailbox": "Bot1",
    "poll_interval": 30,
    "smtp_host": "smtp.resend.com",
    "smtp_port": 587,
    "smtp_user": "resend",
    "smtp_pass": "<Resend API Key>",
    "from_addr": "bot1@example.com",
    "from_name": "Bot1",
    "allowed_to": ["bot1@example.com"],
    "daily_limit": 3,
    "reply_subject_prefix": "Re: "
  },
  {
    "id": "email_bot2",
    "type": "email",
    "enable": true,
    "imap_host": "imap.qq.com",
    "imap_port": 993,
    "imap_user": "shared@qq.com",
    "imap_pass": "<授权码>",
    "mailbox": "Bot2",
    "from_addr": "bot2@example.com",
    "from_name": "Bot2",
    "allowed_to": ["bot2@example.com"],
    "daily_limit": 3
  }
]
```

## 行为说明

- 每轮用独立的 IMAP 连接 `UNSEEN` 搜索新邮件，处理后将邮件标记为 `\Seen`，连接用完即关。
- 按 `Message-ID` 去重，去重集合按实例隔离。
- 正文优先取 `text/plain`，没有时从 `text/html` 提取纯文本。
- 会话 ID / 发件人 ID 使用邮件发件人地址；`subject / to / message_id / bot_id` 保存在事件 `extra` 中。
- 拒收：`block_from` 命中发件人即跳过（优先于收件人过滤），记 debug 日志；支持精确地址与正则（`re.fullmatch`，大小写不敏感）。
- 收件人过滤：`allowed_to` 为空时不限制；否则匹配 `To` / `Delivered-To` / `X-Original-To`（大小写不敏感），不匹配的邮件忽略并记 debug 日志。
- 每日限额分两级、按实例隔离：`daily_limit`（每个发件人）与 `daily_total_limit`（本实例总数，`0` 不限）。任一超限即**在收信阶段跳过该邮件**（不产生事件、不调用 LLM、不回复，仅 debug 日志）；计数仍按发送次数在 `send()` 时消耗，并持久化到 `data/email_limit_<id>.json`，跨日自动重置。
- `silent_on_llm_error` 开启时，默认 LLM 请求失败**不回错误信息**（只记 debug 日志）；通过 `on_llm_request`/`on_llm_response` 钩子追踪请求生命周期，并结合核心的错误标记判断。
- 每个实例独立协程与状态，单个实例异常只记日志，不影响其他实例。

## 依赖

Python 标准库 `imaplib` / `smtplib` / `email`，无需额外安装。
