"""分层缓存与失效传播。

三层（按失效传播顺序）：

1. ``edge_cdn``     —— 边缘节点缓存；
2. ``app_cache``    —— 平台应用层缓存；
3. ``saved_store``  —— 游客已保存内容对应的离线副本。

更正与撤回必须传播到每一层，并记录每层的**实际**失效时间
（``effective_at`` 可能晚于发起时间 ``requested_at``，例如边缘节点
回包延迟、游客设备下次同步才生效）——复盘时以实际时间为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


class CacheLayer(Protocol):
    name: str

    def put(self, menu_id: str, revision: int, at: str) -> None: ...

    def get(self, menu_id: str) -> int | None: ...

    def invalidate(self, menu_id: str, at: str) -> str: ...


@dataclass
class DictLayer:
    """默认同步层：失效立即生效，实际时间等于发起时间。"""

    name: str
    _store: dict[str, int] = field(default_factory=dict)

    def put(self, menu_id: str, revision: int, at: str) -> None:
        self._store[menu_id] = revision

    def get(self, menu_id: str) -> int | None:
        return self._store.get(menu_id)

    def invalidate(self, menu_id: str, at: str) -> str:
        self._store.pop(menu_id, None)
        return at


@dataclass(frozen=True)
class InvalidationRecord:
    menu_id: str
    layer: str
    requested_at: str
    effective_at: str
    reason: str
    revision_before: int | None


@dataclass
class CacheBus:
    layers: list[CacheLayer]
    # menu_id -> 逐层失效记录（按时间追加，更正与撤回都留痕）
    invalidations: dict[str, list[InvalidationRecord]] = field(default_factory=dict)
    # 可选的逐层时钟：name -> (requested_at) -> effective_at
    clocks: dict[str, Callable[[str], str]] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "CacheBus":
        return cls(
            layers=[
                DictLayer("edge_cdn"),
                DictLayer("app_cache"),
                DictLayer("saved_store"),
            ]
        )

    def publish(self, menu_id: str, revision: int, *, at: str) -> None:
        for layer in self.layers:
            layer.put(menu_id, revision, at)

    def layer(self, name: str) -> CacheLayer:
        for candidate in self.layers:
            if candidate.name == name:
                return candidate
        raise KeyError(name)

    def invalidate(self, menu_id: str, *, at: str, reason: str) -> str:
        """逐层失效；返回失效事件标识。每层实际失效时间被记录。"""

        records: list[InvalidationRecord] = []
        for layer in self.layers:
            revision_before = layer.get(menu_id)
            # 层先真正失效；延迟时钟只决定"实际失效时间"如何记账，
            # 例如游客设备到下一次同步（更晚时刻）才丢掉离线副本。
            immediate = layer.invalidate(menu_id, at)
            clock = self.clocks.get(layer.name)
            effective_at = clock(at) if clock is not None else immediate
            records.append(
                InvalidationRecord(
                    menu_id=menu_id,
                    layer=layer.name,
                    requested_at=at,
                    effective_at=effective_at,
                    reason=reason,
                    revision_before=revision_before,
                )
            )
        self.invalidations.setdefault(menu_id, []).extend(records)
        return f"inv:{menu_id}:{len(self.invalidations[menu_id])}"

    def invalidation_log(self, menu_id: str) -> tuple[InvalidationRecord, ...]:
        return tuple(self.invalidations.get(menu_id, ()))

    def all_layers_cleared(self, menu_id: str) -> bool:
        return all(layer.get(menu_id) is None for layer in self.layers)
