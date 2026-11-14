# ccswitchskill — Codex 供应商切换技能

给 DSH（或任何读 SKILL.md 的 agent）用的本地技能：在 **官方 ChatGPT 登录** 与 **第三方中转站**
之间切换 Codex，并处理由此引发的一堆坑（历史线程消失、token 失效、应用打不开、计费走错）。

> 全部示例用 **chenxiapi（晨夕中转）**，密钥一律 `sk-REPLACE_ME`。本仓库不含任何真实凭证。

---

## 1. 安装

技能根目录（DSH 会扫描；取第一个最省事）：

```
~/.dsh/skills/ccswitchskill/SKILL.md          # 用户级，所有项目可用
<project>/.dsh/skills/ccswitchskill/SKILL.md  # 项目级，优先级更高
```

**步骤**

```bash
# macOS / Linux
mkdir -p ~/.dsh/skills
cp -r ccswitchskill ~/.dsh/skills/

# Windows (PowerShell)
New-Item -ItemType Directory -Force "$env:USERPROFILE\.dsh\skills" | Out-Null
Copy-Item -Recurse ccswitchskill "$env:USERPROFILE\.dsh\skills\"
```

放好后**新开一个会话**（不需要重启 DSH，技能目录是 watch 的），在技能列表里应看到 `ccswitchskill`。

验证目录结构（必须能直接读到 `SKILL.md`，不能多套一层）：

```
~/.dsh/skills/ccswitchskill/SKILL.md      ✅
~/.dsh/skills/ccswitchskill/skill/SKILL.md ❌（嵌套不会被发现）
```

---

## 2. 前置要求

| 依赖 | 说明 |
|---|---|
| Python 3.9+ | `scripts/ccswitch.py` 只用标准库 |
| Codex CLI | `codex` 在 PATH 里（Windows 通常在 `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe`）|
| CC Switch（可选）| 只有需要同步 GUI 状态/账号库时才用；没有也能纯文件操作 |

平台差异：

- **Windows**：Codex 桌面版可能是 MSIX（进程名 `ChatGPT.exe`），`scripts/restart-codex.ps1` 用 AUMID 启动。
- **macOS/Linux**：路径换成 `~/.codex`、`~/.cc-switch`；重启应用就是退出后重新打开（`restart-codex.ps1` 不适用）。

---

## 3. 五分钟上手

```bash
cd ~/.dsh/skills/ccswitchskill

# 看当前状态（认证形态 / 路由 / 线程桶）
python scripts/ccswitch.py show

# 看 CC Switch 里有哪些供应商，base_url 对不对
python scripts/ccswitch.py list

# 切到 chenxiapi 中转
python scripts/ccswitch.py relay \
  --name 晨夕API \
  --base-url https://chenxiapi.com \
  --model gpt-6.1-sol \
  --api-key sk-REPLACE_ME \
  --provider-id 1231        # 可选：同步 CC Switch 的 current 标记（id 或名字）

# 验证（会调用 codex login status + 一次真实请求）
python scripts/ccswitch.py verify

# 切官方（从账号库现刷 token；需要先登录过一次）
python scripts/ccswitch.py official --account 0 --provider-id "OpenAI Official"
```

切完**必须重启 Codex 应用**，配置才会被读取。

---

## 4. 文件说明

| 文件 | 内容 |
|---|---|
| `SKILL.md` | 主文档：心智模型、数据地图、chenxiapi 示例、安全规则、检查清单 |
| `references/cc-switch-data-model.md` | CC Switch 的 DB/文件结构、GUI 切换做了什么、回写覆盖坑、手工加供应商 |
| `references/codex-config-and-buckets.md` | `config.toml` 逐项说明、三种 provider 形态、线程桶迁移与回滚 |
| `references/token-and-login.md` | refresh token 轮换、事故时间线、设备码登录、账号库结构 |
| `references/troubleshooting.md` | 症状 → 根因 → 命令（含 4xx/5xx 速查、假死急救） |
| `scripts/ccswitch.py` | 切换/查看/同步/桶迁移（无密钥，纯标准库） |
| `scripts/restart-codex.ps1` | Windows：清锁 + AUMID 重拉桌面版 |
| `assets/relay-config.example.toml` | 中转配置模板（占位 key） |
| `assets/official-config.example.toml` | 官方配置模板 |

---

## 5. 安全须知

1. **先备份**：脚本每次写文件前都会备份到 `~/.cc-switch/backups/<动作>-<时间戳>/`。
2. **切号前先同步 token**：否则官方登录态可能被覆盖丢失，之后只能重新登录。
   ```bash
   python scripts/ccswitch.py sync-tokens --check
   ```
3. **一次只让一个程序写 `auth.json`**：切换时先退出 CC Switch 或先退出 Codex。
4. **不要把 `auth.json` / `cc-switch.db` / 本仓库里的示例 key 提交到任何仓库**。
5. **不要用旧备份的 `auth.json` 恢复凭证** —— 里面的 refresh token 早被轮换消费；备份只用于恢复格式。

---

## 6. 卸载

```bash
rm -rf ~/.dsh/skills/ccswitchskill
```

不会影响 `~/.codex` 或 `~/.cc-switch` 里的任何数据；如需回滚某次切换，用对应备份目录。

---

## 7. 常见问题（详见 references/troubleshooting.md）

| 现象 | 一句话处理 |
|---|---|
| `Work browser access requires an authenticated account` | `[desktop] conversationDetailMode = "STEPS_COMMANDS"` |
| 点图标打不开 / 无窗口 | `scripts/restart-codex.ps1`（清进程树 + 僵尸锁 + AUMID 重拉） |
| 历史线程不见 | `bucket-report` → `bucket-migrate --from X --to custom --apply` |
| `refresh_token_reused` | 只能重新登录（`codex login --device-auth`），登完立刻 `sync-tokens` |
| 官方报 model not supported | 官方用 `gpt-6-astra`，别用中转的模型名 |
| 中转 403 `INSUFFICIENT_BALANCE` | 余额为 0，充值/换 key，与模型无关 |
| `codex exec` 卡住 | 后台运行时 stdin 没关；前台跑或重定向 `< /dev/null` |
