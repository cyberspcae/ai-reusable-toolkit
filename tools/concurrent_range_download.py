#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单文件、纯标准库的「并发分块下载器」——断点续传 + sha256 校验 + 原子落盘。

何时用：
  * 单连接被限速（境外源、模型权重、大视频），多线程按 Range 分片能把带宽吃满；
  * 网络不稳、任务可能被中断，需要「重跑即续传」；
  * AI Agent 在受限环境里下载大文件，装不了 aria2 / requests / axel。

何时别用：
  * 服务端不支持 Range（会自动退化为单连接，不会更快）；
  * 小文件（分片与合并的开销不划算）。

设计要点（都是实测踩出来的）：
  * 探测用 `Range: bytes=0-0` 而不是 HEAD —— 很多 CDN 禁 HEAD，但 Range 会返回
    `Content-Range: bytes 0-0/TOTAL`，一次请求同时拿到「总大小」和「是否支持 206」。
  * 分片先写 `.tmp` 再 `os.replace()`，中断时不会留下半截分片被误判为完成。
  * 合并写 `.part`，校验通过才改名为最终文件；校验失败保留分片目录，重跑续传。
  * 从不把用户输入拼进 shell，全程 urllib。

用法：
  python3 concurrent_range_download.py URL -o out.bin
  python3 concurrent_range_download.py URL -o out.bin --workers 24 --sha256 <hex>
  python3 concurrent_range_download.py URL -o out.bin --header 'Cookie: a=b' --json

可作为模块：
  from concurrent_range_download import download, probe
  download(url, "out.bin", workers=16)
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

MB = 1024 * 1024
DEFAULT_CHUNK = 8 * MB


def _open(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def probe(url, headers=None, timeout=30):
    """探测 (总大小, 是否支持 Range, ETag)。失败时返回 size=None。"""
    h = dict(headers or {})
    h["Range"] = "bytes=0-0"
    try:
        r = _open(url, h, timeout)
        try:
            cr = r.headers.get("Content-Range") or ""
            if r.status == 206 and "/" in cr:
                total = int(cr.rsplit("/", 1)[1])
                return {"size": total, "ranges": True,
                        "etag": r.headers.get("ETag"), "error": None}
            # 返回 200 说明忽略了 Range
            cl = r.headers.get("Content-Length")
            return {"size": int(cl) if cl and cl.isdigit() else None,
                    "ranges": False, "etag": r.headers.get("ETag"), "error": None}
        finally:
            r.close()
    except Exception as e:
        return {"size": None, "ranges": False, "etag": None,
                "error": "%s: %s" % (type(e).__name__, e)}


def _single(url, out, sha256, headers, timeout, log):
    """不支持 Range 时的退化路径：单连接流式下载。"""
    tmp = out + ".part"
    h = hashlib.sha256() if sha256 else None
    total = 0
    with _open(url, headers, timeout) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1024 * 1024)
            if not b:
                break
            f.write(b)
            if h:
                h.update(b)
            total += len(b)
    if sha256 and h.hexdigest() != sha256.lower():
        os.remove(tmp)
        raise RuntimeError("sha256 校验失败（单连接模式）")
    os.replace(tmp, out)
    log("完成（单连接），%d 字节" % total)
    return {"mode": "single", "bytes": total, "verified": bool(sha256)}


def download(url, out, workers=16, chunk=DEFAULT_CHUNK, sha256=None,
             headers=None, retries=4, timeout=90, quiet=False, keep_chunks=False):
    """下载 url 到 out。返回结构化结果字典。"""
    def log(*a):
        if not quiet:
            print(*a, file=sys.stderr)

    if os.path.exists(out) and sha256:
        # 手写流式哈希，兼容 Python 3.8+（不依赖 3.11 的 hashlib.file_digest）
        h = hashlib.sha256()
        with open(out, "rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    break
                h.update(b)
        if h.hexdigest() == sha256.lower():
            return {"mode": "skip", "bytes": os.path.getsize(out),
                    "verified": True, "reason": "目标已存在且校验通过"}

    info = probe(url, headers, timeout=30)
    if info["error"]:
        log("探测失败：", info["error"], "—— 仍尝试单连接")
    size, supports = info["size"], info["ranges"]
    if not supports or not size:
        return _single(url, out, sha256, headers, timeout, log)

    cdir = out + ".chunks"
    os.makedirs(cdir, exist_ok=True)
    n = (size + chunk - 1) // chunk
    log("分块下载：%d 字节 / %d 块 / %d 并发%s" %
        (size, n, workers, "（已有分片会跳过）" if os.listdir(cdir) else ""))

    lock = threading.Lock()
    state = {"done": 0, "bytes": 0, "t0": time.time()}

    def one(i):
        s, e = i * chunk, min((i + 1) * chunk, size) - 1
        expect = e - s + 1
        p = os.path.join(cdir, "%06d" % i)
        if os.path.exists(p) and os.path.getsize(p) == expect:
            with lock:
                state["done"] += 1
            return 0, True
        last = None
        for attempt in range(retries):
            try:
                hh = dict(headers or {})
                hh["Range"] = "bytes=%d-%d" % (s, e)
                with _open(url, hh, timeout) as r:
                    if r.status != 206:
                        raise RuntimeError("期望 206，实际 %d（服务端中途不再支持 Range）" % r.status)
                    data = r.read()
                if len(data) != expect:
                    raise ValueError("分块 %d 长度 %d != %d" % (i, len(data), expect))
                tmp = p + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, p)
                with lock:
                    state["done"] += 1
                    state["bytes"] += len(data)
                    el = time.time() - state["t0"]
                    log("\r  %d/%d 块  %.1f MiB  %.0f KiB/s   " %
                        (state["done"], n, state["bytes"] / MB,
                         state["bytes"] / 1024 / max(el, 0.001)))
                return len(data), False
            except Exception as ex:
                last = ex
                time.sleep(1 + attempt)
        raise RuntimeError("分块 %d 失败（重试 %d 次）：%s" % (i, retries, last))

    downloaded = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, i) for i in range(n)]
        for f in as_completed(futs):
            b, _cached = f.result()
            downloaded += b
    log("")

    h = hashlib.sha256() if sha256 else None
    tmp = out + ".part"
    with open(tmp, "wb") as fo:
        for i in range(n):
            with open(os.path.join(cdir, "%06d" % i), "rb") as fi:
                while True:
                    b = fi.read(1024 * 1024)
                    if not b:
                        break
                    fo.write(b)
                    if h:
                        h.update(b)
    if sha256 and h.hexdigest() != sha256.lower():
        os.remove(tmp)
        raise RuntimeError("sha256 校验失败；分片保留在 %s，重跑可续传" % cdir)
    os.replace(tmp, out)
    if not keep_chunks:
        shutil.rmtree(cdir, ignore_errors=True)
    return {"mode": "ranges", "bytes": size, "chunks": n,
            "downloaded": downloaded, "verified": bool(sha256)}


def main():
    ap = argparse.ArgumentParser(description="并发分块下载（断点续传 + sha256）")
    ap.add_argument("url")
    ap.add_argument("-o", "--output", required=True, help="输出文件路径")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="分块字节数，默认 8 MiB")
    ap.add_argument("--sha256", help="期望的 sha256，提供则强制校验")
    ap.add_argument("--header", action="append", default=[],
                    help="附加请求头 'K: V'，可重复")
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--keep-chunks", action="store_true", help="成功后保留分片目录")
    ap.add_argument("--json", action="store_true", help="结构化输出（供程序解析）")
    a = ap.parse_args()

    headers = {}
    for item in a.header:
        if ":" in item:
            k, v = item.split(":", 1)
            headers[k.strip()] = v.strip()
    try:
        r = download(a.url, a.output, workers=a.workers, chunk=a.chunk,
                     sha256=a.sha256, headers=headers, retries=a.retries,
                     timeout=a.timeout, quiet=a.json, keep_chunks=a.keep_chunks)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json
              else "失败：%s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(dict(ok=True, **r), ensure_ascii=False))
    else:
        print("完成：%s（%d 字节，%s）" %
              (a.output, r["bytes"], "已校验" if r["verified"] else "未校验"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
