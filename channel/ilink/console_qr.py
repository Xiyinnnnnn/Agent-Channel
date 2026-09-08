# -*- coding: utf-8 -*-
"""终端二维码渲染：优先 segno（user-site 已装），失败降级只打印 URL。"""
import shutil, sys

def _show_segno(url):
    import segno
    qr = segno.make(url, error='l')
    try:
        out = qr.terminal(out=sys.stdout, border=1, compact=True)
        return out
    except TypeError:
        qr.terminal(out=sys.stdout, border=1)
        return None

def show(url, title="请用手机微信扫描二维码:"):
    import time as _t
    print("[" + _t.strftime("%H:%M:%S") + "] " + title)
    print("-" * 40)
    try:
        _show_segno(url)
    except Exception:
        print(url)
    print("-" * 40)
    print()
