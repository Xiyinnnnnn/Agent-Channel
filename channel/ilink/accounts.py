# -*- coding: utf-8 -*-
"""登录态持久化：accounts/index + accounts/<accountId>.json（0600）。
格式对齐官方 openclaw-weixin（token/savedAt/baseUrl/userId），重启免重扫。"""
import json, os, time

# 运行时目录默认由 manager 传入；本模块缓存便于单测/独立调用
_STATE_DIR = None

def set_state_dir(d):
    global _STATE_DIR
    _STATE_DIR = d

def _dirs():
    base = _STATE_DIR or os.path.join(os.path.expanduser("~"), ".local", "share", "agent-terminal", "channel")
    wx = os.path.join(base, "ilink-weixin")
    return wx, os.path.join(wx, "accounts")

def _ensure():
    wx, acc = _dirs()
    os.makedirs(acc, exist_ok=True)
    return wx, acc

def _idx_path(wx): return os.path.join(wx, "accounts.json")

def list_account_ids():
    wx, _ = _ensure()
    p = _idx_path(wx)
    try:
        if os.path.exists(p):
            v = json.load(open(p, encoding="utf-8"))
            if isinstance(v, list):
                return [x for x in v if isinstance(x, str) and x.strip()]
    except Exception:
        pass
    return []

def load_account(aid):
    _, acc = _ensure()
    p = os.path.join(acc, aid + ".json")
    try:
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
    except Exception:
        pass
    return None

def register(aid):
    wx, _ = _ensure()
    cur = list_account_ids()
    if aid not in cur:
        cur.append(aid)
        tmp = _idx_path(wx) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _idx_path(wx))
        os.chmod(_idx_path(wx), 0o600)

def save_account(aid, token=None, base_url=None, user_id=None, extra=None):
    _, acc = _ensure()
    existing = load_account(aid) or {}
    if token:  existing["token"] = token.strip(); existing["savedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if base_url: existing["baseUrl"] = base_url.strip()
    if user_id is not None:
        if user_id.strip(): existing["userId"] = user_id.strip()
        else: existing.pop("userId", None)
    if extra:
        existing.update(extra)
    p = os.path.join(acc, aid + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    if token:
        register(aid)   # 凭证落盘即视为可用账号

def unregister(aid):
    wx, acc = _dirs()
    ids = list_account_ids()
    if aid in ids:
        ids.remove(aid)
        try:
            with open(_idx_path(wx), "w", encoding="utf-8") as f:
                json.dump(ids, f, ensure_ascii=False, indent=2)
        except Exception: pass
    try:
        p = os.path.join(acc, aid + ".json")
        if os.path.exists(p): os.remove(p)
    except Exception: pass
