# -*- coding: utf-8 -*-
"""适配器基类：只负责平台通信，不含 Agent 逻辑。"""
class BaseAdapter:
    name = "base"
    def __init__(self, cfg): self.cfg = cfg
    def attach(self, manager):
        """把自身注册进 manager 并开始接收消息。manager 侧通过 on_message 回流。"""
        self.manager = manager
        self.start()
    def start(self): raise NotImplementedError
    def stop(self): pass
    def receive(self, msg): self.manager.on_message(msg)
    def send_text(self, msg, text): raise NotImplementedError
    def send_file(self, msg, path): raise NotImplementedError
