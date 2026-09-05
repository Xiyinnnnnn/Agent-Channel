# -*- coding: utf-8 -*-
"""
QQ Adapter —— OneBot11/NapCat/Lagrange 方向，零第三方依赖：
- 接收：内置 HTTP 服务承载 OneBot「反向HTTP」事件推送（NapCat 的 HTTP服务器->反向）
- 发送：HTTP POST 到 OneBot 正向 API
- 群聊：仅 @本机器人 触发，且传给 Agent 的正文去掉 @；私聊直接触发
配置(置于 config.json 的 "qq" 内):
{
  "bot_qq": "123456",
  "onebot_http": "http://127.0.0.1:3000",       # 正向 API 地址
  "webhook_local": "127.0.0.1:18086",           # 本机反向HTTP接收端口
  "webhook_path": "/onebot"                     # 反向事件路径
}
"""
import json, os, re, sys, urllib.request, urllib.error, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .base import BaseAdapter
from . import _cq

class _Handler(BaseHTTPRequestHandler):
    adapter = None
    def log_message(self, *a): pass
    def do_POST(self):
        body = self._read_body()
        # 先回 200，让 NapCat 上报方快速确认收到（避免其重试堆积）
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        except Exception:
            pass
        try:
            ev = json.loads(body)
        except Exception as e:
            sys.stderr.write(f"[QQAdapter] 事件 body 解析失败: {e} body={body[:200]!r}\n")
            return
        threading.Thread(target=self.adapter._on_event, args=(ev,), daemon=True).start()

    def _read_body(self):
        """兼容两种上报编码：
        - Content-Length（curl 等常规 POST）
        - Transfer-Encoding: chunked（NapCat 真实上报无 Content-Length，只读 CL 会 ln=0 → 空 body → 事件被静默丢弃）
        """
        import sys as _sys
        try:
            ln = self.headers.get("Content-Length")
            if ln:
                try:
                    return self.rfile.read(int(ln))
                except Exception:
                    return b""
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                data = b""
                while True:
                    line = self.rfile.readline()
                    if not line:
                        break
                    try:
                        size = int(line.split(b";")[0].strip(), 16)
                    except Exception:
                        break
                    if size <= 0:
                        self.rfile.readline()  # 吃掉结尾空行
                        break
                    data += self.rfile.read(size)
                    self.rfile.readline()      # 吃掉块尾 CRLF
                return data
            return self.rfile.read()  # 兜底：无 CL 也无 chunked，读到 EOF
        except Exception as e:
            _sys.stderr.write(f"[QQAdapter] 读取 body 失败: {e}\n")
            return b""

class QQAdapter(BaseAdapter):
    name = "qq"
    def __init__(self, cfg):
        super().__init__(cfg)
        c = cfg.get("qq") or {}
        self.bot_qq  = str(c.get("bot_qq") or "")
        self.api     = (c.get("onebot_http") or "").rstrip("/")
        wh          = (c.get("webhook_local") or "127.0.0.1:18086").split(":")
        self.wh_host, self.wh_port = wh[0], int(wh[1])
        self.wh_path = c.get("webhook_path") or "/onebot"
        self._http = None
        self._t = None

    # ---- 接收 ----
    def start(self):
        _Handler.adapter = self
        # allow_reuse_address=1：端口处于 TIME_WAIT/残留时可立即重绑，
        # 避免"Address already in use"导致整个 manager 连带微信一起起不来
        ThreadingHTTPServer.allow_reuse_address = True
        self._http = ThreadingHTTPServer((self.wh_host, self.wh_port), _Handler)
        self._http.daemon_threads = True
        self._t = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._t.start()
        print(f"[QQAdapter] 反向HTTP接收 {self.wh_host}:{self.wh_port}{self.wh_path} | bot={self.bot_qq or '(未设)'} | API={self.api or '(未设)'}",
              flush=True)

    def _on_event(self, ev):
        try:
            if ev.get("post_type") != "message":
                return
            mtype = ev.get("message_type")
            print(f"[QQAdapter] 收到消息 type={mtype} uid={ev.get('user_id')} "
                  f"gid={ev.get('group_id','-')} mid={ev.get('message_id')}", flush=True)
            raw = self._segments_str(ev.get("message") or [])
            if mtype == "private":
                uid = str(ev.get("user_id") or ev.get("sender", {}).get("user_id"))
                text = _cq.strip_cq(raw, keep_at=False)
                if not (text.strip() or self._attachments(ev)):
                    return
                print(f"[QQAdapter] 触发任务 text='{text[:80]}' conv=private:{uid}", flush=True)
                self.receive({
                    "platform": "qq",
                    "conversation_id": f"private:{uid}",
                    "sender_id": uid,
                    "message_id": str(ev.get("message_id") or ""),
                    "text": text,
                    "attachments": self._attachments(ev),
                })
            elif mtype == "group":
                gid = str(ev.get("group_id") or "")
                sender = str(ev.get("sender", {}).get("user_id") or ev.get("user_id") or "")
                if not self._at_me(ev):
                    return  # 群消息未@机器人 → 不触发
                text = _cq.strip_cq(raw, keep_at=False)  # 去掉所有@，只留任务正文
                if not (text.strip() or self._attachments(ev)):
                    return
                self.receive({
                    "platform": "qq",
                    "conversation_id": f"group:{gid}",
                    "sender_id": sender,
                    "message_id": str(ev.get("message_id") or ""),
                    "text": text,
                    "attachments": self._attachments(ev),
                })
        except Exception as e:
            print(f"[QQAdapter] 处理事件出错: {e}", file=__import__("sys").stderr)

    def _at_me(self, ev):
        if not self.bot_qq:
            return True  # 未配置 bot_qq 时按 @ 任意 at 触发（宽松模式）
        segs = ev.get("message") or []
        cq = self._segments_str(segs)          # 重建为 CQ 字符串，@在此可识别
        # ① segment 形式：{"type":"at","data":{"qq":"<bot>"}}
        for s0 in segs:
            if isinstance(s0, dict) and s0.get("type") == "at":
                qq = (s0.get("data") or {}).get("qq")
                if str(qq) == self.bot_qq:
                    return True
        # ② CQ 字符串形式兜底
        return f"[CQ:at,qq={self.bot_qq}]" in cq or f"qq={self.bot_qq}]" in cq

    def _segments_str(self, segs):
        parts = []
        for s in segs:
            if isinstance(s, dict):
                if s.get("type") == "text":
                    parts.append(s.get("data", {}).get("text") or "")
                else:
                    parts.append(_cq.dumps(s.get("type"), s.get("data") or {}))
            elif isinstance(s, str):
                parts.append(s)
        return "".join(parts)

    def _attachments(self, ev):
        """把 CQ image/file 段转成可下载 URL 列表。"""
        out = []
        for s in ev.get("message") or []:
            if not isinstance(s, dict): continue
            t = s.get("type"); d = s.get("data") or {}
            if t == "image" and d.get("url"):
                out.append(d["url"])
            elif t == "image" and d.get("file") and d["file"].startswith(("http://", "https://")):
                out.append(d["file"])
        return out

    # ---- 发送 ----
    def _send(self, action, payload):
        if not self.api:
            raise RuntimeError("未配置 onebot_http API 地址")
        req = urllib.request.Request(
            f"{self.api}/{action}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"OneBot {action} 失败 {e.code}: {e.read().decode('utf-8','ignore')[:200]}")
        except Exception as e:
            raise RuntimeError(f"OneBot {action} 网络错误: {e}")

    def send_text(self, msg, text):
        cid = msg.get("conversation_id")
        if cid.startswith("group:"):
            self._send("send_group_msg", {"group_id": int(cid[6:]), "message": text})
        elif cid.startswith("private:"):
            self._send("send_private_msg", {"user_id": int(cid[8:]), "message": text})
        else:
            raise RuntimeError(f"未知会话类型 {cid}")
        print(f"[QQAdapter] 回复成功 conv={cid} text='{str(text)[:60]}'", flush=True)

    def send_file(self, msg, path):
        if path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            payload = f"[CQ:image,file={path}]"
        else:
            payload = f"[CQ:file,file={path}]"
        cid = msg.get("conversation_id")
        if cid.startswith("group:"):
            self._send("send_group_msg", {"group_id": int(cid[6:]), "message": payload})
        elif cid.startswith("private:"):
            self._send("send_private_msg", {"user_id": int(cid[8:]), "message": payload})
        else:
            raise RuntimeError(f"未知会话类型 {cid}")

    def stop(self):
        try:
            if self._http:
                self._http.shutdown()
                self._http.server_close()
        except Exception:
            pass
