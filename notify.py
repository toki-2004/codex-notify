# -*- coding: utf-8 -*-
"""Codex 手机通知：notify hook 入口 + 同任务去重 + 延迟发送结论。

触发方式（写入 ~/.codex/config.toml）：
    notify = ["C:/Users/TOKI/miniconda3/python.exe",
              "D:/pythonitems/codex-notify/notify.py"]

每次 Codex 完成一轮（agent-turn-complete）都会调用本脚本，脚本只负责：
1. 把本轮信息（thread_id / turn_id / cwd / 结论）写入 state/<thread_id>.json；
2. 启动一个脱离当前进程的 finalizer，等待 debounce_seconds 秒；
   期间若又有新的一轮完成（turn_id 变化），旧 finalizer 醒来发现已过期会放弃，
   只有"最后一个 turn 后静默满 debounce 秒"的那个 finalizer 才真正推送。
   这就是"同一任务只发最终结论"的去重逻辑。

推送渠道：Server酱（微信服务号消息）或 PushPlus，配置见 config.json。
本机网络注意：系统 DNS 冷解析极慢，推送前先走 127.0.0.100:53 快速解析
（方案来自 desktop-pet/weather.py），并带看门狗超时，绝不让推送拖住 Codex。
"""

import json
import os
import random
import socket
import struct
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
    "debounce_seconds": 90,           # 同一任务连续多轮，静默满该秒数才发结论
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


# ---------------- 快速 DNS：直查本机隧道解析器，绕开系统慢解析 ----------------

FAST_DNS_SERVER = ("127.0.0.100", 53)
FAST_DNS_TIMEOUT_S = 2.0


def _build_dns_query(host):
    tid = random.randrange(0x10000)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in host.split("."))
    return tid, header + qname + b"\x00" + struct.pack(">HH", 1, 1)


def _skip_name(resp, pos):
    """跳过 DNS 报文里的名字（含压缩指针）。"""
    while pos < len(resp):
        length = resp[pos]
        if length & 0xC0 == 0xC0:  # 压缩指针：0b11 开头
            return pos + 2
        pos += length + 1
        if length == 0:
            return pos
    return pos


def _parse_a_records(resp):
    if len(resp) < 12:
        return []
    _tid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", resp[:12])
    if not (flags & 0x8000) or qd == 0:
        return []
    pos = 12
    for _ in range(qd):  # 跳过问题段
        pos = _skip_name(resp, pos) + 4
    addrs = []
    for _ in range(an):
        pos = _skip_name(resp, pos)
        if pos + 10 > len(resp):
            break
        typ, _cls, _ttl, rdlen = struct.unpack(">HHIH", resp[pos:pos + 10])
        pos += 10
        rdata = resp[pos:pos + rdlen]
        pos += rdlen
        if typ == 1 and len(rdata) == 4:  # A 记录
            addrs.append(".".join(str(b) for b in rdata))
    return addrs


def fast_resolve(host):
    """UDP 直查本机隧道 DNS，返回 A 记录列表；失败返回空列表。"""
    sock = None
    try:
        if not isinstance(host, str) or not host:
            return []
        tid, query = _build_dns_query(host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(FAST_DNS_TIMEOUT_S)
        sock.sendto(query, FAST_DNS_SERVER)
        resp, _ = sock.recvfrom(4096)
        if len(resp) >= 2 and struct.unpack(">H", resp[:2])[0] == tid:
            return _parse_a_records(resp)
        return []
    except Exception:
        return []
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


_real_getaddrinfo = socket.getaddrinfo


def _fast_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """优先用隧道 DNS 解析；失败直接报错（系统解析慢达 10-20s，会拖住进程）。"""
    if isinstance(host, str) and host:
        parts = host.split(".")
        is_ip = len(parts) == 4 and all(p.isdigit() for p in parts)
        if not is_ip:
            ips = fast_resolve(host)
            if ips:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port)))
                        for ip in ips]
            raise socket.gaierror("fast DNS failed: %s" % host)
    return _real_getaddrinfo(host, port, family, type, proto, flags)


class fast_dns:
    """上下文管理器：HTTP 请求期间把 socket.getaddrinfo 换成快速解析。"""

    def __enter__(self):
        socket.getaddrinfo = _fast_getaddrinfo

    def __exit__(self, *exc):
        socket.getaddrinfo = _real_getaddrinfo


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
    """从环境变量里找出本轮信息（兼容不同前缀写法，按后缀匹配）。"""
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
    thread_id = info.get("thread_id")
    turn_id = info.get("turn_id")
    if not thread_id or not turn_id:
        log("WARN on_turn skipped: no thread_id/turn_id in env")
        return
    data = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "ts": time.time(),
        "cwd": info.get("cwd", ""),
        "client": info.get("client", ""),
        "last_assistant_message": info.get("last_assistant_message", ""),
    }
    save_state(thread_id, data)
    debounce = int(cfg.get("debounce_seconds", 90) or 90)
    log("TURN thread=%s turn=%s cwd=%s debounce=%ss"
        % (thread_id, turn_id, info.get("cwd", ""), debounce))
    spawn_finalizer(thread_id, turn_id, debounce)


# ---------------- HTTP（带快速 DNS 与看门狗） ----------------

def _http_request(url, timeout, data=None, headers=None):
    """在子线程里发请求并限时等待；返回 (body, error)。"""
    result = {}

    def worker():
        try:
            with fast_dns():
                req = urllib.request.Request(url, data=data, headers=headers or {})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    (ok, detail), _title, _content = send_notification(cfg, st)
    if ok:
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
