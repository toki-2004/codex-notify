# -*- coding: utf-8 -*-
"""自检：推送渠道路由（本地假服务器，不联网）。

用法：python test/test_providers.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402

PORT = 8766
SEEN = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        SEEN.append((self.path, body))
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        code = 1000 if "appToken" in payload else 200  # WxPusher=1000, PushPlus=200
        data = json.dumps({"code": code, "msg": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def state():
    return {"cwd": r"D:\pythonitems\demo", "client": "self-test",
            "ts": 0, "last_assistant_message": "结论文本"}


passed = 0
failed = []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print("  PASS", name)
    else:
        failed.append(name)
        print("  FAIL", name, detail)


def main():
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = dict(notify.DEFAULT_CONFIG)
    base["http_timeout_seconds"] = 5
    local = "http://127.0.0.1:%d" % PORT
    try:
        # 1. WxPusher：成功码 1000 + uids 解析（逗号分隔）
        cfg = dict(base)
        cfg.update({"service": "wxpusher", "wxpusher_apptoken": "AT_test",
                    "wxpusher_uids": "UID_a, UID_b",
                    "wxpusher_api": local + "/api/send/message"})
        (ok, detail), _t, content = notify.send_notification(cfg, state())
        check("wxpusher sent", ok, detail)
        sent = json.loads(SEEN[-1][1]) if SEEN else {}
        check("wxpusher uids parsed", sent.get("uids") == ["UID_a", "UID_b"], sent.get("uids"))
        check("wxpusher no pay check", sent.get("verifyPayType") == 0, sent.get("verifyPayType"))
        check("message carries the conclusion", "结论文本" in content, content)

        # 2. WxPusher 缺 key/UID：明确报未配置（主流程不重试）
        cfg2 = dict(base)
        cfg2.update({"service": "wxpusher", "wxpusher_apptoken": "", "wxpusher_uids": ""})
        (ok2, detail2), _, _ = notify.send_notification(cfg2, state())
        check("wxpusher not configured", not ok2 and detail2.startswith("not configured"),
              detail2)

        # 3. PushPlus 仍可用（成功码 200）
        cfg3 = dict(base)
        cfg3.update({"service": "pushplus", "pushplus_token": "tok",
                     "pushplus_api": local + "/send"})
        (ok3, detail3), _, _ = notify.send_notification(cfg3, state())
        check("pushplus still works", ok3, detail3)

        # 4. 未知渠道：明确报错，不当成 Server酱 发出去
        cfg4 = dict(base)
        cfg4.update({"service": "nope", "serverchan_sendkey": "x",
                     "serverchan_api": local + "/send"})
        before = len(SEEN)
        (ok4, detail4), _, _ = notify.send_notification(cfg4, state())
        check("unknown service rejected",
              (not ok4) and detail4.startswith("unknown service") and len(SEEN) == before,
              detail4)
    finally:
        srv.shutdown()

    print("")
    print("RESULT: %d passed, %d failed" % (passed, len(failed)))
    if failed:
        print("FAILED:", " | ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
