# -*- coding: utf-8 -*-
"""微信 bot 高层管理：列出已登录账号 / 执行扫码登录并持久化 / 移除账号。
由控制中心或 manager 调用，屏蔽 iLink 细节。
"""
import os, sys

from . import accounts as acc_store
from . import login as qrlogin
from . import console_qr
from .client import Client

def _state_dir():
    """持久化根目录（accounts.set_state_dir 会再拼 /ilink-weixin/accounts）。
    统一固定 HOME 持久目录：~/.local/share/agent-terminal/channel
    → 登录态与 wechat adapter / 控制中心 wx_login_state 读的是同一份，重启免重扫。"""
    return os.path.join(os.path.expanduser("~"), ".local", "share", "agent-terminal", "channel")

def set_runtime_dir(d):
    """由 manager / control 显式注入（它们知道真实 runtime_dir）。"""
    acc_store.set_state_dir(d)

def list_accounts():
    """返回 [{account_id,userId,savedAt,token_masked,baseUrl}]"""
    out = []
    for aid in acc_store.list_account_ids():
        d = acc_store.load_account(aid) or {}
        tok = d.get("token") or ""
        out.append({
            "account_id": aid,
            "user_id": d.get("userId") or "",
            "saved_at": d.get("savedAt") or "",
            "token_masked": (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "(短)",
            "base_url": d.get("baseUrl") or "",
            "logged_in": bool(tok),
        })
    return out

def has_login():
    return any(a["logged_in"] for a in list_accounts())

def _persist(result):
    aid = result.get("account_id") or ""
    acc_store.save_account(aid, token=result.get("token"),
                           base_url=result.get("base_url"),
                           user_id=result.get("user_id"))
    return aid

def do_qr_login(total_timeout_s=480):
    """控制中心前台扫码登录。返回 (ok, message)"""
    acc_store.set_state_dir(_state_dir())
    def on_qr(url):
        console_qr.show(url)
    def on_status(status, note):
        label = {"wait": "等待扫码…", "scaned": "已扫码，请在手机确认",
                 "confirmed": "确认成功", "expired": "二维码过期",
                 "refreshing": "刷新二维码", "redirect": "切换节点",
                 "need_verifycode": "需要配对码", "verify_code_blocked": "配对码被拦截",
                 "binded_redirect": "已连接过"}.get(status, status)
        if status in ("scaned", "confirmed", "refreshing", "binded_redirect", "redirect"):
            print("\r  · " + label + ("  " + note if note else "") + "    ", flush=True)
        sys.stdout.flush()
    def read_verify(prompt):
        try:
            return input("\n  " + prompt)
        except EOFError:
            return ""
    res = qrlogin.login_flow(accounts_module=acc_store, total_timeout_s=total_timeout_s,
                             on_qr=on_qr, on_status=on_status, read_verify=read_verify)
    if res.get("connected"):
        aid = _persist(res)
        res["saved_account_id"] = aid
        return True, res
    if res.get("alreadyConnected"):
        return True, res
    return False, res

def remove_account(account_id):
    acc_store.set_state_dir(_state_dir())
    acc_store.unregister(account_id)

def make_client_for_account(account_id):
    """由已保存账号构造可用的 iLink Client（供 manager 轮询/发送）。"""
    acc_store.set_state_dir(_state_dir())
    d = acc_store.load_account(account_id) or {}
    if not d.get("token"):
        return None
    return Client({"account_id": account_id,
                   "base_url": d.get("baseUrl") or "",
                   "token": d["token"]})

# 供 manager 在同一进程注入真实 runtime_dir
def accounts_store():
    acc_store.set_state_dir(_state_dir())
    return acc_store
