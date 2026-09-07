# -*- coding: utf-8 -*-
"""AES-128-ECB + PKCS7 加密（官方 src/cdn/aes-ecb.ts 的 Python 移植）。

官方: createCipheriv("aes-128-ecb", key, null)——Node 默认 PKCS7 padding。
注意 Python cryptography 的 ECB *不*自动补 padding，必须手动 PKCS7 后再入 ECB，
否则 16/17B 与空输入行为与官方不一致（已实测，见 P1-12 单测）。
- key = 16 bytes（32 hex）
- IV = None / mode = ECB / padding = PKCS7
"""
import os

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAVE_CRYPTOGRAPHY = True
except Exception:  # pragma: no cover
    _HAVE_CRYPTOGRAPHY = False

_BLOCK = 16


def padded_size(plaintext_size: int) -> int:
    """AES-128-ECB(PKCS7) 密文长度。要求 0->16,1->16,15->16,16->32,17->32。
    公式 = ceil((n + 1) / 16) * 16（至少补 1 字节，整除也补一整块）。"""
    if plaintext_size < 0:
        raise ValueError(f"size 不能为负: {plaintext_size}")
    return ((plaintext_size + 1 + 15) // 16) * 16


def _pkcs7_pad(data: bytes) -> bytes:
    pad_len = _BLOCK - (len(data) % _BLOCK)
    return data + bytes([pad_len]) * pad_len


def encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB(PKCS7) 加密；key 必须 16 bytes。密文长度恒为 padded_size(len(data))。"""
    if not _HAVE_CRYPTOGRAPHY:
        raise RuntimeError("缺少 cryptography 库（pip install cryptography）")
    if len(key) != 16:
        raise ValueError(f"AES-128 key 必须 16 bytes，实际 {len(key)}")
    padded = _pkcs7_pad(data)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def random_key() -> bytes:
    """16 字节 AES 随机 key（os.urandom，禁时间戳/固定值）。"""
    return os.urandom(16)


def _verify():
    """固定 vector 自检：key=000102..0f（官方 AES 固定测试键）。"""
    key = bytes(range(16))
    c0 = encrypt(b"", key)
    assert len(c0) == 16, "空输入必须产生一个 PKCS7 padding 块"
    c1 = encrypt(b"A" * 16, key)
    assert len(c1) == 32
    assert c1[:16].hex() == "dd4b1a0b47daa7067d0b59d95d58a6ae"
    return c0.hex()


if __name__ == "__main__":
    print("padded_size:", {n: padded_size(n) for n in (0, 1, 15, 16, 17)})
    print("empty-ct:", _verify())
