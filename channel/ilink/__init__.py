# -*- coding: utf-8 -*-
"""自研 微信 iLink 轻量协议客户端（不依赖 OpenClaw / node，纯 Python 标准库 + segno）。
协议逆向自腾讯官方 @tencent-weixin/openclaw-weixin v2.4.8（MIT）。
路径：QR登录 → 登录态存 accounts/<accountId>.json → getUpdates 长轮询收消息 → sendmessage 回消息。
"""
__version__ = "0.1.0"
