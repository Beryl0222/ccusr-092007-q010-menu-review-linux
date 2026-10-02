"""三方签署用的签名工具：HMAC-SHA256，签署内容为规范化 JSON。

私钥只在签署方手中；复盘材料保存签署者、时间与签名，可用登记的公钥值
（此处为对称密钥的校验值）验证。签名覆盖业务字段，任何字段被替换都会
导致校验失败。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping


def canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def sign(role: str, signer: str, at: str, payload: Mapping[str, Any], key: bytes) -> str:
    body = canonical({
        "role": role, "signer": signer, "at": at,
        "payload": payload,
    })
    return "hmac-sha256:" + hmac.new(key, body, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Signature:
    role: str
    signer: str
    at: str
    value: str

    def verify(self, payload: Mapping[str, Any], key: bytes) -> bool:
        expected = sign(self.role, self.signer, self.at, payload, key)
        return hmac.compare_digest(expected, self.value)

    @classmethod
    def create(cls, role: str, signer: str, at: str,
               payload: Mapping[str, Any], key: bytes) -> "Signature":
        return cls(role, signer, at, sign(role, signer, at, payload, key))
