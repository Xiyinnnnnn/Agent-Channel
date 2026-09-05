# -*- coding: utf-8 -*-
"""Adapter 工厂：统一 receive_message/send_text/send_file，上层不依赖具体平台。"""
from .base import BaseAdapter
from .local import LocalAdapter

def create_adapters(cfg):
    ads = {"local": LocalAdapter(cfg)}
    # 微信/QQ 适配器按环境后续启用（Phase4/5）
    try:
        from .wechat import WechatAdapter
        ads["wechat"] = WechatAdapter(cfg)
    except Exception as e:
        print(f"[Channel] wechat adapter 未加载: {e}", file=__import__("sys").stderr)
    try:
        from .qq import QQAdapter
        ads["qq"] = QQAdapter(cfg)
    except Exception as e:
        print(f"[Channel] qq adapter 未加载: {e}", file=__import__("sys").stderr)
    return ads
