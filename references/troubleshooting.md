# 故障排查（症状 → 根因 → 处理）

按你看到的现象查。所有命令都在 skill 根目录执行。

---

## A. `Work browser access requires an authenticated account`

**现象**：恢复/继续会话时报红，附带"重试"按钮；换成 key 模式后才出现。

**根因**（Codex 桌面端源码级判定）：

```js
requireWorkBrowserIdentity = (getEffective('conversationDetailMode') === 'STEPS_PROSE')
if (local && requireWorkBrowserIdentity && mcp_servers.node_repl != null && !authenticatedPrincipal)
    throw Error('Work browser access requires an authenticated account')
```

四个条件同时成立才报：本地主机 + 详细模式 = `STEPS_PROSE` + 配了 `node_repl` MCP + 没有 ChatGPT 登录身份。

**处理（二选一）**

1. 纯中转（保留 key 模式）：

   ```toml
   [desktop]
   conversationDetailMode = "STEPS_COMMANDS"   # 应用默认值
   ```

   → 条件不成立，插件一个都不用关。

2. 保留官方身份：

   ```json
   // auth.json 用 chatgpt 模式（见 references/token-and-login.md）
   ```

   ```toml
   [model_providers.custom]
   experimental_bearer_token = "sk-REPLACE_ME"   # 让模型仍走中转
   ```

**兜底**（服务端强制 `STEPS_PROSE` 时）：禁用 `browser` / `computer-use` / `unified-computer-use` 插件，
并删掉 `[mcp_servers.node_repl]` 与 `[mcp_servers.node_repl.env]`。
注意：`node_repl` 段是桌面端启动时自己写回 `config.toml` 的，删了可能被重新加回来。

---

## B. 点图标打不开 / 无窗口 / 一直白屏

**先分清两种情况**

| 情况 | 判断 | 处理 |
|---|---|---|
| 刚启动 | 进程在、`MainWindowTitle` 空、CPU 在动 | 等 30–60 秒（白屏是正常的） |
| 假死 | 进程在、无窗口、再点图标也不出新窗口 | 见下 |

**假死三要素**：Electron 单实例 + 残留锁 + 起来的 `codex.exe app-server` 立刻退出。

**处理**：`scripts/restart-codex.ps1`（Windows）。手工版：

```powershell
# 1) 关进程树
taskkill /F /T /IM ChatGPT.exe

# 2) 清残留
foreach ($n in 'codex.exe','codex-code-mode-host.exe','node_repl.exe','extension-host.exe') {
  taskkill /F /T /IM $n 2>$null
}

# 3) 删僵尸锁
Remove-Item "$env:USERPROFILE\.codex\thread-writer-locks\*" -Force -EA SilentlyContinue
Remove-Item "$env:USERPROFILE\.codex\project-metadata-locks\*" -Force -EA SilentlyContinue
Remove-Item "$env:USERPROFILE\.codex\*.lock" -Force -EA SilentlyContinue
Remove-Item "$env:USERPROFILE\.codex\*.guard" -Force -EA SilentlyContinue

# 4) 用 AUMID 重新拉起
$pkg = Get-AppxPackage OpenAI.Codex | Select-Object -First 1
Start-Process explorer.exe -ArgumentList ("shell:AppsFolder\" + $pkg.PackageFamilyName + "!App")
```

如果还起不来，看日志：

```
%LOCALAPPDATA%\Packages\OpenAI.Codex_<publisher>\LocalCache\Local\Codex\Logs\<UTC 日期>\
```

常见原因：Windows Update 正在部署 AppX / WindowsAppRuntime（等它跑完重试即可）、
上一次崩溃留下占用的 WebView2 用户数据目录（重启系统最干净）。

---

## C. 历史线程不见了

```bash
python scripts/ccswitch.py bucket-report
```

输出示例：

```
threads 总数: 900
  OpenAI    archived=0  400
  custom    archived=0  500
当前 model_provider: custom
```

→ `OpenAI` 那 400 条被过滤了。迁移：

```bash
python scripts/ccswitch.py bucket-migrate --from OpenAI --to custom          # dry-run
python scripts/ccswitch.py bucket-migrate --from OpenAI --to custom --apply  # 真写
```

细节与回滚见 `references/codex-config-and-buckets.md`。

> 迁移前请**关闭 Codex 应用**（避免它持有 DB/正在写 rollout）。

---

## D. 中转返回错误

先直接打接口，把"Codex 的问题"和"中转的问题"分开：

```bash
python - <<'PY'
import json, urllib.request, urllib.error, uuid
key, base, model = "sk-REPLACE_ME", "https://chenxiapi.com", "gpt-6.1-sol"
body = json.dumps({"model": model, "instructions": "Reply with exactly: ok",
  "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ok"}]}],
  "stream": True, "store": False}).encode()
req = urllib.request.Request(base + "/responses", data=body, method="POST", headers={
  "Authorization": "Bearer " + key, "Content-Type": "application/json",
  "Accept": "text/event-stream", "session_id": str(uuid.uuid4())})
try:
    print("HTTP", urllib.request.urlopen(req, timeout=60).status)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read(200).decode("utf-8", "ignore"))
PY
```

| 返回 | 含义 | 处理 |
|---|---|---|
| `401 INVALID_API_KEY` | key 错/失效 | 换 key |
| `403 INSUFFICIENT_BALANCE` | 余额 0 | 充值或换 key（与模型无关） |
| `403 Cloudflare 1010` | 该站反爬，通常是 `/v1/models` 之类被拦 | 忽略；以 `/responses` 为准 |
| `404` | base_url 拼错 | 检查是否多了/少了 `/v1` |
| `502 upstream_error` | 中转上游故障 | 换模型或等恢复 |
| `503 Service temporarily unavailable` | 该模型不可用 | 换模型，逐个探 |
| 200 但很慢（30–70s） | 中转排队 | 可接受；必要时换模型 |

一次性试探多个模型：

```bash
for m in gpt-6.1-sol gpt-6-astra gpt-5.6; do echo "== $m"; done   # 配合上面的脚本改 model
```

---

## E. 官方模式报 "model is not supported … ChatGPT account"

```
The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.
```

官方后端只认官方模型名。用 `gpt-6-astra` 之类；`gpt-6.1-sol` / `gpt-5.6` 这类是第三方中转的命名。

---

## F. 配置改了没生效

1. `config.toml` 是**启动时读取**的 —— 必须重启 Codex 应用。
2. 重启后应用可能把界面里选中的模型写回 `config.toml`（覆盖你手改的 `model`），
   以界面模型选择器为准。
3. CC Switch 在运行且被点过供应商 → 会重写 `config.toml`。想保留手改，先退出 CC Switch。

---

## G. `codex exec` 卡住不返回（尤其在后台/脚本里）

**现象**：命令跑几分钟没输出；前台跑同样的命令几秒就完成。

**根因**：`codex exec` 在 stdin 是管道且没关闭时会"读取额外输入"（`Reading additional input from stdin...`），
一直等到 EOF。

**处理**：

- 前台跑，或
- 显式关闭 stdin：`codex exec ... < /dev/null`（Windows：`Get-Content NUL | codex exec ...`），或
- 用 `--json`/`-o` 输出到文件再读。

---

## H. 路径 / 编码类小坑

- Codex 桌面版日志目录按 **UTC** 日期分：北京时间凌晨 0–8 点的日志可能落在"前一天"的目录。
- `~/.codex/config.toml` 里的 `notify` 含本机绝对路径（`…\cua_node\<hash>\bin\…`），
  拷到别的机器要删掉或改路径，否则启动报错。
- 归档会话是 `.jsonl.zst`，但 DB 里 `rollout_path` 常写 `.jsonl`——按 `.zst` 找文件。
- Windows 路径在 DB 里可能带 `\\?\` 前缀，读写前先去掉。
