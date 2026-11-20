# astrbot_plugin_reminder

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 用的**到点主动提醒**插件：读取 [paadm](https://github.com/Huy4z/paadm) 的 `reminders` 表，在提醒时间到达后通过 QQ / WebChat 等平台主动推送消息，并把提醒状态从 `pending` 写回 `triggered`。

> 这是 paadm 的配套 Scheduler。`paadm` 负责创建、取消、查询提醒；本插件负责“到点真正发出来”。

## 功能

- 轮询 SQLite `reminders` 表，处理所有 `status='pending'` 且 `julianday(remind_at) <= now` 的提醒；
- 通过 AstrBot `Context.send_message` 主动推送，支持 QQ 官方机器人私聊、WebChat 等支持主动消息的平台；
- 发送成功后原子地把 `status` 改为 `triggered`（`WHERE status='pending'`），不会覆盖用户刚取消的提醒；
- 发送失败按指数退避自动重试，连续失败达到上限后暂停，提醒仍保留为 `pending`；
- 过期保护：默认只补发 24 小时内的提醒，避免停机后一次性收到大量旧提醒；
- `/reminder` 命令查看状态、待提醒、立即扫描、发送测试消息。

## 工作原理

```text
paadm CLI                 SQLite (paa.db)                 AstrBot 插件
   │                           │                              │
   ├─ reminder add ──────────▶ pending ──────────────────────▶ 每 20s 轮询
   │                           │                              │
   │                           │  remind_at <= now            ├─ users 表映射 provider/openid
   │                           │                              ├─ Context.send_message(UMO)
   │                           │                              │
   └─ reminder cancel ───────▶ cancelled ◀── 不改写 ──────────┤
                               │                              │
                               └──── triggered ◀─────────────┘ 发送成功才写回
```

关键点：

1. `remind_at` 是带时区偏移的 ISO 8601 字符串（例如 `2026-11-19T07:00:00+08:00`），SQL 使用 `julianday()` 比较，因此不同时区偏移都能正确排序；
2. `users` 表的 `(provider, external_user_id)` 映射到 AstrBot 会话：
   - QQ 官方机器人私聊：`provider=qq`，`external_user_id` 是 QQ openid；
   - WebChat：`provider=webchat`，`external_user_id` 是 WebChat 会话里的用户标识；
3. UMO（unified_msg_origin）格式为 `platform_id:MessageType:session_id`，例如 `<PLATFORM_ID>:FriendMessage:<QQ_OPENID>`；
4. 私聊默认 `FriendMessage`；群聊或特殊会话需要在 `session_overrides_json` 里手动指定完整 UMO。

## 兼容性

- AstrBot `>=4.29,<5`（已在 4.29.4 验证）；
- Python 3.10+；
- 无第三方 Python 依赖，只用标准库；
- 平台：
  - `qq_official`（QQ 官方机器人）：**私聊**支持主动推送。AstrBot `Context.send_message` 的文档字符串写着“qq_official 不支持”，但 4.29.4 的适配器已实现主动发送，私聊不需要缓存 `msg_id`；
  - `webchat`：支持主动消息，会持久化到 WebChat 会话；
  - `aiocqhttp` / `napcat` / `onebot`：通常可以主动发送私聊消息，具体取决于 OneBot 实现；
  - 群聊：需要 `session_overrides_json` 指定完整 UMO（群聊主动消息通常还需要平台侧的场景/msg_id 支持）。

## 安装

### 1. 放入插件目录

把本仓库放到 AstrBot 的 `data/plugins/` 下，目录名必须是 `astrbot_plugin_reminder`：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/Huy4z/astrbot_plugin_reminder.git
```

Docker 部署时，`data/plugins` 通常挂载在宿主机上，直接把目录放到宿主机对应路径即可。

### 2. 配置

在 AstrBot WebUI 的「插件配置」里填写，或直接编辑：

```text
data/config/astrbot_plugin_reminder_config.json
```

最小配置只需要保证 `db_path` 与 paadm 使用同一个数据库：

```json
{
  "enabled": true,
  "db_path": "/AstrBot/data/paa/paa.db",
  "poll_interval_seconds": 20,
  "max_overdue_hours": 24
}
```

### 3. 重启 / 重载

重启 AstrBot 容器，或在 WebUI 插件页重载。日志出现以下内容表示成功：

```text
[reminder] 插件启动 v1.0.0：db=/AstrBot/data/paa/paa.db，轮询=20 秒，过期阈值=24 小时
[reminder] 数据库就绪：pending=2，下一条=2026-11-21 07:00:00
```

## 配置项

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 是否自动轮询 |
| `db_path` | string | `/AstrBot/data/paa/paa.db` | paadm 数据库路径（容器内） |
| `poll_interval_seconds` | int | `20` | 轮询间隔，最小 5 秒 |
| `startup_delay_seconds` | int | `10` | 等待 AstrBot 加载完成的最长秒数 |
| `max_overdue_hours` | int | `24` | 最多补发多少小时内的提醒；`0` 表示不限制 |
| `batch_limit` | int | `20` | 单轮最多处理条数 |
| `message_template` | string | `⏰ 提醒：{content}` | 消息模板 |
| `timezone` | string | `Asia/Shanghai` | 展示时间用的时区 |
| `max_message_chars` | int | `800` | 单条消息最大字符数 |
| `retry_base_seconds` | int | `30` | 首次重试间隔 |
| `retry_max_seconds` | int | `600` | 重试间隔上限 |
| `max_delivery_attempts` | int | `20` | 最多自动重试次数 |
| `send_interval_ms` | int | `300` | 连续发送之间的间隔 |
| `platform_map_json` | string(JSON) | `""` | provider → 平台 ID，例如 `{"qq":"qq-main"}` |
| `session_type_map_json` | string(JSON) | `""` | provider → 会话类型，默认 `FriendMessage` |
| `session_overrides_json` | string(JSON) | `""` | 手动指定完整 UMO |

模板占位符：`{content}`、`{remind_at}`、`{remind_at_local}`、`{item_id}`、`{user_id}`、`{reminder_id}`。

### 多平台 / 群聊

同一 provider 有多个已加载平台时，用 `platform_map_json` 指定：

```json
{"qq": "<PLATFORM_ID>"}
```

群聊或特殊会话用 `session_overrides_json`：

```json
{
  "qq:<QQ_OPENID>": "<PLATFORM_ID>:GroupMessage:<GROUP_ID>",
  "webchat": "webchat:FriendMessage:webchat!<USER>!<THREAD>"
}
```

键可以是 `user_id`、`provider:external_user_id` 或 `provider:<provider>`；值必须是完整的 `platform:MessageType:session_id`。群聊没有通用映射规则，必须显式指定。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/reminder` / `/reminder status` | 查看运行状态、待提醒数量、下一条提醒、最近扫描结果 |
| `/reminder check` | 立即扫描一次（会重置失败重试计数） |
| `/reminder list` | 查看自己的待提醒 |
| `/reminder test` | 向当前会话发送一条测试消息，验证主动推送 |
| `/reminder help` | 帮助 |
| `/提醒` | `/reminder` 的别名 |

## 安全与边界

- 插件只读取 `reminders`、`users`，只更新 `reminders.status`，不会创建或删除提醒；
- 不会改写 `cancelled` / 已 `triggered` 的记录；
- 发送语义是 **at-least-once**：先发送，成功后才写回 `triggered`。如果发送成功但数据库写回失败，进程内会记住这条提醒并只补写状态，不会重复发送；插件重启后极端情况下可能重发一次；
- 日志会打印 UMO（包含平台侧用户 ID），不会打印消息正文以外的数据库内容；请勿把生产日志直接贴到公开 issue；
- README、配置、代码中不要提交真实 ECS IP、数据库、QQ openid、Token。

## 开发与测试

```bash
cd astrbot_plugin_reminder
python3 -m unittest discover -s tests -v
```

测试不依赖真实 AstrBot：`tests/_support.py` 注入最小 stub；数据库测试使用临时 SQLite。

```text
.
├── main.py               # 插件类、命令、生命周期、Context.send_message 包装
├── reminder_store.py     # SQLite 只读查询 + pending→triggered 写回
├── targets.py            # users 表 → AstrBot UMO 映射
├── scheduler.py          # asyncio 轮询、重试、过期保护
├── _conf_schema.json     # WebUI 配置 schema
├── metadata.yaml
└── tests/
```

## 常见问题

**Q：QQ 官方机器人能主动发私聊吗？**

A：AstrBot 4.29.4 的 `Context.send_message` 文档说不能，但 `QQOfficialPlatformAdapter.send_by_session` 对 `FriendMessage` 已实现主动推送（不需要 `msg_id`）。用 `/reminder test` 可以快速验证。群聊主动消息限制更多，需要平台侧支持。

**Q：提醒到点了但没收到？**

A：依次检查：

1. `/reminder status` 中数据库是否正常、`pending` 是否大于 0；
2. 日志里是否有“发送失败”及具体平台错误；
3. `users` 表是否有一条 `provider` / `external_user_id` 与当前账号匹配的记录；
4. 是否超过了 `max_overdue_hours`（超期提醒会被跳过并保持 pending）；
5. 平台是否支持主动消息，必要时先用 `/reminder test` 验证。

**Q：机器重启后会把旧提醒全发出来吗？**

A：默认不会。超过 `max_overdue_hours`（默认 24 小时）的提醒会跳过，保持 `pending`，并在日志里提示一次。需要补发更久以前的提醒时调大该值；设为 `0` 表示不限制。

## License

MIT
