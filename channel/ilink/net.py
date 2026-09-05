# -*- coding: utf-8 -*-
"""iLink HTTP 网络层：与腾讯官方后端一致的请求头/超时/错误分类。"""
import json, os, random, ssl, time, urllib.request, urllib.error

# ---- 固定常量（逆向自官方源码，勿随意改） ----
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"                       # package.json ilink_appid
CHANNEL_VERSION = "0.1.0"                  # 我方 channel 版本（用于 iLink-App-ClientVersion 编码）
CLIENT_VERSION_INT = (0 << 16) | (1 << 8) | 0   # 0x000100 = 1.0.0
BOT_AGENT = "AgentTerminalChannel/0.1.0 (local weixin ilink)"  # 观测用 bot_agent
LONG_POLL_MS = 35000
API_TIMEOUT_MS = 15000

_CTX = ssl.create_default_context()


def _build_common_headers():
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(CLIENT_VERSION_INT),
    }


def _random_uin():
    return __import__("base64").b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")


def _build_headers(token=None):
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_uin(),
    }
    h.update(_build_common_headers())
    if token:
        h["Authorization"] = "Bearer " + token.strip()
    return h


class IlinkError(Exception):
    def __init__(self, msg, kind="unknown", code=None, resp=None):
        super().__init__(msg)
        self.kind = kind; self.code = code; self.resp = resp


def _classify(err):
    s = f"{err} {getattr(err, 'reason', '')} {getattr(err, 'code', '')}"
    if isinstance(err, urllib.error.HTTPError):
        return f"http_{err.code}"
    low = s.lower()
    if "timed out" in low or "timeout" in low: return "timeout"
    if "name or service not known" in low or "getaddrinfo" in low: return "dns"
    if "connection refused" in low: return "refused"
    if "ssl" in low or "certificate" in low or "tls" in low: return "tls"
    return "unknown"


def _do(url, body=None, token=None, timeout=API_TIMEOUT_MS, method=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_build_headers(token), method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise IlinkError(f"HTTP {e.code}: {raw[:200]}", "http", code=e.code, resp=raw)
    except Exception as e:
        raise IlinkError(f"网络错误: {e}", _classify(e))


def post(base_url, endpoint, body, token=None, timeout=API_TIMEOUT_MS):
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    return _do(url, body=body, token=token, timeout=timeout)


def get(base_url, endpoint, token=None, timeout=API_TIMEOUT_MS):
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    return _do(url, body=None, token=token, timeout=timeout)


def build_base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}
