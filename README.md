# Minecraft 服务器管理插件（RCON + 鹊桥桥接）

通过 RCON 协议连接 Minecraft 服务器，支持多服务器管理、AI 自然语言控制、定时任务、性能监控；并内置鹊桥（QueQiao）mod 对接，实现 MC 与 QQ 群之间的双向聊天与事件桥接。

## 功能

- `/mc status` — 查看服务器综合状态（在线玩家、世界种子、时间、天气、难度）
- `/mc players` — 查看在线玩家列表
- `/mc servers` — 列出所有配置的服务器
- `/mc use <服务器名>` — 切换默认服务器
- `/mc monitor` — 性能监控（TPS/内存）
- `/mc whitelist` — 白名单管理（查看/添加/移除）
- `/mc ban / unban / banlist / kick` — 封禁与踢人
- `/mc op / deop` — OP 管理
- `/mc gamemode / time / weather / difficulty` — 游戏控制
- `/mc give / tp / kill` — 物品与传送
- `/mc announce <消息>` — 全服公告
- `/mc backup` — 手动备份（save-all）
- `/mc batch give <物品> [数量]` — 给所有在线玩家发物品
- `/mc ai <自然语言>` — AI 自然语言执行指令
- `/mc cmd <指令>` — 执行任意服务端指令
- `/mc silent ...` — 静默模式（定时关闭 MC→QQ 转发，消息缓存并在结束后回放）
- `/mc bridge on|off|status` — 鹊桥（QueQiao）MC↔QQ 聊天桥接开关与状态
- `/mc help` — 完整帮助
- LLM 工具 `mc_rcon_command` — 让 AI 直接调用 RCON

## 安装

1. 安装方式一：在 AstrBot WebUI 的「插件市场」搜索并安装；方式二：将本插件文件夹复制到 `data/plugins/` 目录后启用。

2. 确保 Minecraft 服务器已启用 RCON：

   ```properties
   # server.properties
   enable-rcon=true
   rcon.password=你的密码
   rcon.port=25575
   ```

3. 在 AstrBot WebUI 中启用插件，并填写 RCON 配置。

4. （可选）如需 MC↔QQ 聊天桥接：在 MC 服务端安装鹊桥（QueQiao）mod，开启其 WebSocket Server，并在插件配置中填写 `queqiao_ws_url` / `queqiao_token` / `queqiao_server_name`。

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| 启用 RCON 连接 | 是否启用 MC 插件 | `true` |
| RCON 服务器地址 / 端口 / 密码 | 单服务器模式（servers 为空时生效） | `""` / `25575` / 必填 |
| RCON 超时时间 | 连接超时（秒） | `10` |
| 默认服务器名称 | 默认目标服务器 | 自动取第一个 |
| 仅管理员可执行指令 | 限制使用权限 | `true` |
| 多服务器配置 | JSON 数组，见下方示例 | `[]` |
| 启用自动备份 | 定时执行 save-all | `false` |
| 自动备份间隔 | 备份间隔（分钟） | `60` |
| 启用自动公告 | 定时发送公告 | `false` |
| 公告间隔 / 公告内容 | 循环发送的公告 | `30` / `[]` |
| 启用性能监控 | 允许查看 TPS/内存 | `false` |
| 启用 AI 自然语言控制 | 允许 /mc ai | `true` |
| 启用鹊桥 mod 对接 | 通过 WS 对接鹊桥实现双向桥接 | `false` |
| 鹊桥 WS 地址 / token / server_name | 鹊桥连接参数 | `""` / 空 / 空 |
| 桥接转发目标会话 | 见下方说明 | `""` |

### 多服务器配置示例

在 WebUI 的 `多服务器配置` 字段中填写 JSON 数组：

```json
[
  {"name": "survival", "host": "127.0.0.1", "port": 25575, "password": "密码1"},
  {"name": "creative", "host": "192.168.1.100", "port": 25575, "password": "密码2"}
]
```

### 桥接转发目标

`桥接转发目标会话` 使用 AstrBot 的 unified_msg_origin 格式：`平台ID:消息类型:会话ID`。

- QQ 群示例：`aiocqhttp:group:123456`
- 也可以留空：先执行一次 `/mc bridge on`，插件会把执行该指令的会话记为转发目标。

## 使用

| 指令 | 说明 |
|------|------|
| `/mc help` | 查看完整帮助 |
| `/mc status` | 查看服务器状态 |
| `/mc players` | 查看在线玩家 |
| `/mc servers` | 列出所有服务器 |
| `/mc use survival` | 切换到 survival 服务器 |
| `/mc monitor` | 查看性能监控 |
| `/mc whitelist add Xaunli` | 添加白名单 |
| `/mc ban Xaunli 违规` | 封禁玩家 |
| `/mc kick Xaunli 请文明游戏` | 踢出玩家 |
| `/mc op Xaunli` | 给予 OP |
| `/mc gamemode creative Xaunli` | 切换创造模式 |
| `/mc time set day` | 设置白天 |
| `/mc weather clear 600` | 晴天 10 分钟 |
| `/mc give Xaunli diamond 64` | 给 64 个钻石 |
| `/mc tp Xaunli 100 64 100` | 传送到坐标 |
| `/mc announce 服务器将在 10 分钟后维护` | 全服公告 |
| `/mc backup` | 手动备份 |
| `/mc batch give diamond 1` | 给所有在线玩家 1 个钻石 |
| `/mc ai 把所有在线玩家传送到出生点` | AI 执行指令 |
| `/mc cmd <指令>` | 执行任意指令 |
| `/mc silent 2h` | 静默 2 小时 |
| `/mc silent until 23:00` | 静默到 23:00 |
| `/mc silent schedule 21:00-08:00` | 每晚 21:00 至次日 08:00 静默 |
| `/mc silent off` | 立即结束静默并回放缓存 |
| `/mc bridge on` | 开启 MC↔QQ 聊天桥接 |
| `/mc bridge off` | 关闭聊天桥接 |
| `/mc bridge status` | 查看桥接状态 |

## 依赖

- `aiohttp`（图表路由与鹊桥 WebSocket 依赖）
- > 说明：本插件**不再依赖**第三方 RCON 库 mcrcon，内置了基于 `socket.settimeout` 的线程安全 RCON 客户端，避免 mcrcon 基于 SIGALRM 在主线程之外执行时的崩溃问题。

## 注意事项

- RCON 密码明文存储于 AstrBot 配置中，请确保网络环境安全。
- `/mc stop` 会直接停止服务器，请谨慎使用。
- AI 指令执行依赖 LLM 提供商，请确保已配置可用的聊天模型。
- 多服务器模式下，未指定 `use` 时默认使用 `default_server` 配置的服务器。
- 鹊桥桥接需要 MC 服务端安装鹊桥 mod 并开启 WebSocket Server，且插件配置需与之匹配。

## License

AGPL-3.0
