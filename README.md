# astrbot_plugin_web_check

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

AstrBot 插件，用于检查指定网站在当前网络环境下能否被正常访问，支持单站点检查和批量检查。

## 功能

- **单站点检查**：通过指令 `/check <URL>` 检查单个网站
- **批量检查**：通过 `/批量检查` 一键检查配置的站点列表
- **SSRF 防护**：自动拦截私有地址、环回地址等内网地址
- **响应时间统计**：显示每个站点的响应耗时
- **异步并发**：批量检查时并发请求，大幅提升速度
- **连接复用**：复用 HTTP 会话，减少握手开销
- 支持自动补全协议（未指定时默认添加 `http://`）
- 10 秒超时保护，避免请求卡死

## 指令

- **指令**: `/check <url>` — 检查单个网站
- **指令**: `/批量检查` — 检查 watchlist 中所有站点
- **指令**: `/check列表` — 查看当前配置的检查列表
- **指令**: `/check状态` — 查看插件当前配置
- **示例**: `/check https://www.baidu.com`

## 安装

在 AstrBot 插件市场安装使用，或将插件目录放入 AstrBot 的 `data/plugins/` 目录下。

依赖：

```bash
pip install aiohttp
```

## 配置

在 AstrBot 管理后台 → 插件配置中：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `check_timeout` | int | 10 | 单次检查超时时间（秒） |
| `watchlist` | list | 内置默认站点 | 批量检查的目标 URL 列表 |

## 使用示例

```text
用户: /check https://www.baidu.com
Bot: ✅ **www.baidu.com** 可访问
　状态码：200
　响应时间：45 ms

用户: /check 127.0.0.1
Bot: ⛔ 出于安全原因，不允许检查内网/私有地址

用户: /批量检查
Bot: 📊 **批量检查结果**
　总计：3 个站点
　✅ 可访问：2
　🔌 不可访问：1
```

## 项目结构

```text
astrbot_plugin_web_check/
├── main.py            # 插件主逻辑
├── __init__.py        # 插件元数据
├── _conf_schema.json  # 配置模式
└── README.md          # 本文件
```

## 参考

- [AstrBot 项目地址](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

## 开源协议

本项目基于 [GNU Affero General Public License v3](LICENSE) 发布。