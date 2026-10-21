"""
AstrBot 网站连通性检查插件 v2.0.2
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
import json
from collections.abc import Mapping
from copy import deepcopy
from functools import wraps
from pathlib import Path
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
_conf_schema = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# 默认值常量
# ---------------------------------------------------------------------------
_DEFAULT_TIMEOUT: int = _conf_schema["check_timeout"]["default"]
_DEFAULT_RETRY: int = _conf_schema["retry_count"]["default"]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """用户可修正的配置错误。"""


class CommandError(ValueError):
    """命令目标数量等输入边界错误。"""


def _split_messages(text: str):
    pending = ""
    for line in text.splitlines(keepends=True):
        while line:
            space = 1800 - len(pending)
            pending += line[:space]
            line = line[space:]
            if len(pending) == 1800:
                yield pending
                pending = ""
    if pending:
        yield pending


def _command_output(function):
    @wraps(function)
    async def wrapped(self, event, *args, **kwargs):
        try:
            self._ensure_running()
            self._validated_config()
            async for text in function(self, event, *args, **kwargs):
                for part in _split_messages(text):
                    yield event.plain_result(part)
        except (ConfigError, CommandError) as exc:
            yield event.plain_result(str(exc))
    return wrapped


def _short_err(exc: Exception, max_len: int = 160) -> str:
    """底层异常可能带凭据，用户只看到异常类别。"""
    return type(exc).__name__[:max_len]


def _display_url(url: str) -> str:
    try:
        parsed = urlsplit(_normalize_url(url))
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
    retriable: bool = False

    @property
    def received_http(self) -> bool:
        return self.status_code != 0

    @property
    def is_accessible(self) -> bool:
        """兼容既有 HTTP 连通性语义，不用于成功统计。"""
        return self.received_http and not self.error

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    @property
    def category(self) -> str:
        if self.error_kind in ("blocked", "invalid"):
            return self.error_kind
        if self.error or not self.received_http:
            return "network_error"
        if 200 <= self.status_code < 300:
            return "success"
        if self.is_redirect:
            return "redirect"
        return "http_error"

    @property
    def status_label(self) -> str:
        return {"success": "正常响应", "redirect": "重定向", "http_error": "HTTP 错误",
                "network_error": "网络失败", "blocked": "安全拦截", "invalid": "输入错误"}[self.category]

    @property
    def status_emoji(self) -> str:
        return {"success": "✅", "redirect": "↪️", "http_error": "⚠️",
                "network_error": "🔌", "blocked": "⛔", "invalid": "✏️"}[self.category]

    def format_brief(self) -> str:
        notes = " [HTTPS 证书未验证]" if not self.tls_verified else ""
        notes += " [正文截断]" if self.content_truncated else ""
        notes += f" [重试 {self.retries} 次]" if self.retries else ""
        description = self.error if self.error else f"HTTP {self.status_code}  {self.response_time_ms:.0f}ms"
        return f"{self.status_emoji} {self.hostname} [{self.status_label}] {description}{notes}"

    def format_detail(self) -> str:
        lines = [f"{self.status_emoji} **{self.hostname}** {self.status_label}"]
        if self.received_http:
            lines.extend([f"  状态码：{self.status_code}",
                          f"  收到响应头耗时：{self.response_time_ms:.0f} ms"])
        if self.error:
            lines.append(f"  错误：{self.error}")
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
    verify_ssl: bool = _conf_schema["verify_ssl"]["default"],
    *,
    result: Optional[CheckResult] = None,
    started_at: Optional[float] = None,
) -> CheckResult:
    if result is None:
        result = CheckResult(url=url, hostname="无效地址", tls_verified=verify_ssl)
    start = time.monotonic() if started_at is None else started_at
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
                result.status_code = resp.status
                result.response_time_ms = (time.monotonic() - start) * 1000
                try:
                    declared = int(resp.headers.get("Content-Length", ""))
                    result.declared_length = declared if declared >= 0 else None
                except (ValueError, TypeError):
                    result.declared_length = None
                if method == "HEAD" and resp.status == 405:
                    method = "GET"
                    continue
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
                    while result.content_length < 65536:
                        chunk = await resp.content.read(min(8192, 65536 - result.content_length))
                        if not chunk:
                            break
                        result.content_length += len(chunk)
                    result.content_truncated = result.content_length == 65536 and not resp.content.at_eof()
                break
    except aiohttp.ClientSSLError:
        result.error_kind = "tls"
        result.error = "HTTPS 证书或 TLS 验证失败"
    except ValueError:
        result.error_kind = "invalid"
        result.error = "输入错误：网址或重定向地址不符合要求"
    except UnsafeTargetError:
        result.error_kind = "blocked"
        result.error = "安全拦截：目标地址不属于允许的公网范围"
    except asyncio.TimeoutError:
        result.retriable = True
        result.error_kind = "network"
        result.error = f"连接超时（总期限 {timeout:g}s）"
    except aiohttp.ClientConnectionError as exc:
        result.retriable = not isinstance(exc, aiohttp.ClientConnectorDNSError)
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
    use_head: bool = _conf_schema["use_head"]["default"],
    follow_redirects: bool = _conf_schema["follow_redirects"]["default"],
    retry: int = _DEFAULT_RETRY,
    verify_ssl: bool = _conf_schema["verify_ssl"]["default"],
) -> CheckResult:
    """一个总期限覆盖解析、请求、回退、重定向、正文及重试等待。"""
    started = time.monotonic()
    last = CheckResult(url, "无效地址", tls_verified=verify_ssl)

    async def run() -> CheckResult:
        nonlocal last
        for attempt in range(retry + 1):
            before_attempt = time.monotonic() - started
            remaining = max(0.001, timeout - before_attempt)
            last = CheckResult(url, "无效地址", tls_verified=verify_ssl, retries=attempt)
            await _check_once(session, url, remaining, use_head, follow_redirects, verify_ssl,
                              result=last, started_at=started)
            if not last.error or not last.retriable or attempt == retry:
                return last
            await asyncio.sleep(0.5 * (2 ** attempt))
        return last

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except asyncio.TimeoutError:
        last.error_kind = "network"
        last.error = f"检查超时（总期限 {timeout:g}s）"
        last.retriable = False
        if last.hostname == "无效地址":
            try:
                last.hostname = URL(_normalize_url(url)).host or ""
            except ValueError:
                pass
        return last


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_web_check",
    "AceSalm0n",
    "检查网站在当前网络环境下能否被正常访问，支持单站点、多站点和批量检查",
    "2.0.2",
    "https://github.com/AceSalm0n/astrbot_plugin_web_check",
)
class WebCheckPlugin(Star):
    """网站连通性检查插件 v2"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._limiter = None
        self._concurrency_limit = None
        self._runs = 0
        self._pending_targets = 0
        self._stopping = False
        self._tasks = set()
        self._terminate_lock = asyncio.Lock()
        logger.info("[WebCheck] 插件已加载 v2.0.2")

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _validated_config(self) -> dict:
        if not isinstance(self.config, Mapping):
            raise ConfigError("配置错误：插件配置必须是对象")
        values = {key: self.config.get(key, deepcopy(spec["default"])) for key, spec in _conf_schema.items()}
        bounds = {"check_timeout": (1, 120), "max_concurrent": (1, 20),
                  "retry_count": (0, 3), "max_sites": (1, 100),
                  "max_pending_targets": (1, 1000), "queue_timeout": (1, 120)}
        for key, (low, high) in bounds.items():
            value = values[key]
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"配置错误：{key} 必须为 {low}–{high} 的整数")
        for key in ("use_head", "follow_redirects", "verify_ssl"):
            if type(values[key]) is not bool:
                raise ConfigError(f"配置错误：{key} 必须为布尔值")
        if not isinstance(values["watchlist"], list):
            raise ConfigError("配置错误：watchlist 必须为列表")
        agent = values["user_agent"]
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in agent):
            raise ConfigError("配置错误：user_agent 必须是 1–256 字符且无控制字符的文本")
        return values

    def _cfg(self, key: str):
        return self._validated_config()[key]

    def _limit_targets(self, targets):
        maximum = self._cfg("max_sites")
        if len(targets) > maximum:
            raise CommandError(f"检查被拒绝：{len(targets)} 个目标超过上限 {maximum}，请拆分检查或调整 max_sites。")

    # ------------------------------------------------------------------
    # HTTP 会话（懒创建 + 复用）
    # ------------------------------------------------------------------

    def _ensure_running(self):
        if self._stopping:
            raise CommandError("插件已停止，无法执行检查；请重新加载插件。")

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            self._ensure_running()
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    headers={"User-Agent": self._cfg("user_agent")},
                    connector=SafeTCPConnector(), cookie_jar=aiohttp.DummyCookieJar(),
                )
            return self._session

    async def terminate(self):
        """停止新检查，取消并等待本插件的检查任务，然后关闭会话。"""
        async with self._terminate_lock:
            async with self._session_lock:
                self._stopping = True
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            async with self._session_lock:
                if self._session is not None:
                    await self._session.close()
                    self._session = None
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

    def _make_limiter(self) -> asyncio.Queue:
        limit = self._cfg("max_concurrent")
        if self._concurrency_limit != limit:
            if self._runs:
                raise ConfigError("配置错误：max_concurrent 已变更，请等待在途检查结束后重试。")
            self._limiter = asyncio.Queue(maxsize=limit)
            for _ in range(limit):
                self._limiter.put_nowait(None)
            self._concurrency_limit = limit
        return self._limiter

    async def _collect_results(self, raw_list: List) -> List[CheckResult]:
        self._ensure_running()
        self._limit_targets(raw_list)
        limiter = self._make_limiter()
        capacity = self._cfg("max_pending_targets")
        if self._pending_targets + len(raw_list) > capacity:
            raise CommandError(f"检查繁忙：待处理目标容量 {capacity} 已不足，本批未开始，请稍后重试。")
        queue_timeout = self._cfg("queue_timeout")

        async def bounded(raw) -> CheckResult:
            try:
                url = _normalize_url(raw)
            except ValueError as exc:
                return CheckResult("", "无效地址", error=str(exc), error_kind="invalid")
            acquisition = asyncio.create_task(limiter.get())
            try:
                try:
                    await asyncio.wait_for(acquisition, timeout=queue_timeout)
                except asyncio.TimeoutError:
                    return CheckResult(url, URL(url).host or "", error_kind="queue",
                                       error=f"排队超时（等待上限 {queue_timeout}s），请稍后重试")
                self._ensure_running()
                return await self._do_check(url)
            finally:
                # A completed acquisition owns a token even if cancellation won the await.
                acquisition.cancel()
                try:
                    await acquisition
                except asyncio.CancelledError:
                    pass
                else:
                    limiter.put_nowait(None)

        async def run():
            children = [asyncio.create_task(bounded(raw)) for raw in raw_list]
            try:
                return list(await asyncio.gather(*children))
            finally:
                for child in children:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)

        self._runs += 1
        self._pending_targets += len(raw_list)
        task = asyncio.create_task(run(), name="webcheck-batch")
        self._tasks.add(task)
        try:
            return await task
        finally:
            self._tasks.discard(task)
            self._runs -= 1
            self._pending_targets -= len(raw_list)

    def _format_results(self, results: List[CheckResult]) -> str:
        if len(results) == 1:
            return results[0].format_detail()
        counts = {key: 0 for key in ("success", "redirect", "http_error", "network_error", "blocked", "invalid")}
        for result in results:
            counts[result.category] += 1
        received = [r for r in results if r.received_http]
        lines = ["📊 检查结果", f"总计：{len(results)}", f"HTTP 有响应：{len(received)}",
                 f"✅ 正常响应：{counts['success']}", f"↪️ 重定向：{counts['redirect']}",
                 f"⚠️ HTTP 错误：{counts['http_error']}", f"🔌 网络失败：{counts['network_error']}",
                 f"⛔ 安全拦截：{counts['blocked']}", f"✏️ 输入错误：{counts['invalid']}"]
        if received:
            average = sum(r.response_time_ms for r in received) / len(received)
            lines.append(f"收到响应头平均耗时：{average:.0f} ms")
        lines.append("")
        lines.extend(r.format_brief() for r in sorted(results, key=lambda r: r.category == "success"))
        return "\n".join(lines)

    @filter.command("check")
    @_command_output
    async def cmd_check(self, event: AstrMessageEvent, url: GreedyStr):
        """检查一个或多个 URL。"""
        raw_parts = url.split()
        if not raw_parts:
            yield ("📌 用法：/check <url> [url2 ...]\n示例：/check baidu.com github.com\n唤醒前缀以后台配置为准。")
            return
        self._limit_targets(raw_parts)
        label = _display_url(raw_parts[0]) if len(raw_parts) == 1 else f"{len(raw_parts)} 个站点"
        yield (f"🔍 正在检查 {label} ...")
        results = await self._collect_results(raw_parts)
        yield (self._format_results(results))

    @filter.command("批量检查")
    @_command_output
    async def cmd_batch_check(self, event: AstrMessageEvent):
        """批量检查配置的 watchlist。"""
        watchlist = self._cfg("watchlist")
        if not watchlist:
            yield ("⚠ 未配置站点列表，请在插件配置中设置 watchlist。")
            return
        self._limit_targets(watchlist)
        yield (f"🔍 正在检查 {len(watchlist)} 个站点...")
        results = await self._collect_results(watchlist)
        yield (self._format_results(results))

    @filter.command("check列表")
    @_command_output
    async def cmd_list_watchlist(self, event: AstrMessageEvent):
        """查看 watchlist 中配置的所有站点"""
        watchlist = self._cfg("watchlist")
        if not watchlist:
            yield (
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
        yield ("\n".join(lines))

    # ------------------------------------------------------------------
    # /check状态
    # ------------------------------------------------------------------

    @filter.command("check状态")
    @_command_output
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件当前配置和运行状态"""
        session_ok = self._session is not None and not self._session.closed
        lines = [
            "🌐 **WebCheck 插件状态**\n",
            f"版本：2.0.2",
            f"超时：{self._cfg('check_timeout')}s",
            f"最大并发：{self._cfg('max_concurrent')}（插件共享）",
            f"每次目标上限：{self._cfg('max_sites')}",
            f"在途批次预留目标：{self._pending_targets}/{self._cfg('max_pending_targets')}",
            f"排队等待上限：{self._cfg('queue_timeout')}s",
            f"失败重试：{self._cfg('retry_count')} 次",
            f"HEAD 优先：{'是' if self._cfg('use_head') else '否'}",
            f"跟随重定向：{'是' if self._cfg('follow_redirects') else '否'}",
            f"Watchlist：{len(self._cfg('watchlist'))} 个站点",
            f"HTTP 会话：{'✅ 活跃' if session_ok else '⏹ 未创建'}",
        ]
        yield ("\n".join(lines))

    # ------------------------------------------------------------------
    # /check帮助
    # ------------------------------------------------------------------

    @filter.command("check帮助")
    @_command_output
    async def cmd_help(self, event: AstrMessageEvent):
        """显示所有可用命令及说明"""
        yield (
            "🌐 **WebCheck 帮助**\n\n"
            "/check <url> [url2 ...]   检查一个或多个网址\n"
            "/批量检查                  检查 watchlist 中的所有站点\n"
            "/check列表                查看 watchlist 内容\n"
            "/check状态                查看插件配置和运行状态\n"
            "/check帮助                显示此帮助\n\n"
            "💡 以上 / 为示例唤醒前缀，实际以后台配置为准\n"
            "💡 URL 可省略 https://，插件会自动补全\n"
            "💡 批量检查的站点列表请在管理后台插件配置中设置"
        )
