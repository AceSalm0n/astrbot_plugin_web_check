# astrbot_plugin_web_check

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

WebCheck 2.0.1 是 AstrBot 网站连通性检查插件，支持单站点、多站点和配置列表检查。检查反映 AstrBot 所在服务器的网络状况。

## 安装

在 AstrBot 插件市场安装，或将仓库克隆到 AstrBot 的 `data/plugins/astrbot_plugin_web_check/` 目录。在 AstrBot 使用的 Python 环境中安装依赖，再重载插件：

```bash
python -m pip install -r requirements.txt
```

当前验证环境为 Python 3.10.6、aiohttp 3.13.5。未安装 AstrBot，尚未验证真实框架加载与消息平台运行；不声明未经验证的 AstrBot 兼容版本范围。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/check <url> [url2 ...]` | 空格分隔，一个目标返回详情，多个目标返回汇总 |
| `/批量检查` | 检查配置的 watchlist |
| `/check列表` | 展示列表及逐项输入错误，不发起 DNS 或 HTTP 请求 |
| `/check状态` | 查看配置和 HTTP 会话状态 |
| `/check帮助` | 查看帮助 |

网址省略协议时补 HTTPS，只接受 HTTP/HTTPS，检查主机与端口，拒绝含用户名或密码的地址。显示时隐藏查询参数值及片段；无效目标逐项反馈，不影响同批其他目标。

## 检查行为

- 默认优先 HEAD，遇到 405 释放响应后回退 GET。GET 最多读取 64 KiB，达到预算即停止；区分服务器声明长度与实际读取长度，注明截断。关闭自动解压，读取预算按传输正文计算。
- 默认验证 HTTPS 证书；关闭 `verify_ssl` 后，结果明确提示未验证。默认不跟随重定向；开启后手动跟随，最多 5 跳，每跳重新校验 URL 和实际连接地址。
- 实际连接器校验 DNS、缓存结果及 IP 字面量。非公网、多播、保留地址及映射到受限 IPv4 的 IPv6 地址均被拦截；混合公网与受限地址的解析也会被拒绝。保留域名用于 Host/SNI。
- 单站点总期限从取得共享并发名额后开始，覆盖 DNS、请求、HEAD 回退、跳转、正文和重试等待，不包含并发排队。仅暂时性连接错误重试，等待依次为 0.5、1、2 秒；输入错误、安全拦截、证书错误、永久解析错误及 HTTP 错误不重试。
- 会话不保存 Cookie。卸载时阻止新检查，取消并等待在途任务后关闭会话，重复卸载安全。

## 配置

在 AstrBot 管理后台的插件配置中修改。默认值统一读取 [_conf_schema.json](_conf_schema.json)，缺失配置使用默认值，错误类型或越界值明确报错。

| 配置项 | 类型 | 默认值 | 范围或行为 |
| --- | --- | --- | --- |
| `check_timeout` | int | 10 | 1–120 秒，单站点总期限 |
| `max_concurrent` | int | 5 | 1–20，跨命令共享 |
| `retry_count` | int | 1 | 0–3，暂时性连接错误的重试次数 |
| `max_sites` | int | 50 | 1–100，超限拒绝整次检查并说明数量 |
| `use_head` | bool | true | HEAD 优先，405 回退 GET |
| `follow_redirects` | bool | false | 手动跟随，最多 5 跳 |
| `verify_ssl` | bool | true | HTTPS 证书验证 |
| `user_agent` | string | `Mozilla/5.0 (compatible; AstrBot-WebCheck/2.0)` | 1–256 字符，无控制字符 |
| `watchlist` | list | 下列四个 HTTPS 地址 | 保留已有列表；显式空列表保持为空；无效条目逐项反馈 |

默认列表为 `https://www.baidu.com`、`https://www.google.com`、`https://www.github.com`、`https://www.bing.com`。

检查进行中变更 `max_concurrent` 后，新检查会提示等待在途检查结束再重试，避免多个并发限制同时生效。User-Agent 在创建会话时应用；修改后重载插件以应用到已有会话。

## 结果说明

结果分别统计 2xx 正常响应、3xx 重定向、4xx/5xx HTTP 错误、网络失败、安全拦截和输入错误。204 属于正常响应，404/500 计入 HTTP 错误。另行统计“HTTP 有响应”，包括错误状态码以及收到响应头后正文超时的目标。

汇总包含全部输入，异常目标排列在前，消息自动分段，每条不超过 1800 字符。响应耗时记录从检查开始到收到当前响应头的时间，包括此前跳转与重试等待；不包含后续正文读取时间。

## 开发与验证

```bash
python -B -X utf8 -m unittest discover -s tests -v
```

测试采用标准库 unittest、AstrBot 接口桩和受控 DNS/HTTP 响应，覆盖命令、输入、安全地址、TLS、跳转、正文预算、总期限、重试、配置、共享并发、消息分段与卸载竞态。这些测试不代表真实 AstrBot 环境运行验收。

| 文件 | 职责 |
| --- | --- |
| [main.py](main.py) | 命令、检查、安全连接器、统计及生命周期 |
| [_conf_schema.json](_conf_schema.json) | 后台配置及默认值 |
| [metadata.yaml](metadata.yaml) | 插件元数据 |
| [requirements.txt](requirements.txt) | aiohttp 依赖声明 |
| [tests/](tests/) | 模拟回归测试 |
| [update_log.md](update_log.md) | 更新日志 |
| [agent.md](agent.md) | 插件开发代理说明 |

## 参考与协议

- [AstrBot 项目](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [GNU Affero General Public License v3](LICENSE)
