#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把浏览器「Copy as cURL」复制出来的整条命令，转换成等价、可粘贴的 wget 命令。

何时用：
  * 目标环境只有 wget 没有 curl（iSH / Alpine / BusyBox / 精简容器）；
  * curl 因为 TLS、代理、证书、HTTP/2 等问题失败，而 wget 能成功；
  * 需要一个「不执行、先给人看」的命令预览（凭证默认打码）。

为何不能直接照搬（实测坑）：
  * `Range: bytes=0-1` 是浏览器播放器发的，照搬只能下到一小段残片；
  * `Host` 手动指定会在重定向后指向错误的主机；
  * `Accept-Encoding: gzip` —— wget 不会解压，存下来的文件是压缩包；
  * `Connection / Content-Length / Transfer-Encoding` 属于逐跳头，交给 wget 自己管；
  * BusyBox wget 只认 `--header 'K: V'`（两个参数）、`-O`、`-T`；
    `--header=X`、`--tries`、`--no-verbose`、`--no-check-certificate` 会直接报错。

通用性：
  * 纯标准库；用 shlex 解析，绝不把用户输入拼进 shell（无命令注入）；
  * 支持 bash `\\` 续行与 Windows `^` 续行；支持直接粘一个 http(s) 直链；
  * 打印时自动给 Cookie / Authorization / Token / API-Key 打码，执行时用真值；
  * 遇到 POST（curl -d）时给出 wget --post-data 形式，并提供 Python 兜底函数
    post_download()，供极简 wget 不支持 POST 时使用。

用法：
  python3 curl_to_wget.py 'curl -H "..." https://x/y.mp4'
  python3 curl_to_wget.py -f cmd.txt -o a.mp4 --run
  python3 curl_to_wget.py 'curl ...' --json          # 结构化输出，供 AI 解析
  python3 curl_to_wget.py 'curl ...' --gnu           # GNU wget 专有参数
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request

# 照搬会出问题的头
STRIP_HEADERS = {
    "range", "host", "connection", "keep-alive", "proxy-connection",
    "transfer-encoding", "te", "trailer", "upgrade", "content-length",
    "proxy-authorization", "accept-encoding", "sec-fetch-dest",
    "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
}
SECRET_WORDS = ("cookie", "authorization", "proxy-authorization",
                "x-auth", "token", "api-key", "x-api-key", "set-cookie")

# 带值的 curl 选项（解析时连同值一起跳过，避免把值误当成 URL）
TAKES_VALUE = {
    "-o", "--output", "-w", "--write-out", "-D", "--dump-header",
    "-c", "--cookie-jar", "-K", "--config", "-F", "--form", "-T", "--upload-file",
    "-u", "--user", "-x", "--proxy", "--resolve", "--cacert", "--cert",
    "--key", "--limit-rate", "--max-time", "--connect-timeout",
    "--max-filesize", "--retry", "--retry-delay", "--retry-max-time",
    "--interface", "--dns-servers", "-t", "--telnet-option", "-Q", "--quote",
    "-r", "--range", "-X", "--request", "-A", "--user-agent",
    "-e", "--referer", "-b", "--cookie", "-H", "--header", "--url",
}


def parse_curl(text):
    """解析为 dict：url / headers / method / post_data / out_hint / stripped / error。"""
    s = text.strip()
    s = re.sub(r"[ \t]*[\^\\][ \t]*\r?\n[ \t]*", " ", s)   # ^ 与 \ 续行
    s = s.replace("\\\n", " ").replace("\r", " ")
    try:
        toks = shlex.split(s, posix=True)
    except ValueError as e:
        return {"error": "命令解析失败（引号不配对？）：%s" % e}
    if toks and os.path.basename(toks[0]).lower() in ("curl", "curl.exe"):
        toks = toks[1:]

    url, headers, method, post_data, out_hint = None, {}, "GET", None, None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-H", "--header") and i + 1 < len(toks):
            v = toks[i + 1]
            if ":" in v:
                k, val = v.split(":", 1)
                headers[k.strip()] = val.strip()
            i += 2
            continue
        if t in ("-A", "--user-agent") and i + 1 < len(toks):
            headers["User-Agent"] = toks[i + 1]; i += 2; continue
        if t in ("-e", "--referer") and i + 1 < len(toks):
            headers["Referer"] = toks[i + 1]; i += 2; continue
        if t in ("-b", "--cookie") and i + 1 < len(toks):
            headers["Cookie"] = toks[i + 1]; i += 2; continue
        if t in ("-o", "--output") and i + 1 < len(toks):
            out_hint = toks[i + 1]; i += 2; continue
        if t in ("-X", "--request") and i + 1 < len(toks):
            method = toks[i + 1]; i += 2; continue
        if t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii") and i + 1 < len(toks):
            post_data = toks[i + 1]
            method = method if method != "GET" else "POST"
            i += 2; continue
        if t == "--url" and i + 1 < len(toks):
            url = toks[i + 1]; i += 2; continue
        if t in ("-r", "--range"):
            i += 2; continue                      # 忽略，改为完整下载
        if t in TAKES_VALUE:
            i += 2; continue
        if t.startswith("-"):
            i += 1; continue
        if url is None and (t.startswith("http://") or t.startswith("https://")):
            url = t
        i += 1

    if not url:
        return {"error": "没找到 http(s) 开头的 URL"}
    stripped = sorted({k for k in headers if k.lower() in STRIP_HEADERS})
    cleaned = {k: v for k, v in headers.items() if k.lower() not in STRIP_HEADERS}
    return {"url": url, "headers": cleaned, "method": method,
            "post_data": post_data, "out_hint": out_hint,
            "stripped": stripped, "error": None}


def default_name(url):
    n = os.path.basename(urllib.parse.urlsplit(url).path)
    n = urllib.parse.unquote(n) or "download"
    return n if os.path.splitext(n)[1] else n + ".bin"


def build_wget(parsed, out, timeout=1800, gnu=False):
    """返回 wget 参数数组（不拼 shell 字符串，交给 subprocess 直接执行）。"""
    argv = ["wget"]
    for k, v in parsed["headers"].items():
        argv += ["--header", "%s: %s" % (k, v)]
    if parsed["post_data"] is not None:
        argv += ["--post-data", parsed["post_data"]]
    argv += ["-O", out, "-T", str(timeout)]
    if gnu:
        argv += ["--tries=1", "--no-verbose"]
    argv.append(parsed["url"])
    return argv


def fmt_cmd(argv, show_secrets=False):
    """格式化为可粘贴命令；敏感头默认打码。"""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if not show_secrets and a == "--header" and i + 1 < len(argv):
            kv = argv[i + 1]
            if ":" in kv:
                k, v = kv.split(":", 1)
                if any(w in k.lower() for w in SECRET_WORDS):
                    kv = "%s: <已打码，%d 字符>" % (k, len(v.strip()))
            out.append(shlex.quote(a) + " " + shlex.quote(kv))
            i += 2
            continue
        if not show_secrets and a == "--post-data" and i + 1 < len(argv):
            out.append(shlex.quote(a) + " " + shlex.quote("<已打码>"))
            i += 2
            continue
        out.append(shlex.quote(a))
        i += 1
    return " ".join(out)


def post_download(parsed, out, timeout=60):
    """极简 wget 不支持 POST 时的标准库兜底：带同样的头发起 POST 并落盘。"""
    data = (parsed["post_data"] or "").encode()
    h = dict(parsed["headers"])
    h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(parsed["url"], data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out + ".part", "wb") as f:
        while True:
            b = r.read(1024 * 1024)
            if not b:
                break
            f.write(b)
    os.replace(out + ".part", out)
    return os.path.getsize(out)


def safe_url(url):
    p = urllib.parse.urlsplit(url)
    return "%s://%s%s" % (p.scheme, p.netloc, p.path)   # 去掉查询串里的 token


def main():
    ap = argparse.ArgumentParser(description="curl 命令转 wget 命令（含脱敏）")
    ap.add_argument("input", nargs="?", help="整条 curl 命令，或一个 http(s) 直链")
    ap.add_argument("-f", "--file", help="从文件读取 curl 命令")
    ap.add_argument("-o", "--output", help="输出文件名")
    ap.add_argument("--run", action="store_true", help="直接执行生成的 wget 命令")
    ap.add_argument("--show-secrets", action="store_true", help="打印真实凭证（谨慎）")
    ap.add_argument("--timeout", type=int, default=1800, help="wget 超时秒数")
    ap.add_argument("--gnu", action="store_true", help="加 GNU wget 专有参数")
    ap.add_argument("--json", action="store_true", help="结构化输出，供程序解析")
    a = ap.parse_args()

    text = a.input or ""
    if a.file:
        try:
            text = open(a.file, "r", encoding="utf-8").read()
        except OSError as e:
            print("读取文件失败：", e, file=sys.stderr)
            return 1
    if not text.strip():
        print("没有输入。示例：python3 curl_to_wget.py 'curl ...'", file=sys.stderr)
        return 1

    p = parse_curl(text)
    if p["error"]:
        print("解析失败：", p["error"], file=sys.stderr)
        return 1
    if p["method"].upper() not in ("GET", "HEAD", "POST"):
        print("提示：curl 使用 %s，wget 只能 GET/POST，已按 GET 处理" % p["method"],
              file=sys.stderr)

    out = a.output or p["out_hint"] or default_name(p["url"])
    argv = build_wget(p, out, a.timeout, a.gnu)

    if a.json:
        print(json.dumps({
            "ok": True, "url": safe_url(p["url"]), "output": out,
            "method": p["method"], "headers": list(p["headers"]),
            "stripped": p["stripped"], "post": p["post_data"] is not None,
            "command": fmt_cmd(argv, a.show_secrets), "argv": argv if a.show_secrets else None,
        }, ensure_ascii=False))
    else:
        print("URL        :", safe_url(p["url"]))
        print("保存为     :", out)
        if p["stripped"]:
            print("已自动剥掉 :", ", ".join(p["stripped"]))
        print("保留请求头 :", ", ".join(p["headers"]) or "(无)")
        print("\n等效 wget 命令：\n" + fmt_cmd(argv, a.show_secrets))
        if not a.show_secrets:
            print("\n提示：Cookie / Authorization 已打码；--run 执行时用的是真值。")

    if a.run:
        print("\n开始下载…", file=sys.stderr)
        rc = subprocess.call(argv)
        print("wget 退出码:", rc, file=sys.stderr)
        if rc == 0 and os.path.isfile(out):
            print("完成：%s（%d 字节）" % (out, os.path.getsize(out)), file=sys.stderr)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
