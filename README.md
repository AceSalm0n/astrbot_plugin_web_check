# astrbot_plugin_web_check

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

WebCheck 2.0.1 是 AstrBot 网站连通性检查插件，支持单站点、多站点和配置列表检查，检查反映 AstrBot 所在服务器的网络状况。历史验收问题及修复进度见下方记录。

## 安装

在 AstrBot 插件市场安装，或将仓库克隆到 AstrBot 的 `data/plugins/astrbot_plugin_web_check/` 目录。在 AstrBot 使用的 Python 环境中安装依赖，再重载插件：

```bash
python -m pip install -r requirements.txt
```

已在本机 AstrBot 4.28.0、Python 3.10.6、aiohttp 3.13.5 中进行真实 WebChat 验收，结果为部分通过，详见下方记录。该结果只覆盖本次环境，不声明其他 AstrBot 版本或消息平台的兼容范围。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/check <url> [url2 ...]` | 单站点返回详情；多个目标返回汇总，空白分隔 |
| `/批量检查` | 检查配置的 watchlist |
| `/check列表` | 展示列表及逐项输入错误，不发起 DNS 或 HTTP 请求 |
| `/check状态` | 查看配置和 HTTP 会话状态 |
| `/check帮助` | 查看帮助 |

以上示例使用 `/` 唤醒前缀，实际使用应以 AstrBot 后台配置为准。本次环境的前缀为 `#`，例如 `#check www.baidu.com`、`#批量检查`；插件内置帮助仍显示 `/`。

网址省略协议时补 HTTPS，只接受 HTTP/HTTPS，检查主机与端口，拒绝含用户名或密码的地址。显示时隐藏查询参数值及片段；watchlist 中的无效目标逐项反馈，不影响其他目标。

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
| `max_sites` | int | 50 | 1–100，命令输入或 watchlist 超限均拒绝整次检查 |
| `use_head` | bool | true | HEAD 优先，405 回退 GET |
| `follow_redirects` | bool | false | 手动跟随，最多 5 跳 |
| `verify_ssl` | bool | true | HTTPS 证书验证 |
| `user_agent` | string | `Mozilla/5.0 (compatible; AstrBot-WebCheck/2.0)` | 1–256 字符，无控制字符 |
| `watchlist` | list | 下列四个 HTTPS 地址 | 保留已有列表；显式空列表保持为空；无效条目逐项反馈 |

默认列表为 `https://www.baidu.com`、`https://www.google.com`、`https://www.github.com`、`https://www.bing.com`。

检查进行中变更 `max_concurrent` 后，新检查会提示等待在途检查结束再重试，避免多个并发限制同时生效。User-Agent 在创建会话时应用；修改后重载插件以应用到已有会话。

## 结果说明

结果分别统计 2xx 正常响应、3xx 重定向、4xx/5xx HTTP 错误、网络失败、安全拦截和输入错误。204 属于正常响应，404/500 计入 HTTP 错误。另行统计“HTTP 有响应”，包括错误状态码以及收到响应头后正文超时的目标。

汇总包含全部条目，异常目标排列在前，消息自动分段，每条不超过 1800 字符。响应耗时记录从检查开始到收到当前响应头的时间，包括此前跳转与重试等待；不包含后续正文读取时间。

## 开发与验证

测试目录仅保留在本地开发环境，已通过 `.gitignore` 排除，不随插件仓库分发。以下命令仅适用于保有 `tests/` 的开发副本：

```bash
python -B -X utf8 -m unittest discover -s tests -v
```

测试采用标准库 unittest、AstrBot 接口桩和受控 DNS/HTTP 响应，覆盖命令、输入、安全地址、TLS、跳转、正文预算、总期限、重试、配置、共享并发、消息分段与卸载竞态。这些测试不代表真实 AstrBot 环境运行验收。

## 真实环境验收（2026-10-10）

环境：AstrBot 4.28.0、Python 3.10.6、aiohttp 3.13.5、WebCheck 2.0.1。Launcher 的运行进程使用系统 Python，目录内另一个 venv 的 aiohttp 3.13.2 不代表正在运行的环境。

通过运行中 AstrBot 的插件管理接口确认插件已启用，使用独立 WebChat 测试会话和本机 `#` 前缀发送命令，实际经过命令解析、插件处理和消息输出。16 项命令验收中 13 项通过、3 项未通过；后台热重载及重载后命令通过。没有调整插件源码、插件配置或代理设置。

| 验收项目 | 实际结果 |
| --- | --- |
| 插件加载、帮助、状态、列表、空参数提示 | 通过 |
| 非 HTTP 协议、畸形 IPv6、非法端口、含凭据 URL | 返回输入错误；密码不回显 |
| IPv4/IPv6 回环地址 | 安全拦截 |
| 查询参数脱敏、单条消息长度 | 查询值隐藏；本次所有输出均不超过 1800 字符 |
| 默认 watchlist 批量检查 | 4 个目标均有结果：百度 200，GitHub 301，Bing 302，Google 约 10 秒后超时；HTTP 有响应 3 个 |
| `/check` 多目标 | **未通过**：第二个及后续网址被忽略 |
| `/check` 51 个目标、配置上限 50 | **未通过**：只检查第一个，未触发整次拒绝 |
| 过期证书 `expired.badssl.com` | **未通过**：证书被拒绝，但错误分类显示“输入错误” |
| 后台热重载 | 成功；重载后状态显示会话未创建，命令正常 |

独立驱动还使用真实 AstrBot 的注册器、CommandFilter、AstrMessageEvent 和 MessageEventResult（未替换 AstrBot 接口）验证了重复卸载及停止后拒绝新建会话。该驱动使用临时数据目录，没有替代运行进程中的热重载测试。DNS/请求/排队期间卸载竞态、跨命令并发、长消息分段、重定向和 64 KiB 正文边界仍由模拟回归测试覆盖，未全部进行运行中服务验收；本次 50 项 unittest 回归通过。未测试 QQ、Telegram 等外部消息平台。

已知问题与使用限制：

- 历史多网址解析问题已修复：移除 GreedyStr 参数默认值，使用真实 AstrBot 4.28.0 注册器和 CommandFilter 回归验证多网址、空参数及数量限制；旧验收结果保留用于对照。后续运行中服务验收见[优化验收记录](docs/optimization-results.md)。
- 历史证书错误分类问题已修复：TLS 异常优先于 ValueError 捕获，真实 ClientConnectorCertificateError 回归验证分类为 TLS 失败且不重试；证书验证始终生效。
- 此前 DNS 诊断曾得到 Mihomo Fake-IP 地址 `198.18.0.x`、`2001:2::x` 并复现安全拦截。上述地址属于受限范围；本次实时 WebChat 的批量结果已收到三个站点响应，应分别看待不同时间和执行环境的网络结果。Google 连通性本次未通过。

本次仅记录验收结果，保留上述问题，不能据此宣称插件整体运行验收通过。

## 项目结构

| 文件 | 职责 |
| --- | --- |
| [main.py](main.py) | 命令、检查、安全连接器、统计及生命周期 |
| [_conf_schema.json](_conf_schema.json) | 后台配置及默认值 |
| [metadata.yaml](metadata.yaml) | 插件元数据 |
| [requirements.txt](requirements.txt) | aiohttp 依赖声明 |
| [update_log.md](update_log.md) | 更新日志 |

## 参考与协议

- [AstrBot 项目](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [GNU Affero General Public License v3](LICENSE)
