"""AstrBot plugin: manage a local Minecraft Forge server."""

from __future__ import annotations

import asyncio
import re
import shlex
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_mc_server_manager"
PLUGIN_VERSION = "1.0.0"

BRAND = 0x57F287
OK = 0x2ECC71
WARN = 0xF1C40F
ERR = 0xE74C3C
INFO = 0x5865F2

DEFAULTS: dict[str, Any] = {
    "service_name": "minecraft-forge-1.20.1.service",
    "server_dir": "/root/minecraft/forge-1.20.1-server",
    "world_dir": "/root/minecraft/forge-1.20.1-server/world",
    "log_file": "/root/minecraft/forge-1.20.1-server/server-systemd.log",
    "fallback_log_file": "/root/minecraft/forge-1.20.1-server/server-console.log",
    "port": 39498,
    "allow_user_ids": [],
    "public_status": True,
    "max_log_lines": 200,
    "max_log_chars": 6000,
    "embed_enabled": True,
    "rcon_enabled": False,
    "rcon_host": "127.0.0.1",
    "rcon_port": 25575,
    "rcon_password": "",
    "deny_commands": [
        "stop",
        "restart",
        "op",
        "deop",
        "whitelist",
        "pardon",
        "ban",
        "ban-ip",
        "kick",
        "save-off",
        "rm",
    ],
}


class RconError(RuntimeError):
    """Raised when an RCON request fails."""


def _clip(text: Any, limit: int, suffix: str = "…") -> str:
    value = "" if text is None else str(text)
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))] + suffix


def _human_bytes(value: str | int | None) -> str:
    try:
        num = int(value or 0)
    except (TypeError, ValueError):
        return "未知"
    if num <= 0:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(num)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.1f} {unit}"


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()]


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "是", "开启"}


def _as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


@register(
    PLUGIN_NAME,
    "Xia",
    "管理本地 Minecraft Forge 服务端（Discord Embed 优先，文本安全降级）",
    PLUGIN_VERSION,
    "",
)
class AstrbotPluginMcServerManager(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._op_lock = asyncio.Lock()

    # ──────────────────────────────── Config ────────────────────────────────

    def _cfg(self, key: str) -> Any:
        default = DEFAULTS[key]
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        if value is None:
            return default
        return value

    @property
    def service_name(self) -> str:
        return str(self._cfg("service_name") or DEFAULTS["service_name"]).strip()

    @property
    def server_dir(self) -> str:
        return str(self._cfg("server_dir") or DEFAULTS["server_dir"]).strip()

    @property
    def world_dir(self) -> str:
        return str(self._cfg("world_dir") or DEFAULTS["world_dir"]).strip()

    @property
    def log_file(self) -> str:
        return str(self._cfg("log_file") or DEFAULTS["log_file"]).strip()

    @property
    def fallback_log_file(self) -> str:
        return str(self._cfg("fallback_log_file") or DEFAULTS["fallback_log_file"]).strip()

    @property
    def port(self) -> int:
        return _as_int(self._cfg("port"), int(DEFAULTS["port"]), 1, 65535)

    @property
    def max_log_lines(self) -> int:
        return _as_int(self._cfg("max_log_lines"), int(DEFAULTS["max_log_lines"]), 1, 1000)

    @property
    def max_log_chars(self) -> int:
        return _as_int(self._cfg("max_log_chars"), int(DEFAULTS["max_log_chars"]), 500, 20000)

    @property
    def embed_enabled(self) -> bool:
        return _as_bool(self._cfg("embed_enabled"), True)

    @property
    def public_status(self) -> bool:
        return _as_bool(self._cfg("public_status"), True)

    @property
    def allow_user_ids(self) -> list[str]:
        return _normalize_list(self._cfg("allow_user_ids"))

    @property
    def deny_commands(self) -> list[str]:
        configured = _normalize_list(self._cfg("deny_commands"))
        return [item.lower().lstrip("/").strip() for item in configured if item.strip()]

    # ─────────────────────────────── Permissions ─────────────────────────────

    def _is_astrbot_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            astrbot_config = self.context.get_config(event.unified_msg_origin)
            admins = [str(x) for x in astrbot_config.get("admins_id", [])]
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        return self._is_astrbot_admin(event) or sender in self.allow_user_ids

    def _can_view(self, event: AstrMessageEvent) -> bool:
        return self.public_status or self._can_manage(event)

    def _is_astrbot_admin_id(self, event: AstrMessageEvent, user_id: str | int) -> bool:
        try:
            astrbot_config = self.context.get_config(event.unified_msg_origin)
            admins = [str(x) for x in astrbot_config.get("admins_id", [])]
            return str(user_id) in admins
        except Exception:
            return False

    def _can_manage_id(self, event: AstrMessageEvent, user_id: str | int) -> bool:
        return self._is_astrbot_admin_id(event, user_id) or str(user_id) in self.allow_user_ids

    def _can_view_id(self, event: AstrMessageEvent, user_id: str | int) -> bool:
        return self.public_status or self._can_manage_id(event, user_id)

    # ─────────────────────────────── Rendering ───────────────────────────────

    @staticmethod
    def _is_discord_event(event: AstrMessageEvent) -> bool:
        try:
            return event.get_platform_name() == "discord"
        except Exception:
            return False

    def _discord_embed_component(
        self,
        event: AstrMessageEvent,
        title: str,
        description: str = "",
        color: int = BRAND,
        fields: list[dict[str, Any]] | None = None,
        footer: str | None = None,
    ):
        # Do not use AstrBot's built-in Discord component wrappers here.
        # They are intentionally bypassed because this plugin uses native
        # discord.py Embed/View/Button for Discord and plain text elsewhere.
        return None

    def _text_card(self, title: str, description: str = "", fields: list[dict[str, Any]] | None = None) -> str:
        parts = [f"【{title}】"]
        if description:
            parts.append(str(description))
        for field in fields or []:
            name = str(field.get("name", "")).strip() or "信息"
            value = str(field.get("value", "")).strip() or "-"
            parts.append(f"\n{name}\n{value}")
        return "\n".join(parts)

    def _card_result(
        self,
        event: AstrMessageEvent,
        title: str,
        description: str = "",
        color: int = BRAND,
        fields: list[dict[str, Any]] | None = None,
        footer: str | None = None,
    ):
        embed = self._discord_embed_component(event, title, description, color, fields, footer)
        if embed is not None:
            return event.chain_result([embed]).stop_event()
        return event.plain_result(_clip(self._text_card(title, description, fields), self.max_log_chars)).stop_event()

    async def _send_native_card(
        self,
        event: AstrMessageEvent,
        title: str,
        description: str = "",
        color: int = BRAND,
        fields: list[dict[str, Any]] | None = None,
        *,
        view: Any = None,
        footer: str | None = None,
        ephemeral: bool = False,
    ) -> bool:
        if not self.embed_enabled or not self._is_discord_event(event):
            return False
        try:
            embed = self._native_embed(title, description, color, fields, footer)
            webhook = getattr(event, "interaction_followup_webhook", None)
            if webhook is not None:
                await webhook.send(embeds=[embed], view=view, wait=True, ephemeral=ephemeral)
            else:
                channel = None
                get_channel = getattr(event, "_get_channel", None)
                if callable(get_channel):
                    channel = await get_channel()
                if channel is None:
                    return False
                await channel.send(embed=embed, view=view)
            event.stop_event()
            return True
        except Exception as exc:
            logger.warning("Discord 原生 Embed/组件发送失败，降级为文本：%s", exc, exc_info=True)
            return False

    async def _rich_card_result(
        self,
        event: AstrMessageEvent,
        title: str,
        description: str = "",
        color: int = BRAND,
        fields: list[dict[str, Any]] | None = None,
        footer: str | None = None,
    ):
        if await self._send_native_card(event, title, description, color, fields, footer=footer):
            return None
        return self._card_result(event, title, description, color, fields, footer)

    def _deny_result(self, event: AstrMessageEvent):
        return self._card_result(
            event,
            "Minecraft 服务端管理",
            "⚠️ 无权限：该操作仅允许 AstrBot 管理员或配置 allow_user_ids 中的用户执行。",
            ERR,
        )

    def _native_embed(
        self,
        title: str,
        description: str = "",
        color: int = BRAND,
        fields: list[dict[str, Any]] | None = None,
        footer: str | None = None,
    ):
        import discord

        embed = discord.Embed(
            title=_clip(title, 256),
            description=_clip(description, 3900),
            color=color,
        )
        embed.set_footer(text=_clip(footer or "AstrBot MC Server Manager", 2048))
        for field in fields or []:
            embed.add_field(
                name=_clip(field.get("name", "") or " ", 256),
                value=_clip(field.get("value", "") or " ", 1024),
                inline=bool(field.get("inline", False)),
            )
        return embed

    async def _panel_payload(self) -> tuple[str, list[dict[str, Any]], int]:
        desc, fields, color = await self._status_payload()
        panel_fields = list(fields)
        panel_fields.append(
            {
                "name": "面板交互",
                "value": (
                    "优先使用下方 Discord 按钮：刷新状态、启动、停止、重启、查看日志、路径。\n"
                    "组件 payload: `mcsm:refresh`, `mcsm:start`, `mcsm:stop`, "
                    "`mcsm:restart`, `mcsm:logs`, `mcsm:path`"
                ),
                "inline": False,
            }
        )
        return desc, panel_fields, color

    async def _panel_card_result(self, event: AstrMessageEvent):
        if not self._can_view(event):
            return self._deny_result(event)

        desc, fields, color = await self._panel_payload()
        native_view = self._discord_native_panel_view(event)
        if await self._send_native_card(
            event,
            "Minecraft Forge 管理面板",
            desc,
            color,
            fields,
            view=native_view,
            ephemeral=getattr(event, "interaction_followup_webhook", None) is not None,
        ):
            return None

        fallback = (
            "当前平台不支持 Discord 原生按钮，已降级为文本交互入口。\n"
            "可继续使用：`/mc status`、`/mc start`、`/mc stop`、"
            "`/mc restart`、`/mc logs 40`、`/mc path`。"
        )
        fields = list(fields) + [{"name": "降级交互入口", "value": fallback, "inline": False}]
        return self._card_result(event, "Minecraft Forge 管理面板", desc, color, fields)

    def _discord_native_panel_view(self, event: AstrMessageEvent):
        try:
            import discord
        except Exception as exc:
            logger.debug("Discord native View 不可用：%s", exc)
            return None

        plugin = self

        class McServerPanelView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=600)

            async def _send_ephemeral(self, interaction, text: str) -> None:
                content = _clip(text, 1900)
                if interaction.response.is_done():
                    await interaction.followup.send(content=content, ephemeral=True)
                else:
                    await interaction.response.send_message(content=content, ephemeral=True)

            async def _edit(self, interaction, title: str, desc: str, color: int, fields: list[dict[str, Any]]) -> None:
                embed = plugin._native_embed(title, desc, color, fields)
                view = plugin._discord_native_panel_view(event)
                if interaction.response.is_done():
                    await interaction.edit_original_response(embed=embed, view=view)
                else:
                    await interaction.response.edit_message(embed=embed, view=view)

            async def _refresh_panel(self, interaction, note: str = "") -> None:
                desc, fields, color = await plugin._panel_payload()
                if note:
                    desc = f"{note}\n\n{desc}"
                await self._edit(interaction, "Minecraft Forge 管理面板", desc, color, fields)

            async def _show_logs(self, interaction) -> None:
                desc, fields, color = await plugin._logs_payload(40)
                await self._edit(interaction, "Minecraft 服务端日志", desc, color, fields)

            async def _show_paths(self, interaction) -> None:
                desc, fields, color = plugin._paths_payload()
                await self._edit(interaction, "Minecraft 服务端路径", desc, color, fields)

            def _interaction_user_id(self, interaction) -> str:
                return str(getattr(getattr(interaction, "user", None), "id", ""))

            async def _require_view(self, interaction) -> bool:
                user_id = self._interaction_user_id(interaction)
                if plugin._can_view_id(event, user_id):
                    return True
                await self._send_ephemeral(
                    interaction,
                    "⚠️ 无权限：当前 public_status=false，只有 AstrBot 管理员或 allow_user_ids 用户可查看面板。",
                )
                return False

            async def _require_manage(self, interaction) -> bool:
                user_id = self._interaction_user_id(interaction)
                if plugin._can_manage_id(event, user_id):
                    return True
                await self._send_ephemeral(
                    interaction,
                    "⚠️ 无权限：启动/停止/重启仅允许 AstrBot 管理员或 allow_user_ids 用户执行。",
                )
                return False

            @discord.ui.button(
                label="刷新状态",
                style=discord.ButtonStyle.secondary,
                emoji="🔄",
                custom_id="mcsm:refresh",
            )
            async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._require_view(interaction):
                    return
                await interaction.response.defer()
                await self._refresh_panel(interaction)

            @discord.ui.button(
                label="启动",
                style=discord.ButtonStyle.success,
                emoji="▶️",
                custom_id="mcsm:start",
            )
            async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._require_manage(interaction):
                    return
                await interaction.response.defer()
                desc, fields, color = await plugin._action_payload("start")
                await self._edit(interaction, "Minecraft 服务端启动", desc, color, fields)

            @discord.ui.button(
                label="停止",
                style=discord.ButtonStyle.danger,
                emoji="⏹️",
                custom_id="mcsm:stop",
            )
            async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._require_manage(interaction):
                    return
                await interaction.response.defer()
                desc, fields, color = await plugin._action_payload("stop")
                await self._edit(interaction, "Minecraft 服务端停止", desc, color, fields)

            @discord.ui.button(
                label="重启",
                style=discord.ButtonStyle.primary,
                emoji="🔁",
                custom_id="mcsm:restart",
            )
            async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._require_manage(interaction):
                    return
                await interaction.response.defer()
                desc, fields, color = await plugin._action_payload("restart")
                await self._edit(interaction, "Minecraft 服务端重启", desc, color, fields)

            @discord.ui.button(
                label="查看日志",
                style=discord.ButtonStyle.secondary,
                emoji="📜",
                custom_id="mcsm:logs",
            )
            async def logs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._require_view(interaction):
                    return
                await interaction.response.defer()
                await self._show_logs(interaction)

            @discord.ui.button(
                label="路径",
                style=discord.ButtonStyle.secondary,
                emoji="📁",
                custom_id="mcsm:path",
            )
            async def path_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._require_view(interaction):
                    return
                await interaction.response.defer()
                await self._show_paths(interaction)

            async def on_error(self, error: Exception, item, interaction) -> None:
                logger.error("MC Server Discord panel callback failed: %s", error, exc_info=True)
                await self._send_ephemeral(interaction, f"⚠️ 面板操作失败：{type(error).__name__}: {error}")

        try:
            return McServerPanelView()
        except Exception as exc:
            logger.debug("Discord native View 创建失败：%s", exc)
            return None

    # ───────────────────────────── Subprocess I/O ────────────────────────────

    async def _run_exec(self, *args: str, timeout: float = 20) -> tuple[int, str, str]:
        """Run a command without shell=True and return rc/stdout/stderr."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return 127, "", str(exc)
        except Exception as exc:
            return 1, "", f"{type(exc).__name__}: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            return 124, "", f"命令超时（>{timeout:.0f}s）"

        return (
            int(proc.returncode or 0),
            stdout.decode("utf-8", "replace").strip(),
            stderr.decode("utf-8", "replace").strip(),
        )

    async def _systemctl(self, action: str) -> tuple[int, str, str]:
        return await self._run_exec("systemctl", action, self.service_name, timeout=90)

    async def _systemd_show(self) -> dict[str, str]:
        props = [
            "Id",
            "Names",
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "MainPID",
            "ControlPID",
            "MemoryCurrent",
            "ExecMainStatus",
            "ExecMainCode",
            "FragmentPath",
            "Description",
            "NRestarts",
        ]
        rc, out, err = await self._run_exec(
            "systemctl",
            "show",
            self.service_name,
            "--no-page",
            *(f"--property={prop}" for prop in props),
            timeout=15,
        )
        data: dict[str, str] = {"_rc": str(rc)}
        if err:
            data["_error"] = err
        for line in out.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value
        return data

    async def _port_status(self) -> str:
        port = str(self.port)
        rc, out, err = await self._run_exec("ss", "-H", "-ltn", "sport", "=", f":{port}", timeout=5)
        if rc == 0 and out.strip():
            return "LISTEN（ss 检测到监听）"
        if rc not in {0, 1} and err:
            logger.debug("ss 检查端口失败，改用 TCP 探测：%s", err)
        try:
            conn = asyncio.open_connection("127.0.0.1", self.port)
            reader, writer = await asyncio.wait_for(conn, timeout=2)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            # keep references alive until close completes
            _ = reader
            return "可连接（127.0.0.1 TCP 探测成功）"
        except Exception:
            return "未监听或不可连接"

    # ─────────────────────────────── Log Helpers ─────────────────────────────

    def _existing_log_path(self) -> str:
        primary = Path(self.log_file)
        if primary.exists():
            return str(primary)
        fallback = Path(self.fallback_log_file)
        if fallback.exists():
            return str(fallback)
        return str(primary)

    @staticmethod
    def _read_last_lines_sync(path: str, limit: int) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return [line.rstrip("\n") for line in deque(handle, maxlen=max(1, limit))]
        except FileNotFoundError:
            return []
        except OSError as exc:
            return [f"读取日志失败：{type(exc).__name__}: {exc}"]

    async def _read_last_lines(self, path: str, limit: int) -> list[str]:
        return await asyncio.to_thread(self._read_last_lines_sync, path, limit)

    def _clip_lines_to_chars(self, lines: list[str], limit: int) -> str:
        text = "\n".join(_strip_ansi(line) for line in lines)
        if len(text) <= limit:
            return text
        return "…\n" + text[-max(0, limit - 2) :]

    async def _recent_log_info(self) -> tuple[str, list[str], str]:
        path = self._existing_log_path()
        lines = await self._read_last_lines(path, max(self.max_log_lines, 300))
        interesting = [line for line in lines if re.search(r"\b(Done|ERROR|WARN)\b", line, re.I)]
        return path, interesting[-8:], self._parse_online_from_logs(lines)

    async def _read_server_properties(self) -> dict[str, str]:
        path = Path(self.server_dir) / "server.properties"

        def load() -> dict[str, str]:
            props: dict[str, str] = {}
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    for raw in handle:
                        line = raw.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        props[key.strip()] = value.strip()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug("读取 server.properties 失败：%s", exc)
            return props

        return await asyncio.to_thread(load)

    def _parse_online_from_logs(self, lines: list[str]) -> str:
        max_players = "?"
        # Prefer exact output produced by the Minecraft `list` command.
        list_pattern = re.compile(r"There are\s+(\d+)\s+of\s+a\s+max\s+of\s+(\d+)\s+players online", re.I)
        for line in reversed(lines):
            match = list_pattern.search(line)
            if match:
                return f"{match.group(1)}/{match.group(2)}（来自最近 list 输出）"

        # Best-effort reconstruction from join/leave messages after the latest boot marker.
        start_index = 0
        boot_pattern = re.compile(r"(Done \([^)]+\)!|Starting minecraft server|Loading properties|Stopping server)", re.I)
        for idx, line in enumerate(lines):
            if boot_pattern.search(line):
                start_index = idx
        players: set[str] = set()
        join_re = re.compile(r"\]:\s+([^\s\[]+) joined the game", re.I)
        left_re = re.compile(r"\]:\s+([^\s\[]+) left the game", re.I)
        lost_re = re.compile(r"\]:\s+([^\s\[]+) lost connection", re.I)
        for line in lines[start_index:]:
            if match := join_re.search(line):
                players.add(match.group(1))
            if match := left_re.search(line):
                players.discard(match.group(1))
            if match := lost_re.search(line):
                players.discard(match.group(1))
        if players:
            shown = ", ".join(sorted(players)[:8])
            more = "…" if len(players) > 8 else ""
            return f"约 {len(players)}/{max_players}（日志估算：{shown}{more}）"
        return "未知（未发现最近 list 或 join/leave 信息）"

    # ─────────────────────────────── RCON ────────────────────────────────────

    async def _rcon_exchange(self, command: str) -> str:
        host = str(self._cfg("rcon_host") or "127.0.0.1").strip()
        port = _as_int(self._cfg("rcon_port"), int(DEFAULTS["rcon_port"]), 1, 65535)
        password = str(self._cfg("rcon_password") or "")
        if not password:
            raise RconError("RCON 已启用但未配置 rcon_password。")

        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        request_id = int(time.time()) & 0x7FFFFFFF

        async def send_packet(packet_id: int, packet_type: int, payload: str) -> None:
            assert writer is not None
            data = payload.encode("utf-8")
            body = struct.pack("<ii", packet_id, packet_type) + data + b"\x00\x00"
            writer.write(struct.pack("<i", len(body)) + body)
            await writer.drain()

        async def read_packet() -> tuple[int, int, str]:
            assert reader is not None
            raw_len = await asyncio.wait_for(reader.readexactly(4), timeout=8)
            (length,) = struct.unpack("<i", raw_len)
            if length < 10 or length > 4_196_000:
                raise RconError(f"RCON 响应长度异常：{length}")
            body = await asyncio.wait_for(reader.readexactly(length), timeout=8)
            packet_id, packet_type = struct.unpack("<ii", body[:8])
            payload = body[8:-2].decode("utf-8", "replace")
            return packet_id, packet_type, payload

        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=8)
            await send_packet(request_id, 3, password)
            auth_id, _auth_type, _auth_payload = await read_packet()
            if auth_id == -1:
                raise RconError("RCON 认证失败，请检查 rcon_password。")
            await send_packet(request_id + 1, 2, command)
            _pid, _ptype, payload = await read_packet()
            return payload.strip() or "（RCON 没有返回内容）"
        except asyncio.TimeoutError as exc:
            raise RconError("RCON 连接或响应超时。") from exc
        except (OSError, EOFError, struct.error) as exc:
            raise RconError(f"RCON 请求失败：{type(exc).__name__}: {exc}") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    def _is_denied_rcon_command(self, command: str) -> tuple[bool, str]:
        text = command.strip().lstrip("/")
        if not text:
            return True, "空命令"
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        first = (parts[0] if parts else text).lower().lstrip("/")
        first_plain = first.split(":", 1)[-1]
        deny = set(self.deny_commands)
        if first in deny or first_plain in deny:
            return True, first
        lower = text.lower()
        for denied in deny:
            if lower == denied or lower.startswith(denied + " ") or lower.startswith("minecraft:" + denied):
                return True, denied
        return False, ""

    # ───────────────────────────── Command Payloads ──────────────────────────

    def _extract_subcommand_args(self, event: AstrMessageEvent, subcommand: str) -> str:
        text = re.sub(r"\s+", " ", event.get_message_str().strip())
        aliases = ("mcserver", "mc")
        pattern = re.compile(rf"^(?:{'|'.join(re.escape(x) for x in aliases)})\s+{re.escape(subcommand)}(?:\s+|$)", re.I)
        match = pattern.match(text)
        if not match:
            return ""
        return text[match.end() :].strip()

    async def _status_payload(self) -> tuple[str, list[dict[str, Any]], int]:
        show_task = asyncio.create_task(self._systemd_show())
        port_task = asyncio.create_task(self._port_status())
        log_task = asyncio.create_task(self._recent_log_info())
        props_task = asyncio.create_task(self._read_server_properties())

        show = await show_task
        port_status = await port_task
        log_path, interesting, online_from_log = await log_task
        props = await props_task

        active = show.get("ActiveState", "unknown")
        sub = show.get("SubState", "unknown")
        pid = show.get("MainPID") or "0"
        memory = _human_bytes(show.get("MemoryCurrent"))
        max_players = props.get("max-players")
        online = online_from_log
        if max_players and online.startswith("未知"):
            online = f"未知/{max_players}（未发现最近在线日志）"
        color = OK if active == "active" else WARN if active in {"activating", "deactivating"} else ERR

        desc = f"服务 `{self.service_name}`：**{active} / {sub}**"
        fields = [
            {"name": "进程与资源", "value": f"PID: `{pid}`\n内存: `{memory}`\n端口 {self.port}: {port_status}", "inline": True},
            {"name": "在线人数", "value": online, "inline": True},
            {
                "name": "目录",
                "value": f"Server: `{self.server_dir}`\nWorld: `{self.world_dir}`\nLog: `{log_path}`",
                "inline": False,
            },
        ]
        if show.get("_error"):
            fields.append({"name": "systemctl", "value": _clip(show["_error"], 1000), "inline": False})
        recent = "\n".join(interesting[-6:]) if interesting else "未在最近日志中发现 Done / ERROR / WARN。"
        fields.append({"name": "最近 Done / ERROR / WARN", "value": f"```text\n{_clip(_strip_ansi(recent), 980)}\n```", "inline": False})
        return desc, fields, color

    async def _action_payload(self, action: str) -> tuple[str, list[dict[str, Any]], int]:
        async with self._op_lock:
            rc, out, err = await self._systemctl(action)
        output = "\n".join(part for part in (out, err) if part).strip() or "systemctl 未返回输出。"
        desc = f"执行：`systemctl {action} {self.service_name}`\n退出码：`{rc}`"
        status_desc, status_fields, status_color = await self._status_payload()
        fields = [
            {"name": "执行输出", "value": f"```text\n{_clip(output, 980)}\n```", "inline": False},
            *status_fields[:3],
        ]
        color = OK if rc == 0 else ERR if rc != 0 else status_color
        if rc == 0 and action in {"stop", "restart"}:
            color = status_color
        return desc + "\n" + status_desc, fields, color

    async def _logs_payload(self, requested_lines: int) -> tuple[str, list[dict[str, Any]], int]:
        n = max(1, min(int(requested_lines), self.max_log_lines))
        path = self._existing_log_path()
        lines = await self._read_last_lines(path, n)
        if not lines:
            body = f"日志文件不存在或为空：{path}"
        else:
            body = self._clip_lines_to_chars(lines, self.max_log_chars)
        # Discord embed description is capped, so reserve room for code fences/path.
        embed_body = _clip(body, 3600)
        desc = f"来源：`{path}`\n行数：`{len(lines)}/{n}`\n```text\n{embed_body}\n```"
        fields: list[dict[str, Any]] = []
        return desc, fields, INFO

    def _paths_payload(self) -> tuple[str, list[dict[str, Any]], int]:
        desc = "当前 Minecraft Forge 服务端路径配置。"
        fields = [
            {"name": "systemd service", "value": f"`{self.service_name}`", "inline": False},
            {"name": "server_dir", "value": f"`{self.server_dir}`", "inline": False},
            {"name": "world_dir", "value": f"`{self.world_dir}`", "inline": False},
            {"name": "log_file", "value": f"`{self.log_file}`", "inline": False},
            {"name": "fallback_log_file", "value": f"`{self.fallback_log_file}`", "inline": False},
            {"name": "port", "value": f"`{self.port}`", "inline": True},
        ]
        return desc, fields, INFO

    # ─────────────────────────────── Commands ────────────────────────────────

    @staticmethod
    def _join_command_text(event: AstrMessageEvent, *args: Any, **kwargs: Any) -> str:
        """Build a stable command text from AstrBot text and Discord slash params."""
        base = re.sub(r"\s+", " ", (event.get_message_str() or "").strip())
        extras: list[str] = []
        for item in args:
            if item is not None:
                text = str(item).strip()
                if text:
                    extras.append(text)
        for key in ("params", "raw", "subcommand", "command"):
            value = kwargs.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    extras.append(text)
        # Discord native slash may expose `/mc` in message_str and the selected
        # subcommand/options separately. Prefer appending missing params instead
        # of trusting message_str alone.
        if extras:
            if not base:
                return " ".join(extras)
            tail = " ".join(extras)
            if tail and tail not in base:
                return f"{base} {tail}"
        return base

    async def _dispatch_root_result(self, event: AstrMessageEvent, *args: Any, **kwargs: Any):
        """Compatibility entry for Discord native /mc and /mcserver slash commands.

        AstrBot's current Discord adapter only auto-registers top-level commands as
        native slash commands, not command-group subcommands. The real command group
        remains below; this dispatcher makes `/mc status` and `/mcserver status`
        work from Discord's single string `params` option too.
        """
        text = self._join_command_text(event, *args, **kwargs)
        parts = text.split(" ", 2)
        sub = parts[1].lower() if len(parts) >= 2 else ""
        arg_text = parts[2].strip() if len(parts) >= 3 else ""

        if not sub or sub in {"help", "-h", "--help"}:
            return await self._panel_card_result(event)

        if sub == "status":
            if not self._can_view(event):
                return self._deny_result(event)
            desc, fields, color = await self._status_payload()
            return await self._rich_card_result(event, "Minecraft 服务端状态", desc, color, fields)

        if sub in {"start", "stop", "restart"}:
            if not self._can_manage(event):
                return self._deny_result(event)
            desc, fields, color = await self._action_payload(sub)
            titles = {
                "start": "Minecraft 服务端启动",
                "stop": "Minecraft 服务端停止",
                "restart": "Minecraft 服务端重启",
            }
            return await self._rich_card_result(event, titles[sub], desc, color, fields)

        if sub == "logs":
            if not self._can_view(event):
                return self._deny_result(event)
            n = 40
            if arg_text:
                try:
                    n = int(arg_text.split()[0])
                except (TypeError, ValueError):
                    n = 40
            desc, fields, color = await self._logs_payload(n)
            return await self._rich_card_result(event, "Minecraft 服务端日志", desc, color, fields)

        if sub == "path":
            if not self._can_view(event):
                return self._deny_result(event)
            desc, fields, color = self._paths_payload()
            return await self._rich_card_result(event, "Minecraft 服务端路径", desc, color, fields)

        if sub == "cmd":
            if not self._can_manage(event):
                return self._deny_result(event)
            command = arg_text
            if not command:
                return await self._rich_card_result(event, "Minecraft RCON", "用法：`/mc cmd <command>`", WARN)
            if not _as_bool(self._cfg("rcon_enabled"), False):
                return await self._rich_card_result(
                    event,
                    "Minecraft RCON 未启用",
                    "不会假装能写入 systemd stdin。请在 Minecraft server.properties 启用 RCON，并在插件配置中设置 rcon_enabled/rcon_host/rcon_port/rcon_password 后再使用 `/mc cmd`。",
                    WARN,
                )
            denied, token = self._is_denied_rcon_command(command)
            if denied:
                return await self._rich_card_result(
                    event,
                    "Minecraft RCON 已拦截",
                    f"命令 `{token}` 在 deny_commands 中，已拒绝执行。",
                    ERR,
                )
            try:
                output = await self._rcon_exchange(command)
            except RconError as exc:
                return await self._rich_card_result(event, "Minecraft RCON 失败", str(exc), ERR)
            desc = f"已执行 RCON：`/{_clip(command, 200)}`"
            fields = [{"name": "返回", "value": f"```text\n{_clip(output, 1800)}\n```", "inline": False}]
            return await self._rich_card_result(event, "Minecraft RCON", desc, OK, fields)

        return await self._rich_card_result(
            event,
            "Minecraft 服务端管理",
            f"未知子命令：`{_clip(sub, 60)}`\n可用：`status/start/stop/restart/logs/cmd/path`",
            WARN,
        )

    @filter.command("mc")
    async def mc_root_short(self, event: AstrMessageEvent, params: str = "", raw: str = ""):
        """Minecraft 服务端管理（别名入口，Discord slash 兼容）。"""
        result = await self._dispatch_root_result(event, params=params, raw=raw)
        if result is not None:
            yield result

    @filter.command("mcserver")
    async def mc_root(self, event: AstrMessageEvent, params: str = "", raw: str = ""):
        """Minecraft 服务端管理（Discord slash 兼容）。"""
        result = await self._dispatch_root_result(event, params=params, raw=raw)
        if result is not None:
            yield result

    async def terminate(self):
        pass
