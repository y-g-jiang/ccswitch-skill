# Codex 配置与线程桶

本文件讲两件事：`config.toml` 里与切换相关的键，以及"历史线程为什么看不见 / 怎么迁"。

---

## 1. config.toml 结构（按重要性排序）

```toml
model_provider = "custom"        # 当前用哪个 [model_providers.*]；也是线程桶名
model = "gpt-6.1-sol"
review_model = "gpt-5.5"

model_reasoning_effort = "medium"   # low / medium / high / xhigh
service_tier = "priority"           # 可选；第三方中转常有，官方模式建议删掉
disable_response_storage = true     # 老键，新版本会提示 ignored，无害
network_access = "enabled"          # 同上

notify = [ "…\\codex-computer-use.exe", "turn-ended" ]   # 带本机绝对路径，换机需改/删

[model_providers.custom]
name = "晨夕API"                  # 显示名；name="OpenAI" 会启用官方能力门控
base_url = "https://chenxiapi.com"  # 不带 /v1；Codex 自己拼 /responses
wire_api = "responses"            # 必须是 responses（不是 chat）
requires_openai_auth = true       # 用 auth.json 里的凭证
# experimental_bearer_token = "sk-…"   # 可选：provider 级 key（混合模式用）
# model_catalog_url = "https://…/v1/models"  # 可选：模型目录

[features]
goals = true
api_key_model_discovery = true    # key 模式下按 model_catalog_url 发现模型

[desktop]
conversationDetailMode = "STEPS_COMMANDS"   # STEPS_PROSE 会触发"工作浏览器身份"闸门

[projects.'c:\path\to\project']
trust_level = "trusted"

[plugins."browser@openai-bundled"]
enabled = true

[mcp_servers.node_repl]
command = '…\node_repl.exe'
# 浏览器/电脑操作的后端；纯 API key 模式下它是"Work browser access"报错的必要条件
```

写配置的两条铁律：

1. **只改 provider 块与 `model`/`model_provider`**，其它段（projects/plugins/mcp/marketplaces/desktop）原样保留。
2. 改完用解析器验一遍：`python -c "import tomllib;tomllib.load(open('config.toml','rb'));print('ok')"`。

---

## 2. 三种供应商块的形态

| 形态 | 关键行 | 适用 |
|---|---|---|
| 中转 | `name="晨夕API"` + `base_url` + `requires_openai_auth` | 第三方中转（chenxiapi 等） |
| 官方（统一形态） | `name="OpenAI"` + `requires_openai_auth` + `supports_websockets`，**无 base_url** | ChatGPT 登录、且想与第三方共享历史列表 |
| 官方（原生形态） | `model_provider = "openai"` 或干脆不写 provider | 只想用官方、且能接受历史分桶 |

三种形态里，只有第一种和第二种共用 `custom` 桶；第三种会落到 `openai` 桶。

---

## 3. 线程桶（thread bucket）机制

### 3.1 数据在哪

- 正文：`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，**首行**是 `session_meta`：

  ```json
  {"type":"session_meta","payload":{"session_id":"…","cwd":"…","model_provider":"custom","creator_account_id":"…"}}
  ```

- 索引：`~/.codex/state_5.sqlite` 的 `threads` 表，列 `model_provider`。

会话"属于哪个桶"由这两处共同决定；列表读取以 **DB** 为准，落到具体会话时还会参考 rollout 首行。

### 3.2 为什么会"消失"

Codex 的历史/续聊列表**按 `model_provider` 过滤**：当前 provider 是 `custom`，就只显示 `custom`。
所以换到一家新中转（用 `model_provider = "OpenAI"` 之类）后，之前 `custom` 的会话全部"看不见"，
但它们没丢，只是桶不同。

### 3.3 迁移（两种实现）

**A. 等长原地改写（推荐，快且无损）**

前提：旧桶名和新桶名**长度相同**。经典例子：`"OpenAI"` 与 `"custom"` 都是 6 个字符。

```
1) 备份 state_5.sqlite，记录每个文件的改写偏移
2) 对每个 rollout：读首行 → 找 b'"model_provider":"<old>"' → 定位值起始偏移
   → 用 r+b 打开，seek 到偏移，写 6 字节新值（文件长度、后续内容完全不变）
3) UPDATE threads SET model_provider='<new>' WHERE model_provider='<old>'
4) 校验：DB 分布、抽样首行
```

`python scripts/ccswitch.py bucket-migrate --from OpenAI --to custom`（默认 dry-run）
就是这套；`--apply` 才写，且会把 DB 与偏移账本一起备份到 `~/.cc-switch/backups/bucket-…/`。

**B. JSON 重写（长度不同时）**

读首行 → `json.loads` → 改 `payload.model_provider` → `json.dumps(..., ensure_ascii=False, separators=(',',':'))`
→ 写临时文件 → `os.replace` 原子替换。慢（大文件要整体重写）但通用。

### 3.4 归档会话

`~/.codex/archived_sessions/*.jsonl.zst` 是 zstd 压缩的 rollout。
DB 里的 `rollout_path` 常常写 `.jsonl`（无 `.zst`）。要一并迁桶时：

```python
import zstandard, io
d = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True).read()
# 改首行 → 用 ZstdCompressor(level=3) 重新压缩 → 原子替换（先备份原 .zst）
```

### 3.5 回滚

- 覆盖回备份的 `state_5.sqlite`
- 按 `rollout_offsets.json` 把每处 6 字节写回旧值
- 覆盖回 `archived_zst_originals/`

---

## 4. "统一会话历史"是什么

CC Switch 的该功能做三件事：

1. 给官方供应商注入统一形态的 provider 块（`name="OpenAI"`、`requires_openai_auth`、`supports_websockets`、无 `base_url`），
   让官方也以 `custom` 桶运行 → 官方与第三方共用一个列表。
2. 可选"迁入现有官方会话"：把历史 `openai` 桶的会话改成 `custom`（改前自动备份）。
3. 关闭时可"按备份精确还原"。

闸门（会产生 `live_not_unified` 而跳过迁移）：

- `config.toml` 已有显式 `model_provider`
- 已存在形态不同的 `[model_providers.custom]`（例如带第三方 `base_url`）

所以**纯中转在用的时候不要指望它自动迁**；要迁就用 §3.3 的脚本。

---

## 5. 桶名统一策略（长期建议）

每次换供应商都迁一次桶很烦。更省事的做法：

- 所有中转的 provider 块**统一用 `custom`** 这个 id（只改 `name` / `base_url` / `key`）；
- 官方也用统一形态（`custom`）；
- 这样桶永远只有一个，历史列表从不变。

代价：如果你在 CC Switch 里新增供应商时用了别的 id（例如 `OpenAI`），
它就会自己开一个新桶，需要再迁回来。新增前先看一眼 `python scripts/ccswitch.py list`。
