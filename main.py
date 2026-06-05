from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import asyncio
import httpx
import ipaddress
import socket
from urllib.parse import urlparse


def _is_private_url(url: str) -> bool:
    """检查 URL 是否指向内网地址，防止 SSRF 攻击"""
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    try:
        # 获取所有可能的 DNS 解析结果（同时检查 IPv4 和 IPv6）
        addrs = socket.getaddrinfo(hostname, None)
        for info in addrs:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                return True
    except socket.gaierror:
        # 无法解析，可能是无效域名，放行让后续 HTTP 请求自行报错
        return False
    return False


@register("webcheck", "AceSalm0n", "一个简单的检查网站在当前网络环境下能否被访问的插件", "1.2")
class WebCheck(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    # 注册指令的装饰器。指令名为 webcheck。注册成功后，发送 `/webcheck` 就会触发这个指令，并回复网站是否可访问的结果。
    @filter.command("webcheck")
    async def webcheck(self, event: AstrMessageEvent, url: str):
        """这是一个 webcheck 指令"""
        if not url:
            yield event.plain_result("Please provide a URL to check.")  # 如果用户没有提供 URL，提示他们提供一个 URL
            return

        if not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url  # 如果用户没有输入协议部分，默认使用 http://

        # 检查是否为内网地址
        if _is_private_url(url):
            yield event.plain_result("For security reasons, internal/private network addresses are not allowed")
            return

        try:
            async with httpx.AsyncClient() as client:  # 创建一个异步 HTTP 客户端
                response = await client.get(url, timeout=10)  # 设置超时时间为10秒
                result = response.status_code
        except httpx.HTTPError as e:  # 捕获 HTTP 请求相关的异常
            yield event.plain_result(f"Error occurred while checking {url}: {e}")
            return
        
        if result == 200:  # 200 可成功访问
            yield event.plain_result(f"{url} is accessible.")

        elif result // 100 == 3:  # 3xx 重定向
            yield event.plain_result(f"{url} is accessible but returned a redirect (status code: {result}).")

        else:  # 其他状态码表示无法访问
            yield event.plain_result(f"{url} is not accessible. Status code: {result}")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
