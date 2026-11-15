#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
支持 HTTP Range / 206 的极简静态文件服务（纯标准库，单文件）。

何时用：
  * 在手机 / 平板 / Safari 上直接播放本地视频：Safari 会先发 `Range: bytes=0-1`
    试探，服务端不回 206 就整个播不了；
  * 需要拖进度条、断点续传、大文件分段读取；
  * `python3 -m http.server` 不够用（它完全不支持 Range），又不想装 nginx。

安全默认值（重要）：
  * 默认只绑定 127.0.0.1；要局域网访问必须显式 --bind 0.0.0.0 并自行评估；
  * 所有路径经 realpath 规范化后必须仍在 --root 内，杜绝 `../` 穿越与符号链接逃逸；
  * 可选 --auth user:pass 开 Basic Auth（明文 HTTP，仅用于可信局域网）；
  * 不提供上传 / 删除 / 执行，只 GET 与 HEAD。

实测坑：
  * CSS 里的 `width:100%` 与 Python 老式 `%` 字符串格式化冲突（ValueError:
    unsupported format character），用 f-string 或把 `%` 写成 `%%`；
  * 未知类型要 fallback 到 application/octet-stream，否则 Safari 拒绝播放；
  * 416 必须带 `Content-Range: bytes */SIZE`；206 必须带完整 `bytes S-E/SIZE`；
  * 写文件先落 `.part` 再原子改名（见 download 工具），服务端同理不要暴露半截文件。

用法：
  python3 http_range_server.py --root /path/to/videos --port 8080
  python3 http_range_server.py --root . --port 8080 --bind 0.0.0.0 --auth me:s3cret
  curl -H 'Range: bytes=0-99' -I http://127.0.0.1:8080/a.mp4
"""
import argparse
import base64
import html
import mimetypes
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_range(value, size):
    """解析 Range，返回 (start, end)；None 表示不可满足（回 416）。"""
    if not value:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)$", value.strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "":
        if b == "":
            return None
        n = int(b)
        if n <= 0:
            return None
        start, end = max(0, size - n), size - 1
    else:
        start = int(a)
        end = int(b) if b else size - 1
        end = min(end, size - 1)
    if start >= size or start > end:
        return None
    return start, end


class Handler(BaseHTTPRequestHandler):
    root = os.getcwd()
    auth = None
    allow_list = True
    server_version = "RangeServer/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authorized(self):
        if not self.auth:
            return True
        raw = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(self.auth.encode()).decode()
        if raw == expected:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="RangeServer"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _resolve(self):
        """把 URL 路径映射到 root 内的真实路径；越界返回 None。"""
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        full = os.path.realpath(os.path.join(self.root, path.lstrip("/")))
        root = os.path.realpath(self.root)
        if full != root and not full.startswith(root + os.sep):
            return None
        return full

    def _send_file(self, full, head_only=False):
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            r = parse_range(rng, size)
            if r is None:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end, status = r[0], r[1], 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head_only:
            return
        with open(full, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                b = f.read(min(65536, left))
                if not b:
                    break
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    return          # 用户拖进度条 / 关页面，正常现象
                left -= len(b)

    def _send_listing(self, full, rel):
        items = sorted(os.listdir(full))
        rows = []
        for n in items:
            if n.startswith("."):
                continue
            p = os.path.join(full, n)
            label = n + ("/" if os.path.isdir(p) else "")
            href = urllib.parse.quote(n) + ("/" if os.path.isdir(p) else "")
            size = "" if os.path.isdir(p) else str(os.path.getsize(p))
            rows.append("<li><a href=" + '"' + html.escape(href) + '">' +
                        html.escape(label) + "</a> <small>" + size + "</small></li>")
        # 用拼接而不是 % 格式化：CSS 里的 width:100% 会被 % 当成格式符
        body = ("<!doctype html><meta charset=utf-8><title>Index</title>"
                "<style>body{font-family:monospace;padding:1em}a{text-decoration:none}"
                "li{line-height:1.8}small{color:#888}</style>"
                "<h2>Index of /" + html.escape(rel) + "</h2><ul>" +
                "".join(rows) + "</ul>")
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not self._is_head:
            self.wfile.write(data)

    def do_HEAD(self):
        self._is_head = True
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        self._is_head = head_only
        if not self._authorized():
            return
        full = self._resolve()
        if full is None:
            self.send_error(403, "Forbidden")
            return
        if os.path.isdir(full):
            if not self.allow_list:
                self.send_error(403, "Listing disabled")
                return
            self._send_listing(full, os.path.relpath(full, self.root))
            return
        if not os.path.isfile(full):
            self.send_error(404, "Not Found")
            return
        self._send_file(full, head_only)


def main():
    ap = argparse.ArgumentParser(description="支持 Range/206 的极简静态文件服务")
    ap.add_argument("--root", default=".", help="服务根目录")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1", help="默认仅本机")
    ap.add_argument("--auth", help="Basic Auth，格式 user:pass")
    ap.add_argument("--no-list", action="store_true", help="禁用目录列表")
    a = ap.parse_args()

    Handler.root = os.path.abspath(a.root)
    Handler.auth = a.auth
    Handler.allow_list = not a.no_list
    if not os.path.isdir(Handler.root):
        print("根目录不存在：", Handler.root, file=sys.stderr)
        return 2
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print("服务中： http://%s:%d/  根目录=%s  auth=%s" %
          (a.bind, a.port, Handler.root, "on" if a.auth else "off"), file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
