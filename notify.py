# -*- coding: utf-8 -*-
"""Codex 手机通知：notify hook 入口 + 同任务去重 + 延迟发送结论。

触发方式（写入 ~/.codex/config.toml）：
    notify = ["C:/Users/TOKI/miniconda3/python.exe",
              "D:/pythonitems/codex-notify/notify.py"]

每次 Codex 完成一轮都会调用本脚本（0.151.0 的 notify 只传
CODEX_THREAD_ID / CODEX_SESSION_ID / CODEX_CI，不含结论内容），脚本负责：
1. 以 CODEX_THREAD_ID 定位本地会话文件 ~/.codex/sessions/**/rollout-*.jsonl，
   读取本轮 task_complete 记录（turn_id、结论、耗时）与 session_meta（工作目录），
   连同时间戳写入 state/<thread_id>.json；
2. 启动一个脱离当前进程的 finalizer，等待 debounce_seconds 秒（0 = 立即）；
   期间若又有新的一轮完成（turn_id 变化），旧 finalizer 醒来发现已过期会放弃，
   只有"最后一个 turn 后静默满 debounce 秒"的那个 finalizer 才真正推送。

推送渠道：Server酱（微信服务号消息）或 PushPlus，配置见 config.json。
网络：直接用系统 DNS（2026-09-11 起——原先的 127.0.0.100:53 隧道 DNS 已下线，
它一挂推送就全失败，故整段删除走系统解析）；解析/请求都在 HTTP worker
子线程里跑并带看门狗超时，绝不让推送拖住 Codex。
"""

import json
import hashlib
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
STATE_DIR = os.path.join(PROJECT_DIR, "state")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "notify.log")

DEFAULT_CONFIG = {
    "service": "serverchan",          # serverchan | pushplus
    "serverchan_sendkey": "",         # Server酱 SendKey（sct.ftqq.com）
    "serverchan_api": "https://sctapi.ftqq.com/{key}.send",
    "pushplus_token": "",             # PushPlus token（www.pushplus.plus）
    "pushplus_api": "http://www.pushplus.plus/send",
    "debounce_seconds": 0,            # 0 = 每轮立即发送；>0 = 静默满该秒数才发结论
    "dedupe_seconds": 90,             # 相同结论内容在该秒数内只推一次（跨线程）
    "max_message_chars": 600,         # 结论截断长度（字符）
    "http_timeout_seconds": 8,        # 单次 HTTP 请求超时
}


# ---------------- 日志（纯 ASCII，避免 PowerShell 5.1 中文乱码） ----------------

def log(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(LOG_PATH, "a", encoding="ascii", errors="replace") as f:
            f.write(line)
    except Exception:
        pass


# ---------------- 配置 ----------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if isinstance(user_cfg, dict):
            cfg.update({k: v for k, v in user_cfg.items() if v is not None})
    except FileNotFoundError:
        log("WARN config.json not found, using defaults")
    except Exception as e:
        log("WARN failed to load config.json: %r" % (e,))
    return cfg


# ---------------- 状态存取（按 thread_id 区分会话） ----------------

def state_path(thread_id):
    safe = "".join(ch for ch in str(thread_id) if ch.isalnum() or ch in "-_")
    return os.path.join(STATE_DIR, (safe or "unknown") + ".json")


def save_state(thread_id, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = state_path(thread_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_state(thread_id):
    path = state_path(thread_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log("WARN read_state failed: %r" % (e,))
        return None


SENT_HASHES_PATH = os.path.join(STATE_DIR, "sent_hashes.json")


def _load_sent_hashes():
    try:
        with open(SENT_HASHES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_sent_hashes(data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SENT_HASHES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def prune_sent_hashes(now, window):
    """清掉超出去重窗口的记录，返回剩余哈希表。"""
    cutoff = now - max(0, int(window))
    sent = {k: v for k, v in _load_sent_hashes().items()
            if isinstance(v, (int, float)) and v >= cutoff}
    _save_sent_hashes(sent)
    return sent


def content_hash(title, content):
    return hashlib.sha256((title + "\n" + content).encode("utf-8")).hexdigest()


# ---------------- 读取 Codex 本地会话文件（rollout JSONL） ----------------

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.path.expanduser("~"), ".codex")
SESSIONS_ROOT = os.path.join(CODEX_HOME, "sessions")


def find_rollout_file(thread_id):
    """按线程 ID 在 ~/.codex/sessions 下找 rollout JSONL（取最新 mtime）。"""
    best = None
    best_mtime = 0.0
    suffix = "-" + str(thread_id) + ".jsonl"
    try:
        for dirpath, _dirnames, filenames in os.walk(SESSIONS_ROOT):
            for name in filenames:
                if not (name.startswith("rollout-") and name.endswith(suffix)):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > best_mtime:
                    best, best_mtime = path, mtime
    except OSError:
        return None
    return best


def read_turn_payload(thread_id):
    """读 rollout 文件，返回最近一轮完成信息；找不到返回 None。"""
    path = find_rollout_file(thread_id)
    if not path:
        return None
    payload = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rtype = rec.get("type")
                body = rec.get("payload") or {}
                if rtype == "session_meta":
                    payload.setdefault("cwd", body.get("cwd", ""))
                    payload.setdefault("source", body.get("source", ""))
                    payload.setdefault("originator", body.get("originator", ""))
                elif rtype == "event_msg" and body.get("type") == "task_complete":
                    payload["turn_id"] = body.get("turn_id", "")
                    payload["last_assistant_message"] = body.get(
                        "last_agent_message", "")
                    payload["duration_ms"] = body.get("duration_ms")
    except Exception as e:
        log("WARN read_turn_payload failed: %r" % (e,))
        return None
    return payload or None


# ---------------- notify hook 环境变量读取 ----------------

_ENV_MAPPING = {
    "type": "type",
    "thread_id": "thread_id",
    "turn_id": "turn_id",
    "cwd": "cwd",
    "client": "client",
    "input_messages": "input_messages",
    "last_assistant_message": "last_assistant_message",
}


def env_turn_info():
    """从环境变量里找出本轮信息。

    兼容两套命名：旧版 CODEX_HOOK_AGENT_TURN_COMPLETE_* 后缀匹配，
    以及 0.151.0 实际传入的 CODEX_THREAD_ID / CODEX_SESSION_ID / CODEX_CI。
    """
    info = {}
    for key, value in os.environ.items():
        up = key.upper()
        if "AGENT_TURN_COMPLETE" not in up:
            continue
        suffix = up.split("AGENT_TURN_COMPLETE", 1)[1].strip("_")
        norm = suffix.lower().replace("-", "_")
        field = _ENV_MAPPING.get(norm)
        if field:
            info[field] = value
    if not info.get("thread_id"):
        info["thread_id"] = os.environ.get("CODEX_THREAD_ID", "")
    if not info.get("session_id"):
        info["session_id"] = os.environ.get("CODEX_SESSION_ID", "")
    if os.environ.get("CODEX_CI") and not info.get("client"):
        info["client"] = "exec"
    return info


def argv_turn_info():
    """新版 Codex exec 会话实测（2026-08-31）：notify 钩子把
    agent-turn-complete 事件 JSON 直接作为 argv[1] 传入，此时 env 的
    CODEX_THREAD_ID 为空；解析该 JSON 做兜底。"""
    if len(sys.argv) < 2:
        return {}
    raw = sys.argv[1]
    if not raw.lstrip().startswith("{"):
        return {}
    try:
        ev = json.loads(raw)
    except Exception:
        return {}
    if ev.get("type") != "agent-turn-complete":
        return {}
    info = {}
    for key in ("thread-id", "turn-id", "cwd", "client", "last-assistant-message"):
        value = ev.get(key) or ev.get(key.replace("-", "_"))
        if value:
            info[key.replace("-", "_")] = value
    msgs = ev.get("input-messages") or ev.get("input_messages")
    if msgs:
        info["input_messages"] = msgs
    return info


# ---------------- 去重与延迟发送 ----------------

def spawn_finalizer(thread_id, turn_id, debounce):
    """启动脱离当前进程的 finalizer，防止 Codex 等待推送。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    logfile = os.path.join(
        LOG_DIR,
        "finalize_%s_%s.log" % (str(thread_id)[:8], str(turn_id)[:8]),
    )
    args = [
        sys.executable,
        os.path.abspath(__file__),
        "finalize",
        str(thread_id),
        str(turn_id),
        str(debounce),
    ]
    flags = subprocess.CREATE_NO_WINDOW
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(logfile, "ab") as out:
        subprocess.Popen(
            args,
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )


def on_turn(cfg):
    info = env_turn_info()
    argv_info = argv_turn_info()
    if argv_info:
        for k, v in argv_info.items():
            # 钩子进程环境里 CODEX_THREAD_ID 等变量存在但为空字符串，
            # setdefault 不会覆盖已存在的空值，必须用 argv 的事件字段补齐。
            if v and not info.get(k):
                info[k] = v
        log("INFO turn info from argv json (env thread empty)")
    thread_id = info.get("thread_id") or info.get("session_id")
    if not thread_id:
        log("WARN on_turn skipped: no thread_id in env/argv")
        return
    payload = read_turn_payload(thread_id) or {}
    turn_id = payload.get("turn_id") or info.get("turn_id") or (
        "turn-%d" % int(time.time()))
    cwd = payload.get("cwd") or info.get("cwd") or os.getcwd()
    client = (payload.get("originator") or payload.get("source")
              or info.get("client") or "cli")
    data = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "ts": time.time(),
        "cwd": cwd,
        "client": client,
        "last_assistant_message": payload.get("last_assistant_message", "")
        or info.get("last_assistant_message", ""),
        "duration_ms": payload.get("duration_ms"),
    }
    save_state(thread_id, data)
    debounce_raw = cfg.get("debounce_seconds", 0)
    debounce = int(debounce_raw) if debounce_raw is not None else 0
    log("TURN thread=%s turn=%s cwd=%s client=%s debounce=%ss"
        % (thread_id, turn_id, cwd, client, debounce))
    spawn_finalizer(thread_id, turn_id, debounce)


# ---------------- HTTP（系统 DNS + 看门狗） ----------------

def _ssl_context():
    """certifi CA 包优先；Windows 系统证书库含已过期的旧根证书，
    OpenSSL 会误报 certificate has expired（curl/schannel 正常）。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


def _http_request(url, timeout, data=None, headers=None):
    """在子线程里发请求并限时等待；返回 (body, error)。"""
    result = {}

    def worker():
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            kwargs = {"timeout": timeout}
            if url.lower().startswith("https://"):
                ctx = _ssl_context()
                if ctx is not None:
                    kwargs["context"] = ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                result["status"] = resp.status
                result["body"] = resp.read(8192).decode("utf-8", "replace")
        except Exception as e:
            result["error"] = repr(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout + 4)
    if t.is_alive():
        return None, "watchdog timeout"
    if "error" in result:
        return None, result["error"]
    return result.get("body"), None


def _push_serverchan(cfg, title, content):
    key = (cfg.get("serverchan_sendkey") or "").strip()
    if not key:
        return False, "not configured: serverchan_sendkey"
    api = cfg.get("serverchan_api") or DEFAULT_CONFIG["serverchan_api"]
    url = api.format(key=urllib.parse.quote(key)) if "{key}" in api else api
    sep = "&" if "?" in url else "?"
    url += sep + urllib.parse.urlencode({"title": title, "desp": content})
    body, err = _http_request(url, int(cfg.get("http_timeout_seconds", 8)))
    if err:
        return False, "network error: %s" % err
    try:
        code = json.loads(body).get("code")
        return code == 0, "serverchan code=%s body=%s" % (code, (body or "")[:200])
    except Exception as e:
        return False, "bad response: %r body=%s" % (e, (body or "")[:200])


def _push_pushplus(cfg, title, content):
    token = (cfg.get("pushplus_token") or "").strip()
    if not token:
        return False, "not configured: pushplus_token"
    api = cfg.get("pushplus_api") or DEFAULT_CONFIG["pushplus_api"]
    body = json.dumps({"token": token, "title": title, "content": content},
                      ensure_ascii=False).encode("utf-8")
    resp, err = _http_request(
        api,
        int(cfg.get("http_timeout_seconds", 8)),
        data=body,
        headers={"Content-Type": "application/json"},
    )
    if err:
        return False, "network error: %s" % err
    try:
        code = json.loads(resp).get("code")
        return code == 200, "pushplus code=%s body=%s" % (code, (resp or "")[:200])
    except Exception as e:
        return False, "bad response: %r body=%s" % (e, (resp or "")[:200])


def build_message(cfg, st):
    cwd = st.get("cwd") or ""
    client = st.get("client") or ""
    text = (st.get("last_assistant_message") or "").strip()
    max_chars = int(cfg.get("max_message_chars", 600) or 600)
    if len(text) > max_chars:
        text = text[:max_chars] + "…（已截断）"
    project = os.path.basename(cwd.rstrip("/\\")) if cwd else ""
    title = ("Codex 完成：" + project) if project else "Codex 任务完成"
    lines = []
    lines.append("完成时间：" + time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(st.get("ts", time.time()))))
    if cwd:
        lines.append("工作目录：" + cwd)
    if client:
        lines.append("客户端：" + client)
    duration_ms = st.get("duration_ms")
    if duration_ms:
        lines.append("耗时：%.1f 秒" % (float(duration_ms) / 1000.0))
    if text:
        lines.append("")
        lines.append("Codex 结论：")
        lines.append(text)
    return title, "\n".join(lines)


def send_notification(cfg, st):
    title, content = build_message(cfg, st)
    service = cfg.get("service", "serverchan")
    if service == "pushplus":
        return _push_pushplus(cfg, title, content), title, content
    return _push_serverchan(cfg, title, content), title, content


def run_finalize(cfg, thread_id, turn_id, debounce):
    log("FINALIZE start thread=%s turn=%s wait=%ss" % (thread_id, turn_id, debounce))
    try:
        time.sleep(max(0, int(debounce)))
    except KeyboardInterrupt:
        log("FINALIZE interrupted")
        return
    st = read_state(thread_id)
    if not st or st.get("turn_id") != turn_id:
        log("FINALIZE abort: superseded thread=%s turn=%s" % (thread_id, turn_id))
        return
    # 从 rollout 补全/校验最新一轮（文件可能刚写入，最多等 3 秒）
    deadline = time.time() + 3.0
    payload = None
    while time.time() < deadline:
        payload = read_turn_payload(thread_id)
        if payload and payload.get("turn_id"):
            break
        time.sleep(0.3)
    if payload and payload.get("turn_id"):
        p_turn = payload["turn_id"]
        if p_turn != turn_id and not str(turn_id).startswith("turn-"):
            log("FINALIZE abort: newer turn in rollout thread=%s turn=%s"
                % (thread_id, turn_id))
            return
        st["turn_id"] = p_turn
        if payload.get("cwd"):
            st["cwd"] = payload["cwd"]
        if payload.get("originator") or payload.get("source"):
            st["client"] = payload.get("originator") or payload.get("source")
        if payload.get("last_assistant_message"):
            st["last_assistant_message"] = payload["last_assistant_message"]
        if payload.get("duration_ms"):
            st["duration_ms"] = payload["duration_ms"]
        save_state(thread_id, st)
    # 先构建消息做去重判断，再真正发送
    _title, _content = build_message(cfg, st)
    window = int(cfg.get("dedupe_seconds", 90) or 90)
    sent = prune_sent_hashes(time.time(), window)
    h = content_hash(_title, _content)
    if h in sent:
        log("SEND SKIP duplicate thread=%s turn=%s" % (thread_id, turn_id))
        return
    (ok, detail), _title, _content = send_notification(cfg, st)
    if ok:
        sent[h] = time.time()
        _save_sent_hashes(sent)
        st["sent"] = True
        st["sent_at"] = time.time()
        save_state(thread_id, st)
        log("SENT thread=%s turn=%s service=%s" % (thread_id, turn_id,
                                                   cfg.get("service")))
        return
    log("SEND FAILED thread=%s turn=%s detail=%s" % (thread_id, turn_id, detail))
    if detail.startswith("not configured"):
        return
    time.sleep(5)
    (ok2, detail2), _, _ = send_notification(cfg, st)
    if ok2:
        sent2 = prune_sent_hashes(time.time(), window)
        sent2[h] = time.time()
        _save_sent_hashes(sent2)
        st["sent"] = True
        st["sent_at"] = time.time()
        save_state(thread_id, st)
        log("SENT(retry) thread=%s turn=%s" % (thread_id, turn_id))
    else:
        log("SEND FAILED(retry) thread=%s turn=%s detail=%s"
            % (thread_id, turn_id, detail2))


def main():
    cfg = load_config()
    args = sys.argv[1:]
    if args and args[0] == "finalize":
        if len(args) < 4:
            log("ERR finalize needs: thread_id turn_id debounce")
            return 1
        run_finalize(cfg, args[1], args[2], int(args[3]))
        return 0
    on_turn(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
