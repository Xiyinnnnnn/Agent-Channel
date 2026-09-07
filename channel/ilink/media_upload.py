# -*- coding: utf-8 -*-
"""微信 CDN 媒体上传管线（P1 FILE outbound）——官方 src/cdn/{upload,cdn-upload,cdn-url}.ts 移植。

职责（task 书 11/21 节）：
  prepare file metadata → AES 加密 → getUploadUrl → resolve CDN URL → POST ciphertext → sendMessage FILE
本文件不做任何 HTTP JSON 通信细节（复用 net.py），不涉及 adapter 层。
"""
import hashlib
import json
import os
from dataclasses import dataclass
from urllib.parse import quote

from . import net
from .aes_ecb import encrypt, random_key
from .types import UploadMediaType, MessageItemType

# 官方 cdn-upload.ts: UPLOAD_MAX_RETRIES = 3（服务端错误重试；4xx 立即停）
UPLOAD_MAX_RETRIES = 3

# 日志脱敏：严禁打印完整 CDN URL / param / key / token
REDACT = "<redacted>"


class UploadError(Exception):
    """上传链路失败（getUploadUrl / 缺 URL / CDN 上传失败）"""


class SendFileError(Exception):
    """sendMessage FILE 失败"""


class ContextTokenError(Exception):
    """context_token 缺失/过期——由上层识别 ret=-2 转成此错误"""


@dataclass
class UploadedFile:
    filekey: str
    aeskey_hex: str
    encrypt_query_param: str
    raw_size: int
    encrypted_size: int
    file_name: str


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    """upload_param fallback 的 legacy CDN 上传 URL（官方 cdn-url.ts）：
    {cdn}/upload?encrypted_query_param=<quote(param)>&filekey=<quote(filekey)>"""
    return f"{cdn_base_url}/upload?encrypted_query_param={quote(upload_param, safe='')}&filekey={quote(filekey, safe='')}"


def _file_md5(data: bytes) -> str:
    """明文文件 MD5（官方 upload.ts: md5(plaintext)）"""
    return hashlib.md5(data).hexdigest()


def upload_file_to_cdn(*, client, file_path: str, to_user_id: str,
                       cdn_base_url: str = None) -> UploadedFile:
    """P1 完整上传管线（media_type 恒 FILE=3）。client 提供 base_url/token/get_upload_url。

    注意：to_user_id = 接收者微信 userId（非 account_id）。
    返回 UploadedFile；调用方用 send_file_message 组装 FILE item。
    """
    cdn_base_url = (cdn_base_url or net.CDN_BASE_URL).rstrip("/")
    file_name = os.path.basename(file_path)

    # 5. 读取文件 + rawsize/rawfilemd5（明文）
    try:
        with open(file_path, "rb") as f:
            plaintext = f.read()
    except FileNotFoundError:
        raise UploadError(f"file not found: {file_path}") from None
    except OSError as e:
        raise UploadError(f"file read failed: {file_path}: {e}") from None
    rawsize = len(plaintext)
    rawfilemd5 = _file_md5(plaintext)

    # 6-7. AES 加密 + 密文 size；filekey=random16hex（官方 upload.ts L77）
    aeskey_raw = random_key()
    aeskey_hex = aeskey_raw.hex()
    # filesize = 密文长度（AES-128-ECB PKCS7 后）
    ciphertext = encrypt(plaintext, aeskey_raw)
    filesize = len(ciphertext)
    filekey = os.urandom(16).hex()

    # 8. getUploadUrl（FILE: no_need_thumb=true, 无任何 thumb 字段）
    try:
        resp = client.get_upload_url(
            filekey=filekey, media_type=int(UploadMediaType.FILE),
            to_user_id=to_user_id, rawsize=rawsize, rawfilemd5=rawfilemd5,
            filesize=filesize, aeskey=aeskey_hex, no_need_thumb=True)
    except net.IlinkError as e:
        raise UploadError(f"getUploadUrl failed: {e}") from None
    except Exception as e:
        raise UploadError(f"getUploadUrl error: {e}") from None

    # 9/10. 解析 upload URL（upload_full_url 优先 → upload_param fallback → 明确失败）
    upload_full_url = (resp.get("upload_full_url") or "").strip()
    upload_param = resp.get("upload_param") or ""
    if upload_full_url:
        cdn_url = upload_full_url
    elif upload_param:
        cdn_url = _cdn_upload_url(cdn_base_url, upload_param, filekey)
    else:
        raise UploadError("getUploadUrl returned no upload URL (need upload_full_url or upload_param)")

    # 11. CDN POST 密文（octet-stream; 重试3次; 4xx 立即停）
    download_param = _post_ciphertext(cdn_url, ciphertext, token=client.token,
                                      cdn_base_url=cdn_base_url, filekey=filekey)

    return UploadedFile(filekey=filekey, aeskey_hex=aeskey_hex,
                        encrypt_query_param=download_param,
                        raw_size=rawsize, encrypted_size=filesize, file_name=file_name)


def _post_ciphertext(cdn_url: str, ciphertext: bytes, *, token, cdn_base_url, filekey) -> str:
    """POST 密文到 CDN，返回 x-encrypted-param。4xx 立即抛；5xx/网络错误重试最多3次。"""
    last_err = None
    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        try:
            status, headers, _body = net.post_bytes(
                cdn_base_url, cdn_url, ciphertext, token=token, timeout=net.API_TIMEOUT_MS)
            if 400 <= status < 500:
                msg = headers.get("x-error-message") or ""
                raise UploadError(f"CDN upload client error {status}: {msg or '(no x-error-message)'}")
            if status != 200:
                raise UploadError(f"CDN upload server error: status {status} "
                                  f"{(headers.get('x-error-message') or '')}")
            param = headers.get("x-encrypted-param")
            if not param:
                raise UploadError("CDN upload response missing x-encrypted-param header")
            return param
        except UploadError:
            raise  # 4xx/无参 → 明确失败不重试
        except Exception as e:
            last_err = e
            if attempt < UPLOAD_MAX_RETRIES:
                continue
    raise UploadError(f"CDN upload failed after {UPLOAD_MAX_RETRIES} attempts: {last_err}")


def build_file_item(uploaded: UploadedFile) -> dict:
    """构造 sendMessage 的 file_item（官方 send.ts sendFileMessageWeixin）。

    media.aes_key = base64(UTF8(aeskey_hex))——是 hex 串的 UTF-8 bytes 再 base64，不是 base64(raw16)！
    encrypt_type = 1（打包 media 信息）
    len = 明文 size（不是密文 size）
    """
    import base64
    aes_key_b64 = base64.b64encode(uploaded.aeskey_hex.encode("utf-8")).decode("ascii")
    return {
        "media": {
            "encrypt_query_param": uploaded.encrypt_query_param,
            "aes_key": aes_key_b64,
            "encrypt_type": 1,
        },
        "file_name": uploaded.file_name,
        "len": str(uploaded.raw_size),
    }


def send_file(client, *, to_user_id: str, file_path: str, context_token: str = None,
              cdn_base_url: str = None, run_id: str = None):
    """P1 顶层便捷入口：upload + sendMessage(FILE)。返回消息响应。
    context_token 缺失时允许发送（官方 send.ts 仅 warn）；过期(ret=-2)由调用方捕获转 ContextTokenError。"""
    uploaded = upload_file_to_cdn(client=client, file_path=file_path,
                                  to_user_id=to_user_id, cdn_base_url=cdn_base_url)
    file_item = build_file_item(uploaded)
    resp = client.send_file_message(to_user_id=to_user_id, file_item=file_item,
                                    context_token=context_token, run_id=run_id)
    ret = resp.get("ret")
    if ret not in (None, 0):
        # 官方 ret=-2 = context_token expired/invalid（task 书 19 节）
        if ret in (-2, "prepare failed") or "context" in str(resp.get("errmsg", "")).lower():
            raise ContextTokenError(f"context_token expired or invalid (ret={ret})")
        raise SendFileError(f"sendMessage FILE ret={ret} errmsg={resp.get('errmsg')}")
    return resp
