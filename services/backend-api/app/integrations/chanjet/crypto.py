from __future__ import annotations

import base64

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def decrypt_encrypt_msg(encrypt_msg: str, aes_key: str) -> str:
    key_bytes = aes_key.encode("utf-8")
    if len(key_bytes) != 16:
        raise ValueError("CHANJET_WEBHOOK_AES_KEY must be exactly 16 bytes")

    encrypted = base64.b64decode(encrypt_msg)
    cipher = AES.new(key_bytes, AES.MODE_ECB)
    decrypted = unpad(cipher.decrypt(encrypted), AES.block_size)
    return decrypted.decode("utf-8")
