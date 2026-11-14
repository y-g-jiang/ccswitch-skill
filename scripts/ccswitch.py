#!/usr/bin/env python3
"""ccswitch.py — Codex 供应商切换工具（无任何内置密钥）。

适用：Windows / macOS / Linux；Python 3.9+，只用标准库。

子命令：
  show           打印当前状态（认证形态 / 路由 / 线程桶）
  list           列出 CC Switch 里的 codex 供应商
  sync-tokens    把 ~/.codex/auth.json 里最新的 ChatGPT token 回写账号库
  backup         手工备份关键文件
  relay          切到第三方中转（API key 模式）
  official       切到官方 ChatGPT 登录（从账号库现刷 token）
  bucket-report  查看 threads.model_provider 分布
  bucket-migrate 迁移线程桶（默认 dry-run，加 --apply 才写）
  verify         跑一次真实请求

示例（chenxiapi 中转）：
  python ccswitch.py relay \
      --name 晨夕API --base-url https://chenxiapi.com \
      --model gpt-6.1-sol --api-key sk-REPLACE_ME

注意：relay / official 都会先 sync-tokens 再备份，最后才写文件。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
CC_HOME = Path(os.environ.get("CCSWITCH_HOME") or (HOME / ".cc-switch"))
AUTH = CODEX_HOME / "auth.json"
CONFIG = CODEX_HOME / "config.toml"
STATE_DB = CODEX_HOME / "state_5.sqlite"
CC_DB = CC_HOME / "cc-switch.db"
CC_SETTINGS = CC_HOME / "settings.json"
OAUTH_STORE = CC_HOME / "codex_oauth_auth.json"
BACKUPS = CC_HOME / "backups"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

OFFICIAL_PROVIDER_BLOCK = (
    '[model_providers.custom]\n'
    'name = "OpenAI"\n'
    'requires_openai_auth = true\n'
    'supports_websockets = true\n'
    'wire_api = "responses"\n'
)


# ---------------------------------------------------------------- helpers

def die(msg: str, code: int = 1):
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(code)


def jwt_claims(token: str) -> dict:
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def config_text() -> str:
    return CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""


def config_get(text: str, key: str):
    m = re.search(r"(?m)^%s\s*=\s*\"([^\"]*)\"" % re.escape(key), text)
    return m.group(1) if m else None


def provider_block(text: str):
    m = re.search(r"(?ms)^\[model_providers\.custom\]\n(?:[^\[\n][^\n]*\n|\n)*", text)
    return m


def set_config(text: str, block: str, model: str, provider: str = "custom") -> str:
    m = provider_block(text)
    if m:
        text = text[:m.start()] + block + text[m.end():]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    if re.search(r"(?m)^model_provider\s*=", text):
        text = re.sub(r"(?m)^model_provider\s*=.*$", 'model_provider = "%s"' % provider, text, count=1)
    else:
        text = 'model_provider = "%s"\n' % provider + text
    if re.search(r"(?m)^model\s*=", text):
        text = re.sub(r"(?m)^model\s*=.*$", 'model = "%s"' % model, text, count=1)
    return text


def backup(tag: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = BACKUPS / ("%s-%s" % (tag, stamp))
    dst.mkdir(parents=True, exist_ok=True)
    for p in (AUTH, CONFIG, CC_SETTINGS, CC_DB, OAUTH_STORE):
        if p.exists():
            shutil.copy2(p, dst / p.name)
    print("backup ->", dst)
    return dst


def call_codex(args: list) -> int:
    try:
        return subprocess.call(["codex"] + args)
    except FileNotFoundError:
        print("(找不到 codex 命令，跳过)")
        return 127


# ---------------------------------------------------------------- tokens

def sync_tokens(check_only: bool = False) -> int:
    auth = read_json(AUTH) or {}
    tokens = auth.get("tokens") or {}
    if auth.get("auth_mode") != "chatgpt" or not tokens.get("refresh_token"):
        print("auth.json 是 API key 模式，没有 ChatGPT 凭证可同步")
        return 0
    claims = jwt_claims(tokens.get("id_token", ""))
    email = claims.get("email")
    account = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    store = read_json(OAUTH_STORE)
    if not store:
        die("找不到 %s" % OAUTH_STORE)

    target = None
    for aid, rec in (store.get("accounts") or {}).items():
        if rec.get("email") == email or (account and rec.get("chatgpt_account_id") == account):
            target = aid
            break
    if target is None:
        print("账号库里没有这个账号:", email)
        return 2

    rec = store["accounts"][target]
    same = (rec.get("refresh_token") == tokens.get("refresh_token")
            and rec.get("id_token") == tokens.get("id_token"))
    print("email      :", email)
    print("store 记录 :", target)
    print("已一致     :", same)
    if check_only or same:
        return 0

    now = int(time.time())
    rec.update({
        "chatgpt_account_id": account,
        "email": email,
        "refresh_token": tokens["refresh_token"],
        "id_token": tokens.get("id_token"),
        "authenticated_at": now,
        "token_updated_at_ms": now * 1000,
    })
    write_json(OAUTH_STORE, store)
    print("已同步 refresh_token / id_token 到账号库")
    return 0


def refresh_account(selector: str) -> dict:
    store = read_json(OAUTH_STORE)
    if not store:
        die("找不到 %s（没有账号库，官方模式需要先登录一次）" % OAUTH_STORE)
    accounts = store.get("accounts") or {}
    items = list(accounts.items())
    pick = None
    if selector in ("", "0") and items:
        pick = items[0]
    elif selector.isdigit() and int(selector) < len(items):
        pick = items[int(selector)]
    else:
        for aid, rec in items:
            if selector in (aid, rec.get("email")):
                pick = (aid, rec)
                break
    if not pick:
        die("账号库中找不到账号 %r；可用账号：%s" % (selector, [r.get("email") for _, r in items]))
    aid, rec = pick
    rt = rec.get("refresh_token")
    if not rt:
        die("账号 %s 没有 refresh_token，需要重新登录" % rec.get("email"))

    body = json.dumps({
        "client_id": CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "scope": "openid profile email",
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "ignore")
        print("oauth refresh ->", e.code, payload[:300])
        die("refresh 失败：rt 已失效/已被消费，只能重新登录（codex login --device-auth）")
    except Exception as e:
        die("网络错误：%r" % e)

    claims = jwt_claims(tok["id_token"])
    account = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    now = int(time.time())
    rec.update({
        "refresh_token": tok["refresh_token"],
        "id_token": tok["id_token"],
        "chatgpt_account_id": account,
        "email": claims.get("email"),
        "authenticated_at": now,
        "token_updated_at_ms": now * 1000,
    })
    write_json(OAUTH_STORE, store)
    print("refresh ok:", claims.get("email"),
          "| plan", (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type"))
    return {"tok": tok, "claims": claims, "account": account, "path": aid}


# ---------------------------------------------------------------- cc-switch db

def cc_conn():
    if not CC_DB.exists():
        return None
    return sqlite3.connect(str(CC_DB))


def list_providers():
    con = cc_conn()
    if con is None:
        print("找不到 %s（本机没装 CC Switch？可以只用 --no-db 模式）" % CC_DB)
        return
    rows = con.execute(
        "SELECT id,name,is_current,settings_config FROM providers WHERE app_type='codex' ORDER BY is_current DESC, name"
    ).fetchall()
    con.close()
    for pid, name, cur, sc in rows:
        base = model = key = "-"
        try:
            d = json.loads(sc or "{}")
            cfg = d.get("config") or ""
            base = config_get(cfg, "base_url") or "(官方/无)"
            model = config_get(cfg, "model") or "-"
            key = "set" if (d.get("auth") or {}).get("OPENAI_API_KEY") else "no"
        except Exception:
            pass
        print("%s %-40s current=%s" % ("*" if cur else " ", name[:40], cur))
        print("    id=%s  base=%s  model=%s  key=%s" % (pid, base, model, key))


def cc_set_current(identifier: str):
    con = cc_conn()
    if con is None:
        print("(没有 cc-switch.db，跳过 CC Switch 状态同步)")
        return
    row = con.execute("SELECT id FROM providers WHERE app_type='codex' AND (id=? OR name=?)",
                      (identifier, identifier)).fetchone()
    if not row:
        print("(CC Switch 里没有 id/name = %r 的供应商，跳过)" % identifier)
        con.close()
        return
    pid = row[0]
    con.execute("UPDATE providers SET is_current=0 WHERE app_type='codex'")
    con.execute("UPDATE providers SET is_current=1 WHERE id=?", (pid,))
    con.commit()
    con.close()
    if CC_SETTINGS.exists():
        s = read_json(CC_SETTINGS) or {}
        s["currentProviderCodex"] = pid
        write_json(CC_SETTINGS, s)
    print("cc-switch current ->", identifier)


# ---------------------------------------------------------------- thread buckets

def bucket_report():
    if not STATE_DB.exists():
        die("找不到 %s" % STATE_DB)
    con = sqlite3.connect("file:%s?mode=ro" % STATE_DB.as_posix(), uri=True)
    rows = con.execute("SELECT model_provider, archived, COUNT(*) FROM threads GROUP BY 1,2").fetchall()
    con.close()
    total = sum(r[2] for r in rows)
    print("threads 总数:", total)
    for prov, arch, n in rows:
        print("  %-10s archived=%d  %d" % (prov, arch, n))
    cur = config_get(config_text(), "model_provider")
    print("当前 model_provider:", cur)


def bucket_migrate(src: str, dst: str, apply: bool):
    if not STATE_DB.exists():
        die("找不到 %s" % STATE_DB)
    if len(src) != len(dst):
        die("两个桶名长度不同（%d vs %d）：本脚本只做等长原地改写，请改用 JSON 重写模式" % (len(src), len(dst)))
    con = sqlite3.connect(str(STATE_DB))
    rows = con.execute("SELECT id, rollout_path FROM threads WHERE model_provider=?", (src,)).fetchall()
    print("需要迁移的 thread:", len(rows))
    if not rows:
        con.close()
        return

    todo = []
    for tid, rp in rows:
        p = Path(rp.replace("\\\\?\\", ""))
        if not p.exists():
            continue
        try:
            head = p.read_bytes().split(b"\n", 1)[0]
        except Exception:
            continue
        needle = ('"model_provider":"%s"' % src).encode()
        idx = head.find(needle)
        if idx < 0:
            continue
        off = idx + len('"model_provider":"')
        todo.append((str(p), off, p.stat().st_size))
    print("可改写 rollout 文件:", len(todo))
    if not apply:
        print("dry-run：未写入。加 --apply 执行（会先备份 DB 与偏移账本）")
        con.close()
        return

    dst_bk = backup("bucket-%s-to-%s" % (src, dst))
    write_json(dst_bk / "rollout_offsets.json", todo)
    con.execute("CREATE TABLE IF NOT EXISTS _ccswitch_bucket_ledger(thread_id TEXT, old TEXT, ts INTEGER)")
    for tid, old_provider, _ in [(r[0], src, 0) for r in rows]:
        con.execute("INSERT INTO _ccswitch_bucket_ledger VALUES (?,?,?)", (tid, old_provider, int(time.time())))
    con.commit()

    patched = failed = 0
    for path, off, _size in todo:
        try:
            with open(path, "r+b") as f:
                f.seek(off)
                if f.read(len(src)).decode() != src:
                    failed += 1
                    continue
                f.seek(off)
                f.write(dst.encode())
            patched += 1
        except Exception:
            failed += 1
    con.execute("UPDATE threads SET model_provider=? WHERE model_provider=?", (dst, src))
    con.commit()
    left = con.execute("SELECT COUNT(*) FROM threads WHERE model_provider=?", (src,)).fetchone()[0]
    con.close()
    print("rollout 改写:", patched, "失败:", failed)
    print("threads 迁移完成，剩余 %s 桶: %d" % (src, left))
    print("回滚：恢复", dst_bk / STATE_DB.name, "并按", dst_bk / "rollout_offsets.json", "把首行改回", src)


# ---------------------------------------------------------------- commands

def cmd_show(_args):
    auth = read_json(AUTH) or {}
    text = config_text()
    mode = auth.get("auth_mode") or ("api_key" if auth.get("OPENAI_API_KEY") else "none")
    print("auth mode      :", mode)
    if mode == "chatgpt":
        claims = jwt_claims((auth.get("tokens") or {}).get("id_token", ""))
        print("email          :", claims.get("email"))
        print("plan           :", (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type"))
    print("model_provider :", config_get(text, "model_provider"))
    print("model          :", config_get(text, "model"))
    m = provider_block(text)
    if m:
        base = config_get(m.group(0), "base_url") or "(官方/无)"
        print("provider block : %s  base=%s" % (config_get(m.group(0), "name"), base))
    if CC_SETTINGS.exists():
        s = read_json(CC_SETTINGS) or {}
        print("cc-switch cur  :", s.get("currentProviderCodex"))
    if STATE_DB.exists():
        con = sqlite3.connect("file:%s?mode=ro" % STATE_DB.as_posix(), uri=True)
        rows = con.execute("SELECT model_provider, COUNT(*) FROM threads GROUP BY 1").fetchall()
        con.close()
        print("thread buckets :", rows)


def cmd_relay(args):
    sync_tokens()
    backup("switch-to-%s" % re.sub(r"[^A-Za-z0-9_.-]+", "_", args.name))
    write_json(AUTH, {"OPENAI_API_KEY": args.api_key})
    block = (
        '[model_providers.custom]\n'
        'name = "%s"\n'
        'base_url = "%s"\n' % (args.name, args.base_url.rstrip("/"))
    )
    if args.catalog_url:
        block += 'model_catalog_url = "%s"\n' % args.catalog_url
    block += 'wire_api = "%s"\nrequires_openai_auth = true\n' % args.wire_api
    if args.bearer_token:
        block += 'experimental_bearer_token = "%s"\n' % args.bearer_token
    CONFIG.write_text(set_config(config_text(), block, args.model), encoding="utf-8")
    print("auth.json -> api key 模式")
    print("config.toml -> model=%s base=%s" % (args.model, args.base_url))
    if args.provider_id:
        cc_set_current(args.provider_id)
    print("done：记得重启 Codex 应用，再跑 `python ccswitch.py verify`")


def cmd_official(args):
    info = refresh_account(args.account)
    backup("switch-to-official")
    tok = info["tok"]
    key = info["account"]
    now = time.time()
    last_refresh = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + \
        ".%09d+08:00" % (int(now * 1e9) % 1000000000)
    write_json(AUTH, {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": last_refresh,
        "tokens": {
            "access_token": tok["access_token"],
            "account_id": key,
            "id_token": tok["id_token"],
            "refresh_token": tok["refresh_token"],
        },
    })
    CONFIG.write_text(set_config(config_text(), OFFICIAL_PROVIDER_BLOCK, args.model), encoding="utf-8")
    print("auth.json -> chatgpt 模式")
    print("config.toml -> official（无 base_url），model=%s" % args.model)
    if args.provider_id:
        cc_set_current(args.provider_id)
    print("done：记得重启 Codex 应用")


def cmd_verify(args):
    rc = call_codex(["login", "status"])
    rc2 = call_codex(["exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                      "-C", os.getcwd(), "Reply with exactly: OK"])
    print("login status rc=%s, exec rc=%s" % (rc, rc2))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Codex 供应商切换工具")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("show", help="打印当前状态").set_defaults(func=cmd_show)
    sub.add_parser("list", help="列出 CC Switch 的 codex 供应商").set_defaults(
        func=lambda a: list_providers())

    p = sub.add_parser("sync-tokens", help="auth.json 的 token 回写账号库")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=lambda a: sys.exit(sync_tokens(a.check)))

    p = sub.add_parser("backup", help="备份关键文件")
    p.add_argument("--tag", default="manual")
    p.set_defaults(func=lambda a: backup(a.tag))

    p = sub.add_parser("relay", help="切到第三方中转")
    p.add_argument("--name", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--api-key", required=True)
    p.add_argument("--review-model", default=None, help="(保留兼容，不写入)")
    p.add_argument("--wire-api", default="responses")
    p.add_argument("--catalog-url", default=None)
    p.add_argument("--bearer-token", default=None, help="同时写 provider 级 bearer（混合模式用）")
    p.add_argument("--provider-id", default=None, help="同步 CC Switch 当前供应商（id 或 name）")
    p.set_defaults(func=cmd_relay)

    p = sub.add_parser("official", help="切到官方 ChatGPT 登录")
    p.add_argument("--account", default="0", help="账号库里的序号/id/email")
    p.add_argument("--model", default="gpt-6-astra")
    p.add_argument("--provider-id", default=None)
    p.set_defaults(func=cmd_official)

    p = sub.add_parser("bucket-report", help="线程桶分布")
    p.set_defaults(func=lambda a: bucket_report())

    p = sub.add_parser("bucket-migrate", help="迁移线程桶（默认 dry-run）")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", dest="dst", required=True)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=lambda a: bucket_migrate(a.src, a.dst, a.apply))

    p = sub.add_parser("verify", help="跑一次真实请求")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
