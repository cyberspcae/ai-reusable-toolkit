#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 GitHub Contents API 批量上传文件：自动建仓、自动更新、逐字节校验。

何时用：
  * 需要把一批本地文件推到 GitHub，但环境里没有 git / 没有 SSH / git push 被墙；
  * AI Agent 需要「提交完还能证明远端内容确实一致」的可验证交付；
  * 只要 REST API 通（api.github.com 可达），HTTPS 出去即可，不需要 22 端口。

为何值得单独存一份（实测坑）：
  * 更新已存在的文件必须在 payload 里带 `sha`，否则 422；先 GET 拿 sha 是必修课；
  * `content` 必须是原始字节的 base64，不能用文本编码后的字符串；
  * API 偶尔抖动（超时 / 403），必须带重试；
  * 上传完「看起来 201」不等于内容对 —— 要 GET 回来 base64 解码做逐字节比较；
  * 令牌绝不写进代码、绝不打印；从环境变量或 0600 权限的文件读。

用法：
  export GITHUB_TOKEN=ghp_xxx
  python3 github_contents_push.py --repo my-tools --private \
      --file README.md=./README.md --file tools/a.py=./tools/a.py
  python3 github_contents_push.py --repo my-tools --from-dir ./dist --dry-run
  python3 github_contents_push.py --repo my-tools --from-dir ./dist --json

说明：默认 private=True（安全优先）；需要给其他 Agent 复用时显式加 --public。
"""
import argparse
import base64
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def get_token(a):
    if a.token:
        return a.token.strip()
    for v in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        if os.environ.get(v):
            return os.environ[v].strip()
    candidates = [a.token_file, os.path.expanduser("~/.config/github/token")]
    for c in candidates:
        if c and pathlib.Path(c).is_file():
            return pathlib.Path(c).read_text().strip()
    return None


class GH:
    def __init__(self, token, retries=3, timeout=40):
        self.h = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "contents-push/1.0",
        }
        self.retries, self.timeout = retries, timeout

    def req(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        last = None
        for i in range(self.retries):
            try:
                r = urllib.request.Request(
                    API + path, data=data, headers=dict(self.h), method=method)
                if data is not None:
                    r.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                # 4xx（除 429）不重试，重试也没用
                if 400 <= e.code < 500 and e.code != 429:
                    return e.code, {"error": body[:500]}
                last = "%d %s" % (e.code, body[:200])
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
            time.sleep(1 + i)
        return 0, {"error": "重试 %d 次仍失败：%s" % (self.retries, last)}

    def whoami(self):
        code, d = self.req("GET", "/user")
        return d.get("login") if code == 200 else None

    def ensure_repo(self, owner, repo, private, description):
        code, d = self.req("GET", "/repos/%s/%s" % (owner, repo))
        if code == 200:
            return "exists", d.get("default_branch", "main")
        if code != 404:
            return "error: %s" % (d.get("error") or code), None
        code, d = self.req("POST", "/user/repos", {
            "name": repo, "private": private, "description": description,
            "has_issues": False, "has_projects": False, "has_wiki": False,
            "auto_init": False,
        })
        if code not in (200, 201):
            return "error: %s" % (d.get("error") or code), None
        time.sleep(1)
        return "created", d.get("default_branch", "main")

    def put_file(self, owner, repo, remote, data, message):
        path = "/repos/%s/%s/contents/%s" % (owner, repo, remote)
        code, cur = self.req("GET", path)
        sha = cur.get("sha") if code == 200 else None
        payload = {"message": message, "content": base64.b64encode(data).decode()}
        if sha:
            payload["sha"] = sha
        code, d = self.req("PUT", path, payload)
        if code not in (200, 201):
            return {"ok": False, "action": "update" if sha else "create",
                    "error": d.get("error") or str(code)}
        time.sleep(0.3)
        code2, v = self.req("GET", path)
        if code2 != 200:
            return {"ok": False, "action": "verify", "error": "读回失败 %s" % code2}
        try:
            remote_bytes = base64.b64decode(v["content"])
        except Exception as e:
            return {"ok": False, "action": "verify", "error": str(e)}
        return {"ok": remote_bytes == data, "action": "update" if sha else "create",
                "bytes": len(remote_bytes),
                "error": None if remote_bytes == data else "远端字节不一致"}


def collect(a):
    files = []
    for spec in a.file:
        if "=" not in spec:
            raise ValueError("--file 需要 REMOTE=LOCAL 形式：%s" % spec)
        remote, local = spec.split("=", 1)
        files.append((remote.lstrip("/"), pathlib.Path(local)))
    if a.from_dir:
        root = pathlib.Path(a.from_dir)
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                if rel.startswith(".") or "__pycache__" in rel or p.suffix == ".pyc":
                    continue
                files.append(((a.prefix.rstrip("/") + "/" if a.prefix else "") + rel, p))
    if not files:
        raise ValueError("没有待上传文件（用 --file 或 --from-dir）")
    seen = {}
    for remote, p in files:
        if remote in seen:
            raise ValueError("远端路径重复：%s" % remote)
        if not p.is_file():
            raise ValueError("本地文件不存在：%s" % p)
        seen[remote] = p
    return sorted(seen.items())


def main():
    ap = argparse.ArgumentParser(description="GitHub Contents API 批量上传 + 逐字节校验")
    ap.add_argument("--repo", required=True, help="仓库名")
    ap.add_argument("--owner", help="默认用令牌所属账号")
    ap.add_argument("--description", default="")
    ap.add_argument("--private", action="store_true", default=True)
    ap.add_argument("--public", action="store_true", help="建公开仓库（给其他 Agent 复用时用）")
    ap.add_argument("--file", action="append", default=[], help="REMOTE=LOCAL，可重复")
    ap.add_argument("--from-dir", help="上传整个目录（保持相对路径）")
    ap.add_argument("--prefix", default="", help="--from-dir 时的远端前缀")
    ap.add_argument("--message", default="chore: sync via contents API")
    ap.add_argument("--token", help="令牌（优先用环境变量，不建议命令行传）")
    ap.add_argument("--token-file", help="令牌文件，权限建议 0600")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.public:
        a.private = False

    token = get_token(a)
    if not token:
        print("缺少令牌：设置 GITHUB_TOKEN 或 --token-file", file=sys.stderr)
        return 2
    try:
        files = collect(a)
    except ValueError as e:
        print("参数错误：", e, file=sys.stderr)
        return 2

    if a.dry_run:
        print(json.dumps({"dry_run": True, "repo": a.repo,
                          "files": [[r, str(p), p.stat().st_size] for r, p in files]},
                         ensure_ascii=False, indent=2))
        return 0

    gh = GH(token)
    owner = a.owner or gh.whoami()
    if not owner:
        print("无法识别账号（令牌无效或 API 不通）", file=sys.stderr)
        return 1
    state, branch = gh.ensure_repo(owner, a.repo, a.private, a.description)
    if state.startswith("error"):
        print("建仓失败：", state, file=sys.stderr)
        return 1

    results = []
    for remote, p in files:
        data = p.read_bytes()
        r = gh.put_file(owner, a.repo, remote, data, a.message)
        r.update(remote=remote, path=str(p), local_bytes=len(data))
        results.append(r)
        if not a.json:
            print("  %-46s %-6s %s" %
                  (remote, r["action"], "OK" if r["ok"] else "FAIL " + str(r["error"])))

    ok = all(r["ok"] for r in results)
    summary = {"ok": ok, "owner": owner, "repo": a.repo, "repo_state": state,
               "branch": branch, "private": a.private, "files": results}
    if a.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print("\n%s/%s  private=%s  文件 %d/%d 校验通过" %
              (owner, a.repo, a.private, sum(r["ok"] for r in results), len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
