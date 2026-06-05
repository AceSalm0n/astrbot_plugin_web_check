# astrbot-plugin-webcheck

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

一个 AstrBot 插件，用于检查指定网站在当前网络环境下能否被正常访问。

## 功能

- 通过指令 `/webcheck <URL>` 检查目标网站的可访问性
- 返回 HTTP 状态码和访问结果
- 支持自动补全协议（未指定时默认添加 `http://`）
- 支持 3xx 重定向识别
- 10 秒超时保护，避免请求卡死
- **内网地址防护**：自动拦截私有地址、环回地址等内网地址，防止 SSRF 攻击
- 完善的异常处理（超时、连接失败等）

## 指令

- **指令**: `/webcheck <url>`
- **说明**: 检查指定 URL 是否可访问
- **示例**: `/webcheck https://www.baidu.com`

## 安装

在Astrbot插件市场安装使用
将本插件目录放入 AstrBot 的 `addons/` 或 `plugins/` 目录下即可。

依赖：

```bash
pip install httpx
```

## 使用示例

```text
用户: /webcheck https://www.baidu.com
Bot: ✅ www.baidu.com is accessible.

用户: /webcheck baidu.com
Bot: ✅ baidu.com is accessible.

用户: /webcheck https://example.invalid
Bot: 🔌 Error occurred while checking https://example.invalid: ...

用户: /webcheck 127.0.0.1
Bot: ⛔ For security reasons, internal/private network addresses are not allowed

用户: /webcheck 192.168.1.1
Bot: ⛔ For security reasons, internal/private network addresses are not allowed
```

## 项目结构

```text
astrbot_plugin_web_check/
├── main.py        # 插件主逻辑
└── README.md      # 本文件
```

## 参考

- [AstrBot 项目地址](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

## 开源协议

本项目基于 [GNU Affero General Public License v3](LICENSE) 发布。
