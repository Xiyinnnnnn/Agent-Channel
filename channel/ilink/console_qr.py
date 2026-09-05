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

def show(url, title="请用手机微信扫描下方二维码："):
    try:
        print("")
        print("  " + title)
        print("  " + "-" * 48)
        try:
            _show_segno(url)
        except Exception:
            # compact 兼容失败再试标准宽度
            import segno
            segno.make(url, error='l').terminal(out=sys.stdout, border=1)
        print("  " + "-" * 48)
    except Exception:
        pass
    # 无论二维码是否渲染成功，总打印可点链接
    print("  若二维码无法显示，请复制下方链接到浏览器打开（手机/电脑均可，需已登录微信）：")
    print("  " + url)
    print("")
