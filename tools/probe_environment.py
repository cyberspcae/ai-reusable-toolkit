#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沙箱 / 容器能力自检：一条命令回答「这台机器能干什么、不能干什么」。

何时用：
  * AI Agent 刚进入一个陌生环境，准备装 Docker、起 VPN/TUN、跑大模型、开服务之前；
  * 需要先判断限制再选方案，而不是试错半天才发现「没有 /dev/net/tun」；
  * 把探测结果结构化交给上游模型做决策。

它回答的典型问题（都是真实踩过的）：
  * cgroup 内存硬限是多少？→ 决定 27B 模型能不能加载，而不是等 OOM 被 kill；
  * 有 /dev/net/tun 吗？→ 决定 Tailscale 要不要用 userspace-networking；
  * PID 1 是 systemd 吗？→ 决定能不能 systemctl，还是必须手动 nohup；
  * wget 是 BusyBox 还是 GNU？→ 决定能不能用 --tries / --no-check-certificate；
  * 磁盘还剩多少？→ 决定大文件下载是否该先清理。

输出 JSON（默认），或 --human 看表格。绝不输出任何凭证。

用法：
  python3 probe_environment.py
  python3 probe_environment.py --human
  python3 probe_environment.py --net                 # 额外测外网连通
  python3 probe_environment.py --socks5 127.0.0.1:1055
"""
import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.request


def read_text(p):
    try:
        return open(p, "r").read().strip()
    except OSError:
        return None


def cgroup_memory():
    v2 = read_text("/sys/fs/cgroup/memory.max")
    if v2 and v2 != "max":
        try:
            return int(v2)
        except ValueError:
            pass
    v1 = read_text("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v1 and v1.isdigit() and int(v1) < (1 << 60):
        return int(v1)
    return None


def pid1():
    c = read_text("/proc/1/comm")
    return c or "unknown"


def tool_version(cmd, args):
    exe = shutil.which(cmd)
    if not exe:
        return {"present": False}
    try:
        r = subprocess.run([exe] + args, capture_output=True, timeout=5)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")[:4000]
    except Exception as e:
        return {"present": True, "error": str(e)}
    return {"present": True, "path": exe, "excerpt": out}


def wget_flavor():
    v = tool_version("wget", ["--help"])
    if not v.get("present"):
        return {"present": False, "flavor": "missing"}
    e = v.get("excerpt", "")
    flavor = "busybox" if "BusyBox" in e or "busybox" in e.lower() else "gnu?"
    return {
        "present": True, "flavor": flavor, "path": v.get("path"),
        "supports_tries": "--tries" in e,
        "supports_post_data": "--post-data" in e,
        "supports_no_check_cert": "--no-check-certificate" in e,
        "note": "BusyBox wget 只认 --header 'K: V' / -O / -T；不要把 GNU 参数照搬过去",
    }


def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def net_check(urls):
    out = {}
    for u in urls:
        try:
            req = urllib.request.Request(u, method="HEAD",
                                         headers={"User-Agent": "probe/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                out[u] = {"ok": True, "status": r.status}
        except Exception as e:
            out[u] = {"ok": False, "error": type(e).__name__}
    return out


def main():
    ap = argparse.ArgumentParser(description="沙箱能力自检")
    ap.add_argument("--human", action="store_true", help="人类可读输出")
    ap.add_argument("--net", action="store_true", help="测试外网连通")
    ap.add_argument("--socks5", help="检查 SOCKS5 代理是否可达，如 127.0.0.1:1055")
    a = ap.parse_args()

    mem = cgroup_memory()
    disk = shutil.disk_usage(os.getcwd())
    tun = os.path.exists("/dev/net/tun")
    is_systemd = pid1() in ("systemd", "init")
    wget = wget_flavor()

    result = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "cwd": os.getcwd(),
        "cgroup_memory_bytes": mem,
        "cgroup_memory_human": "%.2f GiB" % (mem / 1024 ** 3) if mem else "无限制/未知",
        "tun_device": tun,
        "pid1": pid1(),
        "systemd_available": is_systemd,
        "disk_free_bytes": disk.free,
        "disk_free_human": "%.1f GiB" % (disk.free / 1024 ** 3),
        "wget": wget,
        "curl": tool_version("curl", ["--version"]),
        "capabilities": {
            "can_create_tun": tun,
            "can_use_systemd": is_systemd,
            "can_load_20gb_model": (mem is None) or mem >= 20 * 1024 ** 3,
            "can_run_docker": tun and os.geteuid() == 0,
            "is_busybox_wget": wget.get("flavor") == "busybox",
            "disk_low": disk.free < 2 * 1024 ** 3,
        },
        "hints": [],
    }
    if not tun:
        result["hints"].append("无 /dev/net/tun：Tailscale/WireGuard 需 --tun=userspace-networking")
    if not is_systemd:
        result["hints"].append("PID1 非 systemd：用 nohup 手动拉起服务，并准备沙箱休眠后的重启清单")
    if mem and mem < 12 * 1024 ** 3:
        result["hints"].append("cgroup 内存硬限偏小：大模型只能下载不能加载，先算权重缓冲")
    if wget.get("flavor") == "busybox":
        result["hints"].append("BusyBox wget：只生成 --header 'K: V' / -O / -T")
    if result["capabilities"]["disk_low"]:
        result["hints"].append("磁盘不足 2 GiB：先清理或改下载目标盘")
    if a.socks5:
        try:
            host, port = a.socks5.rsplit(":", 1)
            result["socks5"] = {"target": a.socks5, "reachable": port_open(host, int(port))}
        except ValueError:
            result["socks5"] = {"error": "格式应为 host:port"}
    if a.net:
        result["net"] = net_check(["https://api.github.com", "https://pypi.org"])

    if a.human:
        print("Python %s | %s | %s 核" %
              (result["python"], result["platform"], result["cpu_count"]))
        print("cgroup 内存上限 : %s" % result["cgroup_memory_human"])
        print("磁盘可用        : %s" % result["disk_free_human"])
        print("/dev/net/tun    : %s" % ("有" if tun else "无"))
        print("PID 1           : %s（systemd=%s）" % (result["pid1"], is_systemd))
        print("wget            : %s @ %s" % (wget.get("flavor"), wget.get("path")))
        print("\n能力判定：")
        for k, v in result["capabilities"].items():
            print("  %-24s %s" % (k, v))
        if result["hints"]:
            print("\n建议：")
            for h in result["hints"]:
                print("  - " + h)
        for k in ("socks5", "net"):
            if k in result:
                print("\n%s: %s" % (k, json.dumps(result[k], ensure_ascii=False)))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
