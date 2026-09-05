# -*- coding: utf-8 -*-
"""
微信 Adapter —— 真实 iLink 通道（自研轻量协议，逆向后端与腾讯官方 openclaw-weixin 相同）：
- 收：对每个已扫码登录的账号 getUpdates 长轮询；上下文由 getupdates 返回 context_token 记忆。
- 发：sendmessage 文本回复。
- 登录：由控制中心「微信登录」菜单执行（ilink/weixin_bot.do_qr_login），登录态存
      <runtime_dir>/ilink-weixin/accounts/<accountId>.json → 重启免重扫。
配置(置于 config.json 的 "wechat" 内，全部可选；空 = 未登录)：
  { "accounts": [] }   # 留空时自动使用 ilink-weixin/accounts 里已保存的账号
"""
import json, os, sys, threading, time, traceback

# channel/ 目录入 sys.path，保证 manager(cwd=channel) / control 都能 import ilink
_CH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CH not in sys.path:
    sys.path.insert(0, _CH)

from ilink.client import Client
from ilink import accounts as acc_store
from .base import BaseAdapter

class WechatAdapter(BaseAdapter):
    name = "wechat"
    def __init__(self, cfg):
        super().__init__(cfg)
        c = cfg.get("wechat") or {}
        self.runtime_dir = cfg.get("runtime_dir") or "/tmp/agent-channel"
        self.accounts_cfg = c.get("accounts") or []
        self._stop = threading.Event()
        self._threads = []
        self._ctx_token = {}      # sender_id -> context_token（每条新消息覆盖）
        self._clients = {}
        # 登录态固定存 HOME 持久目录（ilink.accounts 默认 ~/.local/share/agent-terminal/channel）
        # → Channel/系统重启均免重扫

    # ---- 生命周期 ----
    def start(self):
        self._reload_clients()
        print(f"[WechatAdapter] 已登录账号: {list(self._clients)}（长轮询收消息 / sendmessage 回复）", flush=True)

    def _reload_clients(self):
        # 配置显式 accounts 优先，否则用持久化登录态
        want = []
        if isinstance(self.accounts_cfg, list) and self.accounts_cfg:
            want = [str(x) for x in self.accounts_cfg]
        else:
            want = acc_store.list_account_ids()
        for aid in want:
            d = acc_store.load_account(aid)
            if not d or not d.get("token"):
                continue
            self._clients[aid] = Client({"account_id": aid,
                                         "base_url": d.get("baseUrl") or "",
                                         "token": d["token"]})
            t = threading.Thread(target=self._poll_loop, args=(aid,), daemon=True)
            t.start(); self._threads.append(t)

    def _poll_loop(self, aid):
        cl = self._clients.get(aid)
        if not cl: return
        fail = 0
        while not self._stop.is_set():
            try:
                resp = cl.get_updates_once()
                fail = 0
                # 业务错误处理
                ret = resp.get("ret"); err = resp.get("errcode")
                if (ret is not None and ret != 0) or (err is not None and err != 0):
                    if err == -14 or ret == -14:
                        print(f"[WechatAdapter] 账号 {aid} token 失效(-14)，停止轮询。请重新扫码登录。", file=sys.stderr, flush=True)
                        return
                    print(f"[WechatAdapter] 账号 {aid} getUpdates 错误 ret={ret} errcode={err} errmsg={resp.get('errmsg')}", file=sys.stderr, flush=True)
                    fail += 1
                    if fail >= 3:
                        time.sleep(10); fail = 0
                    continue
                for full in (resp.get("msgs") or []):
                    self._handle_msg(aid, full)
            except Exception as e:
                fail += 1
                if self._stop.is_set(): return
                if fail >= 3:
                    print(f"[WechatAdapter] 账号 {aid} 轮询连续失败，10s 后重试: {e}", file=sys.stderr, flush=True)
                    time.sleep(10); fail = 0
                else:
                    time.sleep(1.5)

    # ---- 收 ----
    def _handle_msg(self, aid, full):
        try:
            frm = str(full.get("from_user_id") or "").strip()
            if not frm:
                return
            text = Client.extract_text(full)
            # 自己发的(echo)跳过：BOT 类型不处理
            if full.get("message_type") == 2:
                return
            # context_token 记忆（官方按 userId 记忆最新）
            ct = full.get("context_token")
            if ct:
                self._ctx_token[frm] = ct
            att = []
            if Client.has_media(full):
                # 图片/文件暂不自动下载 → 摘要提示，避免无 key 解密
                att = ["[收到一条图片/文件消息，本机自研通道暂未自动下载]"]
            body = (text or "").strip()
            if not body and not att:
                return
            self.receive({
                "platform": "wechat",
                "conversation_id": f"private:{frm}",
                "sender_id": frm,
                "account_id": aid,
                "message_id": str(full.get("message_id") or full.get("seq") or ""),
                "text": body,
                "attachments": att,
            })
        except Exception as e:
            print(f"[WechatAdapter] 处理消息失败: {e}", file=sys.stderr, flush=True)

    # ---- 发 ----
    def send_text(self, msg, text):
        sid = msg.get("sender_id") or ""
        aid = msg.get("account_id")
        # 未标注账号且只有一个账号 → 用它
        if not aid and len(self._clients) == 1:
            aid = next(iter(self._clients))
        cl = self._clients.get(aid or "")
        if cl is None:
            raise RuntimeError("微信未登录或账号不存在，请先在控制中心执行「微信登录」")
        ct = self._ctx_token.get(sid)
        import time as _t
        _t0 = _t.time()
        try:
            cl.send_text(sid, text, context_token=ct)
        except Exception as e:
            print(f"[WechatAdapter] send_text 失败 sid={sid} len={len(text)} err={e!r}", file=sys.stderr, flush=True)
            raise
        print(f"[WechatAdapter] send_text 成功 sid={sid} len={len(text)} 耗时={_t.time()-_t0:.2f}s", file=sys.stderr, flush=True)

    def send_file(self, msg, path):
        # 文本回复已可用；文件上传需 CDN+AES，暂返回明确提示由 Agent 转文字/链接
        raise RuntimeError("微信文件发送尚未接入 CDN（图片/文件请用文字+链接传达），文本回复正常。")

    def stop(self):
        self._stop.set()
        # 轮询线程可能正阻塞在长轮询网络请求上(最长35s+5s)，daemon 不阻塞进程退出，
        # 这里给一个宽限让线程自然返回；超时不强等。
        for t in self._threads:
            try: t.join(timeout=3)
            except Exception: pass
