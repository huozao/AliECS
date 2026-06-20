from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def derive_aes_key(encrypt_key: str) -> bytes:
    if not encrypt_key:
        raise ValueError("FEISHU_ENCRYPT_KEY is required to decrypt encrypted payload")
    return hashlib.sha256(encrypt_key.encode("utf-8")).digest()


def decrypt_encrypted_payload(encrypted: str, encrypt_key: str) -> dict[str, Any]:
    key = derive_aes_key(encrypt_key)
    cipher_bytes = base64.b64decode(encrypted)
    if len(cipher_bytes) < AES.block_size:
        raise ValueError("Feishu encrypted payload shorter than AES block size")
    iv = cipher_bytes[: AES.block_size]
    ciphertext = cipher_bytes[AES.block_size :]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
    decoded = json.loads(decrypted.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Decrypted Feishu payload must be a JSON object")
    return decoded


def compute_signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    content = timestamp.encode("utf-8") + nonce.encode("utf-8") + encrypt_key.encode("utf-8") + body
    digest = hashlib.sha256(content).hexdigest()
    return digest


def verify_signature(
    *,
    timestamp: str,
    nonce: str,
    signature: str,
    encrypt_key: str,
    body: bytes,
) -> bool:
    if not (timestamp and nonce and signature and encrypt_key):
        return False
    expected = compute_signature(timestamp, nonce, encrypt_key, body)
    return hmac.compare_digest(expected, signature)
