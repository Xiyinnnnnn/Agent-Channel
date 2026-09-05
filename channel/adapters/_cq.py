# -*- coding: utf-8 -*-
"""OneBot/CQ 码轻量工具（零依赖）。"""
import re

def dumps(t, data):
    seg = [t]
    for k, v in (data or {}).items():
        s = str(v).replace(",", "&#44;").replace("]", "&#93;")
        seg.append(f"{k}={s}")
    return "[CQ:" + ",".join(seg) + "]"

_CQ = re.compile(r"\[CQ:([a-z]+),[^\]]*\]")

def strip_cq(text, keep_at=False):
    """去掉 CQ 码，返回纯文本。keep_at=True 时保留 at 码文本。"""
    def rep(m):
        tag = m.group(1)
        if tag == "at":
            return m.group(0) if keep_at else ""
        return ""
    return _CQ.sub(rep, text).strip()
