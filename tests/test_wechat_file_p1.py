# -*- coding: utf-8 -*-
"""Agent-Channel 微信 FILE outbound P1 单元测试（离线，全 mock 网络）。
运行: python3 -m pytest tests/test_wechat_file_p1.py -q  或  python3 tests/test_wechat_file_p1.py
覆盖 task 书 32-41 节测试一~测试十。"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "channel"))

from ilink import aes_ecb, mime, media_upload, context_store
from ilink.types import UploadMediaType, MessageItemType
from ilink.client import Client


# ---------- 测试一：AES ----------
class TestAes(unittest.TestCase):
    def test_padded_size(self):
        self.assertEqual([aes_ecb.padded_size(n) for n in (0, 1, 15, 16, 17)],
                         [16, 16, 16, 32, 32])

    def test_ciphertext_len_multiple_of_16(self):
        key = bytes(range(16))
        for n in (0, 1, 15, 16, 17, 1000):
            out = aes_ecb.encrypt(b"A" * n, key)
            self.assertEqual(len(out), aes_ecb.padded_size(n))
            self.assertEqual(len(out) % 16, 0)

    def test_fixed_vector(self):
        # key=000102..0f 加密 16 个 'A'：首块固定已知
        key = bytes(range(16))
        out = aes_ecb.encrypt(b"A" * 16, key)
        self.assertEqual(out[:16].hex(), "dd4b1a0b47daa7067d0b59d95d58a6ae")
        # 空输入 → 一个整 PKCS7 padding 块
        self.assertEqual(len(aes_ecb.encrypt(b"", key)), 16)

    def test_key_validation(self):
        with self.assertRaises(ValueError):
            aes_ecb.encrypt(b"x", b"short")
        self.assertEqual(len(aes_ecb.random_key()), 16)


# ---------- 测试二：MD5 / size（明文） ----------
class TestMeta(unittest.TestCase):
    def test_rawsize_md5(self):
        data = b"hello world" * 3  # 33B
        self.assertEqual(len(data), 33)
        self.assertEqual(hashlib.md5(data).hexdigest(),
                         media_upload._file_md5(data))


# ---------- 测试三：AES key 编码 ----------
class TestKeyEncoding(unittest.TestCase):
    def test_hex_vs_b64_of_utf8_hex(self):
        key = bytes(range(16))
        hexs = key.hex()
        self.assertEqual(len(hexs), 32)
        # 官方 send.ts: Buffer.from(aeskey/*hex string*/).toString("base64")
        b64_of_utf8_hex = base64.b64encode(hexs.encode("utf-8")).decode("ascii")
        b64_of_raw = base64.b64encode(key).decode("ascii")
        # 绝不能是 base64(raw16)
        self.assertNotEqual(b64_of_utf8_hex, b64_of_raw)
        self.assertEqual(len(b64_of_utf8_hex) % 4, 0)


# ---------- 测试四/五/六/七/八/九：上传管线 + 双枚举 + URL ----------
class _FakeNet:
    """mock 网络层：getUploadUrl 响应可配置；CDN POST 记录 URL/body"""
    def __init__(self, upload_resp):
        self.upload_resp = upload_resp
        self.posted_url = None
        self.posted_body = None
        self.upload_payload = None

    def post(self, base, endpoint, body, **kw):
        self.upload_payload = body
        return json.dumps(self.upload_resp)

    def post_bytes(self, base, url, data, **kw):
        self.posted_url = url
        self.posted_body = data
        return 200, {"x-encrypted-param": "ENC_PARAM_XYZ"}, b""


class TestUploadPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "测试文件.txt")
        with open(self.path, "wb") as f:
            f.write(b"hello world" * 3)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, resp):
        c = Client({"account_id": "aid", "base_url": "https://b", "token": "tok"})
        return c

    def test_enum_split(self):
        # 测试五：双枚举强制分离
        self.assertEqual(int(UploadMediaType.FILE), 3)
        self.assertEqual(int(MessageItemType.FILE), 4)
        self.assertNotEqual(int(UploadMediaType.FILE), int(MessageItemType.FILE))
        # sendMessage 用 FILE=4（在 file_item type 外层）
        self.assertEqual(MessageItemType.FILE, 4)

    def test_full_url_preferred(self):
        # 测试六：upload_full_url 优先
        fake = _FakeNet({"upload_full_url": "https://example/up", "upload_param": "legacy"})
        c = Client({"account_id": "a", "base_url": "https://b", "token": "t"})
        with mock.patch.object(media_upload.net, "post", fake.post), \
             mock.patch.object(media_upload.net, "post_bytes", fake.post_bytes):
            up = media_upload.upload_file_to_cdn(client=c, file_path=self.path,
                                                 to_user_id="u", cdn_base_url="https://cdn")
        # getUploadUrl payload 断言（测试四）
        pl = fake.upload_payload
        self.assertEqual(pl["media_type"], 3)
        self.assertIs(pl["no_need_thumb"], True)
        self.assertEqual(pl["rawsize"], 33)
        self.assertEqual(pl["rawfilemd5"], hashlib.md5(b"hello world" * 3).hexdigest())
        self.assertEqual(pl["filesize"], 48)  # 密文长 ceil(34/16)*16
        self.assertEqual(pl["aeskey"], up.aeskey_hex)
        self.assertEqual(len(pl["aeskey"]), 32)
        self.assertEqual(fake.posted_url, "https://example/up")  # 优先 full_url
        self.assertEqual(up.encrypt_query_param, "ENC_PARAM_XYZ")

    def test_upload_param_fallback(self):
        # 测试七：仅 upload_param → legacy URL（正确 URL encode）
        fake = _FakeNet({"upload_param": "a b&c=d/e"})
        c = Client({"account_id": "a", "base_url": "https://b", "token": "t"})
        with mock.patch.object(media_upload.net, "post", fake.post), \
             mock.patch.object(media_upload.net, "post_bytes", fake.post_bytes):
            up = media_upload.upload_file_to_cdn(client=c, file_path=self.path,
                                                 to_user_id="u", cdn_base_url="https://cdn")
        fk = up.filekey
        exp = f"https://cdn/upload?encrypted_query_param={__import__('urllib.parse').parse.quote('a b&c=d/e', safe='')}&filekey={__import__('urllib.parse').parse.quote(fk, safe='')}"
        # 由于 quote 不带 safe，空格→%20, &→%26, /→%2F
        self.assertEqual(fake.posted_url, exp)

    def test_no_url_raises(self):
        # 测试八：两 URL 都缺失 → UploadError
        fake = _FakeNet({})
        c = Client({"account_id": "a", "base_url": "https://b", "token": "t"})
        with mock.patch.object(media_upload.net, "post", fake.post), \
             mock.patch.object(media_upload.net, "post_bytes", fake.post_bytes):
            with self.assertRaises(media_upload.UploadError):
                media_upload.upload_file_to_cdn(client=c, file_path=self.path,
                                                to_user_id="u", cdn_base_url="https://cdn")

    def test_file_message_payload(self):
        # 测试九：sendMessage FILE payload 组装（经由 send_file 全链路）
        fake = _FakeNet({"upload_full_url": "https://example/up"})
        c = Client({"account_id": "a", "base_url": "https://b", "token": "t"})
        sent = {}
        orig_sfm = c.send_file_message
        def spy(**kw):
            sent.update(kw)
            return {"ret": 0}
        c.send_file_message = spy
        with mock.patch.object(media_upload.net, "post", fake.post), \
             mock.patch.object(media_upload.net, "post_bytes", fake.post_bytes):
            media_upload.send_file(c, to_user_id="u", file_path=self.path,
                                   context_token="ct", run_id="r1")
        fi = sent["file_item"]
        self.assertEqual(fi["file_name"], "测试文件.txt")
        self.assertEqual(fi["len"], "33")   # 明文长
        self.assertEqual(fi["media"]["encrypt_query_param"], "ENC_PARAM_XYZ")
        # aes_key = base64(utf8(hex key))；与 getUploadUrl 里发的 aeskey 一致
        self.assertEqual(fi["media"]["aes_key"],
                         base64.b64encode(fake.upload_payload["aeskey"].encode("utf-8")).decode("ascii"))
        self.assertEqual(fi["media"]["encrypt_type"], 1)
        self.assertEqual(sent["context_token"], "ct")
        self.assertEqual(sent["to_user_id"], "u")
        # CDN POST 是密文(48B) 且 octet-stream
        self.assertEqual(len(fake.posted_body), 48)


# ---------- 测试十：context_token ----------
class TestContextStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_update_isolate_restore(self):
        # 保存
        context_store.save("private:a", "tokA", self.dir)
        self.assertEqual(context_store.get("private:a", self.dir), "tokA")
        # 第二条更新
        context_store.save("private:a", "tokA2", self.dir)
        self.assertEqual(context_store.get("private:a", self.dir), "tokA2")
        # 隔离
        context_store.save("private:b", "tokB", self.dir)
        self.assertEqual(context_store.get("private:b", self.dir), "tokB")
        self.assertEqual(context_store.get("private:a", self.dir), "tokA2")
        # 重启恢复（新 load）
        data = context_store.load(self.dir)
        self.assertEqual(data["private:a"]["context_token"], "tokA2")
        self.assertEqual(data["private:b"]["context_token"], "tokB")
        # 无值
        self.assertIsNone(context_store.get("private:zz", self.dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
