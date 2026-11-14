# CC Switch 数据模型（供 ccswitchskill 参考）

本文只讲**与切换 Codex 有关**的部分：库里存什么、GUI 点一下发生了什么、什么时候会被写坏、怎么查。

---

## 1. 目录与文件

| 路径 | 作用 | 切换时会被改吗 |
|---|---|---|
| `~/.cc-switch/settings.json` | 应用设置 + `currentProviderCodex` | 会 |
| `~/.cc-switch/cc-switch.db` | SQLite：供应商库、公共配置、日志等 | 会（`providers.is_current`） |
| `~/.cc-switch/codex_oauth_auth.json` | ChatGPT 账号库（多账号） | 切官方会刷新并回写 |
| `~/.cc-switch/backups/` | 备份目录（手工 + 应用自身） | 只增 |
| `~/.cc-switch/logs/cc-switch*.log` | 运行日志（滚动，单文件可达 20 MB） | — |

`settings.json` 里与 Codex 相关的键：

```json
{
  "currentProviderCodex": "<provider id>",
  "preserveCodexOfficialAuthOnSwitch": true,   // 切第三方时是否保留官方登录（v3.16.1+）
  "unifyCodexSessionHistory": true,            // 统一会话历史（让官方也以 custom 桶运行）
  "unifyCodexMigrateExisting": true,           // 是否把历史官方会话迁入
  "localMigrations": { "codexOfficialHistoryUnifyV1": { ... } }
}
```

> `localMigrations.codexOfficialHistoryUnifyV1` 记录了一次性迁移的结果：
> `completedAt` / `migratedJsonlFiles` / `migratedStateRows`。
> 如果 `migrated*` 都是 0 且没有生成 `backups/codex-official-history-unify-v1/`，
> 说明**迁移从来没真正跑过**（被"live 未统一"等闸门跳过），历史里可能还留着旧桶。

---

## 2. `providers` 表

```sql
CREATE TABLE providers (
  id TEXT PRIMARY KEY,          -- uuid 或 'default'
  app_type TEXT,                -- 'codex' / 'claude' / 'gemini' / 'grokbuild'
  name TEXT,                    -- 显示名，可随时改
  settings_config TEXT,         -- JSON：{ "auth": {...}, "config": "<TOML 文本>" }
  meta TEXT,                    -- JSON：注入策略、账号绑定等
  is_current INTEGER,
  sort_index INTEGER,
  created_at INTEGER,
  website_url TEXT, category TEXT, notes TEXT, icon TEXT, icon_color TEXT,
  in_failover_queue INTEGER, cost_multiplier REAL,
  limit_daily_usd REAL, limit_monthly_usd REAL, provider_type TEXT
);
```

### `settings_config` 的两种写法

**中转（key 写在 auth）**

```json
{
  "auth": { "OPENAI_API_KEY": "sk-REPLACE_ME" },
  "config": "model_provider = \"custom\"\nmodel = \"gpt-6.1-sol\"\nreview_model = \"gpt-5.5\"\n\n[model_providers.custom]\nname = \"晨夕API\"\nbase_url = \"https://chenxiapi.com\"\nwire_api = \"responses\"\nrequires_openai_auth = true\n"
}
```

**官方（绑定账号库，不写 token）**

```json
{
  "auth": {},
  "meta": {
    "commonConfigEnabled": true,
    "endpointAutoSelect": true,
    "authBinding": { "source": "managed_account", "authProvider": "codex_oauth", "accountId": "<账号库 uuid>" },
    "providerType": "codex_oauth"
  },
  "config": "model = \"gpt-6-astra\"\n"
}
```

官方供应商的 `config` 往往只有一行 `model`，真正那段 `[model_providers.custom]`（`name="OpenAI"`、
`requires_openai_auth`、`supports_websockets`）是 CC Switch 的「统一会话历史」注入进去的。

### 公共配置

`settings` 表里 `key='common_config_codex'` 存的是**所有 Codex 供应商共享的 TOML 片段**：
`[desktop]`、`[windows]`、`[features]`、`[marketplaces.*]`、`[plugins.*]`、`[mcp_servers.*]`、
`[shell_environment_policy.set]`、`[projects.*]` 等。

切换时最终写入 `~/.codex/config.toml` 的内容 ≈ **public_config 与供应商 config 的合并**。
所以：

- 想让某个设置对所有供应商生效 → 改 `common_config_codex`
- 只想对某一家生效 → 改那家的 `settings_config.config`

---

## 3. GUI 切换到底做了什么

```
用户点供应商
  → 写 ~/.codex/config.toml   （common_config_codex + 该供应商 config 合并）
  → 写 ~/.codex/auth.json     （key 模式写 key；官方模式现刷 token 写 chatgpt 结构）
  → 改 settings.json / providers.is_current
```

**不碰** `~/.codex/sessions/`、`state_5.sqlite`。

### 已知副作用：回写覆盖

切换时 CC Switch 会把"当前 live 的 config"**回写到当时被标记为 current 的供应商**的
`settings_config` 里。于是会出现这种情况：

```
你手工把 live 配置改成「中转 B」（脚本切换，没动 DB）
CC Switch 仍认为 current = 「中转 A」
CC Switch 某次动作 → 把 live(中转 B) 回写进「中转 A」的 settings_config
结果：「中转 A」那一条变成了「中转 B」的 base_url + key
```

**症状**：GUI 里点「中转 A」，实际连到「中转 B」。
**检查**：`python scripts/ccswitch.py list`，看每家 `base=` 是否符合预期。
**修复**：从之前的备份 DB 里取回那一条 `settings_config`：

```bash
python - <<'PY'
import sqlite3, json, glob
old = sorted(glob.glob('/Users/you/.cc-switch/backups/*/cc-switch.db'))[0]
src = sqlite3.connect(old)
dst = sqlite3.connect('/Users/you/.cc-switch/cc-switch.db')
pid = '<provider id>'
row = src.execute("SELECT settings_config FROM providers WHERE id=?", (pid,)).fetchone()
dst.execute("UPDATE providers SET settings_config=? WHERE id=?", (row[0], pid))
dst.commit()
print('restored', pid)
PY
```

（Windows 路径同理，把 `~` 换成 `%USERPROFILE%`。）

---

## 4. 手工新增一个供应商（不用 GUI）

```python
import sqlite3, json, time, uuid

pid = str(uuid.uuid4())
settings_config = {
    "auth": {"OPENAI_API_KEY": "sk-REPLACE_ME"},
    "config": (
        'model_provider = "custom"\n'
        'model = "gpt-6.1-sol"\n'
        'review_model = "gpt-5.5"\n\n'
        '[model_providers.custom]\n'
        'name = "晨夕API"\n'
        'base_url = "https://chenxiapi.com"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n'
    ),
}
meta = {"commonConfigEnabled": True, "endpointAutoSelect": True, "apiFormat": "openai_responses"}

con = sqlite3.connect('/path/to/.cc-switch/cc-switch.db')
con.execute("""INSERT INTO providers
  (id, app_type, name, settings_config, website_url, category, created_at, sort_index,
   notes, icon, icon_color, meta, is_current, in_failover_queue, cost_multiplier,
   limit_daily_usd, limit_monthly_usd, provider_type)
  VALUES (?, 'codex', ?, ?, NULL, NULL, ?, 1, NULL, NULL, NULL, ?, 0, 0, 1, NULL, NULL, NULL)""",
  (pid, "晨夕中转", json.dumps(settings_config, ensure_ascii=False),
   int(time.time()*1000), json.dumps(meta, ensure_ascii=False)))
con.commit()
```

注意：**容器格式**（`[model_providers.custom]` 用 `custom` 还是别的 id）决定线程桶，
见 `codex-config-and-buckets.md`。

---

## 5. 排查用 SQL / 命令

```bash
# 当前供应商
python -c "import json;print(json.load(open('~/.cc-switch/settings.json'))['currentProviderCodex'])"

# 所有 codex 供应商（id/name/current）
sqlite3 ~/.cc-switch/cc-switch.db \
  "SELECT id,name,is_current FROM providers WHERE app_type='codex' ORDER BY is_current DESC;"

# 看某家存的 base_url / model
python - <<'PY'
import sqlite3, json, re
con = sqlite3.connect('/path/to/.cc-switch/cc-switch.db')
for pid, name, sc in con.execute("SELECT id,name,settings_config FROM providers WHERE app_type='codex'"):
    cfg = (json.loads(sc or '{}').get('config') or '')
    base = (re.search(r'base_url\s*=\s*"([^"]+)"', cfg) or [None, '-'])[1]
    model = (re.search(r'(?m)^model\s*=\s*"([^"]+)"', cfg) or [None, '-'])[1]
    print(pid, name, base, model)
PY
```

> CC Switch 运行时会持有数据库；只读查询没问题，**写入前最好先退出应用**，
> 否则它退出时可能用内存状态覆盖你的修改。
