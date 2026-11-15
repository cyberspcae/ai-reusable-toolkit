# ai-reusable-toolkit

> 给 AI Agent 的「受限环境生存工具包」：从真实调试现场沉淀出来的单文件、零第三方依赖工具，
> 外加一份按「现象 → 根因 → 解法 → 可复用规则」整理的踩坑记录。

## 这个仓库是干什么的

这里的代码不是从教程里抄的示例，而是在下面这类环境里**真的跑通过**才留下来的：

- 一台隔离云端沙箱：32 核但 **cgroup 内存硬限 8 GiB**、无 GPU、无 systemd、无法创建 `/dev/net/tun`、后台进程会被回收；
- 一台 iOS 上的 iSH（Alpine/BusyBox 用户态）：只有 `wget`（BusyBox 1.33.x）、没有 `curl` 可用、磁盘只剩 1 GiB 量级；
- 到 GitHub / Hugging Face / 各类 CDN 的连接时好时坏，代理通道会瞬断；
- 要处理的任务包括：装本地大模型、拉 17 GiB 权重、起能被 Safari 播放的媒体服务、把 curl 命令移植成 wget、解析带密码的分享页、把成果推到 GitHub。

**「有价值」在这里的定义**：与具体业务无关、换个任务还能直接用、并且附带「什么时候不该用」和「为什么会踩坑」的代码。
配套仓库 [`cyberspcae/tailnet-cookbook`](https://github.com/cyberspcae/tailnet-cookbook) 单独收录 Tailscale 在无 TUN 容器里的实战方案。

## 设计原则（给其他 Agent 的约定）

| 原则 | 原因 |
|---|---|
| 单文件、纯 Python 标准库 | 目标环境往往装不了包，也没有 pip 网络 |
| 既能 `python3 x.py --help`，也能 `import` | Agent 可以先用 CLI 探路，再嵌进自己的流程 |
| 绝大多数工具支持 `--json` | 结构化输出便于上游模型解析，不用正则去猜人话 |
| 破坏性操作默认 dry-run | 整理文件、上传仓库这类动作，先给计划再执行 |
| 绝不硬编码凭证 / 主机名 / 绝对路径 | 打印时自动给 Cookie、Token、Authorization 打码 |
| 校验是交付的一部分 | 下载要 sha256，上传要 base64 逐字节读回比对 |

## 工具索引

| 文件 | 解决什么问题 | 典型触发场景 | 依赖 |
|---|---|---|---|
| `tools/probe_environment.py` | 一条命令判断这台机器能干什么 | 准备装 Docker / 起 TUN / 跑大模型之前 | 标准库 |
| `tools/concurrent_range_download.py` | 单连接被限速时并发分块下载，断点续传 + sha256 | 拉十几 GiB 模型权重、境外大文件 | 标准库 |
| `tools/curl_to_wget.py` | 把浏览器复制的 curl 命令转成可粘贴的 wget 命令 | 目标机只有 BusyBox wget；curl 失败但 wget 成功 | 标准库 |
| `tools/http_range_server.py` | 支持 Range / 206 的极简静态服务 | Safari / iOS 播放本地视频、拖进度条 | 标准库 |
| `tools/safe_file_tidy.py` | 只移动 / 不删除 / 不覆盖 / 可一键回滚地归类文件 | 被要求整理用户目录，且绝不能弄丢数据 | 标准库 |
| `tools/github_contents_push.py` | 走 Contents API 批量上传并逐字节校验 | 没有 git / SSH，`git push` 不通 | 标准库 |

## 三条可复制的流水线

**① 陌生环境先体检，再决定方案**

```bash
python3 tools/probe_environment.py --human
# 看 can_create_tun / cgroup_memory_bytes / is_busybox_wget / disk_low
```

**② 单连接慢 → 并发分块下载 → 校验**

```bash
python3 tools/concurrent_range_download.py "$URL" -o model.bin \
    --workers 24 --sha256 "$EXPECTED" --json
# 中断后重跑同一条命令即可续传（分片缓存在 model.bin.chunks/）
```

**③ 有 curl 命令但目标机只有 wget → 转换 → 预览 → 执行**

```bash
python3 tools/curl_to_wget.py -f cmd.txt --json     # 先拿结构化计划
python3 tools/curl_to_wget.py -f cmd.txt -o a.mp4   # 打印命令（凭证已打码）
python3 tools/curl_to_wget.py -f cmd.txt -o a.mp4 --run
```

**④ 交付前证明远端内容一致**

```bash
export GITHUB_TOKEN=...                              # 不要写进命令行历史
python3 tools/github_contents_push.py --repo my-tools --public \
    --from-dir ./dist --json
```

## 文档

- [`docs/PITFALLS.md`](docs/PITFALLS.md) —— **本仓库最值得读的部分**。按主题收录 20+ 条真实故障：
  容器限制、下载限速、BusyBox 与 GNU 差异、Safari 的 Range 要求、SPA/验证页/表单提交的误判、
  Shell 测试脚本的退出码陷阱、凭证泄露处置等。每条都给出「可复用规则」。
- 每个工具文件顶部的 docstring 也包含「何时用 / 何时别用 / 实测坑」，可单独复制使用。

## 安全约定

- 本仓库**不含任何真实令牌、Cookie、账号密码、节点 IP 或 tailnet 域名**，示例中的地址与凭证均为占位符。
- 工具默认把敏感头打码；`--show-secrets` 需要显式指定。
- `http_range_server.py` 默认只绑定 `127.0.0.1`；`safe_file_tidy.py` 默认只读预览。
- 如果曾在对话里贴过 PAT / SSH 口令，事后应当撤销或改密，而不是只在文档里打码。

## License

MIT —— 可自由复制、修改、用于其他 Agent 或项目，无需通知作者。见 [`LICENSE`](LICENSE)。
