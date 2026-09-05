# -*- coding: utf-8 -*-
"""本地测试适配器：消息走 stdin 注入，输出写日志文件（用于无微信/QQ时验证全链路）。"""
import os, datetime, threading
from .base import BaseAdapter

class LocalAdapter(BaseAdapter):
    name = "local"
    LOCK = threading.Lock()
    def start(self):
        print("[LocalAdapter] 就绪：--test 注入消息；回复/文件写入 /tmp/agent-channel/outbox-local.log", flush=True)
    def _log(self, seg):
        with self.LOCK:
            with open("/tmp/agent-channel/outbox-local.log", "a", encoding="utf-8") as f:
                f.write(datetime.datetime.now().strftime("%H:%M:%S") + " | " + seg + "\n")
    def send_text(self, msg, text):
        self._log(f"[回复→{msg['conversation_id']}] {text}")
        print(f"\n[Local回复→{msg['conversation_id']}]\n{text}", flush=True)
    def send_file(self, msg, path):
        sz = os.path.getsize(path) if os.path.exists(path) else -1
        self._log(f"[文件→{msg['conversation_id']}] {path} ({sz}字节)")
        print(f"[Local发文件→{msg['conversation_id']}] {path} ({sz}字节)", flush=True)
