# -*- coding: utf-8 -*-
"""iLink 消息收发：getUpdates 长轮询（收）+ sendMessage（发）。
消息格式对齐官方 types.ts（WeixinMessage / SendMessageReq）。
"""
import json, random, time, urllib.parse

from . import net
from .net import build_base_info

def _client_id():
    return f"agent-term:{int(time.time()*1000)}-{random.randbytes(4).hex()}"

class Client:
    """一个已登录微信 bot 账号的 iLink 会话。"""
    def __init__(self, account):
        self.account_id = account.get("account_id") or account.get("id") or ""
        self.base_url   = (account.get("base_url") or net.DEFAULT_BASE_URL).rstrip("/")
        self.token      = (account.get("token") or "").strip()
        self.buf        = ""          # get_updates_buf 游标

    # ---------- 收（长轮询单次） ----------
    def get_updates_once(self, timeout_ms=None, buf=None):
        """单次长轮询。超时无消息返回 {"ret":0,"msgs":[]}。
        返回原始 dict；调用方负责更新 self.buf、处理 msgs。"""
        t = timeout_ms or net.LONG_POLL_MS
        payload = {"get_updates_buf": self.buf if buf is None else buf,
                   "base_info": build_base_info()}
        raw = net.post(self.base_url, "ilink/bot/getupdates", payload,
                       token=self.token, timeout=t / 1000.0 + 5)
        resp = json.loads(raw)
        if resp.get("get_updates_buf"):
            self.buf = resp["get_updates_buf"]
        if resp.get("longpolling_timeout_ms") and resp.get("ret", 0) == 0:
            pass  # 服务端建议超时，保留可调
        return resp

    # ---------- 发 ----------
    def send_text(self, to_user_id, text, context_token=None, run_id=None):
        body = {"msg": {
            "from_user_id": "", "to_user_id": to_user_id,
            "client_id": _client_id(),
            "message_type": 2, "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            **({"context_token": context_token} if context_token else {}),
            **({"run_id": run_id} if run_id else {}),
        }, "base_info": build_base_info()}
        raw = net.post(self.base_url, "ilink/bot/sendmessage", body, token=self.token)
        resp = json.loads(raw)
        if resp.get("ret") not in (None, 0):
            raise RuntimeError(f"sendmessage ret={resp.get('ret')} errmsg={resp.get('errmsg')}")
        return resp

    # ---------- 收消息归一化（文本/附件摘要） ----------
    @staticmethod
    def extract_text(full):
        """从 WeixinMessage 提取首个 TEXT 段正文。"""
        try:
            for it in (full.get("item_list") or []):
                if it.get("type") == 1 and it.get("text_item", {}).get("text") is not None:
                    return str(it["text_item"]["text"])
        except Exception:
            pass
        return ""

    @staticmethod
    def has_media(full):
        for it in (full.get("item_list") or []):
            t = it.get("type")
            if t in (2, 3, 4, 5):  # image/voice/file/video
                return True
        return False


def from_account_data(data):
    """把 accounts.py 落盘格式转为 Client 需要的 account dict。"""
    return {"account_id": data.get("_account_id") or "",
            "base_url": data.get("baseUrl") or net.DEFAULT_BASE_URL,
            "token": data.get("token") or ""}
