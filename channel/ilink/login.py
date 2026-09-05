# -*- coding: utf-8 -*-
"""微信 iLink 二维码登录状态机（逆向自官方 openclaw-weixin/src/auth/login-qr.ts）。
流程：fetch QR(bot_type=3) → 轮询 get_qrcode_status（长轮询 35s/次，最多 8 分钟）
状态：wait / scaned / confirmed / expired / need_verifycode / verify_code_blocked /
      scaned_but_redirect(换 baseUrl 续轮询) / binded_redirect(已绑过=成功)。
登录成功后返回 (account_id=ilink_bot_id, bot_token, baseurl, ilink_user_id)。
"""
import json, time, urllib.parse, uuid

from . import net

DEFAULT_BOT_TYPE = "3"
ACTIVE_TTL_S = 5 * 60
WAIT_STEP_S = 1.0
MAX_QR_REFRESH = 3

def _poll_once(base, qrcode, verify_code=None):
    ep = "ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(qrcode, safe="")
    if verify_code:
        ep += "&verify_code=" + urllib.parse.quote(verify_code, safe="")
    raw = net.get(base, ep, timeout=net.LONG_POLL_MS + 5000)
    return json.loads(raw)

def _fetch_qr(base, bot_type, local_tokens):
    raw = net.post(base, "ilink/bot/get_bot_qrcode?bot_type=" + urllib.parse.quote(bot_type, safe=""),
                   {"local_token_list": local_tokens})
    return json.loads(raw)

def _is_expired_started(started):  # 由调用方控制 total deadline，这里保留语义位
    return time.time() - started > ACTIVE_TTL_S

def login_flow(accounts_module=None, api_base_url=None, bot_type=DEFAULT_BOT_TYPE,
               total_timeout_s=480, on_qr=None, on_status=None, read_verify=None):
    """执行完整扫码登录。
    on_qr(url): 二维码 url 回调（显示终端二维码/备用链接）。
    on_status(status, note): 状态变更回调（用于 TUI 展示）。
    read_verify(prompt)->str: 需配对码时回调读输入；缺省直接抛错提示。
    返回 dict：connected / alreadyConnected / account_id / token / base_url / user_id / message
    """
    base = (api_base_url or net.DEFAULT_BASE_URL).rstrip("/")
    local_tokens = []
    if accounts_module is not None:
        for aid in accounts_module.list_account_ids():
            d = accounts_module.load_account(aid)
            if d and d.get("token"):
                local_tokens.append(d["token"].strip())
        local_tokens = local_tokens[-10:]

    try:
        qr = _fetch_qr(base, bot_type, local_tokens)
    except Exception as e:
        return {"connected": False, "message": f"获取二维码失败: {e}"}
    qrcode = qr.get("qrcode") or ""
    qr_url = qr.get("qrcode_img_content") or ""
    if not qrcode:
        return {"connected": False, "message": f"服务端未返回 qrcode: {qr}"}
    if on_qr:
        on_qr(qr_url)

    deadline = time.time() + total_timeout_s
    current_base = base
    pending_verify = None
    scanned_printed = False
    qr_refresh = 0
    start = time.time()

    def refresh_qr():
        nonlocal qr, qr_url, start, scanned_printed
        r = _fetch_qr(current_base, bot_type, local_tokens)
        qrcode_new = r.get("qrcode") or ""
        if not qrcode_new:
            return False
        qr, qr_url = qrcode_new, r.get("qrcode_img_content") or qr_url
        start = time.time(); scanned_printed = False
        if on_qr: on_qr(qr_url)
        return True

    while time.time() < deadline:
        if _is_expired_started(start):
            qr_refresh += 1
            if qr_refresh > MAX_QR_REFRESH:
                return {"connected": False, "message": "二维码多次失效，请稍后再试。"}
            if on_status: on_status("refreshing", "二维码已过期，正在刷新…")
            if not refresh_qr():
                return {"connected": False, "message": "刷新二维码失败。"}
            continue
        try:
            st = _poll_once(current_base, qr, pending_verify)
        except Exception as e:
            # 网络抖动按 wait 继续
            if on_status: on_status("wait", f"网络重试: {e}")
            time.sleep(WAIT_STEP_S)
            continue
        status = st.get("status") or "wait"
        if on_status: on_status(status, st.get("errmsg") or "")
        if status == "wait":
            if on_status: on_status("wait", "")
            time.sleep(WAIT_STEP_S)
        elif status == "scaned":
            pending_verify = None
            if not scanned_printed:
                if on_status: on_status("scaned", "已扫码，请在手机上确认")
                scanned_printed = True
        elif status == "need_verifycode":
            prompt = "❌ 数字不匹配，请重新输入: " if pending_verify else "输入手机微信显示的数字，以继续连接: "
            if read_verify is None:
                return {"connected": False, "message": "需要配对码但无输入通道。"}
            pending_verify = (read_verify(prompt) or "").strip()
        elif status == "expired":
            qr_refresh += 1
            if qr_refresh > MAX_QR_REFRESH:
                return {"connected": False, "message": "二维码多次失效，流程已停止。"}
            if on_status: on_status("refreshing", "二维码已过期，正在刷新…")
            if not refresh_qr():
                return {"connected": False, "message": "刷新二维码失败。"}
        elif status == "verify_code_blocked":
            pending_verify = None
            qr_refresh += 1
            if qr_refresh > MAX_QR_REFRESH:
                return {"connected": False, "message": "多次输入错误，流程已停止。"}
            if on_status: on_status("refreshing", "多次输入错误，正在刷新二维码…")
            if not refresh_qr():
                return {"connected": False, "message": "刷新二维码失败。"}
        elif status == "binded_redirect":
            return {"connected": False, "alreadyConnected": True,
                    "message": "该微信号已连接过本机 Channel，凭证仍有效，无需重复连接。"}
        elif status == "scaned_but_redirect":
            host = st.get("redirect_host") or ""
            if host:
                current_base = "https://" + host
                if on_status: on_status("redirect", f"跳转轮询节点: {host}")
        elif status == "confirmed":
            bot_id = st.get("ilink_bot_id") or ""
            if not bot_id:
                return {"connected": False, "message": "登录成功但未返回 ilink_bot_id。"}
            return {"connected": True, "alreadyConnected": False,
                    "account_id": bot_id,
                    "token": st.get("bot_token") or "",
                    "base_url": (st.get("baseurl") or "").strip() or net.DEFAULT_BASE_URL,
                    "user_id": st.get("ilink_user_id") or "",
                    "message": "微信连接成功！凭证已保存，重启免重扫。"}
    return {"connected": False, "message": "登录超时，请重试。"}
