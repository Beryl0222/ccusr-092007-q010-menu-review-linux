"""读取项目已确认的最小数据合同。

schema_version=1 的样例在扩展字段（菜品、批次、经营事实等）后仍可由此处
读取；未知键被忽略，领域字段由 :mod:`menu_review.claims` 解析。
新增状态必须在 README 中说明迁移方式，旧代码读取新字段时只能忽略，
不得报错丢弃整条记录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def load_record(path: Path) -> DomainRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(DomainRecord)}
    return DomainRecord(**{k: v for k, v in payload.items() if k in known})
