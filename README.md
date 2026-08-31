# Codex 手机通知（codex-notify）

在 Codex CLI 完成任务时，把最终结论推送到手机微信（Server酱服务号消息）。
同一任务连续多轮对话只会收到一条通知，去重窗口默认 90 秒。

## 工作原理

- Codex CLI 的 `notify` 钩子会在**每完成一轮**（agent-turn-complete）时触发一次，
  由 `~/.codex/config.toml` 里的 `notify` 配置调用本项目的 `notify.py`。
- `notify.py` 把本轮信息（会话 ID、轮次 ID、工作目录、最后结论）写入
  `state/<thread_id>.json`，并启动一个脱离 Codex 的延迟进程 `notify.py finalize ...`。
- finalizer 等待去重窗口（默认 90 秒）后检查状态：如果期间又出现了新的一轮
  （turn_id 已变化），说明任务还在继续，旧 finalizer 自动放弃；只有"最后一轮
  完成后静默满 90 秒"才真正发送通知。这就是"同一任务只给结论"。
- 推送渠道默认 **Server酱**（sct.ftqq.com，微信服务号消息），也支持 PushPlus，
  在 `config.json` 里切换。

## 安装步骤

1. 注册 Server酱：打开 <https://sct.ftqq.com>，用 GitHub 登录，扫码绑定微信，
   进入"SendKey"页面复制你的 SendKey。
2. 把项目里的 `config.example.json` 复制为 `config.json`（此文件含密钥，已加入
   .gitignore，不会提交），将 `serverchan_sendkey` 填为你的 SendKey。
3. 确认 `~/.codex/config.toml` 里已有以下配置（本项目已替你写入）：

   ```toml
   notify = ["C:/Users/TOKI/miniconda3/python.exe",
             "D:/pythonitems/codex-notify/notify.py"]
   ```

   修改 config.toml 后，新开的 Codex 会话立即生效。

## 配置项说明

| 配置键 | 说明 | 默认值 |
| --- | --- | --- |
| `service` | 推送渠道：`serverchan` 或 `pushplus` | `serverchan` |
| `serverchan_sendkey` | Server酱 SendKey | 空（不发，仅记日志） |
| `pushplus_token` | PushPlus token | 空 |
| `debounce_seconds` | 同一任务去重静默窗口（秒） | 90 |
| `max_message_chars` | 结论截断长度（字符） | 600 |
| `http_timeout_seconds` | 单次 HTTP 超时（秒） | 8 |

## 手动测试

没有 SendKey 时，脚本只写日志、不发送（不会报错），可先这样验证链路：

```powershell
cd D:\pythonitems\codex-notify
$env:CODEX_HOOK_AGENT_TURN_COMPLETE_THREAD_ID = "test-thread-1"
$env:CODEX_HOOK_AGENT_TURN_COMPLETE_TURN_ID = "turn-1"
$env:CODEX_HOOK_AGENT_TURN_COMPLETE_CWD = "D:\pythonitems"
$env:CODEX_HOOK_AGENT_TURN_COMPLETE_CLIENT = "cli"
python notify.py
```

然后查看 `logs\notify.log` 与 `state\` 下的状态文件。
填入真实 SendKey 后，每次任务结束手机会收到一条微信消息；发送明细记录在
`logs\notify.log`。

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

## 隐私提示

发送给微信的内容包含工作目录、客户端和最后一轮结论（截断到 600 字符）。
如需更保守，可调小 `max_message_chars`，或删掉 `notify.py` 中 `build_message`
里对应的行。
