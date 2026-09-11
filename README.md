# Codex 手机通知（codex-notify）

在 Codex CLI 完成任务时，把最终结论推送到手机（默认 WxPusher，微信/客户端实时收）。
同一任务连续多轮对话只会收到一条通知，去重窗口默认 90 秒。

## 工作原理

- Codex CLI 的 `notify` 钩子会在**每完成一轮**时触发一次，由
  `~/.codex/config.toml` 里的 `notify` 配置调用本项目的 `notify.py`。
  钩子传参有两种：TUI 会话通过环境变量 `CODEX_THREAD_ID` / `CODEX_SESSION_ID`；
  exec 会话（Codex App/子代理）把 `agent-turn-complete` 事件 JSON 作为命令行参数传入
  （环境变量为空）。notify.py 两者都支持（环境变量优先，argv JSON 兜底），据此读取本地会话文件
  `~/.codex/sessions/**/rollout-*.jsonl`：从 `task_complete` 记录拿到轮次 ID、
  结论与耗时，从 `session_meta` 拿到工作目录与客户端。
  注意：钩子进程环境里 `CODEX_THREAD_ID` 等变量存在但为空字符串，argv JSON 字段
  必须显式覆盖空值（不能用 `setdefault`），否则 thread_id 取不到、推送被跳过。
- 平台自动审查/守护代理等临时线程（无 rollout 会话文件）也会触发钩子推送，
  用户认可这类临时结论，故不做线程过滤；所有线程的结论都会推送。
- `dedupe_seconds`（默认 90）：相同结论内容在窗口内只推一次（跨线程同样生效），
  发送记录存于 `state/sent_hashes.json`（gitignore）。
- `notify.py` 把本轮信息写入 `state/<thread_id>.json`，并启动一个脱离 Codex 的
  延迟进程 `notify.py finalize ...`。
- finalizer 等待 `debounce_seconds`（默认 0，即立刻发送）后检查状态。如果该值大于 0：
  期间又出现新的一轮（turn_id 已变化）说明任务还在继续，旧 finalizer 自动放弃；
  只有"最后一轮完成后静默满该秒数"才真正发送通知，实现"同一任务只给结论"。
- 注意：设为 0（立刻发送）时，同一任务里每追问一轮就会立刻再收到一条新通知
  （系统无法预知你之后还会不会继续问）；想要合并连续多轮，把该值调大即可。
- 推送渠道默认 **WxPusher**（wxpusher.zjiecode.com，官方称永久免费，微信 + 各平台
  客户端实时收），也支持 Server酱、PushPlus，在 `config.json` 的 `service` 里切换。
  注：Server酱已改订阅制（免费额度受限），故不再作为默认渠道。

## 安装步骤（一键配置）

前提：Windows 已安装 Python 3（<https://www.python.org/downloads/>，安装时勾选
"Add python.exe to PATH"，或使用系统自带的 py 启动器）。

1. 按下方"获取 appToken 与 UID"注册 WxPusher 并拿到两个值。
2. 双击项目根目录的 `setup.bat`（或运行 `python setup_notify.py`）。
3. 按提示选渠道（默认 1 = WxPusher）并粘贴对应密钥，工具会自动完成：
   - 写入本项目 `config.json`：填 `wxpusher_apptoken` + `wxpusher_uids`
     （或 Server酱 `serverchan_sendkey` / PushPlus `pushplus_token`），
     其余配置项保持不变；
   - 向 `~/.codex/config.toml` 注入 `notify` 钩子：只新增或替换 `notify` 这一行，
     不修改文件中任何其他条目；文件不存在会自动创建；
   - 询问是否发送一条测试消息验证（推荐 y）。
4. 若 Codex 正在运行，重启会话；新开的会话立即生效。

不想用一键工具时，可手动配置：把 `config.example.json` 复制为 `config.json`
（含密钥，已在 .gitignore 中，不会提交），填入 `wxpusher_apptoken` 与
`wxpusher_uids`；再在
`~/.codex/config.toml` 根级添加一行：

```toml
notify = ["你的python解释器路径", "本项目绝对路径/notify.py"]
```

也可以直接下载 Releases 页面里的 `codex-notify-setup-vX.Y.Z.zip`（含配置程序与
配套脚本，最新为 v1.1.0），解压到任意目录后双击 `codex-notify-setup.exe`，
效果与 setup.bat 相同
（系统仍需装有 Python 3，配置程序会自动探测）。

## 获取 appToken 与 UID（WxPusher 注册方法，免费）

WxPusher 需要两个值：应用令牌 `appToken`（发信用）和你的 `UID`（发给谁）。

1. 打开 <https://wxpusher.zjiecode.com/admin/>，用微信扫码登录。
2. 左侧「应用管理」→「创建应用」（名称随意，比如 codex-notify），创建后
   在应用的 appToken 页复制 `AT` 开头的 appToken。
3. 微信关注公众号 `wxpusher`（或下载 WxPusher 客户端），在「我的」→「我的UID」
   里复制 `UID_` 开头的 UID；一个应用可绑多个 UID，多个用逗号分隔填进
   `wxpusher_uids`。
4. 把两个值填进本项目 `config.json`（`service` 保持 `wxpusher`），保存。
5. 可选验证：`python notify.py` 手动跑一次，或直接看手机是否收到本轮结论。

注意事项：

- appToken / UID 等同账号凭证，不要提交到公开仓库（本项目 config.json 已在
  .gitignore 中，提交的是不含密钥的 `config.example.json`）。
- 官方限制（以官网文档为准）：发送接口约 2 QPS，单个 UID 每天约 3000 条后
  不再进通知栏；`debounce_seconds` 默认 0（每轮立刻发送），正常使用远不会触顶。
- 备选渠道：Server酱（已改订阅制）与 PushPlus（需实名认证）仍可用，把
  `config.json` 的 `service` 分别改成 `serverchan` / `pushplus` 并填对应密钥即可。
- 如果收不到消息，先看本项目 `logs\notify.log` 的发送结果，再对照官网文档排查。

## 配置项说明

| 配置键 | 说明 | 默认值 |
| --- | --- | --- |
| `service` | 推送渠道：`wxpusher` / `serverchan` / `pushplus` | `wxpusher` |
| `wxpusher_apptoken` | WxPusher 应用 appToken（AT 开头） | 空（不发，仅记日志） |
| `wxpusher_uids` | WxPusher UID，多个用逗号分隔 | 空 |
| `serverchan_sendkey` | Server酱 SendKey | 空（不发，仅记日志） |
| `pushplus_token` | PushPlus token | 空 |
| `debounce_seconds` | 去重静默窗口（秒）；0 = 每轮立刻发送 | 0 |
| `dedupe_seconds` | 相同结论内容去重窗口（秒），跨线程生效 | 90 |
| `max_message_chars` | 结论截断长度（字符） | 600 |
| `http_timeout_seconds` | 单次 HTTP 超时（秒） | 8 |

## 手动测试

没有填密钥时，脚本只写日志、不发送（不会报错），可先这样验证链路：

```powershell
cd D:\pythonitems\codex-notify
$env:CODEX_THREAD_ID = "test-thread-1"
python notify.py
```

然后查看 `logs\notify.log` 与 `state\` 下的状态文件。
填入真实 appToken/UID 后，每次任务结束手机会收到一条消息；发送明细记录在
`logs\notify.log`。手动测试没有对应会话文件时，会以当前目录为工作目录、
结论为空，但推送/日志链路同样会被验证。

## 日志与排错

- `logs\notify.log`：所有事件（轮次到达、finalizer 启动/放弃/发送成败）均记在此，
  纯 ASCII。
- `logs\finalize_<thread>_<turn>.log`：每个 finalizer 进程的独立输出。
- `state\*.json`：每个会话最近一轮的状态与发送标记。
- 解析走系统 DNS（早期版本内置的 127.0.0.100:53 快速解析已删除：它一挂就
  让所有推送静默失败，而慢解析只发生在推送子线程里）；HTTP 请求带看门狗超时，
  推送失败会先写日志再重试一次，不会影响 Codex 本身。
- TLS：本机 Python 默认证书库含已过期的旧根证书，曾导致推送 HTTPS 误报
  `certificate has expired`（curl 正常）；notify.py 已改用 certifi CA 包校验
  （miniconda 自带），无需额外安装。

## 常见问题

- **收不到消息**：先看 `logs\notify.log` 里的发送结果；确认 `config.json` 中
  `wxpusher_apptoken` 与 `wxpusher_uids` 已填写且没有多余空格
  （`service` 与所填渠道要一致）。
- **想合并同一任务的连续多轮**：把 `config.json` 的 `debounce_seconds` 调大
  （如 15–30 秒），期间的新一轮会合并，只在静默满该秒数后发一条。
- **想每轮立刻收到**：`debounce_seconds` 保持 0 即可，任务每完成一轮约 1 秒内推送。

## 隐私提示

发送给微信的内容包含工作目录、客户端和最后一轮结论（截断到 600 字符）。
如需更保守，可调小 `max_message_chars`，或删掉 `notify.py` 中 `build_message`
里对应的行。
