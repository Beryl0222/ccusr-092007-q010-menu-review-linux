"""游客收藏：保存的是公开版本的快照副本，必须清楚标出是否仍有效。

收藏页不再"冻结"一周前的过敏原说明：每次查看都对照登记处当前版本
与供应时效判定状态——

- ``CURRENT``        保存的 revision 即当前在架版本，且在供应窗口内；
- ``SUPERSEDED``     菜单已有更正版本（原料/批次/换油可能变化）；
- ``WITHDRAWN``      菜单已被紧急撤回；
- ``EXPIRED``        版本仍在架但已过当日供应窗口；
- ``PENDING``        保存的是待核实版本，存在开放追问，无确定结论。

判定只读取公开投影，机密配方在游客与竞争商家侧同样不可见。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .catalog import MenuCatalog, PublicVersion
from .workflow import Certainty


class SavedStatus(str, Enum):
    CURRENT = "current"
    PENDING = "pending"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


@dataclass(frozen=True)
class SavedItem:
    tourist_id: str
    menu_id: str
    saved_revision: int
    snapshot: PublicVersion
    saved_at: str


@dataclass(frozen=True)
class SavedView:
    item: SavedItem
    status: SavedStatus
    current_revision: int | None
    detail: str

    @property
    def still_valid(self) -> bool:
        return self.status is SavedStatus.CURRENT


class SavedCollection:
    def __init__(self, catalog: MenuCatalog) -> None:
        self.catalog = catalog
        self._items: dict[tuple[str, str], SavedItem] = {}

    def save(
        self,
        tourist_id: str,
        version: PublicVersion,
        *,
        at: str,
    ) -> SavedItem:
        item = SavedItem(
            tourist_id=tourist_id,
            menu_id=version.menu_id,
            saved_revision=version.revision,
            snapshot=version,
            saved_at=at,
        )
        self._items[(tourist_id, version.menu_id)] = item
        return item

    def view(self, tourist_id: str, menu_id: str, *, now: datetime) -> SavedView:
        item = self._items[(tourist_id, menu_id)]
        current = self.catalog._versions.get(menu_id)
        status, detail = self._classify(item, current, now)
        return SavedView(
            item=item,
            status=status,
            current_revision=current.revision if current else None,
            detail=detail,
        )

    @staticmethod
    def _classify(
        item: SavedItem,
        current: PublicVersion | None,
        now: datetime,
    ) -> tuple[SavedStatus, str]:
        if current is None:
            history = item.snapshot
            if history.withdrawn_at:
                return SavedStatus.WITHDRAWN, "该菜单已紧急撤回，保存内容不再有效"
            return SavedStatus.WITHDRAWN, "该菜单当前无在架版本，保存内容不再有效"
        if item.saved_revision != current.revision:
            return (
                SavedStatus.SUPERSEDED,
                f"菜单已有更正版本（revision {current.revision}），"
                "保存的过敏原与加工说明可能已变化",
            )
        if current.certainty is Certainty.UNCERTAIN:
            return SavedStatus.PENDING, "该版本尚待核实，存在开放追问，没有确定安全结论"
        if not current.is_current(now):
            return SavedStatus.EXPIRED, "已过当日供应窗口，原料批次可能不同，请重新确认"
        return SavedStatus.CURRENT, "保存内容与当前在架版本一致"
