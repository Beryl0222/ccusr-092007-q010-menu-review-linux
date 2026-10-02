"""时间处理：全部使用带时区的 ISO-8601，比较时一律换算为 UTC。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Union


def parse(value: Union[str, datetime]) -> datetime:
    """解析时间；不带时区的输入按 UTC 处理。"""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def is_fresh(observed_at: Union[str, datetime], valid_seconds: int, *,
             at: Union[str, datetime, None] = None) -> bool:
    """observed_at + valid_seconds 是否仍覆盖 at（默认当前时间）。"""
    reference = now() if at is None else parse(at)
    return parse(observed_at).timestamp() + valid_seconds >= reference.timestamp()
