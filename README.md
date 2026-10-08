# Astrbot Email 适配器

![AstrBot](https://img.shields.io/badge/AstrBot-plugin-5865f2?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square)
![License](https://img.shields.io/badge/license-AGPL-3b82f6?style=flat-square)

AstrBot Email 平台适配器（IMAP 收信 / SMTP 发信），支持多个 Bot 实例并行。
收发均支持 SSL / STARTTLS / 明文与可选证书校验，适配 Gmail、Outlook、QQ 邮箱、163、
自建服务器及各类事务邮件服务.

启用后可在创建机器人-「消息平台类别」底部 `email` 类型并新增实例。

## 多实例配置

在「平台」页新增多个 `type: "email"` 的条目。每个实例完全独立：各自配置自己的 IMAP/SMTP 账户、凭据、文件夹与发件地址；IMAP 与 SMTP 也可以使用不同的服务商。若多个实例需要共享同一账户，可只靠不同的 mailbox 或 allowed_to 地址来区分。

| 字段 | 说明 |
| --- | --- |
| `id` | 实例唯一标识，用于日志前缀和限额文件名，如 `email_bot1` |
| `imap_host` / `imap_port` | 收信服务器与端口。SSL 常用 `993`，STARTTLS/明文常用 `143` |
| `imap_security` | IMAP 加密方式：`ssl`（默认，隐式 TLS）/ `starttls` / `plain`（仅本地/内网） |
| `imap_user` / `imap_pass` | IMAP 账户与应用专用密码/授权码（每实例独立配置，也可多实例共享）。Gmail/Outlook 需使用应用专用密码 |
| `imap_send_id` | 是否发送 IMAP `ID` 命令；163、Outlook 等要求发送后才能收信，默认 `false` |
| `imap_timeout` | IMAP 连接与读写超时秒数，默认 30 |
| `mailbox` | 本实例只轮询该文件夹，默认 `INBOX`。可直接填可读名（中文等会自动编码为 modified UTF-7）；支持自建文件夹的服务商（如 QQ 邮箱）可用不同文件夹隔离实例。文件夹不存在时启动日志会列出可用文件夹 |
| `poll_interval` | 轮询间隔秒数，默认 30 |
| `smtp_host` / `smtp_port` | 发信服务器与端口。SSL 常用 `465`，STARTTLS 常用 `587`，明文常用 `25` |
| `smtp_security` | SMTP 加密方式：`starttls`（默认）/ `ssl`（隐式 TLS）/ `plain`（仅本地 relay） |
| `smtp_auth` | 是否需要认证，默认 `true`；关闭后不发送凭据，适用于本地/内网无需认证的 relay |
| `smtp_user` / `smtp_pass` | SMTP 账户与应用专用密码/API Key（每实例独立配置，也可多实例共享；留空且关闭认证时不登录 |
| `smtp_timeout` | SMTP 连接与发送超时秒数，默认 30 |
| `tls_verify` | 是否校验 TLS 证书，默认 `true`；仅在自建服务使用自签名证书时关闭 |
| `from_addr` / `from_name` | 本实例发件地址与显示名，如 `bot1@example.com` |
| `allowed_to` | 允许的收件人列表；**留空则允许所有收件人**，填写后仅处理命中地址的邮件，是多实例按地址路由的推荐方式 |
| `block_from` | 拒收发件人黑名单；普通地址精确匹配，含正则元字符的条目（如 `.*@spam\.com`）按正则整串匹配（大小写不敏感），命中即拒收。留空不过滤 |
| `daily_limit` | 每个发件人每天的回复上限，默认 3 |
| `daily_total_limit` | 本实例每天发送邮件总数上限（所有收件人合计），超出后静默丢弃；`0` 表示不限制 |
| `silent_on_llm_error` | LLM 请求失败时是否静默（不回错误信息，只记日志），默认 `true` |
| `reply_subject_prefix` | 回复主题前缀，默认 `Re: `，留空则不添加 |

### 常见服务商对照

| 服务商 | IMAP | SMTP | 加密 / 端口 | 凭据 |
| --- | --- | --- | --- | --- |
| Gmail | `imap.gmail.com` | `smtp.gmail.com` | `ssl` / 993、465 | 应用专用密码（需开启两步验证） |
| Outlook / Office365 | `outlook.office365.com` | `smtp.office365.com` | `ssl` / 993，`starttls` / 587 | 应用专用密码（部分账户强制 OAuth，本插件暂不支持） |
| QQ 邮箱 | `imap.qq.com` | `smtp.qq.com` | `ssl` / 993、465 | 授权码 |
| 163 邮箱 | `imap.163.com` | `smtp.163.com` | `ssl` / 993、465 | 授权码，建议开启 `imap_send_id` |
| 自建 / 事务邮件服务 | 你的域名 | 你的域名 | 视部署而定 | 视部署而定，可填服务商 SMTP 凭据 |

示例（Gmail 单实例，收发均走 SSL）：

```json
[
  {
    "id": "email_bot",
    "type": "email",
    "enable": true,
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "imap_security": "ssl",
    "imap_user": "bot@gmail.com",
    "imap_pass": "<应用专用密码>",
    "mailbox": "INBOX",
    "poll_interval": 30,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_security": "ssl",
    "smtp_auth": true,
    "smtp_user": "bot@gmail.com",
    "smtp_pass": "<应用专用密码>",
    "from_addr": "bot@gmail.com",
    "from_name": "Bot",
    "allowed_to": ["bot@gmail.com"],
    "daily_limit": 3,
    "reply_subject_prefix": "Re: "
  }
]
```

示例（自建服务器，STARTTLS 收信、内网无认证 relay 发信）：

```json
[
  {
    "id": "email_selfhosted",
    "type": "email",
    "enable": true,
    "imap_host": "mail.example.com",
    "imap_port": 143,
    "imap_security": "starttls",
    "imap_user": "bot@example.com",
    "imap_pass": "<密码>",
    "mailbox": "INBOX",
    "smtp_host": "127.0.0.1",
    "smtp_port": 25,
    "smtp_security": "plain",
    "smtp_auth": false,
    "from_addr": "bot@example.com",
    "from_name": "Bot"
  }
]
```

## 行为说明

- 启动时（进入轮询前）连接服务器 `LIST` 文件夹，用配置的 `mailbox` 匹配可读名/原始名；命中则用其线上名收信，**未命中会 WARNING 并列出服务器可用文件夹**（最多 30 个），之后只记一次，不再每轮刷 error。
- `mailbox` 支持直接填写可读名（中文等会自动进行 modified UTF-7 编码），也兼容已填写的原始编码名。
- 每轮按 `imap_security`（SSL / STARTTLS / 明文）建立独立的 IMAP 连接，`UNSEEN` 搜索新邮件，处理后将邮件标记为 `\Seen`，连接用完即关；`imap_timeout` 防止连接挂死。
- `imap_send_id` 开启时在登录后发送 IMAP `ID` 命令，兼容 163、Outlook 等要求该命令的服务商。
- 按 `Message-ID` 去重，去重集合按实例隔离并有容量上限。
- 正文优先取 `text/plain`，没有时从 `text/html` 提取纯文本。
- 会话 ID / 发件人 ID 使用邮件发件人地址；`subject / to / message_id / references / bot_id` 保存在事件 `extra` 中。
- 回复自动写入 `In-Reply-To` / `References` 头，在 Gmail、Outlook 等客户端中与原邮件归入同一会话。
- 拒收：`block_from` 命中发件人即跳过（优先于收件人过滤），记 debug 日志；支持精确地址与正则（`re.fullmatch`，大小写不敏感）。
- 收件人过滤：`allowed_to` 为空时不限制；否则匹配 `To` / `Delivered-To` / `X-Original-To`（大小写不敏感），不匹配的邮件忽略并记 debug 日志。
- 每日限额分两级、按实例隔离：`daily_limit`（每个发件人）与 `daily_total_limit`（本实例总数，`0` 不限）。任一超限即**在收信阶段跳过该邮件**（不产生事件、不调用 LLM、不回复，仅 debug 日志）；计数在邮件**发送成功后**才消耗，发送失败不占用额度，并持久化到 `data/email_limit_<id>.json`，跨日自动重置。
- `silent_on_llm_error` 开启时，默认 LLM 请求失败**不回错误信息**（只记 debug 日志）；通过 `on_llm_request`/`on_llm_response` 钩子追踪请求生命周期，并结合核心的错误标记判断。
- 每个实例独立协程与状态，单个实例异常只记日志，不影响其他实例。

## 许可证

本项目基于 [AGPL-3.0](LICENSE) 许可证开源。

