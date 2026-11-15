#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「只移动、不删除、不覆盖、可一键回滚」的文件归类器。

何时用：
  * AI Agent 被要求整理用户目录（个人文件、下载、备份），但绝不能弄丢数据；
  * 需要先把计划给人/给上游模型审阅，再执行；
  * 需要一份可以证明「随时能还原」的 undo 脚本。

四条硬约束（比分类规则本身更重要）：
  1. 默认 dry-run：不带 --apply 时一个字节都不动；
  2. 只 shutil.move()，代码里没有任何 os.remove / rmdir；
  3. 目标已存在同名 → 跳过并告警，绝不覆盖（后来用户新建的同名文件优先）；
  4. 认不出的进 _unsorted，不猜用途、不擅自改名；隐藏文件与 .git 一律不碰。

另一个实测坑：undo.sh 必须「扫描当前已归类目录」生成，而不是只写本次移动清单。
否则第二次运行 --apply 会把一份有效的回滚脚本覆盖成空的。

用法：
  python3 safe_file_tidy.py --root /path/to/dir                # 预览
  python3 safe_file_tidy.py --root /path/to/dir --json          # 计划交给程序
  python3 safe_file_tidy.py --root /path/to/dir --apply         # 执行
  python3 safe_file_tidy.py --root /path/to/dir --rules r.json  # 自定义分类
  sh /path/to/dir/undo.sh                                      # 一键还原

自定义规则 JSON：{"视频": [".mp4", ".mkv"], "_private": [".pem", ".key"]}
"""
import argparse
import json
import os
import shlex
import shutil
import sys
import time

DEFAULT_RULES = {
    "视频": [".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"],
    "音频": [".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"],
    "图片": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp"],
    "文档": [".pdf", ".doc", ".docx", ".txt", ".md", ".epub", ".rtf"],
    "表格": [".xls", ".xlsx", ".csv", ".tsv", ".ods"],
    "演示": [".ppt", ".pptx", ".key"],
    "压缩包": [".zip", ".tar", ".gz", ".tgz", ".xz", ".7z", ".rar", ".bz2"],
    "代码": [".py", ".sh", ".js", ".ts", ".go", ".c", ".cpp", ".java", ".rb", ".pl", ".html", ".css"],
    "配置与数据": [".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".sql", ".db"],
    "_private": [".pem", ".key", ".p12", ".pfx", ".pcap", ".env", ".kdbx", ".asc", ".gpg"],
}
GENERATED = {"README.md", "undo.sh", "tidy.log", "safe_file_tidy.py",
             "ish_tidy.py", "tidy.py", "rules.json"}


def classify(name, rules):
    low = name.lower()
    # 长后缀优先，避免 .tar.gz 被 .gz 抢走
    for cat, exts in sorted(rules.items(), key=lambda kv: -max(len(e) for e in kv[1])):
        for e in exts:
            if low.endswith(e.lower()):
                return cat
    return "_unsorted"


def size_of(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f%s" % (n, u)
        n /= 1024.0
    return "%.1fTB" % n


def build_plan(root, rules):
    entries = sorted(e for e in os.listdir(root)
                     if e not in GENERATED and e not in rules
                     and e != "_unsorted" and not e.startswith("."))
    plan, warnings = [], []
    for name in entries:
        src = os.path.join(root, name)
        cat = classify(name, rules)
        dst = os.path.join(root, cat, name)
        if os.path.lexists(dst):
            warnings.append("%s → %s/ 已有同名，跳过（不覆盖）" % (name, cat))
            continue
        plan.append({"name": name, "category": cat, "src": src,
                     "dst": dst, "size": size_of(src)})
    return plan, warnings


def write_undo(root, rules):
    """扫描已归类目录生成还原脚本，保证重复运行不会清空有效回滚信息。"""
    pairs = []
    for cat in list(rules) + ["_unsorted"]:
        d = os.path.join(root, cat)
        if os.path.isdir(d):
            pairs += [(cat, n) for n in sorted(os.listdir(d))]
    lines = [
        "#!/bin/sh",
        "# 生成于 %s，共 %d 条。逐条还原；目标已存在则跳过，避免覆盖。" %
        (time.strftime("%Y-%m-%d %H:%M:%S"), len(pairs)),
        "set -e", "",
    ]
    for cat, name in pairs:
        src = os.path.join(root, cat, name)
        dst = os.path.join(root, name)
        lines.append("[ -e %s ] || mv %s %s" %
                     (shlex.quote(dst), shlex.quote(src), shlex.quote(dst)))
    lines += ["", "echo '还原完成；空目录可自行 rmdir。'", ""]
    p = os.path.join(root, "undo.sh")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.chmod(p, 0o755)


def write_readme(root, rules):
    lines = ["# %s 索引" % root, "",
             "由 safe_file_tidy.py 生成于 %s。" % time.strftime("%Y-%m-%d %H:%M"),
             "规则：只移动不删除；认不出的放 _unsorted；_private 权限 700，不要放进公开仓库。",
             "还原：执行同目录 undo.sh。", ""]
    for cat in list(rules) + ["_unsorted"]:
        d = os.path.join(root, cat)
        items = sorted(os.listdir(d)) if os.path.isdir(d) else []
        lines += ["## %s（%d 项）" % (cat, len(items)), ""]
        lines += ["（空）", ""] if not items else [
            "| 名称 | 大小 | 用途（请补） |", "|---|---|---|"] + [
            "| `%s` | %s | _TODO_ |" %
            (it.replace("|", "\\|"), human(size_of(os.path.join(d, it)))) for it in items
        ] + [""]
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="安全文件归类（默认预览，不改动）")
    ap.add_argument("--root", default=".", help="要整理的目录")
    ap.add_argument("--apply", action="store_true", help="真正执行移动")
    ap.add_argument("--rules", help="自定义分类规则 JSON 文件")
    ap.add_argument("--json", action="store_true", help="输出结构化计划")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print("目录不存在：", root, file=sys.stderr)
        return 2
    rules = DEFAULT_RULES
    if a.rules:
        try:
            rules = json.loads(open(a.rules, "r", encoding="utf-8").read())
        except Exception as e:
            print("规则文件读取失败：", e, file=sys.stderr)
            return 2
        if not isinstance(rules, dict):
            print("规则必须是 {分类: [扩展名...]}", file=sys.stderr)
            return 2

    plan, warnings = build_plan(root, rules)
    if a.json:
        print(json.dumps({"root": root, "apply": a.apply, "plan": plan,
                          "warnings": warnings, "total_bytes": sum(p["size"] for p in plan)},
                         ensure_ascii=False))
    else:
        print("=" * 60)
        print("目录: %s  |  模式: %s" % (root, "执行" if a.apply else "预览（不改文件）"))
        print("=" * 60)
        for cat in list(rules) + ["_unsorted"]:
            group = [p for p in plan if p["category"] == cat]
            if group:
                print("\n[%s] %d 项" % (cat, len(group)))
                for p in group:
                    print("    %-50s %s" % (p["name"], human(p["size"])))
        print("\n合计 %d 项 / %s" % (len(plan), human(sum(p["size"] for p in plan))))
        for w in warnings:
            print("  ! " + w)
        if not a.apply:
            print("\n这是预览，一个文件都没动。确认后加 --apply。")
            return 0

    if not a.apply:
        return 0

    for cat in list(rules) + ["_unsorted"]:
        os.makedirs(os.path.join(root, cat), exist_ok=True)
    moved = 0
    for p in plan:
        # 二次确认，防止预览与执行之间目标被别的进程创建
        if os.path.lexists(p["dst"]):
            continue
        shutil.move(p["src"], p["dst"])
        moved += 1
    try:
        os.chmod(os.path.join(root, "_private"), 0o700)
    except OSError:
        pass
    write_readme(root, rules)
    write_undo(root, rules)
    with open(os.path.join(root, "tidy.log"), "a", encoding="utf-8") as f:
        f.write("[%s] 计划 %d，实际移动 %d，跳过 %d\n" %
                (time.strftime("%Y-%m-%d %H:%M:%S"), len(plan), moved, len(warnings)))
    if not a.json:
        print("\n已移动 %d 项。生成 README.md / undo.sh / tidy.log。" % moved)
        print("还原： sh %s" % os.path.join(root, "undo.sh"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
