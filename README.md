# AstrBot Minecraft Server Manager

管理本地 Minecraft Forge 服务端的 AstrBot 插件。Discord 平台优先使用原生 Embed/Button 面板，非 Discord 平台自动降级为纯文本。

## 初始化使用说明

1. 将插件安装到 AstrBot 插件目录并重载/重启 AstrBot。
2. 在 Discord 中输入 `/mc` 或 `/mcserver` 打开 Minecraft 管理面板。
3. 如需启用游戏内 AI 聊天桥，先启用 RCON，并安装下文的 KubeJS 桥接脚本，然后在目标 Discord 频道执行 `/mc bindchat`。

默认管理的本地服务：

- systemd service: `minecraft-forge-1.20.1.service`
- server dir: `/root/minecraft/forge-1.20.1-server`
- world dir: `/root/minecraft/forge-1.20.1-server/world`
- port: `39498`

## 面板与指令

- `/mc`、`/mcserver`：打开管理面板。
- `/mc status`：显示 systemd 状态、PID、内存、端口监听、在线人数尽力解析、路径、最近 Done/ERROR/WARN。
- `/mc start` / `/mc stop` / `/mc restart`：管理 systemd 服务。
- `/mc logs [n=40]`：显示最近日志，受 `max_log_lines` 和 `max_log_chars` 限制。
- `/mc cmd <command>`：通过 RCON 执行命令。
- `/mc path`：显示 service/server/world/log/port 配置。
- `/mc bindchat`：仅 Discord；管理员或 `allow_user_ids` 在当前频道绑定游戏内 AI 聊天桥。
- `/mc bindstatus`：查看聊天桥绑定状态。
- `/mc unbindchat`：仅 Discord；管理员或 `allow_user_ids` 解绑聊天桥。

权限：`allow_user_ids` 可额外允许执行管理操作；`public_status=false` 时 status/logs/path 也只允许管理员或 allow_user_ids。

## RCON 说明

`/mc cmd`、LLM RCON Tool、游戏内聊天桥都只使用标准库实现的 Minecraft RCON 协议，不写 systemd stdin。启用前请在 Minecraft `server.properties` 中设置：

```properties
enable-rcon=true
rcon.port=25575
rcon.password=your-strong-password
```

插件配置：

- `rcon_enabled=true`
- `rcon_host=127.0.0.1`
- `rcon_port=25575`
- `rcon_password=your-strong-password`

默认 `deny_commands` 会拒绝 `stop/restart/op/deop/whitelist/pardon/ban/ban-ip/kick/save-off/rm` 等危险命令。

## 游戏内 AI 聊天桥（RCON Poll + KubeJS）

Vanilla Minecraft RCON **不能读取聊天历史**，因此本插件不再 tail 服务端日志，也不臆造不存在的 vanilla 命令。稳定方案是：服务端用 KubeJS 捕获玩家聊天写入内存队列；AstrBot 插件定时通过 RCON 执行 `mcai_bridge poll` 读取队列，处理后用 `mcai_bridge ack` 确认消费，避免重复。

### 安装服务端脚本

仓库内提供脚本：

```text
kubejs/server_scripts/mcai_rcon_bridge.js
```

复制到服务端目录：

```bash
cp kubejs/server_scripts/mcai_rcon_bridge.js /root/minecraft/forge-1.20.1-server/kubejs/server_scripts/mcai_rcon_bridge.js
```

确保 Forge 服务端已安装 KubeJS（适配 1.20.1 的 KubeJS 6），然后重启服务端；或在确认 KubeJS 已加载后执行 `/reload`。可用 RCON 或控制台验证：

```text
mcai_bridge size
mcai_bridge poll 20
mcai_bridge ack 1,2,3
```

`poll` 输出格式为 `MCAI_QUEUE_V1`，每行包含 `id<TAB>base64(player)<TAB>base64(message)<TAB>timestamp`；插件侧会解析并 ACK。

脚本注册 `/ai <内容>` 命令并写入队列；不再监听 `!ai` 聊天前缀。

### 插件侧配置

关键配置：

- `enable_chat_bridge=true`：启用 RCON 游戏内聊天桥（默认关闭）。
- `chat_require_discord_binding=true`：默认强制绑定 Discord 频道；未绑定时命中前缀不会进入 LLM，会尝试在 MC 内提示管理员先绑定。
- `chat_prefix`/`chat_prefixes`：仅用于兼容旧队列消息；新入口是 KubeJS `/ai <内容>` 命令。
- `chat_poll_interval=2`、`chat_poll_limit=20`：RCON poll 频率和批量。
- `chat_allowed_players=[]`、`chat_blocked_players=[]`：玩家 allow/block。
- `chat_message_max_chars`、`chat_player_cooldown_seconds=0`、`chat_global_cooldown_seconds=0`、`chat_dedupe_ttl_seconds`：裁剪、限流（默认关闭，方便连续对话）、去重。
- `chat_reply_max_chars`、`chat_reply_chunk_chars`：回 MC 的 `tellraw @a` 输出裁剪与分块；失败时降级为 `say`。
- `chat_discord_sync=true`：LLM 回复同步发送到绑定 Discord 频道（Embed，含玩家、问题、回复、event_id）。

使用流程：

1. 安装 KubeJS 脚本并启用 RCON。
2. 在插件配置中设置 `enable_chat_bridge=true`。
3. 在 Discord 目标频道执行 `/mc bindchat`。
4. OP 玩家在游戏内执行 `/ai 你的问题`。
5. 插件通过 RCON poll 到消息后构造 synthetic event 注入 AstrBot；最终回复会双路输出到 Minecraft 和绑定 Discord 频道。

## LLM RCON Tool

插件提供 `@filter.llm_tool`：`minecraft_rcon_command`，但默认隐藏/关闭。

配置：

- `enable_llm_tool=false`：默认关闭。
- `llm_tool_allowed_in_mc_chat=false`：默认禁止 Minecraft synthetic event 使用该 tool。
- `allowed_rcon_for_tool=[]`：可选收窄 allowlist；留空表示只按 `deny_commands` 黑名单拦截。
- `rcon_tool_timeout_seconds=8`
- `rcon_tool_output_max_chars=2000`

工具可见性会在 `@filter.on_llm_request()` 中动态控制：未启用、无权限、RCON 未启用、或 MC synthetic event 被禁止时移除 tool。工具调用时仍会二次检查权限、RCON、deny_commands 黑名单、可选 allowlist、超时与输出裁剪。
