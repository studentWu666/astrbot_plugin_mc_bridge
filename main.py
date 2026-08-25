"""
AstrBot Minecraft 插件（重写版）
通过 RCON 协议连接 Minecraft 服务器，提供状态查询、玩家管理、指令执行、
定时备份/公告、性能监控、AI 自然语言控制与 QQ 聊天桥接。

相对旧版的主要修复：
- 批量查询复用单个 RCON 连接，不再为每条指令各建一次 TCP
- 移除无效的原版指令（op list / memory），monitor 对非 Paper 端给出明确提示
- 玩家追踪循环改为可配置、默认关闭，且对不可达服务器自动退避，不再刷错误日志
- /mc bridge off 真正关闭 TCP 监听并释放端口
- list 输出解析重写，正确处理 0 人在线与各种服务端输出格式
- 所有用户输入拼进指令前过滤换行，防止一次注入多条指令

本轮修复：
- P0: 弃用 mcrcon 库。其构造/超时依赖 SIGALRM（仅主线程可用），在工作线程里
  100% 抛 ValueError，导致所有 RCON 功能失效。改为内置基于 socket.settimeout
  的线程安全 RCON 客户端
- P1: mc_rcon_command LLM 工具补充 event.is_admin() 校验，防止普通用户借 AI
  越权执行 stop/op 等危险指令
- P2: list 解析前先排除连接失败/空响应文本，防止错误信息被当成玩家名
- P3: 桥接 JSON 非 dict 容错；gamemode 必须指定玩家；help/servers/use/bridge
  不再被 RCON 开关拦截；weather 帮助改为 tick 单位；配置数值解析容错

v2.3.0：新增鹊桥（QueQiao）mod 对接
- WebSocket 客户端连接鹊桥（MC 端 WS Server），取代旧 TCP 行协议桥接
- MC→QQ：玩家聊天/加入/退出/死亡/成就事件转发到群
- QQ→MC：绑定群的普通消息通过 broadcast API 送进游戏
- 断线自动重连（指数退避，上限 60s）
"""

import asyncio
import json
import re
import socket
import struct
import time
import os
from datetime import datetime, time as _dtime, timedelta
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

DEFAULT_TIMEOUT = 10
_UNKNOWN_CMD_PATTERNS = (
    "unknown or incomplete command",
    "unknown command",
    "incorrect argument",
)

# ----------------------------------------------------------------------
# RCON 英文输出中文化（原版/Forge 服务端 RCON 响应只有英文，逐条硬翻
# 常见格式；mod 自定义输出不匹配时原样返回）
# ----------------------------------------------------------------------
_ZH_WEATHER = {"clear": "晴朗", "rain": "降雨", "rainy": "降雨", "thunder": "雷暴", "thundering": "雷暴"}
_ZH_DIFF = {"peaceful": "和平", "easy": "简单", "normal": "普通", "hard": "困难"}
_ZH_MODE = {"survival": "生存", "creative": "创造", "adventure": "冒险", "spectator": "旁观"}


def _zh_line(s: str) -> str:
    """把单行常见 RCON 英文输出翻译成中文，未匹配时原样返回。"""
    if not s:
        return s

    def sub(pattern: str):
        return re.fullmatch(pattern, s, re.IGNORECASE)

    m = sub(r"There are (\d+) of a max of (\d+) players online(?:: ?(.*))?")
    if m:
        return f"当前 {m.group(1)}/{m.group(2)} 人在线" + (f"：{m.group(3)}" if m.group(3) else "")
    m = sub(r"Seed: \[(-?\d+)\]")
    if m:
        return f"世界种子: [{m.group(1)}]"
    m = sub(r"The time is (\d+)")
    if m:
        return f"当前游戏时间: {m.group(1)} tick"
    m = sub(r"The weather is:? ?(clear|rainy?|thundering?)")
    if m:
        return f"当前天气: {_ZH_WEATHER.get(m.group(1).lower(), m.group(1))}"
    m = sub(r"(?:Changing to|Set the weather to) (clear|rainy?|thundering?) weather?")
    if m:
        return f"天气已切换为 {_ZH_WEATHER.get(m.group(1).lower(), m.group(1))}"
    m = sub(r"The difficulty is (\w+)")
    if m:
        return f"当前难度: {_ZH_DIFF.get(m.group(1).lower(), m.group(1))}"
    m = sub(r"Set the difficulty to (\w+)")
    if m:
        return f"难度已设为 {_ZH_DIFF.get(m.group(1).lower(), m.group(1))}"
    m = sub(r"This server is running version (.+?)(?: with (\d+) mods)?")
    if m:
        return f"服务端版本: {m.group(1)}" + (f"（已加载 {m.group(2)} 个 mod）" if m.group(2) else "")
    m = sub(r"There are (\d+) whitelisted players: ?(.*)")
    if m:
        return f"白名单共 {m.group(1)} 人：{m.group(2)}".rstrip("：")
    m = sub(r"There are no whitelisted players")
    if m:
        return "白名单为空"
    m = sub(r"There are (\d+) banned (?:players|IPs): ?(.*)")
    if m:
        return f"封禁列表共 {m.group(1)} 条：{m.group(2)}".rstrip("：")
    m = sub(r"There are no banned (?:players|IPs)")
    if m:
        return "封禁列表为空"
    m = sub(r"Added (\S+) to the whitelist")
    if m:
        return f"已将 {m.group(1)} 加入白名单"
    m = sub(r"Removed (\S+) from the whitelist")
    if m:
        return f"已将 {m.group(1)} 移出白名单"
    m = sub(r"Turned on the whitelist")
    if m:
        return "已开启白名单"
    m = sub(r"Turned off the whitelist")
    if m:
        return "已关闭白名单"
    m = sub(r"Made (\S+) a server operator")
    if m:
        return f"已授予 {m.group(1)} OP 权限"
    m = sub(r"Made (\S+) no longer a server operator")
    if m:
        return f"已移除 {m.group(1)} 的 OP 权限"
    m = sub(r"Banned (\S+)(?:: ?(.*))?")
    if m:
        return f"已封禁 {m.group(1)}" + (f"（原因: {m.group(2)}）" if m.group(2) else "")
    m = sub(r"Unbanned (\S+)")
    if m:
        return f"已解封 {m.group(1)}"
    m = sub(r"Kicked (\S+)(?:: ?(.*))?")
    if m:
        return f"已踢出 {m.group(1)}" + (f"（原因: {m.group(2)}）" if m.group(2) else "")
    m = sub(r"Set the time to (\d+)")
    if m:
        return f"时间已设为 {m.group(1)} tick"
    m = sub(r"Set (.+?)'s game mode to (\w+) Mode")
    if m:
        return f"已将 {m.group(1)} 的游戏模式设为 {_ZH_MODE.get(m.group(2).lower(), m.group(2))}"
    m = sub(r"Set the game mode to (\w+) Mode")
    if m:
        return f"游戏模式已设为 {_ZH_MODE.get(m.group(1).lower(), m.group(1))}"
    m = sub(r"Gave (\d+) \[(.+?)\] to (\S+)")
    if m:
        return f"已给予 {m.group(3)} {m.group(1)} 个 [{m.group(2)}]"
    m = sub(r"Teleported (\S+) to (.+)")
    if m:
        return f"已将 {m.group(1)} 传送到 {m.group(2)}"
    m = sub(r"Killed (\S+)")
    if m:
        return f"已击杀 {m.group(1)}"
    m = sub(r"No player was found.*")
    if m:
        return "未找到该玩家"
    m = sub(r"Unknown or incomplete command.*")
    if m:
        return "未知或不完整的指令，输入 help 查看可用指令"
    m = sub(r"Incorrect argument for command.*")
    if m:
        return "指令参数错误"
    m = sub(r"Saving the game \(this may take a moment!\)")
    if m:
        return "正在保存存档（可能需要一点时间）"
    m = sub(r"Saved the game")
    if m:
        return "存档已保存"
    m = sub(r"Stopping the server")
    if m:
        return "服务器正在关闭"
    return s


def _zh(text: str) -> str:
    """将 RCON 返回文本整体中文化：逐行匹配常见格式，空响应给友好提示。"""
    if not text or not text.strip():
        return "✅ 已执行（服务端无返回内容）"
    if "连接失败" in text:
        return text
    if text in ("（空响应）", "（空指令）"):
        return "✅ 已执行（服务端无返回内容）"
    return "\n".join(_zh_line(line.strip()) for line in text.splitlines())


def _format_tps(resp: str) -> str:
    """把 tabtps / Paper 原生 /tps 输出整理成适合群聊的紧凑文本。

    服务器装了 tabtps 后，/tps 返回的是带表格线、进度条的多行中文，
    直接转发到群里非常难看；这里提取关键指标重新排版。解析失败时回退
    到逐行中文化，保留原有行为。
    """
    if not resp or not resp.strip():
        return "（无返回内容）"
    lines = [ln.strip() for ln in resp.splitlines() if ln.strip()]

    tps: dict[str, str] = {}
    mem = cpu = None
    mspt: dict[str, str] = {}
    for ln in lines:
        m = re.search(
            r"TPS:\s*([\d.]+)\s*\(5s\)(?:,\s*([\d.]+)\s*\(1m\))?"
            r"(?:,\s*([\d.]+)\s*\(5m\))?(?:,\s*([\d.]+)\s*\(15m\))?",
            ln,
        )
        if m:
            for k, v in zip(("5s", "1m", "5m", "15m"), m.groups()):
                if v is not None:
                    tps[k] = v
        m = re.search(r"内存:\s*(\d+)\s*[Mm]\s*/\s*(\d+)\s*[Mm]\s*(?:\(最大\s*(\d+)\s*[Mm]\))?", ln)
        if m:
            mem = (m.group(1), m.group(2), m.group(3))
        m = re.search(r"CPU:\s*([\d.]+)%\s*,\s*([\d.]+)%\s*\(系统,\s*进程\)", ln)
        if m:
            cpu = (m.group(2), m.group(1))  # (进程, 系统)
        m = re.search(r"[├└]─\s*(\d+s)\s*-\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", ln)
        if m:
            mspt[m.group(1)] = m.group(2)  # 取平均值

    if tps:
        out = ["⚡ TPS: " + "  ".join(f"{tps[k]}({k})" for k in ("5s", "1m", "5m", "15m") if k in tps)]
        if mem:
            s = f"🧠 内存: {mem[0]}M/{mem[1]}M"
            if mem[2]:
                s += f"（上限 {mem[2]}M）"
            out.append(s)
        if cpu:
            out.append(f"💻 CPU: {cpu[0]}%（进程）/ {cpu[1]}%（系统）")
        if mspt:
            out.append("⏱ MSPT: " + "  ".join(f"{mspt[k]}({k})" for k in ("5s", "10s", "60s") if k in mspt))
        return "\n".join(out)

    # Paper / Spigot 原生 /tps: "TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0"
    m = re.search(r"tps[^:]*:\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", resp, re.IGNORECASE)
    if m:
        return f"⚡ TPS: {m.group(1)}(1m)  {m.group(2)}(5m)  {m.group(3)}(15m)"

    return "\n".join(_zh_line(ln) for ln in lines)


def _safe_int(value: Any, default: int) -> int:
    """容错的整数解析：配置里填了非数字时回退默认值，而不是让插件加载失败。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class _RconClient:
    """内置的极简线程安全 RCON 客户端。

    不使用 mcrcon 库的原因：mcrcon 0.7 在构造与读超时上依赖
    ``signal.alarm``/``SIGALRM``，该机制只允许在主解释器主线程使用；
    本插件通过 ``asyncio.to_thread`` 在工作线程发起 RCON 调用，会得到
    ``ValueError: signal only works in main thread of the main interpreter``，
    所有 RCON 功能因此全部失效。

    本实现完全基于 ``socket.settimeout`` 做超时，可在任意线程使用。

    协议参考: https://wiki.vg/RCON
    """

    _AUTH = 3
    _AUTH_RESPONSE = 2
    _EXECCOMMAND = 2
    _RESPONSE_VALUE = 0

    def __init__(self, host: str, password: str, port: int = 25575, timeout: float = 10):
        self._request_id = 0
        self._sock = socket.create_connection((host, port), timeout=timeout)
        try:
            self._sock.settimeout(timeout)
            self._auth(password)
        except Exception:
            self.close()
            raise

    # ---- 协议收发 ----
    def _send(self, pkt_type: int, payload: str) -> int:
        self._request_id += 1
        request_id = self._request_id
        body = (
            struct.pack("<ii", request_id, pkt_type)
            + payload.encode("utf-8")
            + b"\x00\x00"
        )
        self._sock.sendall(struct.pack("<i", len(body)) + body)
        return request_id

    def _recv_exact(self, size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = self._sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("RCON 连接被对端关闭")
            buf += chunk
        return buf

    def _recv(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._recv_exact(4))
        if length < 10 or length > 8192:
            raise ConnectionError(f"RCON 响应长度异常: {length}")
        body = self._recv_exact(length)
        request_id, pkt_type = struct.unpack("<ii", body[:8])
        return request_id, pkt_type, body[8:-2].decode("utf-8", errors="replace")

    def _auth(self, password: str) -> None:
        request_id = self._send(self._AUTH, password)
        # 部分服务端在 AUTH_RESPONSE 之前会先发一个空 RESPONSE_VALUE，跳过即可
        while True:
            rid, pkt_type, _ = self._recv()
            if pkt_type == self._AUTH_RESPONSE:
                if rid == -1:
                    raise PermissionError("RCON 认证失败：密码错误")
                return

    def command(self, cmd: str) -> str:
        """执行一条指令并返回服务端响应文本。"""
        request_id = self._send(self._EXECCOMMAND, cmd)
        while True:
            rid, pkt_type, payload = self._recv()
            if pkt_type == self._RESPONSE_VALUE and rid == request_id:
                return payload

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass

    def __enter__(self) -> "_RconClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


@dataclass
class MCServer:
    """单个 Minecraft 服务器的 RCON 连接信息。"""

    name: str
    host: str
    port: int = 25575
    password: str = ""


def _clean(text: str) -> str:
    """清理用户输入：去掉换行与首尾空白，防止一次注入多条 RCON 指令。"""
    return " ".join(str(text or "").split())


def _parse_servers(cfg: dict) -> tuple[dict[str, MCServer], str]:
    """从插件配置解析服务器列表。

    优先读取 ``servers`` 数组；为空时回退到旧版单服务器字段
    ``rcon_host`` / ``rcon_port`` / ``rcon_password``。
    """
    servers: dict[str, MCServer] = {}
    raw = cfg.get("servers")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("host") or f"server{len(servers) + 1}")
            servers[name] = MCServer(
                name=name,
                host=str(item.get("host") or "127.0.0.1"),
                port=_safe_int(item.get("port") or 25575, 25575),
                password=str(item.get("password") or ""),
            )
    if not servers:
        host = str(cfg.get("rcon_host") or "")
        password = str(cfg.get("rcon_password") or "")
        if host or password:
            name = str(cfg.get("default_server") or "default")
            servers[name] = MCServer(
                name=name,
                host=host or "127.0.0.1",
                port=_safe_int(cfg.get("rcon_port") or 25575, 25575),
                password=password,
            )
    default = str(cfg.get("default_server") or "")
    if default not in servers:
        default = next(iter(servers), "")
    return servers, default


class MinecraftPlugin(Star):
    """Minecraft 服务器管理插件（RCON）。"""

    _GAMEMODES = {
        "survival": "survival", "s": "survival", "0": "survival",
        "creative": "creative", "c": "creative", "1": "creative",
        "adventure": "adventure", "a": "adventure", "2": "adventure",
        "spectator": "spectator", "sp": "spectator", "3": "spectator",
    }
    _DIFFICULTIES = {
        "peaceful": "peaceful", "p": "peaceful", "0": "peaceful",
        "easy": "easy", "e": "easy", "1": "easy",
        "normal": "normal", "n": "normal", "2": "normal",
        "hard": "hard", "h": "hard", "3": "hard",
    }

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self._load_config()
        self._tasks: list[asyncio.Task] = []
        self._bridge_server: Optional[asyncio.AbstractServer] = None
        self._bridge_writers: set[asyncio.StreamWriter] = set()
        self._player_cache: dict[str, set[str]] = {}
        # 每个服务器连续失败次数，用于轮询退避
        self._server_failures: dict[str, int] = {}
        self._bridge_target: str = ""
        # 鹊桥 WebSocket 连接状态
        self._qq_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._qq_ws_session: Optional[aiohttp.ClientSession] = None
        self._qq_connected: bool = False

        # 静默模式（关闭 MC -> QQ 转发）运行期状态
        self._silent_until: Optional[datetime] = None          # 单次/自定义时段结束时间
        self._silent_manual: bool = False                      # 持续静默（直到 off）
        self._silent_schedule_start: Optional[_dtime] = None   # 定时：每日开始
        self._silent_schedule_end: Optional[_dtime] = None     # 定时：每日结束
        self._silent_buffer: list = []                         # 静默期间缓存的 MC→QQ 动态
        self._silent_prev_active: bool = False                 # 静默刚结束标记，用于触发回放

        # 静默开始时播报统计：从上次静默结束累计的聊天消息数与 TPS 采样均值
        self._msg_count: int = 0                               # 累计聊天消息数（仅聊天）
        self._tps_samples: list[float] = []                    # 定时采样得到的 TPS 值列表
        self._silent_stats_sent: bool = False                  # 本次静默的统计是否已播报

        # 玩家每日在线时长统计（用 join/quit 事件计算）
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._playtime_sessions: dict[str, datetime] = {}      # 在线中：玩家 -> 加入时间
        self._playtime_daily: dict[str, dict[str, int]] = {}   # 日期 -> {玩家: 累计秒数}
        self._playtime_path = os.path.join(self._plugin_dir, "playtime_state.json")
        self._load_playtime_state()

        self._silent_state_path = os.path.join(self._plugin_dir, "silent_state.json")
        self._load_silent_state()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        cfg = self.config
        self.rcon_enabled = bool(cfg.get("rcon_enabled", True))
        self.admin_only = bool(cfg.get("admin_only_commands", True))
        self.rcon_timeout = _safe_int(cfg.get("rcon_timeout"), DEFAULT_TIMEOUT)
        self.servers, self.default_server = _parse_servers(cfg)

        self.auto_backup = bool(cfg.get("auto_backup", False))
        self.backup_interval = _safe_int(cfg.get("backup_interval_minutes"), 60)
        self.enable_announce = bool(cfg.get("enable_announce", False))
        self.announce_interval = _safe_int(cfg.get("announce_interval_minutes"), 30)
        self.announcements = [str(a) for a in (cfg.get("announcements") or [])]
        self.enable_monitor = bool(cfg.get("enable_monitor", False))
        self.enable_llm = bool(cfg.get("enable_llm_tool", True))

        # 玩家追踪：默认关闭，避免对离线服务器高频轮询
        self.enable_tracker = bool(cfg.get("enable_player_tracker", False))
        self.tracker_interval = max(
            10, _safe_int(cfg.get("player_tracker_interval"), 30)
        )

        self.enable_bridge = bool(cfg.get("enable_chat_bridge", False))
        self.bridge_mc_to_qq = bool(cfg.get("bridge_mc_to_qq", True))
        self.bridge_host = str(cfg.get("bridge_listen_host") or "127.0.0.1")
        self.bridge_port = _safe_int(cfg.get("bridge_listen_port"), 25576)
        self.bridge_target_session = str(cfg.get("bridge_target_session") or "")
        self.bridge_format_mc = str(cfg.get("bridge_format_mc") or "[MC]{player}: {msg}")

        # 鹊桥（QueQiao）mod 对接：WS 客户端
        self.queqiao_enabled = bool(cfg.get("queqiao_enabled", False))
        self.queqiao_url = str(cfg.get("queqiao_ws_url") or "").strip()
        self.queqiao_token = str(cfg.get("queqiao_token") or "").strip()
        self.queqiao_server_name = str(cfg.get("queqiao_server_name") or "").strip()
        self.bridge_qq_to_mc = bool(cfg.get("bridge_qq_to_mc", True))
        self.bridge_format_qq = str(cfg.get("bridge_format_qq") or "[QQ]{sender}: {msg}")
        self.bridge_notify_join_quit = bool(cfg.get("bridge_notify_join_quit", True))
        self.bridge_notify_death = bool(cfg.get("bridge_notify_death", True))

    # ------------------------------------------------------------------
    # RCON 基础
    # ------------------------------------------------------------------
    def _get_server(self, name: Optional[str] = None) -> Optional[MCServer]:
        return self.servers.get(name or self.default_server)

    def _rcon_ready(self, server_name: Optional[str] = None) -> tuple[Optional[MCServer], str]:
        """检查 RCON 是否可用，返回 (服务器, 错误消息)。"""
        if not self.rcon_enabled:
            return None, "RCON 功能未启用"
        srv = self._get_server(server_name)
        if not srv:
            return None, f"未配置服务器: {server_name or self.default_server}"
        if not srv.password:
            return None, f"服务器 [{srv.name}] 未配置 RCON 密码"
        return srv, ""

    async def _rcon(self, command: str, server_name: Optional[str] = None) -> str:
        """在指定服务器上执行单条 RCON 指令，返回服务端输出文本。"""
        results = await self._rcon_multi([command], server_name)
        return results[0]

    async def _rcon_zh(self, command: str, server_name: Optional[str] = None) -> str:
        """执行 RCON 指令并把常见英文输出翻译成中文，用于直接回显给用户。"""
        return _zh(await self._rcon(command, server_name))

    async def _rcon_multi(
        self, commands: Iterable[str], server_name: Optional[str] = None
    ) -> list[str]:
        """在同一 TCP 连接上依次执行多条 RCON 指令，避免反复建连。"""
        commands = [_clean(c) for c in commands]
        srv, err = self._rcon_ready(server_name)
        if srv is None:
            return [err] * len(commands)
        if not any(commands):
            return ["（空指令）"] * len(commands)

        def _sync() -> list[str]:
            outputs: list[str] = []
            with _RconClient(
                srv.host, srv.password, srv.port, timeout=self.rcon_timeout
            ) as rcon:
                for cmd in commands:
                    outputs.append((rcon.command(cmd) or "").strip())
            return outputs

        try:
            results = await asyncio.to_thread(_sync)
            self._server_failures[srv.name] = 0
            return [r or "（空响应）" for r in results]
        except Exception as e:
            self._server_failures[srv.name] = self._server_failures.get(srv.name, 0) + 1
            self.logger.error(f"RCON 执行失败 [{srv.name}]: {e}")
            return [f"[{srv.name}] 连接失败: {e}"] * len(commands)

    def _server_reachable(self, srv: MCServer) -> bool:
        """连续失败次数达到阈值后跳过轮询，成功执行任意指令即恢复。"""
        return self._server_failures.get(srv.name, 0) < 3

    # ------------------------------------------------------------------
    # list 输出解析
    # ------------------------------------------------------------------
    _LIST_RE = re.compile(r"There are (\d+) of a max of \d+ players online", re.IGNORECASE)

    async def _players_online(self, server_name: str) -> set[str]:
        """解析 ``list`` 输出中的在线玩家名集合。

        标准输出: "There are 2 of a max of 20 players online: Steve, Alex"
        0 人时:    "There are 0 of a max of 20 players online:"

        连接失败/未启用/空响应等错误文本里也可能含冒号，必须先排除，
        否则错误信息会被当成玩家名（幻影玩家、错误地批量 give）。
        """
        resp = await self._rcon("list", server_name)
        if "连接失败" in resp or resp == "（空响应）" or resp == "（空指令）":
            return set()
        m = self._LIST_RE.search(resp)
        if m and int(m.group(1)) == 0:
            return set()
        if ":" in resp:
            tail = resp.rsplit(":", 1)[1]
            return {n.strip() for n in tail.split(",") if n.strip()}
        return set()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def _get_status(self, server_name: Optional[str] = None) -> str:
        name = server_name or self.default_server
        icons_cmds = (
            ("👥", "list"),
            ("🌱", "seed"),
            ("🕐", "time query daytime"),
            ("⚔️", "difficulty"),
        )
        # 复用单个连接执行全部查询
        outputs = await self._rcon_multi([c for _, c in icons_cmds], name)
        lines = [f"📊 服务器状态 [{name}]", ""]
        for (icon, _), out in zip(icons_cmds, outputs):
            lines.append(f"{icon} {_zh(out)}")
        return "\n".join(lines)

    async def _get_players(self, server_name: Optional[str] = None) -> str:
        name = server_name or self.default_server
        resp = await self._rcon("list", name)
        m = self._LIST_RE.search(resp)
        if m and int(m.group(1)) == 0:
            return f"[{name}] 当前没有在线玩家"
        if "连接失败" in resp:
            return f"[{name}] {resp}"
        return f"👥 [{name}] 在线玩家：\n{_zh(resp)}"

    async def _get_whitelist(self, server_name: Optional[str] = None) -> str:
        name = server_name or self.default_server
        resp = await self._rcon("whitelist list", name)
        if "there are no whitelisted players" in resp.lower():
            return f"[{name}] 白名单为空"
        return f"📋 [{name}] 白名单：\n{_zh(resp)}"

    async def _get_banlist(self, server_name: Optional[str] = None) -> str:
        name = server_name or self.default_server
        resp = await self._rcon("banlist", name)
        if "there are no banned players" in resp.lower():
            return f"[{name}] 封禁列表为空"
        return f"🚫 [{name}] 封禁列表：\n{_zh(resp)}"

    async def _get_performance(self, server_name: Optional[str] = None) -> str:
        """TPS 查询。仅 Paper/Spigot/Purpur 等服务端支持，原版/Fabric 不支持。"""
        name = server_name or self.default_server
        resp = await self._rcon("tps", name)
        low = resp.lower()
        if any(p in low for p in _UNKNOWN_CMD_PATTERNS):
            return (
                f"📈 [{name}] 当前服务端不支持 tps 指令（仅 Paper/Spigot 系服务端支持）。\n"
                f"服务端返回: {resp}"
            )
        return f"📈 服务器性能 [{name}]\n" + _format_tps(resp)

    # TPS 采样：尽量从服务端返回中提取一个可用的 TPS 数值（5s 优先），失败返回 None
    _TPS_VALUE_RE = re.compile(r"TPS:\s*([\d.]+)")

    async def _get_tps_value(self, server_name: Optional[str] = None) -> Optional[float]:
        """执行 tps 指令并提取数值型 TPS。仅支持能响应 tps 的服务端。"""
        name = server_name or self.default_server
        try:
            resp = await self._rcon("tps", name)
        except Exception:
            return None
        if not resp:
            return None
        low = resp.lower()
        if any(p in low for p in _UNKNOWN_CMD_PATTERNS):
            return None
        m = self._TPS_VALUE_RE.search(resp)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    async def _tps_sample_loop(self) -> None:
        """后台定时采样 TPS，累积样本供静默开始时求平均。"""
        raw_iv = _safe_int(self.config.get("tps_sample_interval_minutes"), 10)
        interval = max(60, (raw_iv if raw_iv > 0 else 10) * 60)
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._server_failures.get(self.default_server, 0) < 3:
                    continue  # 服务器连续失败，跳过本次采样
                val = await self._get_tps_value()
                if val is not None:
                    self._tps_samples.append(val)
                    if len(self._tps_samples) > 1000:  # 只保留最近 1000 个样本
                        self._tps_samples = self._tps_samples[-1000:]
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"TPS 采样异常: {e}")

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------
    async def _backup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.backup_interval * 60)
                for srv in self.servers.values():
                    if srv.password:
                        await self._rcon_zh("save-all", srv.name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"自动备份异常: {e}")

    async def _announce_loop(self) -> None:
        idx = 0
        while True:
            try:
                await asyncio.sleep(self.announce_interval * 60)
                if not self.announcements:
                    continue
                msg = self.announcements[idx % len(self.announcements)]
                idx += 1
                for srv in self.servers.values():
                    if srv.password:
                        await self._rcon_zh(f"say [系统公告] {_clean(msg)}", srv.name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"自动公告异常: {e}")

    async def _player_tracker_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tracker_interval)
                for srv in self.servers.values():
                    if not srv.password or not self._server_reachable(srv):
                        continue  # 未配置密码或近期连续失败，跳过
                    current = await self._players_online(srv.name)
                    prev = self._player_cache.get(srv.name, set())
                    if prev or current:  # 首次轮询不播报
                        for p in current - prev:
                            self.logger.info(f"[{srv.name}] 玩家加入: {p}")
                        for p in prev - current:
                            self.logger.info(f"[{srv.name}] 玩家离开: {p}")
                    self._player_cache[srv.name] = current
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"玩家追踪异常: {e}")

    # ------------------------------------------------------------------
    # MC -> QQ 聊天桥接
    # ------------------------------------------------------------------
    @staticmethod
    def _is_local_peer(ip: str) -> bool:
        """判断桥接对端是否来自本机回环或允许的本地网段。

        桥接绑定默认 127.0.0.1，正常只有本机 MC mod 通过回环连接。
        公网扫描器/恶意连接（如 0.0.0.0 绑定被公网探测到）将被拒绝。
        """
        if not ip:
            return False
        ip = ip.strip()
        if ip in ("127.0.0.1", "::1", "localhost"):
            return True
        if ip.startswith("127."):
            return True
        # 可选：允许常用本地私有网段（若以后桥接部署在局域网，可放开）
        # 192.168/172.16-31/10.
        return ip.startswith(("192.168.", "10.")) or (
            ip.startswith("172.16.") or ip.startswith("172.31.")
        )

    async def _start_bridge_listener(self) -> bool:
        if self._bridge_server is not None:
            return True

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            peer = writer.get_extra_info("peername")
            peer_ip = peer[0] if isinstance(peer, tuple) and peer else "?"
            if not self._is_local_peer(peer_ip):
                # 仅信任本机回环来源；拒绝公网/异地扫描与伪造连接
                self.logger.warning(
                    f"MC 桥接已拒绝非本机来源: {peer} "
                    f"（仅允许 {self.bridge_host or '127.0.0.1'}）"
                )
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                return
            self.logger.info(f"MC 桥接连接来自: {peer}")
            self._bridge_writers.add(writer)
            try:
                while True:
                    data = await reader.readline()
                    if not data:
                        break
                    text = data.decode("utf-8", errors="ignore").strip()
                    if text and not self._is_bridge_noise(text):
                        await self._on_bridge_line(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self._bridge_writers.discard(writer)
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        try:
            self._bridge_server = await asyncio.start_server(
                handle, self.bridge_host, self.bridge_port
            )
            self.logger.info(f"MC 聊天桥接监听中: {self.bridge_host}:{self.bridge_port}")
            return True
        except Exception as e:
            self._bridge_server = None
            self.logger.error(f"MC 聊天桥接监听启动失败: {e}")
            return False

    async def _stop_bridge_listener(self) -> None:
        if self._bridge_server is None:
            return
        server, self._bridge_server = self._bridge_server, None
        for writer in list(self._bridge_writers):
            try:
                writer.close()
            except Exception:
                pass
        self._bridge_writers.clear()
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
        self.logger.info("MC 聊天桥接监听已停止")

    # 常见网络探测/扫描特征（HTTP 请求行、请求头、协议 banner 等），命中即视为噪音丢弃
    _BRIDGE_NOISE_RE = (
        r"(^|\r\n)(HTTP/\d\.\d\b|Host:|User-Agent:|Accept:|Connection:|"
        r"Accept-Encoding:|Content-Length:|Referer:|GET |POST |OPTIONS |"
        r"PUT |HEAD |GET /|HEAD /|\* \d|redis-server|SSH-2\.0|220 |"
        r"220-|250 |EHLO |PING |HELO )"
    )

    def _is_bridge_noise(self, text: str) -> bool:
        """识别并过滤不应进入聊天的网络探测/协议噪音。

        旧 TCP 桥接监听曾因绑定 0.0.0.0 被公网扫描器（如 Infrawatch）主动
        HTTP 探测，Host/User-Agent 等请求头被误当聊天转发进群。这里按特征丢弃。
        """
        if not text:
            return True
        if len(text) > 500:  # 正常 MC 聊天一行远小于此
            return True
        # 控制字符（除 \t\n\r 外）多为恶意/异常载荷
        if any(c for c in text if c.isascii() and ord(c) < 32 and c not in "\t\n\r"):
            return True
        if re.search(self._BRIDGE_NOISE_RE, text, re.IGNORECASE):
            return True
        return False

    async def _on_bridge_line(self, text: str) -> None:
        if not self.enable_bridge or not self.bridge_mc_to_qq:
            return
        player, message = "未知", ""
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                # 合法 JSON 但不是对象（如 "123"），视为无有效载荷
                return
            player = str(payload.get("player", "未知"))
            message = str(payload.get("message", ""))
        except json.JSONDecodeError:
            if ": " not in text:
                return
            player, message = (p.strip() for p in text.split(": ", 1))
        if not message:
            return

        try:
            formatted = self.bridge_format_mc.format(player=player, msg=message)
        except Exception:
            formatted = f"[MC] {player}: {message}"

        target = self._bridge_target or self.bridge_target_session
        if not target:
            self.logger.info(f"MC 聊天（未设置转发目标）: {formatted}")
            return
        try:
            await self.context.send_message(target, MessageChain([Plain(formatted)]))
        except Exception as e:
            self.logger.error(f"MC 聊天转发失败: {e}")

    # ------------------------------------------------------------------
    # 鹊桥（QueQiao）WebSocket 对接
    # ------------------------------------------------------------------
    async def _queqiao_loop(self) -> None:
        """连接鹊桥 WS Server 的常驻循环，断线指数退避重连（上限 60s）。"""
        backoff = 3
        while True:
            try:
                headers = {"x-self-name": self.queqiao_server_name or "AstrBot"}
                if self.queqiao_token:
                    headers["Authorization"] = f"Bearer {self.queqiao_token}"
                self._qq_ws_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
                async with self._qq_ws_session.ws_connect(
                    self.queqiao_url, headers=headers, heartbeat=30
                ) as ws:
                    self._qq_ws = ws
                    self._qq_connected = True
                    backoff = 3
                    self.logger.info(f"鹊桥已连接: {self.queqiao_url}")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                await self._on_queqiao_event(json.loads(msg.data))
                            except json.JSONDecodeError:
                                continue
                            except Exception as e:
                                self.logger.error(f"处理鹊桥事件失败: {e}")
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.warning(f"鹊桥连接断开/失败: {e}，{backoff}s 后重试")
            finally:
                self._qq_connected = False
                self._qq_ws = None
                if self._qq_ws_session is not None and not self._qq_ws_session.closed:
                    try:
                        await self._qq_ws_session.close()
                    except Exception:
                        pass
                self._qq_ws_session = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    @staticmethod
    def _qq_player_name(payload: dict) -> str:
        """从鹊桥事件中宽容提取玩家名（兼容不同版本字段命名）。"""
        player = payload.get("player")
        if isinstance(player, dict):
            for key in ("nickname", "name", "playerName", "displayName"):
                v = player.get(key)
                if v:
                    return str(v)
        elif isinstance(player, str) and player:
            return player
        for key in ("playerName", "player_name"):
            v = payload.get(key)
            if v:
                return str(v)
        return "未知玩家"

    async def _on_queqiao_event(self, payload: dict) -> None:
        """解析鹊桥推送的事件并转发到绑定会话。"""
        if not isinstance(payload, dict) or not self.enable_bridge or not self.bridge_mc_to_qq:
            return
        post_type = str(payload.get("post_type") or "")
        sub_type = str(payload.get("sub_type") or "")
        event_name = str(payload.get("event_name") or "")
        server = str(payload.get("server_name") or "")
        name = self._qq_player_name(payload)
        text = ""

        if post_type == "message" and sub_type in ("player_chat", "chat"):
            msg = str(payload.get("message") or payload.get("content") or "").strip()
            if not msg:
                return
            self._msg_count += 1  # 聊天消息计数（供静默开始统计用）
            try:
                text = self.bridge_format_mc.format(player=name, msg=msg)
            except Exception:
                text = f"[MC] {name}: {msg}"
        elif post_type == "notice" and sub_type in (
            "join", "quit", "player_join", "player_quit", "player_leave"
        ):
            if not self.bridge_notify_join_quit:
                return
            is_join = sub_type in ("join", "player_join")
            self._record_playtime(name, is_join, datetime.now())
            icon = "🟢" if is_join else "🔴"
            verb = "加入了服务器" if is_join else "退出了服务器"
            text = f"{icon} {name} {verb}"
        elif post_type == "notice" and sub_type in ("death", "player_death"):
            if not self.bridge_notify_death:
                return
            death = payload.get("death")
            reason = ""
            if isinstance(death, dict):
                reason = str(death.get("death_message") or death.get("message") or "").strip()
            if not reason:
                reason = str(payload.get("death_message") or payload.get("content") or "").strip()
            text = f"💀 {name} {'— ' + reason if reason else '死了'}"
        elif post_type == "notice" and sub_type in ("advancement", "achievement", "player_advancement"):
            adv = payload.get("advancement")
            title = ""
            if isinstance(adv, dict):
                title = str(adv.get("title") or adv.get("name") or "").strip()
            if not title:
                title = str(payload.get("content") or "").strip()
            text = f"🏆 {name} 达成了成就{'：' + title if title else ''}"
        # V2 格式兜底：QueQiao 用 event_name 标识事件（如 player_join / player_quit）
        elif event_name in ("player_join", "join", "player_leave", "quit"):
            if not self.bridge_notify_join_quit:
                return
            is_join = event_name in ("player_join", "join")
            self._record_playtime(name, is_join, datetime.now())
            icon = "🟢" if is_join else "🔴"
            verb = "加入了服务器" if is_join else "退出了服务器"
            text = f"{icon} {name} {verb}"
        elif event_name in ("player_death", "death"):
            if not self.bridge_notify_death:
                return
            text = f"💀 {name} 死了"
        elif event_name in ("player_advancement", "advancement", "achievement"):
            text = f"🏆 {name} 达成了成就"
        else:
            return

        if server:
            text = f"[{server}] {text}" if not text.startswith("[") else text

        # 静默模式：缓存消息（带时间戳），静默结束后一次性回放，而非直接丢弃
        if self._is_silent():
            # 兜底：手动开启或定时到点都可能走到这，确保"静默开始统计"只播一次
            if not self._silent_stats_sent:
                self._silent_stats_sent = True
                await self._send_silent_start_stats()
            self._buffer_silent(text)
            return

        # 静默刚结束 -> 先回放缓存的聊天记录，再继续正常转发
        if self._silent_prev_active:
            await self._flush_silent_buffer()
            self._silent_prev_active = False
            self._silent_stats_sent = False  # 静默结束，重置统计播报标记

        target = self._bridge_target or self.bridge_target_session
        if not target:
            self.logger.info(f"鹊桥事件（未设置转发目标，忽略）: {text}")
            return
        try:
            await self.context.send_message(target, MessageChain([Plain(text)]))
        except Exception as e:
            self.logger.error(f"鹊桥事件转发失败: {e}")

    async def _queqiao_broadcast(self, text: str) -> bool:
        """通过鹊桥 broadcast API 向游戏内全服广播文本。"""
        ws = self._qq_ws
        if ws is None or self._qq_ws is None or ws.closed:
            return False
        try:
            await ws.send_json({
                "api": "broadcast",
                "data": {"message": [{"text": text}]},
                "echo": f"astrbot-{int(time.time())}",
            })
            return True
        except Exception as e:
            self.logger.error(f"鹊桥广播失败: {e}")
            return False

    async def stop_queqiao(self) -> None:
        """关闭鹊桥 WS 连接与 session（terminate 或手动关闭时调用）。"""
        ws, self._qq_ws = self._qq_ws, None
        session, self._qq_ws_session = self._qq_ws_session, None
        self._qq_connected = False
        for obj in (ws, session):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass

    @staticmethod
    def _is_command_message(msg: str) -> bool:
        """识别消息是否为命令形态（应整条过滤，不转发进游戏）。

        覆盖两类：
        1. 以命令前缀开头的消息：/、！、!、\、.、全角斜杠／ 、全角波浪～ 等
        2. 命令正文形态：如 "/mc status" 前缀被剥离后残留的 "mc status"，或
           "mc playtime" 这类直接以插件主命令字开头、且首词是已知子指令的消息
        """
        if not msg:
            return True
        s = msg.lstrip()
        # 1) 常见命令前缀（含全角变体）
        if s[:1] in ("/", "\\", ".", "!", "！", "／", "・", "~", "～", "'", "`", "-"):
            return True
        # 2) 命令正文：拆第一个词判断是否插件命令族
        first = s.split(None, 1)[0].lower() if s.split(None, 1) else s.lower()
        # 以 mc 开头（/mc 命令族），或首词本身就是已知子指令（前缀被剥离情形）
        if first in MinecraftPlugin._ACTIONS:
            return True
        if first in ("mc", "mcd"):
            return True
        return False

    @staticmethod
    def _session_group_id(origin: str) -> str:
        """从 unified_msg_origin（如 'ST ED:GroupMessage:785925006'）提取群号末段。"""
        if not origin:
            return ""
        return origin.rsplit(":", 1)[-1]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_qq_message(self, event: AstrMessageEvent):
        """QQ→MC：把绑定会话里的普通消息广播进游戏（鹊桥）。"""
        if not (self.queqiao_enabled and self.bridge_qq_to_mc and self._qq_connected):
            return
        if not self.enable_bridge:
            return
        target = self._bridge_target or self.bridge_target_session
        if not target:
            return
        # 宽松匹配：按群号（origin 末段）判断，避免适配器重启/协议差异导致
        # unified_msg_origin 前缀变化，使整批群消息被静默丢弃（服→群不受影响，
        # 因此表现为"服-群通、群-服有时不转发"）
        tg = self._session_group_id(target)
        mg = self._session_group_id(event.unified_msg_origin)
        if tg and mg:
            if tg != mg:
                return
        elif event.unified_msg_origin != target:
            return
        # 忽略 bot 自身的消息，防止循环
        try:
            if event.get_sender_id() == event.get_self_id():
                return
        except Exception:
            pass
        msg = event.message_str.strip()
        if not msg:
            return
        # 命令与唤醒消息不转发（识别 /mc 等命令形态，整条过滤）
        if self._is_command_message(msg):
            return
        sender = event.get_sender_name() or "QQ用户"
        try:
            text = self.bridge_format_qq.format(sender=sender, msg=msg.replace("\n", " "))
        except Exception:
            text = f"[QQ] {sender}: {msg.replace(chr(10), ' ')}"
        ok = await self._queqiao_broadcast(text)
        if not ok:
            self.logger.warning(f"QQ→MC 转发未送达（鹊桥未连接或发送异常）: {text}")

    # ------------------------------------------------------------------
    # 指令分发
    # ------------------------------------------------------------------
    _ACTIONS: dict[str, str] = {
        "help": "_help",
        "servers": "_servers",
        "use": "_use",
        "monitor": "_monitor",
        "ai": "_ai",
        "backup": "_backup",
        "announce": "_announce",
        "say": "_announce",
        "batch": "_batch",
        "bridge": "_bridge",
        "silent": "_silent",
        "playtime": "_playtime",
        "status": "_status",
        "players": "_players",
        "list": "_players",
        "version": "_version",
        "whitelist": "_whitelist",
        "banlist": "_banlist",
        "op": "_op",
        "deop": "_deop",
        "ban": "_ban",
        "unban": "_unban",
        "kick": "_kick",
        "gamemode": "_gamemode",
        "time": "_time",
        "weather": "_weather",
        "difficulty": "_difficulty",
        "save-all": "_save_all",
        "saveall": "_save_all",
        "stop": "_stop",
        "give": "_give",
        "tp": "_tp",
        "kill": "_kill",
        "cmd": "_cmd",
    }

    @filter.command("mc")
    async def mc(self, event: AstrMessageEvent, action: str = "help", args: str = GreedyStr):
        """Minecraft 服务器管理插件"""
        if self.admin_only and not event.is_admin():
            yield event.plain_result("⛔ 仅管理员可使用 MC 指令")
            return
        # 这些子指令不依赖 RCON 连接，不应被 RCON 开关/服务器配置拦截
        action = (action or "help").strip().lower()
        if action not in ("help", "servers", "use", "bridge", "silent", "playtime"):
            if not self.rcon_enabled:
                yield event.plain_result("RCON 功能未启用，请在插件设置中开启")
                return
            if not self.servers:
                yield event.plain_result("未配置任何 MC 服务器，请在插件设置中添加")
                return

        parts = args.split() if args else []
        handler_name = self._ACTIONS.get(action)
        if handler_name is None:
            yield event.plain_result(f"未知子指令: {action}\n使用 /mc help 查看完整帮助")
            return
        handler = getattr(self, handler_name)
        text = await handler(event, parts)
        if text:
            yield event.plain_result(text)

    # ---- 子指令实现，统一签名 (event, parts) -> str ---------------------
    async def _help(self, event: AstrMessageEvent, parts: list[str]) -> str:
        return (
            "⛏️ Minecraft 插件使用帮助\n"
            "\n"
            "指令格式：/mc <子指令> [参数]\n"
            "\n"
            "📊 服务器信息：\n"
            "  status              - 查看综合状态\n"
            "  players / list      - 在线玩家\n"
            "  monitor             - 性能监控 (TPS，需 Paper 系服务端)\n"
            "  playtime [玩家]      - 玩家在线时长（默认当日全部）\n"
            "  version             - 服务器版本\n"
            "  servers             - 列出所有配置的服务器\n"
            "  use <服务器名>       - 切换默认服务器\n"
            "\n"
            "👥 玩家管理：\n"
            "  whitelist [add/remove <玩家>] - 白名单\n"
            "  ban / unban / banlist / kick\n"
            "  op / deop\n"
            "\n"
            "🎮 游戏控制：\n"
            "  gamemode / time / weather / difficulty\n"
            "  save-all / stop\n"
            "\n"
            "📦 物品与传送：\n"
            "  give / tp / kill\n"
            "\n"
            "💬 聊天桥接：\n"
            "  bridge [status/on/off] - 查看/开关桥接\n"
            "\n"
            "🔇 静默模式（关闭 MC→QQ 转发）：\n"
            "  silent                 - 查看状态/用法\n"
            "  silent on [时长]       - 持续/临时静默（如 on 30m）\n"
            "  silent until HH:MM    - 静默到指定时间\n"
            "  silent off            - 取消静默\n"
            "  silent schedule HH:MM-HH:MM - 每日定时（如 23:00-08:00）\n"
            "\n"
            "🤖 AI 与自动化：\n"
            "  ai <自然语言>        - AI 帮你生成并执行指令\n"
            "  announce <消息>      - 全服公告\n"
            "  backup               - 手动备份\n"
            "  batch give <物品> [数量] - 给所有在线玩家发物品\n"
            "  cmd <指令>           - 执行任意指令\n"
            "  help                 - 显示此帮助\n"
            "\n"
            "⚠️  需要服务器启用 RCON"
        )

    async def _servers(self, event: AstrMessageEvent, parts: list[str]) -> str:
        lines = ["📡 已配置服务器：", ""]
        for srv in self.servers.values():
            mark = " 👈 默认" if srv.name == self.default_server else ""
            lines.append(f"  • {srv.name} ({srv.host}:{srv.port}){mark}")
        return "\n".join(lines)

    async def _use(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return f"用法：/mc use <服务器名>\n当前默认: {self.default_server}"
        target = parts[0]
        if target not in self.servers:
            return f"未知服务器: {target}\n可用: {list(self.servers.keys())}"
        self.default_server = target
        return f"✅ 默认服务器已切换为: {target}"

    async def _monitor(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not self.enable_monitor:
            return "性能监控未启用，请在插件设置中开启"
        return await self._get_performance()

    # ------------------------------------------------------------------
    # 静默模式：关闭 MC -> QQ 转发（聊天/进出/死亡/成就事件不再推群）
    # 三种触发：单次(on 时长) / 自定义时段(until HH:MM) / 每日定时(schedule)
    # ------------------------------------------------------------------
    def _load_silent_state(self) -> None:
        try:
            with open(self._silent_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            sched = str(data.get("schedule") or "")
        except Exception:
            sched = ""
        self._set_schedule(sched, persist=False)

    def _save_silent_state(self) -> None:
        sched = ""
        if self._silent_schedule_start is not None and self._silent_schedule_end is not None:
            sched = f"{self._silent_schedule_start.strftime('%H:%M')}-{self._silent_schedule_end.strftime('%H:%M')}"
        try:
            with open(self._silent_state_path, "w", encoding="utf-8") as f:
                json.dump({"schedule": sched}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"静默时段状态保存失败: {e}")

    def _load_playtime_state(self) -> None:
        """加载玩家每日在线时长缓存（供重启后继续累计）。"""
        try:
            with open(self._playtime_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            daily = data.get("daily")
            if isinstance(daily, dict):
                self._playtime_daily = {
                    str(d): {str(k): int(v) for k, v in dd.items() if isinstance(v, (int, float))}
                    for d, dd in daily.items() if isinstance(dd, dict)
                }
        except Exception:
            self._playtime_daily = {}

    def _save_playtime_state(self) -> None:
        """持久化玩家每日在线时长。"""
        try:
            with open(self._playtime_path, "w", encoding="utf-8") as f:
                json.dump({"daily": self._playtime_daily}, f, ensure_ascii=False)
        except Exception as e:
            self.logger.warning(f"玩家在线时长保存失败: {e}")

    def _record_playtime(self, name: str, is_join: bool, now: datetime) -> None:
        """用 join/quit 事件更新玩家在线时长。join 记开始时间，quit 结算并累加到当日。"""
        if not name or name == "未知玩家":
            return
        if is_join:
            self._playtime_sessions[name] = now
        else:
            joined = self._playtime_sessions.pop(name, None)
            if joined is None:
                return  # 无对应 join 记录（如重启后首条 quit），无法计算
            secs = int((now - joined).total_seconds())
            if secs < 0:
                return
            day = now.strftime("%Y-%m-%d")
            self._playtime_daily.setdefault(day, {})[name] = (
                self._playtime_daily.setdefault(day, {}).get(name, 0) + secs
            )
            self._save_playtime_state()

    @staticmethod
    def _fmt_playtime(secs: int) -> str:
        """秒 -> 人类可读时长（如 1小时23分 / 45分 / 30秒）。"""
        if secs < 0:
            secs = 0
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        parts = []
        if h:
            parts.append(f"{h}小时")
        if m:
            parts.append(f"{m}分")
        if s or not parts:
            parts.append(f"{s}秒")
        return "".join(parts)

    def _daily_playtime_text(self, day: Optional[str] = None) -> str:
        """生成某天的玩家在线时长汇总文本（默认今天）。"""
        if day is None:
            day = datetime.now().strftime("%Y-%m-%d")
        dd = self._playtime_daily.get(day, {})
        if not dd:
            return f"📊 {day} 暂无玩家在线时长记录"
        # 加上仍未下线玩家的实时时长
        live = self._playtime_sessions
        lines = [f"📊 {day} 玩家在线时长："]
        for player in sorted(set(list(dd.keys()) + list(live.keys()))):
            secs = dd.get(player, 0)
            jt = live.get(player)
            if jt:
                secs += int((datetime.now() - jt).total_seconds())
            lines.append(f"  • {player}: {self._fmt_playtime(secs)}")
        return "\n".join(lines)

    @staticmethod
    def _parse_duration(s: str) -> Optional[timedelta]:
        """解析时长：30m / 2h / 90s / 1h30m，失败返回 None。"""
        s = (s or "").strip().lower()
        if not s:
            return None
        m = re.findall(r"(\d+)\s*(h|m|s)", s)
        if not m:
            return None
        mult = {"h": 3600, "m": 60, "s": 1}
        total = sum(int(v) * mult[u] for v, u in m)
        return timedelta(seconds=total) if total > 0 else None

    @staticmethod
    def _parse_clock(s: str) -> Optional[_dtime]:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", (s or "").strip())
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        return _dtime(h, mi)

    @staticmethod
    def _next_datetime(t: _dtime) -> datetime:
        now = datetime.now()
        cand = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand

    @staticmethod
    def _fmt_duration(td: timedelta) -> str:
        secs = int(td.total_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h and m:
            return f"{h} 小时 {m} 分钟"
        if h:
            return f"{h} 小时"
        if m:
            return f"{m} 分钟"
        return f"{s} 秒"

    def _set_schedule(self, s: str, persist: bool = True) -> bool:
        """设置每日定时静默时段（HH:MM-HH:MM），空/off 关闭。返回是否成功。"""
        s = (s or "").strip().lower()
        if s in ("", "off", "none", "关闭"):
            self._silent_schedule_start = None
            self._silent_schedule_end = None
        else:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", s)
            if not m:
                return False
            sh, sm, eh, em = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            if sh > 23 or sm > 59 or eh > 23 or em > 59:
                return False
            self._silent_schedule_start = _dtime(sh, sm)
            self._silent_schedule_end = _dtime(eh, em)
        if persist:
            self._save_silent_state()
        return True

    def _in_schedule(self, now: datetime) -> bool:
        if self._silent_schedule_start is None or self._silent_schedule_end is None:
            return False
        cur = now.time()
        st, en = self._silent_schedule_start, self._silent_schedule_end
        if st <= en:
            return st <= cur < en
        return cur >= st or cur < en  # 跨午夜，如 23:00-08:00

    def _buffer_silent(self, text: str) -> None:
        """静默期间缓存一条 MC→QQ 动态（带时间戳），待静默结束后回放。"""
        self._silent_buffer.append({"ts": datetime.now(), "text": text})
        self._silent_prev_active = True
        if len(self._silent_buffer) > 300:
            self._silent_buffer.pop(0)  # 极端情况下防止无限增长

    async def _flush_silent_buffer(self) -> None:
        """把静默期间缓存的动态以聊天记录形式一次性回放到转发目标。"""
        if not self._silent_buffer:
            return
        target = self._bridge_target or self.bridge_target_session
        entries = self._silent_buffer
        self._silent_buffer = []
        if not target:
            self.logger.info("静默缓冲因无转发目标而丢弃")
            return
        first, last = entries[0]["ts"], entries[-1]["ts"]
        lines = [
            f"📥 静默期间收集的动态（共 {len(entries)} 条，"
            f"{first.strftime('%H:%M')}-{last.strftime('%H:%M')}）："
        ]
        for e in entries:
            lines.append(f"[{e['ts'].strftime('%H:%M:%S')}] {e['text']}")
        try:
            await self.context.send_message(target, MessageChain([Plain("\n".join(lines))]))
        except Exception as e:
            self.logger.error(f"静默缓冲回放失败: {e}")

    def _is_silent(self) -> bool:
        now = datetime.now()
        if self._silent_manual:
            return True
        if self._silent_until is not None and now < self._silent_until:
            return True
        if self._in_schedule(now):
            return True
        return False

    def _silent_status(self) -> str:
        lines = ["🔇 静默模式状态："]
        if self._silent_manual:
            lines.append("  • 持续静默中（直到 /mc silent off）")
        elif self._silent_until is not None and datetime.now() < self._silent_until:
            left = self._silent_until - datetime.now()
            lines.append(
                f"  • 静默中，剩余 {self._fmt_duration(left)}"
                f"（至 {self._silent_until.strftime('%H:%M:%S')}）"
            )
        else:
            lines.append("  • 当前未静默")
        if self._silent_schedule_start is not None:
            lines.append(
                f"  • 每日定时：{self._silent_schedule_start.strftime('%H:%M')}"
                f"-{self._silent_schedule_end.strftime('%H:%M')}"
            )
        else:
            lines.append("  • 每日定时：未设置")
        lines.append("")
        lines.append("用法：")
        lines.append("  /mc silent on [时长]     - 持续/临时静默（如 on 30m / on 2h）")
        lines.append("  /mc silent until HH:MM  - 静默到指定时间（自定义时段）")
        lines.append("  /mc silent off          - 取消静默")
        lines.append("  /mc silent schedule HH:MM-HH:MM - 每日定时（如 23:00-08:00）")
        lines.append("  /mc silent schedule off - 关闭每日定时")
        lines.append("")
        lines.append("💡 静默期间 MC→QQ 的聊天/进出/死亡/成就等动态会缓存，")
        lines.append("   静默结束后以「聊天记录」形式一次性回放（带时间戳）。")
        return "\n".join(lines)

    async def _send_silent_start_stats(self) -> None:
        """静默开始时向转发目标播报：累计聊天消息数 + TPS 采样均值。

        统计口径：从上次静默结束（或插件启动）到本次静默开始，即"白天"这段时间的
        聊天消息总数与后台定时采样的平均 TPS。发送后清零计数，作为下一个周期的起点。
        通过 _silent_stats_sent 保证一次静默只播报一次。
        """
        if self._silent_stats_sent:
            return
        self._silent_stats_sent = True
        target = self._bridge_target or self.bridge_target_session
        if not target:
            self.logger.info("静默开始统计：未设置转发目标，跳过播报")
            self._msg_count = 0
            self._tps_samples = []
            return

        avg_tps = None
        if self._tps_samples:
            avg_tps = sum(self._tps_samples) / len(self._tps_samples)
        tps_str = f"{avg_tps:.1f}" if avg_tps is not None else "暂无数据"

        lines = [
            "📊 静默开始，本周期统计：",
            f"  • 聊天消息：{self._msg_count} 条",
            f"  • 平均 TPS：{tps_str}",
        ]
        if self._tps_samples:
            lines.append(f"  （基于 {len(self._tps_samples)} 次采样）")
        # 附带当日玩家在线时长
        day = datetime.now().strftime("%Y-%m-%d")
        pt = self._daily_playtime_text(day)
        if "\n" in pt:
            lines = pt.splitlines() + [""] + lines
        try:
            await self.context.send_message(target, MessageChain([Plain("\n".join(lines))]))
        except Exception as e:
            self.logger.error(f"静默开始统计播报失败: {e}")

        # 播报后清零，作为下一周期起点
        self._msg_count = 0
        self._tps_samples = []

    async def _playtime(self, event: AstrMessageEvent, parts: list[str]) -> str:
        """查询玩家在线时长：/mc playtime [玩家] ；不带参数则列出当日全部玩家。"""
        if parts:
            # 指定玩家：查询其历史每天的在线时长（默认显示今天）
            player = " ".join(parts).strip()
            lines = [f"⏱ {player} 在线时长："]
            found = False
            for day in sorted(self._playtime_daily.keys(), reverse=True):
                secs = self._playtime_daily[day].get(player)
                if secs is not None:
                    lines.append(f"  • {day}: {self._fmt_playtime(secs)}")
                    found = True
            # 若当前在线，额外显示实时时长
            jt = self._playtime_sessions.get(player)
            if jt:
                cur = int((datetime.now() - jt).total_seconds())
                lines.append(f"  • 当前已在线: {self._fmt_playtime(cur)}")
                found = True
            if not found:
                return f"没有找到 {player} 的在线时长记录"
            return "\n".join(lines)
        # 不带参数：当日全部玩家汇总
        return self._daily_playtime_text()

    async def _silent(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return self._silent_status()
        sub = parts[0].lower()
        if sub == "off":
            self._silent_until = None
            self._silent_manual = False
            # 静默已结束，重置统计播报标记，供下次静默使用
            self._silent_stats_sent = False
            # 若每日定时仍在生效，则不回放（仍处静默）
            if self._is_silent():
                return "🔊 已关闭手动静默（每日定时仍生效），MC→QQ 转发将按定时恢复"
            await self._flush_silent_buffer()
            self._silent_prev_active = False
            return "🔊 已关闭静默模式，缓存的动态已回放，MC→QQ 转发恢复"
        if sub == "on":
            rest = " ".join(parts[1:])
            dur = self._parse_duration(rest)
            if dur is None:
                self._silent_manual = True
                self._silent_until = None
                await self._send_silent_start_stats()
                return "🔇 已开启静默模式（持续，直到 /mc silent off）"
            self._silent_until = datetime.now() + dur
            self._silent_manual = False
            await self._send_silent_start_stats()
            return (
                f"🔇 已开启静默模式，将持续 {self._fmt_duration(dur)}"
                f"（至 {self._silent_until.strftime('%H:%M:%S')}）"
            )
        if sub == "until":
            if len(parts) < 2:
                return "用法：/mc silent until <HH:MM>"
            t = self._parse_clock(parts[1])
            if t is None:
                return "时间格式错误，应为 HH:MM（如 23:00）"
            self._silent_until = self._next_datetime(t)
            self._silent_manual = False
            await self._send_silent_start_stats()
            return (
                f"🔇 已开启静默模式（自定义时段），直到 "
                f"{self._silent_until.strftime('%Y-%m-%d %H:%M')}"
            )
        if sub == "schedule":
            arg = parts[1] if len(parts) > 1 else ""
            if not self._set_schedule(arg):
                return "时段格式错误，应为 HH:MM-HH:MM（如 23:00-08:00）"
            if self._silent_schedule_start is None:
                return "⏰ 已关闭每日定时静默"
            # 到点进入静默时由消息兜底处播报统计（/mc silent 无 duration 分支不再立即播）
            return (
                f"⏰ 已设置每日定时静默："
                f"{self._silent_schedule_start.strftime('%H:%M')}"
                f"-{self._silent_schedule_end.strftime('%H:%M')}"
            )
        # 快捷：直接给时长 /mc silent 30m
        dur = self._parse_duration(sub)
        if dur is not None:
            self._silent_until = datetime.now() + dur
            self._silent_manual = False
            await self._send_silent_start_stats()
            return (
                f"🔇 已开启静默模式，将持续 {self._fmt_duration(dur)}"
                f"（至 {self._silent_until.strftime('%H:%M:%S')}）"
            )
        return self._silent_status()

    async def _ai(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not self.enable_llm:
            return "AI 功能未启用，请在插件设置中开启"
        if not parts:
            return "用法：/mc ai <你想对服务器做什么>"
        return await self._ai_execute(event, " ".join(parts))

    async def _backup(self, event: AstrMessageEvent, parts: list[str]) -> str:
        targets = [s for s in self.servers.values() if s.password]
        if not targets:
            return "没有配置了 RCON 密码的服务器"
        results = [
            f"[{srv.name}] {await self._rcon_zh('save-all', srv.name)}" for srv in targets
        ]
        return "💾 备份完成：\n" + "\n".join(results)

    async def _announce(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc announce <消息内容>"
        msg = _clean(" ".join(parts))
        targets = [s for s in self.servers.values() if s.password]
        for srv in targets:
            await self._rcon_zh(f"say [Bot公告] {msg}", srv.name)
        return f"📢 已向 {len(targets)} 个服务器发送公告"

    async def _batch(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if len(parts) < 2 or parts[0].lower() != "give":
            return "用法：/mc batch give <物品> [数量]"
        item = _clean(parts[1])
        count = parts[2] if len(parts) >= 3 and parts[2].isdigit() else "1"
        players = await self._players_online(self.default_server)
        if not players:
            return "当前没有在线玩家"
        # 复用单个连接批量发放
        cmds = [f"give {p} {item} {count}" for p in players]
        outputs = await self._rcon_multi(cmds)
        return f"📦 批量给予 {item} x{count}：\n" + "\n".join(
            f"{p}: {o}" for p, o in zip(players, outputs)
        )

    async def _bridge(self, event: AstrMessageEvent, parts: list[str]) -> str:
        qq_state = "未启用"
        if self.queqiao_enabled:
            qq_state = f"已连接 {self.queqiao_url}" if self._qq_connected else "连接中/重试中"
        if not parts:
            target = self._bridge_target or self.bridge_target_session or "（未设置）"
            listening = self._bridge_server is not None
            return (
                f"💬 MC <-> QQ 聊天桥接状态：\n"
                f"  启用: {self.enable_bridge}\n"
                f"  MC→QQ: {self.bridge_mc_to_qq}（QQ→MC: {self.bridge_qq_to_mc}）\n"
                f"  鹊桥WS: {qq_state}\n"
                f"  TCP监听(旧): {self.bridge_host}:{self.bridge_port}"
                f"{'（运行中）' if listening else '（未运行）'}\n"
                f"  转发目标: {target}\n"
                f"  静默模式: {'开启' if self._is_silent() else '关闭'}"
            )
        action = parts[0].lower()
        if action == "on":
            self.enable_bridge = True
            self._bridge_target = event.unified_msg_origin
            if self._bridge_server is None and not self.queqiao_enabled:
                ok = await self._start_bridge_listener()
                if not ok:
                    return (
                        f"❌ 桥接监听启动失败（{self.bridge_host}:{self.bridge_port} "
                        f"可能被占用），转发目标已记录"
                    )
            qq_desc = f"，鹊桥 {'已连接' if self._qq_connected else '连接中'}" if self.queqiao_enabled else ""
            return f"✅ 聊天桥接已开启，当前会话将作为转发目标{qq_desc}"
        if action == "off":
            self.enable_bridge = False
            await self._stop_bridge_listener()
            return "🛑 聊天桥接已关闭，监听已停止"
        if action == "status":
            return await self._bridge(event, [])
        return "用法：/mc bridge [on/off/status]"

    async def _status(self, event: AstrMessageEvent, parts: list[str]) -> str:
        return await self._get_status()

    async def _players(self, event: AstrMessageEvent, parts: list[str]) -> str:
        return await self._get_players()

    async def _version(self, event: AstrMessageEvent, parts: list[str]) -> str:
        return f"ℹ️ 服务器版本：\n{await self._rcon_zh('version')}"

    async def _whitelist(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return await self._get_whitelist()
        action = parts[0].lower()
        if action == "add" and len(parts) >= 2:
            return f"✅ {await self._rcon_zh(f'whitelist add {_clean(parts[1])}')}"
        if action == "remove" and len(parts) >= 2:
            return f"✅ {await self._rcon_zh(f'whitelist remove {_clean(parts[1])}')}"
        if action == "on":
            return f"✅ {await self._rcon_zh('whitelist on')}"
        if action == "off":
            return f"✅ {await self._rcon_zh('whitelist off')}"
        return "用法：/mc whitelist [add/remove/on/off] 或 /mc whitelist（查看）"

    async def _banlist(self, event: AstrMessageEvent, parts: list[str]) -> str:
        return await self._get_banlist()

    async def _op(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc op <玩家>（原版服务端无 op list 指令，请查看 ops.json）"
        if parts[0].lower() == "list":
            return "⚠️ 原版 Minecraft 不支持列出 OP，请在服务端查看 ops.json 文件"
        return f"✅ {await self._rcon_zh(f'op {_clean(parts[0])}')}"

    async def _deop(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc deop <玩家>"
        return f"✅ {await self._rcon_zh(f'deop {_clean(parts[0])}')}"

    async def _ban(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc ban <玩家> [原因]"
        target = _clean(parts[0])
        reason = _clean(" ".join(parts[1:])) or "违规操作"
        return f"🚫 {await self._rcon_zh(f'ban {target} {reason}')}"

    async def _unban(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc unban <玩家>"
        target = _clean(parts[0])
        resp = await self._rcon_zh(f"pardon {target}")
        return f"✅ 已解封 {target}\n{resp}"

    async def _kick(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc kick <玩家> [原因]"
        target = _clean(parts[0])
        reason = _clean(" ".join(parts[1:])) or "被管理员踢出"
        return f"👢 {await self._rcon_zh(f'kick {target} {reason}')}"

    async def _gamemode(self, event: AstrMessageEvent, parts: list[str]) -> str:
        # RCON 身份是控制台而非玩家，服务端无法把 gamemode 应用到
        # "执行者"身上，必须显式指定目标玩家
        if len(parts) < 2:
            return "用法：/mc gamemode <模式> <玩家>（RCON 为控制台身份，必须指定玩家）"
        mode = self._GAMEMODES.get(parts[0].lower())
        if mode is None:
            return "未知模式，请使用 survival / creative / adventure / spectator"
        target = _clean(parts[1])
        return f"🎮 {await self._rcon_zh(f'gamemode {mode} {target}')}"

    async def _time(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc time <set <数值>/day/night/query>"
        action = parts[0].lower()
        if action in ("day", "night", "noon", "midnight"):
            return f"🕐 {await self._rcon_zh(f'time set {action}')}"
        if action == "set" and len(parts) >= 2:
            return f"🕐 {await self._rcon_zh(f'time set {_clean(parts[1])}')}"
        if action == "query":
            return f"🕐 {await self._rcon_zh('time query daytime')}"
        return "用法：/mc time <set <数值>/day/night/query>"

    async def _weather(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            # 原版没有 weather query 指令，只能设置；duration 单位是 tick（1 秒 = 20 tick）
            return "用法：/mc weather <clear/rain/thunder> [持续时间(tick，1秒=20tick)]"
        action = parts[0].lower()
        if action == "query":
            return "⚠️ 原版服务端不支持查询天气，只能设置：/mc weather <clear/rain/thunder>"
        if action not in ("clear", "rain", "thunder"):
            return "未知天气，请使用 clear / rain / thunder"
        duration = parts[1] if len(parts) >= 2 and parts[1].isdigit() else ""
        return f"🌤️ {await self._rcon_zh(f'weather {action} {duration}'.rstrip())}"

    async def _difficulty(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc difficulty <peaceful/easy/normal/hard>"
        diff = self._DIFFICULTIES.get(parts[0].lower())
        if diff is None:
            return "未知难度，请使用 peaceful / easy / normal / hard"
        return f"⚔️ {await self._rcon_zh(f'difficulty {diff}')}"

    async def _save_all(self, event: AstrMessageEvent, parts: list[str]) -> str:
        return f"💾 {await self._rcon_zh('save-all')}"

    async def _stop(self, event: AstrMessageEvent, parts: list[str]) -> str:
        resp = await self._rcon_zh("stop")
        return f"🛑 服务器正在关闭：\n{resp}"

    async def _give(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if len(parts) < 2:
            return "用法：/mc give <玩家> <物品> [数量]"
        target, item = _clean(parts[0]), _clean(parts[1])
        count = parts[2] if len(parts) >= 3 and parts[2].isdigit() else "1"
        return f"📦 {await self._rcon_zh(f'give {target} {item} {count}')}"

    async def _tp(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if len(parts) < 2:
            return "用法：/mc tp <玩家> <目标玩家/坐标>"
        target = _clean(parts[0])
        coords = _clean(" ".join(parts[1:]))
        return f"🌀 {await self._rcon_zh(f'tp {target} {coords}')}"

    async def _kill(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc kill <玩家>"
        return f"💀 {await self._rcon_zh(f'kill {_clean(parts[0])}')}"

    async def _cmd(self, event: AstrMessageEvent, parts: list[str]) -> str:
        if not parts:
            return "用法：/mc cmd <Minecraft 指令>"
        cmd = _clean(" ".join(parts))
        return f"⚡ 执行: /{cmd}\n📤 返回:\n{await self._rcon_zh(cmd)}"

    # ------------------------------------------------------------------
    # AI 自然语言控制
    # ------------------------------------------------------------------
    @filter.llm_tool(name="mc_rcon_command")
    async def mc_rcon_command(self, event: AstrMessageEvent, command: str):
        """在 Minecraft 服务器上执行一条 RCON 指令（作用于默认服务器）。

        Args:
            command(string): 要执行的 Minecraft 服务端指令，例如 "list"、"time set day"、"whitelist list"。
        """
        # 安全检查：该工具可执行任意服务端指令（含 stop/op/ban），
        # 必须与 /mc 指令走同样的管理员门槛，防止普通用户借 AI 越权
        if self.admin_only and not event.is_admin():
            return "⛔ 仅管理员可通过 AI 工具执行 RCON 指令"
        srv, err = self._rcon_ready()
        if srv is None:
            return err
        # 保持原始英文输出给 LLM（供其理解服务端状态），解析类逻辑不受影响
        return await self._rcon(command)

    async def _ai_execute(self, event: AstrMessageEvent, query: str) -> str:
        srv, err = self._rcon_ready()
        if srv is None:
            return err

        system_prompt = (
            "你是一个 Minecraft 服务器管理助手。根据用户的自然语言请求，"
            "生成最合适的一条 Minecraft 服务端指令。只输出指令本身，不要解释，"
            "不要带 / 前缀，不要包含换行。如果请求无法对应任何指令，只输出: 无法理解"
        )
        user_prompt = f"服务器: {self.default_server}\n用户请求: {query}\n请输出指令:"
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=system_prompt,
                prompt=user_prompt,
            )
            cmd = " ".join((resp.completion_text or "").strip().strip("`").split())
        except Exception as e:
            self.logger.error(f"AI 生成指令失败，改用关键词匹配: {e}")
            cmd = self._simple_nlp(query)

        if not cmd or "无法理解" in cmd:
            return "🤔 我不确定该怎么执行，请尝试更具体的描述"

        result = await self._rcon_zh(cmd)
        return f"🤖 AI 执行: /{cmd}\n📤 返回:\n{result}"

    def _simple_nlp(self, query: str) -> str:
        q = query.lower()
        if "几点" in q or "时间" in q:
            return "time query daytime"
        if "难度" in q:
            return "difficulty"
        if "玩家" in q or "在线" in q:
            return "list"
        if "备份" in q:
            return "save-all"
        if "公告" in q or "say" in q:
            return f"say [Bot] {_clean(query)}"
        return ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件激活时按需启动后台任务。"""
        if self.auto_backup and self.backup_interval > 0:
            self._spawn(self._backup_loop())
        if self.enable_announce and self.announcements and self.announce_interval > 0:
            self._spawn(self._announce_loop())
        if self.enable_tracker and any(s.password for s in self.servers.values()):
            self._spawn(self._player_tracker_loop())
        else:
            self.logger.info(
                "玩家追踪未启用（enable_player_tracker=false 或无可轮询服务器），跳过"
            )
        if self.enable_bridge:
            await self._start_bridge_listener()
        if self.queqiao_enabled and self.queqiao_url:
            self._spawn(self._queqiao_loop())
            self.logger.info(f"鹊桥对接已启用，正在连接 {self.queqiao_url}")
        elif self.queqiao_enabled:
            self.logger.warning("queqiao_enabled=true 但未配置 queqiao_ws_url，跳过")
        # TPS 采样：用于静默开始时播报平均 TPS（需启用性能监控且服务端支持 tps 指令）
        if self.enable_monitor:
            self._spawn(self._tps_sample_loop())
        self.logger.info(
            f"Minecraft 插件初始化完成：{len(self.servers)} 个服务器，默认 {self.default_server or '无'}"
        )

    async def terminate(self) -> None:
        """插件卸载时取消全部后台任务并关闭桥接监听。"""
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        await self._stop_bridge_listener()
        await self.stop_queqiao()
        self.logger.info("Minecraft 插件已卸载，后台任务已停止")

    def _spawn(self, coro: Any) -> None:
        self._tasks.append(asyncio.create_task(coro))
