# astrbot_plugin_web_check

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

AstrBot 插件，用于检查指定网站在当前网络环境下能否被正常访问，支持单/多站点检查和批量检查。

## 功能

- **多站点检查**：`/check` 一次可传入多个 URL，空格分隔
- **批量检查**：`/批量检查` 一键检查配置的 watchlist
- **HEAD 优先**：默认使用 HEAD 请求，不下载响应体，速度更快；对 405 自动降级为 GET
- **重试退避**：请求失败后自动重试，支持指数退避
- **并发限流**：批量检查通过 Semaphore 控制最大并发数，防止请求风暴
- **SSRF 防护**：拦截私有地址、回环、链路本地、多播、IPv6 保留段
- **自动补全协议**：URL 省略协议头时默认补全为 `https://`

## 指令

| 指令 | 说明 |
| --- | --- |
| `/check <url> [url2 ...]` | 检查一个或多个网址 |
| `/批量检查` | 检查 watchlist 中配置的所有站点 |
| `/check列表` | 查看 watchlist 内容 |
| `/check状态` | 查看插件配置和运行状态 |
| `/check帮助` | 显示帮助信息 |

## 安装

在 AstrBot 插件市场安装，或将插件目录放入 `data/plugins/` 后重启。

依赖：

```bash
pip install aiohttp
```

## 配置

在 AstrBot 管理后台 → 插件配置中：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `check_timeout` | int | 10 | HTTP 请求超时时间（秒） |
| `max_concurrent` | int | 5 | 批量检查时的最大并发请求数 |
| `retry_count` | int | 1 | 请求失败后的重试次数（0 = 不重试） |
| `use_head` | bool | true | 优先使用 HEAD 请求 |
| `follow_redirects` | bool | false | 是否跟随重定向 |
| `user_agent` | str | Mozilla/5.0 ... | HTTP User-Agent |
| `watchlist` | list | [] | 批量检查的目标 URL 列表 |

## 使用示例

```text
用户: /check baidu.com github.com
Bot: 📊 2/2 个可访问  平均响应 120ms

     🟢 baidu.com  HTTP 200  88ms
     🟢 github.com  HTTP 200  152ms

用户: /check 127.0.0.1
Bot: ⛔ 所有地址均为内网/私有地址，已拒绝访问

用户: /批量检查
Bot: 📊 **批量检查结果**
     总计 5  ✅ 4  🔴 1  平均响应 135ms

     🔴 example-down.com  ✗ 超时（>10s）
     🟢 baidu.com  HTTP 200  88ms
     ...
```

## 项目结构

```text
astrbot_plugin_web_check/
├── main.py         # 插件主逻辑
├── metadata.yaml   # 插件元数据
├── update_log.md   # 更新日志
└── README.md       # 本文件
```

## 参考

- [AstrBot 项目地址](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

## 开源协议

本项目基于 [GNU Affero General Public License v3](LICENSE) 发布。
