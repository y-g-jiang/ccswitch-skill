---
name: ccswitchskill
description: "管理 Codex 的多供应商切换（CC Switch / cc-switch 与本机 ~/.codex 配置文件）：新增、切换、回滚中转站或官方 ChatGPT 登录，保持会话历史（线程桶）可见，处理 refresh token 轮换、设备码登录、切换后应用异常。以 chenxiapi 中转为完整示例，脚本不内置任何密钥。"
whenToUse: "当用户说『换到 XX 中转 / 切回官方 / 换回我的 ChatGPT 账号 / 这个中转用不了 / 切完 Codex 打不开 / 切完历史没了 / 切换后报 Work browser access / token 失效』，或需要把某台机器的 Codex 切到指定供应商并验证计费走向时使用。"
---

# Codex 供应商切换（cc-switch + ~/.codex）

本 skill 讲的是**同一台机器上，Codex 在「官方 ChatGPT 登录」和「第三方中转站」之间来回切换**这件事：
换哪些文件、怎么验证真的切过去了、线程历史为什么"消失"、refresh token 为什么会失效、以及切完应用打不开时怎么救。

所有示例用 **chenxiapi（晨夕中转）**，密钥一律写成 `sk-REPLACE_ME`。**执行前先读本文件的「安全规则」，再动手。**

---

## 0. 30 秒速查

| 想做 | 怎么做 |
|---|---|
| 看当前是什么模式 | `python scripts/ccswitch.py show` |
| 切到 chenxiapi 中转 | `python scripts/ccswitch.py relay --name 晨夕API --base-url https://chenxiapi.com --model gpt-6.1-sol --api-key sk-REPLACE_ME` |
| 切到官方 ChatGPT | `python scripts/ccswitch.py official --account 0`（用账号库里第 0 个账号现刷 token） |
| 切完打不开 | `scripts/restart-codex.ps1`（Windows）或按 §7 手动清锁 |
| 历史线程不见了 | `python scripts/ccswitch.py bucket-report`，再 `bucket-migrate` |
| 报 `refresh_token_reused` | 只能重新登录（§5），并立刻 `sync-tokens` |

所有子命令都**先备份**再写入，备份落在 `~/.cc-switch/backups/<动作>-<时间戳>/`。

---

## 1. 先建立心智模型：三层完全独立

Codex 的"账号"和"线程"是三件互不相干的事，混在一起看就会得出错误结论：

```
第 1 层  认证身份   ~/.codex/auth.json
         ├─ API key 模式：{"OPENAI_API_KEY":"sk-..."}            → 中转/第三方用
         └─ ChatGPT 模式：{"auth_mode":"chatgpt","tokens":{...}}  → 官方订阅用
         影响：App 认不认识"你这个人"（浏览器、电脑操作、远程、插件）

第 2 层  模型路由   ~/.codex/config.toml
         model_provider = "custom" + [model_providers.custom]
         影响：请求发到哪个 base_url、计费用谁的钱

第 3 层  会话历史   ~/.codex/sessions/**.jsonl       （正文，逐条 rollout）
                    ~/.codex/state_5.sqlite → threads（索引，列表真正读这个）
         影响：历史列表里"看不看得见"
```

**关键不变量**：换账号只动第 1 层；换供应商只动第 2 层；第 3 层不会被切换动作修改 —— 所以**数据不会丢**。真正会变的是每条会话的 `threads.model_provider` 标签，标签和当前第 2 层不一致，列表里就看不见。

---

## 2. 数据文件地图（跨平台路径）

| 内容 | Windows | macOS / Linux |
|---|---|---|
| Codex 主目录 | `%USERPROFILE%\.codex` | `~/.codex` |
| 凭证 | `~/.codex/auth.json` | 同 |
| 配置 | `~/.codex/config.toml` | 同 |
| 会话正文 | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | 同 |
| 归档会话 | `~/.codex/archived_sessions/*.jsonl.zst` | 同 |
| 会话索引 | `~/.codex/state_5.sqlite`（表 `threads`） | 同 |
| 会话明细 | `~/.codex/thread_history_1.sqlite`（大，几 GB） | 同 |
| cc-switch 配置 | `~/.cc-switch/settings.json` | 同 |
| cc-switch 供应商库 | `~/.cc-switch/cc-switch.db`（SQLite，表 `providers` / `settings`） | 同 |
| cc-switch 账号库 | `~/.cc-switch/codex_oauth_auth.json` | 同 |
| cc-switch 备份 | `~/.cc-switch/backups/` | 同 |

> Windows 上如果 Codex 是 **MSIX/Store 安装**，进程名是 `ChatGPT.exe`，安装目录在
> `C:\Program Files\WindowsApps\OpenAI.Codex_<版本>_x64__<publisher>`，启动用 AUMID：
> `shell:AppsFolder\OpenAI.Codex_<publisher>!App`。日志在
> `%LOCALAPPDATA%\Packages\OpenAI.Codex_<publisher>\LocalCache\Local\Codex\Logs\<UTC 日期>\`
> （注意：目录按 **UTC** 日期分，凌晨可能还落在前一天）。

`threads` 表关键列：

```
id, rollout_path, created_at, updated_at,
model_provider,          -- 桶标签，决定列表可见性
title, first_user_message, model, reasoning_effort, archived,
creator_user_id, creator_account_id, history_mode
```

`~/.cc-switch/cc-switch.db` 的 `providers` 表关键列：

```
id, app_type('codex'), name, settings_config(JSON), meta(JSON), is_current, sort_index
```

`settings_config` 结构（这是 CC Switch 写配置的模板）：

```json
{
  "auth":   { "OPENAI_API_KEY": "sk-REPLACE_ME" },
  "config": "model_provider = \"custom\"\nmodel = \"gpt-6.1-sol\"\n\n[model_providers.custom]\n..."
}
```

---

## 3. 两种切换路径，先想清楚用哪种

### 路径 A：在 CC Switch 界面点供应商（推荐给日常使用）

CC Switch 会做三件事：

1. 把 `providers.settings_config.config` + `settings.common_config_codex` 合并写入 `~/.codex/config.toml`
2. 按供应商类型写 `~/.codex/auth.json`
3. 把 `settings.json` 的 `currentProviderCodex` 和 DB 的 `is_current` 指向新供应商

它**不动** `sessions/`、`state_5.sqlite`。

> 坑：切换时 CC Switch 会把"当时的 live 配置"回写到**当前供应商**的 `settings_config` 里。
> 如果你绕过它直接改文件（路径 B），之后在 GUI 里点旧供应商，可能拿到的是被覆盖过的配置。
> 所以**两条路径不要混着用**；混用后要检查 `providers` 表里的 `settings_config` 是否还是对的
> （`python scripts/ccswitch.py list` 会显示每个供应商的 base_url，对不上就是被写坏了）。

### 路径 B：直接改文件（自动化/无 GUI 时用）

本 skill 的脚本走这条路。顺序固定，**顺序错了会丢 token**：

```
1) 同步：auth.json 里的 chatgpt token → cc-switch 账号库   （防丢，见 §5）
2) 备份：auth.json / config.toml / settings.json / cc-switch.db / 账号库
3) 写 auth.json：key 模式或 chatgpt 模式
4) 改 config.toml：只替换 [model_providers.custom] 块 + model/model_provider
5) 更新 cc-switch：settings.json 的 currentProviderCodex + DB is_current
6) 验证：login status、真实请求、线程桶
7) 重启 Codex 应用
```

---

## 4. chenxiapi（晨夕）完整示例

### 4.1 中转（API key 模式）

`~/.codex/auth.json`：

```json
{ "OPENAI_API_KEY": "sk-REPLACE_ME" }
```

`~/.codex/config.toml` 顶部：

```toml
model_provider = "custom"
model = "gpt-6.1-sol"
review_model = "gpt-5.5"

[model_providers.custom]
name = "晨夕API"
base_url = "https://chenxiapi.com"
wire_api = "responses"
requires_openai_auth = true
```

要点：

- `base_url` **不带** `/v1`，Codex 自己拼 `/responses`（实测请求地址就是 `https://chenxiapi.com/responses`）。
- `wire_api = "responses"` 表示走 OpenAI Responses 协议，不是 chat/completions。
- `requires_openai_auth = true` 让 Codex 使用 `auth.json` 里的 key（而不是忽略它）。
- 模型名以该中转的 `/v1/models` 为准；chenxiapi 上 `gpt-6.1-sol`、`gpt-6-astra` 都可用。

一行命令完成：

```bash
python scripts/ccswitch.py relay \
  --name 晨夕API \
  --base-url https://chenxiapi.com \
  --model gpt-6.1-sol \
  --api-key sk-REPLACE_ME
```

### 4.2 官方（ChatGPT 登录模式）

`~/.codex/auth.json`（由登录流程自动写入）：

```json
{
  "OPENAI_API_KEY": null,
  "auth_mode": "chatgpt",
  "last_refresh": "2026-01-01T00:00:00.000000000+08:00",
  "tokens": {
    "access_token": "…",
    "account_id": "…",
    "id_token": "…",
    "refresh_token": "…"
  }
}
```

`~/.codex/config.toml` 顶部：

```toml
model_provider = "custom"
model = "gpt-6-astra"
review_model = "gpt-5.5"

[model_providers.custom]
name = "OpenAI"
requires_openai_auth = true
supports_websockets = true
wire_api = "responses"
```

要点：

- 官方模式 **没有** `base_url`，Codex 回落到官方后端。
- `name = "OpenAI"` 是刻意保留的：官方能力门控（web search 等）按名字判断。
- **官方模式下 `gpt-6.1-sol` 这类第三方模型会报** `The '…' model is not supported when using Codex with a ChatGPT account.` —— 官方请用 `gpt-6-astra` 之类。
- 桶名仍然是 `custom`（和官方"统一会话历史"一致），所以切来切去历史列表不分叉。

### 4.3 验证（一定要做，别只看文件）

```bash
# 1) 认证形态
codex login status
#   API key 模式 → Logged in using an API key - sk-REPL…MASK
#   官方模式     → Logged in using ChatGPT

# 2) 真跑一次（最快的端到端验证）
codex exec --skip-git-repo-check --ephemeral -s read-only -C . "Reply with exactly: OK"

# 3) 直接打中转接口（不经过 Codex，判断 key/模型/余额）
python - <<'PY'
import json,urllib.request,uuid
key="sk-REPLACE_ME"; base="https://chenxiapi.com"
body=json.dumps({"model":"gpt-6.1-sol","instructions":"Reply with exactly: ok",
 "input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"ok"}]}],
 "stream":True,"store":False}).encode()
r=urllib.request.Request(base+"/responses",data=body,method="POST",headers={
 "Authorization":"Bearer "+key,"Content-Type":"application/json",
 "Accept":"text/event-stream","session_id":str(uuid.uuid4())})
print(urllib.request.urlopen(r,timeout=60).status)
PY
```

### 4.4 怎么证明"计费真的走中转"

界面显示官方账号 ≠ 计费走官方。三种独立证明，任选两种即可确信：

1. **控制变量**：把 `base_url` 临时改成一个不存在的地址，
   `codex exec` 报 `url: http://127.0.0.1:9/responses` → 说明模型请求完全跟随 `base_url`。
2. **换假 key**：把 provider 的 key 换成 `sk-INVALID`，得到
   `401 {"code":"INVALID_API_KEY"} url: https://chenxiapi.com/responses`
   → 说明发给中转的是中转 key，不是 ChatGPT token。
3. **抓连接**：跑一个本地 CONNECT 日志代理（只需记录目标 host，不解密），
   一次 `codex exec` 只会看到一条 `CONNECT chenxiapi.com:443`，
   其余是 `chatgpt.com`（插件目录/MCP/work settings/遥测等**非推理**流量）。

---

## 5. refresh token 生命周期：这个坑最贵

### 5.1 规则

- OAuth 的 **refresh token 是一次性轮换的**：每换一次票，旧 rt 立刻作废。
- Codex 会**自己在后台换票**，新 token **只写 `~/.codex/auth.json`**。
- CC Switch 的账号库 `codex_oauth_auth.json` 里那份 rt **不会自动更新** → 变旧 → 再用就报
  `{"code":"refresh_token_reused"}`。

### 5.2 后果

`refresh_token_reused` / `invalid_refresh_token` 出现后，**任何"恢复旧 auth.json"的做法都救不回来**，
唯一解是**重新登录**（§6）。所以每次切换前必须做 sync：

```bash
python scripts/ccswitch.py sync-tokens      # auth.json 的最新 rt → 账号库（幂等）
python scripts/ccswitch.py sync-tokens --check
```

### 5.3 典型事故链（真实踩过）

```
13:55 切到中转（覆盖 auth.json，官方 token 被替换掉）
14:00 Codex 后台换票 → 新 rt 只写进 auth.json
14:05 CC Switch 应用供应商 → 用中转 key 覆盖 auth.json
      → 新 rt 消失，账号库里只剩上一代（已被消费）
次日    切官方 → refresh_token_reused → 只能重新登录
```

对策只有两条：**切换前 sync**、**任何程序写入 auth.json 前先确认没有别的程序在写**。

---

## 6. 只能用设备码登录时（无法本机浏览器回调）

不要用 `codex login`（它会起 `localhost:1455` 等回调，远程/无浏览器时必然失败），改用：

```bash
codex login --device-auth
# 输出：
#   1. https://auth.openai.com/codex/device
#   2. 一次性代码，如 ABCD-1234（15 分钟过期）
```

- 用手机/另一台设备打开链接、输入代码、确认授权。
- **代码过期不消耗登录次数**，重新生成即可；只有"确认授权"那一下才真正消耗一次。
- 登录成功后 `auth.json` 变成 `auth_mode=chatgpt`，**立刻**执行：

```bash
python scripts/ccswitch.py sync-tokens
```

- 登录成功但账号不是想要的（浏览器里是另一个账号）：只能重新走一次，登录前先在浏览器切好账号。

---

## 7. 故障排查

### 7.1 `Work browser access requires an authenticated account`

来源是 Codex 桌面端渲染进程，不是服务器。判定条件（源码级）：

```js
requireWorkBrowserIdentity = (getEffective('conversationDetailMode') === 'STEPS_PROSE')
if (local && requireWorkBrowserIdentity && mcp_servers.node_repl != null && !authenticatedPrincipal)
    throw Error('Work browser access requires an authenticated account')
```

两个解法：

- **纯中转**：把 `~/.codex/config.toml` 的 `[desktop] conversationDetailMode` 改成 `"STEPS_COMMANDS"`（应用默认值），
  身份闸不再触发，**插件无需禁用**。
- **保留官方身份**：`auth.json` 用 chatgpt 模式，同时给中转 provider 加
  `experimental_bearer_token = "sk-REPLACE_ME"`（否则中转会拒绝 ChatGPT token）。

若服务端给账号下发 `work-mode policy = everyday`，会强制生效 `STEPS_PROSE`，那时只能
禁用 `browser` / `computer-use` / `unified-computer-use` 插件并移除 `[mcp_servers.node_repl]`。

### 7.2 切完 Codex 点图标打不开（Windows 常见）

现象：进程列表里有多个 `ChatGPT.exe`，但 `MainWindowTitle` 为空，再点图标也不出新窗口。

原因：Electron 单实例 + 上次异常退出的锁 + 起来的 `codex.exe app-server` 立刻死掉 → UI 永远等后端。

处理（`scripts/restart-codex.ps1` 就是这套）：

```
1. CloseMainWindow() 优雅关；不行就 taskkill /F /T /IM ChatGPT.exe
2. 清残留 codex.exe / codex-code-mode-host.exe / node_repl.exe / extension-host.exe
3. 删僵尸锁：~/.codex/thread-writer-locks/*（含 .coordination.lock）
              ~/.codex/project-metadata-locks/*
              ~/.codex/*.lock  ~/.codex/*.guard
4. explorer.exe shell:AppsFolder\<AUMID> 重新拉起
5. 校验：窗口标题非空 + codex.exe app-server 存活
```

注：正常启动较慢（30–60 秒白屏属正常）；不要在 20 秒内重复点。

### 7.3 历史线程"消失"

```bash
python scripts/ccswitch.py bucket-report
```

看到 `OpenAI: 400 / custom: 500` 这类分布，而当前 provider id 是 `custom` → `OpenAI` 那批被过滤了。
用 `bucket-migrate --from OpenAI --to custom` 迁移（脚本会备份 DB 与改写偏移账本）。
迁移只改 `threads.model_provider` 和每个 rollout 首行的同名标签，**不改正文**。

### 7.4 中转返回 4xx/5xx 怎么区分

| 现象 | 含义 | 处理 |
|---|---|---|
| `401 INVALID_API_KEY` | key 错/被禁 | 换 key |
| `403 INSUFFICIENT_BALANCE` | 余额为 0 | 充值/换 key，与模型无关 |
| `502 upstream_error` | 中转上游故障 | 换模型或等恢复 |
| `503 Service temporarily unavailable` | 该模型上游不可用 | 换模型（先 `/v1/models` 再逐个探） |
| `404` | base_url 拼错（多写/少写 `/v1`） | 看该中转文档 |

探测模板见 §4.3 第 3 段，把 model 换成候选值逐个试。

### 7.5 官方模式报 "model is not supported … ChatGPT account"

官方后端只认官方模型。用 `gpt-6-astra` 之类；`gpt-6.1-sol` / `gpt-5.6` 这类第三方命名只在对应中转可用。

---

## 8. 安全规则（每次都照做）

1. **先备份再写**：脚本自动备份 `auth.json / config.toml / settings.json / cc-switch.db / codex_oauth_auth.json`。
2. **切换前先 sync token**，否则官方登录态可能被覆盖丢失（§5）。
3. **同一时间只允许一个写者**：切号时先退出 CC Switch 或先退出 Codex，不要两头同时动。
4. **不要用旧 `auth.json` 回滚凭证**：它只能当格式模板；凭证必须用当前有效 rt 现刷。
5. **改 config 只改 provider 块**：`[projects.*]`、`[plugins.*]`、`[mcp_servers.*]` 不要重写。
6. **校验三件事**再收工：`login status`、真实请求 200、`threads` 桶分布。
7. **不要提交密钥**：`auth.json`、`cc-switch.db`、本 skill 的示例里都只允许 `sk-REPLACE_ME`。

---

## 9. 切换检查清单

**切换前**

- [ ] `python scripts/ccswitch.py show` 记录当前状态
- [ ] `python scripts/ccswitch.py sync-tokens --check` → `已一致: True`
- [ ] 目标 key/模型已用 §4.3 探过一次（余额、模型可用性）
- [ ] 知道目标模式的计费方（中转 or ChatGPT 套餐）

**切换后**

- [ ] `codex login status` 正确
- [ ] `codex exec … "Reply with exactly: OK"` 成功
- [ ] `python scripts/ccswitch.py bucket-report` 桶与当前 provider 一致
- [ ] 重启应用并确认窗口渲染出来
- [ ] 记下备份目录，便于回滚

---

## 10. 脚本与资源

```
ccswitchskill/
├── SKILL.md                      ← 本文件
├── README.md                     ← 安装与快速上手（给人看）
├── scripts/
│   ├── ccswitch.py               ← 切换/查看/同步/桶迁移（无密钥，纯标准库）
│   └── restart-codex.ps1         ← Windows：假死急救（清锁 + AUMID 重拉）
├── references/
│   ├── cc-switch-data-model.md   ← CC Switch 数据库/文件结构、GUI 与直改的差异
│   ├── codex-config-and-buckets.md ← config.toml 全量对照 + 线程桶迁移细节
│   ├── token-and-login.md        ← refresh token 轮换、设备码登录、账号库
│   └── troubleshooting.md        ← 报错 → 根因 → 处理（可复制命令）
└── assets/
    ├── relay-config.example.toml     ← 中转配置模板（占位 key）
    └── official-config.example.toml  ← 官方配置模板
```

**安装位置**（DSH 会扫描这些根目录，取 `~/.dsh/skills` 最省事）：

```
~/.dsh/skills/ccswitchskill/SKILL.md      # 用户级
<project>/.dsh/skills/ccswitchskill/SKILL.md  # 项目级（优先级更高）
```

放好后重开一个会话即可在技能列表看到 `ccswitchskill`；**不需要重启 DSH**（文件系统技能提供方会 watch）。
