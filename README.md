# Codex 手机通知（codex-notify）

在 Codex CLI 完成任务时，把最终结论推送到手机微信（Server酱服务号消息）。
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
- 推送渠道默认 **Server酱**（sct.ftqq.com，微信服务号消息），也支持 PushPlus，
  在 `config.json` 里切换。

## 安装步骤

1. 按下方"获取 SendKey"注册 Server酱并拿到密钥。
2. 把项目里的 `config.example.json` 复制为 `config.json`（此文件含密钥，已加入
   .gitignore，不会提交），将 `serverchan_sendkey` 填为你的 SendKey。
3. 确认 `~/.codex/config.toml` 里已有以下配置（本项目已替你写入）：

   ```toml
   notify = ["C:/Users/TOKI/miniconda3/python.exe",
             "D:/pythonitems/codex-notify/notify.py"]
   ```

   修改 config.toml 后，新开的 Codex 会话立即生效。

## 获取 SendKey（Server酱注册方法）

SendKey 是 Server酱给每个账号分配的推送密钥，形如 `SCT` 开头的一串字符。
创建步骤如下：

1. 打开 <https://sct.ftqq.com>，点击"登入"，使用 GitHub 账号授权登录
   （首次使用会跳转 GitHub OAuth 授权）。
2. 登录后按页面提示使用微信扫码，关注"Server酱"微信服务号完成绑定
   （也可按页面提示绑定其他接收渠道，微信服务号消息是默认推荐）。
3. 进入"SendKey"页面（登录后首页即可看到），点击"复制"得到 SendKey；
   如果尚未生成，点击页面上的"生成"按钮创建。
4. 把 SendKey 粘贴到本项目 `config.json` 的 `serverchan_sendkey` 字段并保存。
5. 可选验证：在 Server酱官网的"发送消息"测试页直接发一条消息，
   确认微信能收到后，再回到本项目跑一次任务。

注意事项：

- SendKey 等同账号凭证，不要提交到公开仓库（本项目 config.json 已在
  .gitignore 中，提交的是不含密钥的 `config.example.json`）。
- Server酱免费额度有频率限制（具体以官网说明为准）；`debounce_seconds` 默认 0
  （每轮立刻发送），如需合并连续多轮可调大该值，正常使用不会触发限流。
- 如果收不到消息，先看本项目 `logs\notify.log` 的发送结果，再对照官网文档排查。

## 配置项说明

| 配置键 | 说明 | 默认值 |
| --- | --- | --- |
| `service` | 推送渠道：`serverchan` 或 `pushplus` | `serverchan` |
| `serverchan_sendkey` | Server酱 SendKey | 空（不发，仅记日志） |
| `pushplus_token` | PushPlus token | 空 |
| `debounce_seconds` | 去重静默窗口（秒）；0 = 每轮立刻发送 | 0 |
| `max_message_chars` | 结论截断长度（字符） | 600 |
| `http_timeout_seconds` | 单次 HTTP 超时（秒） | 8 |

## 手动测试

没有 SendKey 时，脚本只写日志、不发送（不会报错），可先这样验证链路：

```powershell
cd D:\pythonitems\codex-notify
$env:CODEX_THREAD_ID = "test-thread-1"
python notify.py
```

然后查看 `logs\notify.log` 与 `state\` 下的状态文件。
填入真实 SendKey 后，每次任务结束手机会收到一条微信消息；发送明细记录在
`logs\notify.log`。手动测试没有对应会话文件时，会以当前目录为工作目录、
结论为空，但推送/日志链路同样会被验证。

## 日志与排错

- `logs\notify.log`：所有事件（轮次到达、finalizer 启动/放弃/发送成败）均记在此，
  纯 ASCII。
- `logs\finalize_<thread>_<turn>.log`：每个 finalizer 进程的独立输出。
- `state\*.json`：每个会话最近一轮的状态与发送标记。
- 本机 DNS 冷解析很慢，脚本内置了 127.0.0.100:53 快速解析 + 看门狗超时；
  推送失败会先写日志再重试一次，不会影响 Codex 本身。
- TLS：本机 Python 默认证书库含已过期的旧根证书，曾导致 Server酱 HTTPS 误报
  `certificate has expired`（curl 正常）；notify.py 已改用 certifi CA 包校验
  （miniconda 自带），无需额外安装。

## 常见问题

- **收不到消息**：先看 `logs\notify.log` 里的发送结果；确认 `config.json` 中
  `serverchan_sendkey` 已填写且没有多余空格。
- **想合并同一任务的连续多轮**：把 `config.json` 的 `debounce_seconds` 调大
  （如 15–30 秒），期间的新一轮会合并，只在静默满该秒数后发一条。
- **想每轮立刻收到**：`debounce_seconds` 保持 0 即可，任务每完成一轮约 1 秒内推送。

## 隐私提示

发送给微信的内容包含工作目录、客户端和最后一轮结论（截断到 600 字符）。
如需更保守，可调小 `max_message_chars`，或删掉 `notify.py` 中 `build_message`
里对应的行。
