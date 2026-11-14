# Token 与登录（Codex ChatGPT 凭证）

本文件解释：凭证长什么样、为什么会失效、怎么救、以及"账号库"和 `auth.json` 的关系。

---

## 1. 两种认证形态

### API key 模式（第三方中转）

```json
{ "OPENAI_API_KEY": "sk-REPLACE_ME" }
```

- 没有过期时间，余额/额度由中转方管理。
- 不会被"轮换"影响。
- 缺点：Codex 认为"没有人登录"，官方能力（浏览器/电脑操作/远程/部分插件）不可用。

### ChatGPT 模式（官方订阅）

```json
{
  "OPENAI_API_KEY": null,
  "auth_mode": "chatgpt",
  "last_refresh": "2026-01-01T00:00:00.000000000+08:00",
  "tokens": {
    "access_token": "eyJ…",   // 短寿命（约 10 天）
    "account_id": "…",        // chatgpt_account_id
    "id_token": "eyJ…",       // 约 1 小时，含 email / plan 声明
    "refresh_token": "rt.1…"  // 一次性轮换
  }
}
```

`id_token` 的 payload 里能解出：`email`、`https://api.openai.com/auth.chatgpt_account_id`、
`chatgpt_plan_type`（free/plus/pro…）。脚本用它来做账号匹配。

---

## 2. 轮换规则（最贵的一课）

1. **refresh token 是一次性的**：每换一次票，旧 rt 立刻作废。
2. Codex 在后台自己换票（`last_refresh` 超过阈值时），**新 token 只写 `~/.codex/auth.json`**。
3. CC Switch 的账号库 `~/.cc-switch/codex_oauth_auth.json` **不会自动同步**。
4. 于是账号库里那份 rt 变旧 → 下次用它换票 → `refresh_token_reused`。

### 事故时间线（真实案例）

```
T+0    切到中转，覆盖 auth.json（官方 token 从文件里消失）
T+5m   Codex 后台换票 → 新 rt 只写 auth.json
T+10m  CC Switch 应用供应商 → 用中转 key 覆盖 auth.json
       → 新 rt 蒸发；账号库只剩已被消费的上一代
T+1d   切回官方 → 401 refresh_token_reused → 只能重新登录
```

### 唯一正确的做法

**任何覆盖 `auth.json` 的动作之前**，先执行：

```bash
python scripts/ccswitch.py sync-tokens          # auth.json → 账号库
python scripts/ccswitch.py sync-tokens --check  # 只检查（期望输出 已一致: True）
```

`relay` / `official` 子命令已经内置了这一步；自己手改文件时别忘了。

---

## 3. 失效的表现与含义

| 返回 | 含义 | 能否救 |
|---|---|---|
| `refresh_token_reused` | 这份 rt 已经被消费过（有更新的一份，但没保存下来） | ❌ 只能重新登录 |
| `invalid_refresh_token` | rt 太旧/被吊销/格式不对 | ❌ 只能重新登录 |
| 401 + `token_expired` | access token 过期，但 rt 还有效 | ✅ 会自动刷新 |
| 403 `insufficient_quota` | 账号额度问题 | 换账号/等额度恢复 |

**注意**：用旧备份里的 `auth.json` 去"恢复登录"通常**无效**——里面的 rt 早被后续轮换消费了。
备份的正确用途是：**恢复格式、对照字段**，不是恢复凭证。

---

## 4. 登录流程

### 4.1 有浏览器、本机可回调

```bash
codex login
# 起本地服务 http://localhost:1455 并打开浏览器
```

### 4.2 无浏览器 / 远程 / 只能设备码（推荐写法）

```bash
codex login --device-auth
# 1) 打开 https://auth.openai.com/codex/device
# 2) 输入一次性代码（15 分钟有效）
```

要点：

- 用手机/另一台设备完成授权即可。
- **代码过期不消耗登录次数**；只有"确认授权"那一下才算一次登录。
- 如果浏览器里登录的是别的账号，先退出/切换，否则会登进错误账号。
- 登录成功后**立刻** `python scripts/ccswitch.py sync-tokens`。

### 4.3 验证

```bash
codex login status
# Logged in using ChatGPT                        → chatgpt 模式
# Logged in using an API key - sk-REPL…MASK      → key 模式
```

---

## 5. 账号库结构

`~/.cc-switch/codex_oauth_auth.json`：

```json
{
  "version": 2,
  "default_account_id": "<uuid>",
  "accounts": {
    "<uuid>": {
      "account_id": "<uuid>",
      "chatgpt_account_id": "…",       // 与 id_token 里的对应
      "email": "user@example.com",
      "refresh_token": "rt.1…",        // 一次性，必须保持最新
      "id_token": "eyJ…",
      "authenticated_at": 1700000000,
      "token_updated_at_ms": 1700000000000
    }
  }
}
```

CC Switch 用 `authenticated_at` / `token_updated_at_ms` 判断新鲜度；
`providers.meta.authBinding.accountId` 指向这里的 `<uuid>`。

**常见坑**：账号在 CC Switch 里"重新登录"过，`<uuid>` 会变；而旧供应商的
`authBinding.accountId` 还指着已不存在的旧 uuid → 切换时写入空凭证 → 登录不进去。
修法：把 `authBinding.accountId` 改成现有账号的 uuid（先备份 DB，最好退出应用再改）。

---

## 6. 现刷 token 的 API（脚本用的就是它）

```http
POST https://auth.openai.com/oauth/token
Content-Type: application/json

{
  "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
  "grant_type": "refresh_token",
  "refresh_token": "rt.1…",
  "scope": "openid profile email"
}
```

成功 200，返回 `access_token` / `id_token` / `refresh_token`（**新的 rt**）/ `expires_in`。

保存顺序（很重要）：

1. 先把新 rt 写回账号库
2. 再写 `~/.codex/auth.json`
3. 最后才可以覆盖/切换其它东西

任何一步失败，都要保留已拿到的 token，不要重试到"把新 rt 也消费掉"。
