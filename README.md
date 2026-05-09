# AstrBot Minecraft Server Manager

管理本地 Minecraft Forge 服务端的 AstrBot 插件。Discord 平台使用 **discord.py 原生 `discord.Embed` + `discord.ui.View/Button`** 打开交互式管理面板；非 Discord 平台自动降级为纯文本卡片和指令入口。

## 初始化使用说明

1. 将插件安装到 AstrBot 插件目录并重载/重启 AstrBot。
2. 在 Discord 中输入 `/mc` 或 `/mcserver` 打开 Minecraft 管理面板。
   - 面板会显示当前 systemd 状态、PID、内存、端口监听、在线人数尽力解析、目录和最近 Done/ERROR/WARN。
   - 面板按钮包括：刷新状态、启动、停止、重启、查看日志、路径。
3. 默认管理的本地服务：
   - systemd service: `minecraft-forge-1.20.1.service`
   - server dir: `/root/minecraft/forge-1.20.1-server`
   - world dir: `/root/minecraft/forge-1.20.1-server/world`
   - port: `39498`
4. 权限配置：
   - `allow_user_ids`: 额外允许执行启动/停止/重启/命令的 Discord/AstrBot 用户 ID 列表。
   - `public_status=true`: 所有人可看 `/mc` 面板、`status`、`logs`、`path`。
   - `public_status=false`: 面板/status/logs/path 也只允许 AstrBot 管理员或 `allow_user_ids` 用户查看。
5. 如果 systemd 服务或 Minecraft 目录迁移/改名，请修改：
   - `service_name`
   - `server_dir`
   - `world_dir`
   - `log_file`
   - `fallback_log_file`
   - `port`

## 面板与指令

无参入口：

- `/mc`
- `/mcserver`

子命令仍保留：

- `/mc status`：显示 systemd 状态、PID、内存、端口监听、在线人数尽力解析、路径、最近 Done/ERROR/WARN。
- `/mc start`：执行 `systemctl start`。
- `/mc stop`：执行 `systemctl stop`。
- `/mc restart`：执行 `systemctl restart`。
- `/mc logs [n=40]`：显示最近日志，受 `max_log_lines` 和 `max_log_chars` 限制。
- `/mc cmd <command>`：通过 RCON 执行命令。不会写 systemd stdin；未启用 RCON 时会明确提示配置。
- `/mc path`：显示 service/server/world/log/port 配置。

## RCON 说明

`/mc cmd` 只使用标准库实现 Minecraft RCON 协议，不引入外部依赖。启用前请在 Minecraft `server.properties` 中设置：

```properties
enable-rcon=true
rcon.port=25575
rcon.password=your-strong-password
```

然后在插件配置中设置：

- `rcon_enabled=true`
- `rcon_host=127.0.0.1`
- `rcon_port=25575`
- `rcon_password=your-strong-password`

默认 `deny_commands` 会拒绝 `stop/restart/op/deop/whitelist/pardon/ban/ban-ip/kick/save-off/rm` 等危险命令。
