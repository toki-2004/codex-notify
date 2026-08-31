# -*- coding: utf-8 -*-
"""本地假推送服务器：验证 notify.py 的 HTTP 推送路径（不发真实网络）。

用法：python test/fake_server.py [port]
收到的请求会追加写入 logs/fake_req.txt，便于断言。
"""

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "fake_req.txt",
)


class Handler(BaseHTTPRequestHandler):
    def _record(self, body=b""):
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        query = urllib.parse.urlsplit(self.path).query
        with open(OUT, "ab") as f:
            f.write(("PATH %s\nQUERY %s\nBODY %r\n"
                     % (self.path, query, body.decode("utf-8", "replace")))
                    .encode("utf-8"))

    def _reply(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._record()
        self._reply({"code": 0, "message": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self._record(self.rfile.read(length))
        self._reply({"code": 200, "msg": "success"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print("fake server on 127.0.0.1:%d" % port)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
