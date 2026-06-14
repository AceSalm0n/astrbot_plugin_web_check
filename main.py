"""
AstrBot 网站连通性检查插件 v2.0
检查指定网站在当前网络环境下能否被正常访问

命令：
  /check <url> [url2 ...]   单站点或多站点检查
  /批量检查                   检查配置的 watchlist
  /check列表                 查看 watchlist 内容
  /check状态                 查看插件运行状态
  /check帮助                 显示帮助信息
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# ---------------------------------------------------------------------------
# 配置 Schema（供 AstrBot 管理后台渲染配置表单）
# ---------------------------------------------------------------------------
_conf_schema = {
    "check_timeout": {
        "type": "int",
        "default": 10,
        "description": "HTTP 请求超时时间（秒）",
    },
    "max_concurrent": {
        "type": "int",
        "default": 5,
        "description": "批量检查时的最大并发请求数",
    },
    "retry_count": {
        "type": "int",
        "default": 1,
        "description": "请求失败后的重试次数（0 = 不重试）",
    },
    "use_head": {
        "type": "bool",
        "default": True,
        "description": "优先使用 HEAD 请求（不下载响应体，速度更快）",
    },
    "follow_redirects": {
        "type": "bool",
        "default": False,
        "description": "是否跟随重定向（关闭时直接返回 3xx 状态码）",
    },
    "user_agent": {
        "type": "str",
        "default": "Mozilla/5.0 (compatible; AstrBot-WebCheck/2.0)",
        "description": "发送 HTTP 请求时使用的 User-Agent",
    },
    "watchlist": {
        "type": "list",
        "default": [],
        "description": "批量检查的站点列表，每项一个 URL",
    },
}

# ---------------------------------------------------------------------------
# 默认值常量
# ---------------------------------------------------------------------------
_DEFAULT_TIMEOUT: int = 10
_DEFAULT_RETRY: int = 1


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _short_err(exc: Exception, max_len: int = 80) -> str:
    """将异常信息裁剪为可读的短字符串"""
    msg = str(exc)
    return (msg[:max_len] + "…") if len(msg) > max_len else msg


def _fmt_size(b: float) -> str:
    """将字节数格式化为人类可读的大小字符串"""
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _normalize_url(raw: str) -> str:
    """补全协议头并去除多余空白；默认使用 https://"""
    raw = raw.strip()
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raw = "https://" + raw
    return raw


def _is_reserved_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    判断 IP 是否属于保留/私有范围。
    覆盖：私有段、回环、链路本地、多播、未指定、保留段（IPv4）。
    """
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    # IPv4 额外保留段（169.254/10 已含在 is_private，此处兜底）
    if isinstance(ip, ipaddress.IPv4Address):
        try:
            return ip.is_reserved
        except AttributeError:
            pass
    return False


async def _is_private_url(url: str) -> bool:
    """
    SSRF 防护：解析 URL 的主机名，检测其是否解析到私有/保留地址。
    对 IPv4 和 IPv6 均有效。
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return True  # 无法解析 hostname → 拒绝

    # 主机名本身就是 IP 地址
    try:
        ip = ipaddress.ip_address(hostname)
        return _is_reserved_ip(ip)
    except ValueError:
        pass  # 不是 IP，继续 DNS 解析

    try:
        loop = asyncio.get_running_loop()
        addrs = await loop.getaddrinfo(hostname, None)
        for info in addrs:
            ip = ipaddress.ip_address(info[4][0])
            if _is_reserved_ip(ip):
                return True
    except OSError:
        # DNS 解析失败 —— 留给 HTTP 层自行报错，不在此拦截
        return False

    return False


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """单次网站检查结果"""
    url: str
    hostname: str
    status_code: int = 0
    response_time_ms: float = 0.0
    content_length: int = 0
    redirect_location: str = ""
    error: str = ""
    retries: int = 0

    @property
    def is_accessible(self) -> bool:
        return self.status_code != 0 and not self.error

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    @property
    def status_emoji(self) -> str:
        if self.error:
            return "🔴"
        if 200 <= self.status_code < 300:
            return "🟢"
        if self.is_redirect:
            return "🔀"
        if 400 <= self.status_code < 500:
            return "🟡"
        if 500 <= self.status_code < 600:
            return "🔴"
        return "⚪"

    def format_brief(self) -> str:
        """单行摘要，适合批量结果列表"""
        if self.error:
            return f"{self.status_emoji} {self.hostname}  ✗ {self.error}"
        retry_note = f" (重试{self.retries}次)" if self.retries else ""
        return (
            f"{self.status_emoji} {self.hostname}  "
            f"HTTP {self.status_code}  {self.response_time_ms:.0f}ms{retry_note}"
        )

    def format_detail(self) -> str:
        """多行详情，适合单站点查询"""
        if self.error:
            return (
                f"{self.status_emoji} **{self.hostname}** 无法访问\n"
                f"  错误：{self.error}"
            )
        lines = [
            f"{self.status_emoji} **{self.hostname}** 可访问",
            f"  状态码：{self.status_code}",
            f"  响应时间：{self.response_time_ms:.0f} ms",
        ]
        if self.content_length > 0:
            lines.append(f"  内容大小：{_fmt_size(self.content_length)}")
        if self.redirect_location:
            lines.append(f"  重定向至：{self.redirect_location}")
        if self.retries:
            lines.append(f"  重试次数：{self.retries}")
        if not (200 <= self.status_code < 300):
            lines.append("  ⚠ 非 2xx 响应")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 核心 HTTP 检查
# ---------------------------------------------------------------------------

async def _check_once(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    use_head: bool,
    follow_redirects: bool,
) -> CheckResult:
    """执行一次 HTTP 检查，不含重试逻辑"""
    hostname = urlparse(url).hostname or url
    result = CheckResult(url=url, hostname=hostname)
    _timeout = aiohttp.ClientTimeout(total=timeout)

    try:
        start = time.monotonic()

        # 优先 HEAD；若服务器返回 405 Method Not Allowed，自动降级为 GET
        method = "HEAD" if use_head else "GET"
        async with session.request(
            method, url,
            timeout=_timeout, allow_redirects=follow_redirects, ssl=False,
        ) as resp:
            elapsed = time.monotonic() - start

            if use_head and resp.status == 405:
                # 服务器不支持 HEAD，改用 GET
                async with session.get(
                    url,
                    timeout=_timeout, allow_redirects=follow_redirects, ssl=False,
                ) as resp2:
                    result.status_code = resp2.status
                    result.response_time_ms = (time.monotonic() - start) * 1000
                    result.content_length = len(await resp2.read())
                    if result.is_redirect:
                        result.redirect_location = resp2.headers.get("Location", "")
            else:
                result.status_code = resp.status
                result.response_time_ms = elapsed * 1000
                if not use_head:
                    result.content_length = len(await resp.read())
                if result.is_redirect:
                    result.redirect_location = resp.headers.get("Location", "")

    except asyncio.TimeoutError:
        result.error = f"超时（>{timeout}s）"
    except aiohttp.ClientSSLError as exc:
        result.error = f"SSL 错误：{_short_err(exc)}"
    except aiohttp.ClientConnectorError as exc:
        result.error = f"连接失败：{_short_err(exc)}"
    except aiohttp.ClientError as exc:
        result.error = f"请求异常：{_short_err(exc)}"
    except Exception as exc:
        result.error = f"未知错误：{_short_err(exc)}"

    return result


async def check_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    use_head: bool = True,
    follow_redirects: bool = False,
    retry: int = _DEFAULT_RETRY,
) -> CheckResult:
    """
    带重试和指数退避的 URL 检查入口。
    每次失败后等待 0.5s × (attempt+1) 再重试。
    """
    result: Optional[CheckResult] = None
    for attempt in range(retry + 1):
        result = await _check_once(session, url, timeout, use_head, follow_redirects)
        if not result.error or attempt >= retry:
            result.retries = attempt
            break
        await asyncio.sleep(0.5 * (attempt + 1))

    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_web_check",
    "AceSalm0n",
    "检查网站在当前网络环境下能否被正常访问，支持单站点、多站点和批量检查",
    "2.0.0",
    "https://github.com/AceSalm0n/astrbot_plugin_web_check",
)
class WebCheckPlugin(Star):
    """网站连通性检查插件 v2"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config: AstrBotConfig = config or {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        logger.info("[WebCheck] 插件已加载 v2.0.0")

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _cfg(self, key: str):
        """读取配置项，不存在时回退到 schema 默认值"""
        return self.config.get(key, _conf_schema[key]["default"])

    # ------------------------------------------------------------------
    # HTTP 会话（懒创建 + 复用）
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        headers={"User-Agent": self._cfg("user_agent")}
                    )
        return self._session

    async def terminate(self):
        """插件卸载时释放 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[WebCheck] 插件已卸载")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _do_check(self, url: str) -> CheckResult:
        """读取当前配置，执行带重试的 URL 检查"""
        session = await self._get_session()
        return await check_url(
            session,
            url,
            timeout=self._cfg("check_timeout"),
            use_head=self._cfg("use_head"),
            follow_redirects=self._cfg("follow_redirects"),
            retry=self._cfg("retry_count"),
        )

    async def _filter_safe_urls(
        self, raw_list: List
    ) -> Tuple[List[str], int]:
        """
        过滤 watchlist：规范化 URL 并去除内网地址。
        返回 (safe_urls, blocked_count)。
        """
        safe: List[str] = []
        blocked = 0
        for entry in raw_list:
            if not isinstance(entry, str) or not entry.strip():
                continue
            u = _normalize_url(entry)
            if await _is_private_url(u):
                blocked += 1
            else:
                safe.append(u)
        return safe, blocked

    def _make_semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(self._cfg("max_concurrent"))

    # ------------------------------------------------------------------
    # /check
    # ------------------------------------------------------------------

    @filter.command("check")
    async def cmd_check(self, event: AstrMessageEvent, url: str = ""):
        """
        检查一个或多个 URL 的连通性。
        用法：/check <url> [url2 url3 ...]
        示例：/check https://baidu.com github.com
        """
        raw_parts = url.split() if url.strip() else []

        if not raw_parts:
            yield event.plain_result(
                "📌 用法：/check <url> [url2 ...]\n"
                "示例：/check baidu.com github.com\n"
                "提示：可省略 https://，插件会自动补全"
            )
            return

        urls = [_normalize_url(u) for u in raw_parts]

        # SSRF 过滤
        safe_urls: List[str] = []
        blocked_count = 0
        for u in urls:
            if await _is_private_url(u):
                blocked_count += 1
            else:
                safe_urls.append(u)

        if not safe_urls:
            yield event.plain_result("⛔ 所有地址均为内网/私有地址，已拒绝访问")
            return

        if len(safe_urls) == 1:
            yield event.plain_result(f"🔍 正在检查 {safe_urls[0]} ...")
        else:
            yield event.plain_result(f"🔍 正在检查 {len(safe_urls)} 个站点...")

        sem = self._make_semaphore()

        async def bounded(u: str) -> CheckResult:
            async with sem:
                return await self._do_check(u)

        results = await asyncio.gather(*[bounded(u) for u in safe_urls])

        # 单站点 → 详情；多站点 → 摘要列表
        if len(results) == 1:
            output_lines = [results[0].format_detail()]
        else:
            accessible = sum(1 for r in results if r.is_accessible)
            avg_ms = (
                sum(r.response_time_ms for r in results if r.is_accessible) / accessible
                if accessible else 0.0
            )
            summary = (
                f"📊 {accessible}/{len(results)} 个可访问"
                + (f"  平均响应 {avg_ms:.0f}ms" if accessible else "")
            )
            output_lines = [summary, ""]
            # 失败的置前，便于发现问题
            for r in sorted(results, key=lambda r: r.is_accessible):
                output_lines.append(r.format_brief())

        if blocked_count:
            output_lines.append(f"\n⛔ 已拦截 {blocked_count} 个内网地址")

        yield event.plain_result("\n".join(output_lines))

    # ------------------------------------------------------------------
    # /批量检查
    # ------------------------------------------------------------------

    @filter.command("批量检查")
    async def cmd_batch_check(self, event: AstrMessageEvent):
        """批量检查 watchlist 中配置的所有站点"""
        watchlist = self._cfg("watchlist")
        if not watchlist:
            yield event.plain_result(
                "⚠ 未配置站点列表。\n"
                "请在 AstrBot 管理后台 → 插件配置 → watchlist 中添加站点，\n"
                "然后使用 /批量检查 执行检查。"
            )
            return

        safe_urls, blocked = await self._filter_safe_urls(watchlist)
        if not safe_urls:
            yield event.plain_result("⛔ watchlist 中所有地址均为内网地址，已全部拦截。")
            return

        note = f"\n⛔ 已跳过 {blocked} 个内网地址" if blocked else ""
        yield event.plain_result(
            f"🔍 正在检查 {len(safe_urls)}/{len(watchlist)} 个站点...{note}"
        )

        sem = self._make_semaphore()

        async def bounded(u: str) -> CheckResult:
            async with sem:
                return await self._do_check(u)

        results = list(await asyncio.gather(*[bounded(u) for u in safe_urls]))

        accessible = [r for r in results if r.is_accessible]
        failed = [r for r in results if not r.is_accessible]
        avg_ms = (
            sum(r.response_time_ms for r in accessible) / len(accessible)
            if accessible else 0.0
        )

        lines = [
            "📊 **批量检查结果**",
            (
                f"总计 {len(results)}  ✅ {len(accessible)}  🔴 {len(failed)}"
                + (f"  平均响应 {avg_ms:.0f}ms" if accessible else "")
            ),
            "",
        ]
        # 失败的置前
        for r in (failed + accessible):
            lines.append(r.format_brief())

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # /check列表
    # ------------------------------------------------------------------

    @filter.command("check列表")
    async def cmd_list_watchlist(self, event: AstrMessageEvent):
        """查看 watchlist 中配置的所有站点"""
        watchlist = self._cfg("watchlist")
        if not watchlist:
            yield event.plain_result(
                "📋 当前 watchlist 为空。\n"
                "请在插件配置中设置 watchlist 字段，每项填写一个 URL。"
            )
            return

        lines = ["📋 **Watchlist**\n"]
        for i, entry in enumerate(watchlist, 1):
            if not isinstance(entry, str) or not entry.strip():
                continue
            u = _normalize_url(entry)
            tag = " ⛔ 内网" if await _is_private_url(u) else ""
            lines.append(f"{i}. {u}{tag}")

        lines.append("\n💡 使用 /批量检查 开始检查所有站点")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # /check状态
    # ------------------------------------------------------------------

    @filter.command("check状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件当前配置和运行状态"""
        session_ok = self._session is not None and not self._session.closed
        lines = [
            "🌐 **WebCheck 插件状态**\n",
            f"版本：2.0.0",
            f"超时：{self._cfg('check_timeout')}s",
            f"最大并发：{self._cfg('max_concurrent')}",
            f"失败重试：{self._cfg('retry_count')} 次",
            f"HEAD 优先：{'是' if self._cfg('use_head') else '否'}",
            f"跟随重定向：{'是' if self._cfg('follow_redirects') else '否'}",
            f"Watchlist：{len(self._cfg('watchlist'))} 个站点",
            f"HTTP 会话：{'✅ 活跃' if session_ok else '⏹ 未创建'}",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # /check帮助
    # ------------------------------------------------------------------

    @filter.command("check帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示所有可用命令及说明"""
        yield event.plain_result(
            "🌐 **WebCheck 帮助**\n\n"
            "/check <url> [url2 ...]   检查一个或多个网址\n"
            "/批量检查                  检查 watchlist 中的所有站点\n"
            "/check列表                查看 watchlist 内容\n"
            "/check状态                查看插件配置和运行状态\n"
            "/check帮助                显示此帮助\n\n"
            "💡 URL 可省略 https://，插件会自动补全\n"
            "💡 批量检查的站点列表请在管理后台插件配置中设置"
        )
