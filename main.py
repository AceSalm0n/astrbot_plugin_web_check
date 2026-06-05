"""
AstrBot 网站连通性检查插件
检查指定网站在当前网络环境下能否被正常访问

功能：
- 单站点检查：/check <url>
- 批量检查配置的站点列表
- 内网地址 SSRF 防护
- 响应时间统计
"""

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


# ===========================================================================
# 数据类型
# ===========================================================================

@dataclass
class CheckResult:
    """单次网站检查结果"""
    url: str
    hostname: str
    status_code: int = 0
    response_time_ms: float = 0.0
    content_length: int = 0
    error: str = ""

    @property
    def is_accessible(self) -> bool:
        return self.status_code != 0 and self.error == ""

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    @property
    def status_emoji(self) -> str:
        if self.error:
            return "🔌"
        if self.status_code == 200:
            return "✅"
        if self.is_redirect:
            return "↗️"
        if 400 <= self.status_code < 500:
            return "⚠️"
        if 500 <= self.status_code < 600:
            return "❌"
        return "❓"

    def format_single(self) -> str:
        """格式化单条检查结果"""
        if self.error:
            return (
                f"{self.status_emoji} **{self.hostname}** 无法访问\n"
                f"　错误：{self.error}"
            )
        lines = [
            f"{self.status_emoji} **{self.hostname}** 可访问",
            f"　状态码：{self.status_code}",
            f"　响应时间：{self.response_time_ms:.0f} ms",
        ]
        if self.content_length > 0:
            length_str = self._format_size(self.content_length)
            lines.append(f"　内容大小：{length_str}")
        if self.status_code != 200:
            lines.append(f"　注意：非标准 200 响应")
        return "\n".join(lines)

    @staticmethod
    def _format_size(bytes_: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if bytes_ < 1024:
                return f"{bytes_:.1f}{unit}"
            bytes_ /= 1024
        return f"{bytes_:.1f}TB"


# ===========================================================================
# 工具函数
# ===========================================================================

def _is_private_url(url: str) -> bool:
    """检查 URL 是否指向内网地址，防止 SSRF 攻击"""
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    try:
        addrs = socket.getaddrinfo(hostname, None)
        for info in addrs:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                return True
    except socket.gaierror:
        return False
    return False


def _normalize_url(url: str) -> str:
    """补全 URL 协议头"""
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url


async def check_single_url(session: aiohttp.ClientSession, url: str,
                           timeout: int = 10) -> CheckResult:
    """检查单个 URL 的可访问性"""
    hostname = urlparse(url).hostname or url
    result = CheckResult(url=url, hostname=hostname)

    try:
        start = time.monotonic()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                               allow_redirects=False, ssl=False) as resp:
            elapsed = time.monotonic() - start
            result.status_code = resp.status
            result.response_time_ms = elapsed * 1000
            body = await resp.read()
            result.content_length = len(body)
    except asyncio.TimeoutError:
        result.error = f"连接超时（{timeout}秒）"
    except aiohttp.ClientConnectorError as e:
        result.error = f"连接失败：{e}"
    except aiohttp.ClientError as e:
        result.error = f"请求异常：{e}"
    except Exception as e:
        result.error = f"未知错误：{e}"

    return result


# ===========================================================================
# 插件主类
# ===========================================================================

@register(
    "astrbot_plugin_web_check",
    "AceSalm0n",
    "检查网站在当前网络环境下能否被正常访问，支持单站点和批量检查",
    "1.0.0",
    "",
)
class WebCheckPlugin(Star):
    """网站连通性检查插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else {}

        # HTTP 会话（复用连接）
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        logger.info("网站连通性检查插件已加载")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建复用的 HTTP 会话"""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(
                        total=self.config.get("check_timeout", 10)
                    )
                    self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def terminate(self):
        """插件卸载时关闭会话"""
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("网站连通性检查插件已卸载")

    # ------------------------------------------------------------------
    # 命令处理器
    # ------------------------------------------------------------------

    @filter.command("check")
    async def cmd_check(self, event: AstrMessageEvent, url: str = ""):
        """检查指定 URL 是否可访问。用法：/check <url>"""
        if not url:
            yield event.plain_result(
                "请提供要检查的网址。\n"
                "用法：`/check <url>`\n"
                "示例：`/check https://www.baidu.com`"
            )
            return

        url = _normalize_url(url)

        # SSRF 防护
        if _is_private_url(url):
            yield event.plain_result(
                "⛔ 出于安全原因，不允许检查内网/私有地址"
            )
            return

        yield event.plain_result(f"🔍 正在检查 {url} ...")

        try:
            session = await self._get_session()
            result = await check_single_url(
                session, url,
                timeout=self.config.get("check_timeout", 10)
            )
            yield event.plain_result(result.format_single())
        except Exception as e:
            logger.error(f"检查 URL 异常: {e}", exc_info=True)
            yield event.plain_result(f"🔌 检查失败：{str(e)}")

    @filter.command("批量检查")
    async def cmd_batch_check(self, event: AstrMessageEvent):
        """批量检查配置的站点列表"""
        watchlist = self.config.get("watchlist", [])
        if not watchlist:
            yield event.plain_result(
                "⚠️ 未配置批量检查站点列表。\n"
                "请在 AstrBot 管理后台 → 插件配置中设置 watchlist。"
            )
            return

        # 过滤掉内网地址
        safe_urls = []
        blocked = 0
        for entry in watchlist:
            u = _normalize_url(entry) if isinstance(entry, str) else ""
            if not u:
                continue
            if _is_private_url(u):
                blocked += 1
                continue
            safe_urls.append(u)

        total = len(watchlist)
        yield event.plain_result(
            f"🔍 正在批量检查 {len(safe_urls)}/{total} 个站点..."
            + (f"\n⛔ 已跳过 {blocked} 个内网地址" if blocked else "")
        )

        try:
            session = await self._get_session()
            timeout = self.config.get("check_timeout", 10)
            tasks = [check_single_url(session, u, timeout) for u in safe_urls]

            # 并发执行所有检查
            results = await asyncio.gather(*tasks)

            # 统计
            accessible = [r for r in results if r.is_accessible]
            failed = [r for r in results if r.error]

            lines = ["📊 **批量检查结果**\n"]
            lines.append(f"总计：{len(results)} 个站点")
            lines.append(f"✅ 可访问：{len(accessible)}")
            lines.append(f"🔌 不可访问：{len(failed)}")
            lines.append("")

            # 逐个展示结果
            for r in results:
                lines.append(r.format_single())
                lines.append("")

            yield event.plain_result("\n".join(lines).strip())
        except Exception as e:
            logger.error(f"批量检查异常: {e}", exc_info=True)
            yield event.plain_result(f"批量检查失败：{str(e)}")

    @filter.command("check列表")
    async def cmd_list_watchlist(self, event: AstrMessageEvent):
        """查看配置的检查列表"""
        watchlist = self.config.get("watchlist", [])
        if not watchlist:
            yield event.plain_result(
                "📋 当前未配置检查列表。\n"
                "请在 AstrBot 管理后台 → 插件配置中设置 watchlist 字段。"
            )
            return

        lines = ["📋 **当前检查列表**\n"]
        for i, entry in enumerate(watchlist, 1):
            u = _normalize_url(entry) if isinstance(entry, str) else str(entry)
            private = " ⛔" if _is_private_url(u) else ""
            lines.append(f"{i}. {u}{private}")
        lines.append("\n💡 使用 `/批量检查` 开始检查所有站点")

        yield event.plain_result("\n".join(lines))

    @filter.command("check状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件配置状态"""
        timeout = self.config.get("check_timeout", 10)
        watchlist = self.config.get("watchlist", [])
        session_active = self._session is not None and not self._session.closed

        lines = [
            "🌐 **网站连通性检查插件状态**\n",
            f"超时时间：{timeout} 秒",
            f"检查列表：{len(watchlist)} 个站点",
            f"HTTP 会话：{'✅ 活跃' if session_active else '⏹ 未创建'}",
        ]
        yield event.plain_result("\n".join(lines))