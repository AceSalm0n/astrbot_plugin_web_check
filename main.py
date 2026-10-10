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
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

from yarl import URL

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

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
    "verify_ssl": {"type": "bool", "default": True, "description": "验证 HTTPS 证书"},
    "follow_redirects": {
        "type": "bool",
        "default": False,
        "description": "是否跟随重定向（关闭时直接返回 3xx 状态码）",
    },
    "user_agent": {
        "type": "string",
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

def _short_err(exc: Exception, max_len: int = 160) -> str:
    """底层异常可能带凭据，用户只看到异常类别。"""
    return type(exc).__name__[:max_len]


def _display_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        query = urlencode([(key, "隐藏") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))[:200]
    except (ValueError, TypeError):
        return "无效地址"


def _fmt_size(b: float) -> str:
    """将字节数格式化为人类可读的大小字符串"""
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _normalize_url(raw: str) -> str:
    """校验 HTTP URL；仅缺失协议时补 HTTPS。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("输入错误：网址不能为空")
    raw = raw.strip()
    if any(ch.isspace() or ord(ch) < 32 for ch in raw) or "\\" in raw:
        raise ValueError("输入错误：网址包含无效字符")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("协议或主机无效")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("不接受含凭据的网址")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("端口范围无效")
        normalized = URL(raw).with_scheme(parsed.scheme.lower()).with_fragment(None)
        if not normalized.host:
            raise ValueError("缺少主机名")
        return str(normalized)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("输入错误：网址格式、协议或凭据不符合要求") from exc


def _is_reserved_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_reserved_ip(ip.ipv4_mapped)
    return not ip.is_global or ip.is_multicast or ip.is_reserved


class UnsafeTargetError(aiohttp.ClientError):
    """实际连接地址违反公网访问策略。"""


class SafeTCPConnector(aiohttp.TCPConnector):
    async def _resolve_host(self, host, port, traces=None):
        # 在缓存、IP 字面量和 DNS 解析的共同出口检查，原 hostname 不变。
        addresses = await super()._resolve_host(host, port, traces)
        if not addresses:
            raise OSError("DNS 未返回地址")
        for entry in addresses:
            if _is_reserved_ip(ipaddress.ip_address(entry["host"])):
                raise UnsafeTargetError("安全拦截：目标地址不属于允许的公网范围")
        return addresses


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
    error_kind: str = ""
    tls_verified: bool = True
    declared_length: Optional[int] = None
    content_truncated: bool = False

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
        retry_note += " [HTTPS 证书未验证]" if not self.tls_verified else ""
        retry_note += " [正文截断]" if self.content_truncated else ""
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
        if not self.tls_verified:
            lines.append("  ⚠ HTTPS 证书未验证")
        if self.declared_length is not None:
            lines.append(f"  服务端声明长度：{_fmt_size(self.declared_length)}")
        if self.content_length > 0:
            lines.append(f"  已读取正文：{_fmt_size(self.content_length)}")
        if self.content_truncated:
            lines.append("  正文已截断：达到 64 KiB 上限，未完整下载")
        if self.redirect_location:
            lines.append(f"  重定向至：{_display_url(self.redirect_location)}")
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
    timeout: float,
    use_head: bool,
    follow_redirects: bool,
    verify_ssl: bool = True,
) -> CheckResult:
    result = CheckResult(url=url, hostname="无效地址", tls_verified=verify_ssl)
    start = time.monotonic()
    try:
        current = _normalize_url(url)
        result.hostname = URL(current).host or ""
        method = "HEAD" if use_head else "GET"
        redirects = 0
        while True:
            async with session.request(
                method, current, timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False, ssl=verify_ssl, auto_decompress=False,
            ) as resp:
                if method == "HEAD" and resp.status == 405:
                    method = "GET"
                    continue
                result.status_code = resp.status
                result.response_time_ms = (time.monotonic() - start) * 1000
                try:
                    declared = int(resp.headers.get("Content-Length", ""))
                    result.declared_length = declared if declared >= 0 else None
                except (ValueError, TypeError):
                    result.declared_length = None
                location = resp.headers.get("Location", "")
                if resp.status in (301, 302, 303, 307, 308) and location:
                    target = _normalize_url(str(URL(current).join(URL(location))))
                    result.redirect_location = target
                    if follow_redirects:
                        if redirects >= 5:
                            result.error = "重定向超过 5 跳"
                            result.error_kind = "redirect"
                            break
                        redirects += 1
                        current = target
                        continue
                if method == "GET":
                    async for chunk in resp.content.iter_chunked(8192):
                        result.content_length += len(chunk)
                        if result.content_length >= 65536:
                            result.content_truncated = True
                            break
                break
    except ValueError:
        result.error_kind = "invalid"
        result.error = "输入错误：网址或重定向地址不符合要求"
    except UnsafeTargetError:
        result.error_kind = "blocked"
        result.error = "安全拦截：目标地址不属于允许的公网范围"
    except asyncio.TimeoutError:
        result.error_kind = "network"
        result.error = f"连接超时（总期限 {timeout:g}s）"
    except aiohttp.ClientSSLError:
        result.error_kind = "tls"
        result.error = "HTTPS 证书或 TLS 验证失败"
    except aiohttp.ClientConnectorError as exc:
        result.error_kind = "network"
        result.error = f"连接失败：{_short_err(exc)}"
    except aiohttp.ClientError as exc:
        result.error_kind = "network"
        result.error = f"请求异常：{_short_err(exc)}"
    except Exception as exc:
        result.error_kind = "internal"
        result.error = f"检查异常：{_short_err(exc)}"
    return result


async def check_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    use_head: bool = True,
    follow_redirects: bool = False,
    retry: int = _DEFAULT_RETRY,
    verify_ssl: bool = True,
) -> CheckResult:
    """
    带重试和指数退避的 URL 检查入口。
    每次失败后等待 0.5s × (attempt+1) 再重试。
    """
    result: Optional[CheckResult] = None
    for attempt in range(retry + 1):
        result = await _check_once(session, url, timeout, use_head, follow_redirects, verify_ssl)
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
                        headers={"User-Agent": self._cfg("user_agent")},
                        connector=SafeTCPConnector()
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
            verify_ssl=self._cfg("verify_ssl"),
        )

    def _make_semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(self._cfg("max_concurrent"))

    async def _collect_results(self, raw_list: List) -> List[CheckResult]:
        sem = self._make_semaphore()

        async def bounded(raw) -> CheckResult:
            try:
                url = _normalize_url(raw)
            except ValueError as exc:
                return CheckResult("", "无效地址", error=str(exc), error_kind="invalid")
            async with sem:
                return await self._do_check(url)

        return list(await asyncio.gather(*(bounded(raw) for raw in raw_list)))

    def _format_results(self, results: List[CheckResult]) -> str:
        if len(results) == 1:
            return results[0].format_detail()
        accessible = sum(r.is_accessible for r in results)
        lines = [f"📊 {accessible}/{len(results)} 个可访问", ""]
        lines.extend(r.format_brief() for r in sorted(results, key=lambda r: r.is_accessible))
        return "\n".join(lines)

    @filter.command("check")
    async def cmd_check(self, event: AstrMessageEvent, url: GreedyStr = ""):
        """检查一个或多个 URL。"""
        raw_parts = url.split()
        if not raw_parts:
            yield event.plain_result("📌 用法：/check <url> [url2 ...]\n示例：/check baidu.com github.com")
            return
        label = _display_url(raw_parts[0]) if len(raw_parts) == 1 else f"{len(raw_parts)} 个站点"
        yield event.plain_result(f"🔍 正在检查 {label} ...")
        results = await self._collect_results(raw_parts)
        yield event.plain_result(self._format_results(results))

    @filter.command("批量检查")
    async def cmd_batch_check(self, event: AstrMessageEvent):
        """批量检查配置的 watchlist。"""
        watchlist = self._cfg("watchlist")
        if not watchlist:
            yield event.plain_result("⚠ 未配置站点列表，请在插件配置中设置 watchlist。")
            return
        yield event.plain_result(f"🔍 正在检查 {len(watchlist)} 个站点...")
        results = await self._collect_results(watchlist)
        yield event.plain_result(self._format_results(results))

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
            try:
                u = _normalize_url(entry)
                lines.append(f"{i}. {_display_url(u)}")
            except ValueError:
                lines.append(f"{i}. 输入错误：无效地址")

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
