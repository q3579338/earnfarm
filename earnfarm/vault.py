"""交易所凭据的加密存储。

原版工具把 apiKey/apiSecret/passphrase 以明文 JSON 存进 SQLite，对该文件跑
strings 就能读出全部密钥——卷备份、虚拟机快照、误传文件任何一种都等于账户失守。
这里改成主密码派生密钥 + AES-GCM 加密，落盘的只有密文。

主密码不落盘。进程启动时输入一次，解出的数据密钥只留在内存。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# scrypt 参数：n=2^17 约 128MB 内存、单次派生几百毫秒。
# 对交互式解锁完全可接受，对离线爆破则代价高昂。
_SCRYPT_N = 1 << 17
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_NONCE_LEN = 12
_SALT_LEN = 16

_MAGIC = b"CFV1"


class VaultError(Exception):
    pass


class VaultLocked(VaultError):
    """尚未用主密码解锁。"""


class BadPassword(VaultError):
    """主密码错误，或密文被篡改。"""


def _derive(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


@dataclass(slots=True)
class VaultHeader:
    """存在数据库里的验证头，用来判断主密码对不对，本身不含任何密钥材料。"""

    salt: bytes
    check: bytes

    def to_blob(self) -> bytes:
        return _MAGIC + len(self.salt).to_bytes(1, "big") + self.salt + self.check

    @classmethod
    def from_blob(cls, blob: bytes) -> VaultHeader:
        if not blob.startswith(_MAGIC):
            raise VaultError("凭据库头部格式无法识别")
        salt_len = blob[len(_MAGIC)]
        offset = len(_MAGIC) + 1
        salt = blob[offset:offset + salt_len]
        return cls(salt=salt, check=blob[offset + salt_len:])


class Vault:
    """对称加密的凭据保险箱。

    典型用法::

        vault = Vault()
        header = vault.initialize("主密码")      # 首次：生成盐并返回待存储的头
        blob = vault.seal({"apiKey": "...", "apiSecret": "..."})
        ...
        vault = Vault()
        vault.unlock("主密码", header)          # 之后：用存下的头解锁
        cred = vault.open(blob)
    """

    __slots__ = ("_key",)

    def __init__(self) -> None:
        self._key: bytes | None = None

    # ---- 生命周期 -------------------------------------------------------

    def initialize(self, password: str) -> VaultHeader:
        """首次设置主密码，返回需要持久化的验证头。"""
        if len(password) < 8:
            raise VaultError("主密码至少 8 位")
        salt = secrets.token_bytes(_SALT_LEN)
        key = _derive(password, salt)
        # 验证头本身就是一段用该密钥加密的固定明文，解得开即密码正确
        nonce = secrets.token_bytes(_NONCE_LEN)
        check = nonce + AESGCM(key).encrypt(nonce, _MAGIC, None)
        self._key = key
        return VaultHeader(salt=salt, check=check)

    def unlock(self, password: str, header: VaultHeader) -> None:
        key = _derive(password, header.salt)
        nonce, ct = header.check[:_NONCE_LEN], header.check[_NONCE_LEN:]
        try:
            plain = AESGCM(key).decrypt(nonce, ct, None)
        except Exception as exc:  # cryptography 抛的是 InvalidTag
            raise BadPassword("主密码错误") from exc
        if plain != _MAGIC:
            raise BadPassword("主密码错误")
        self._key = key

    def lock(self) -> None:
        self._key = None

    @property
    def is_unlocked(self) -> bool:
        return self._key is not None

    # ---- 加解密 ---------------------------------------------------------

    def seal(self, payload: dict[str, Any], aad: bytes = b"") -> bytes:
        """加密一份凭据。aad 可传账户ID等上下文，防止密文被移花接木到别的账户。"""
        key = self._require_key()
        nonce = secrets.token_bytes(_NONCE_LEN)
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return nonce + AESGCM(key).encrypt(nonce, raw, aad or None)

    def open(self, blob: bytes, aad: bytes = b"") -> dict[str, Any]:
        key = self._require_key()
        nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
        try:
            raw = AESGCM(key).decrypt(nonce, ct, aad or None)
        except Exception as exc:
            raise BadPassword("凭据解密失败：主密码不对，或数据已被篡改") from exc
        return json.loads(raw)

    def rotate(self, new_password: str, blobs: dict[str, bytes],
               aads: dict[str, bytes] | None = None) -> tuple[VaultHeader, dict[str, bytes]]:
        """换主密码：用旧密钥解开全部密文，再用新密钥重新封装。

        返回新的验证头和重新加密后的密文，调用方需在一个事务里一起写回。
        """
        aads = aads or {}
        plain = {k: self.open(v, aads.get(k, b"")) for k, v in blobs.items()}
        header = self.initialize(new_password)
        return header, {k: self.seal(v, aads.get(k, b"")) for k, v in plain.items()}

    def _require_key(self) -> bytes:
        if self._key is None:
            raise VaultLocked("凭据库未解锁：需要先输入主密码")
        return self._key


def password_from_env() -> str | None:
    """从环境变量读主密码，供无人值守部署使用。

    注意这会让密码出现在进程环境里（/proc/<pid>/environ、docker inspect 可见），
    安全性弱于交互输入，仅在你清楚该权衡时使用。
    """
    return os.environ.get("EARNFARM_PASSWORD") or None
