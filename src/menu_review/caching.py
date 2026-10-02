"""多层缓存与紧急更正/撤回传播。

缓存层示例：应用层缓存、边缘 CDN、游客端离线快照。每一层对同一键的
失效都要记录**实际失效时间**（由该层确认），只有所有层都确认后，
一次紧急更正/撤回才算传播完成。若某层未确认，传播仍保持未完成，
复盘时可见哪一层还在提供旧内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .publishing import CORRECTED, WITHDRAWN, PublishedVersion
from .timeutil import now

CORRECTION = "correction"
WITHDRAWAL = "withdrawal"


class CacheError(ValueError):
    pass


@dataclass(frozen=True)
class CacheEntry:
    version_id: str
    cached_at: str
    invalidated_at: Optional[str] = None
    invalidation_event: Optional[str] = None


@dataclass(frozen=True)
class LayerAck:
    layer: str
    acknowledged_at: str


@dataclass(frozen=True)
class InvalidationEvent:
    event_id: str
    version_id: str
    reason: str
    issued_at: str
    acks: tuple[LayerAck, ...]


class CacheLayer:
    """单层缓存；写入与失效都留痕，失效不可撤销。"""

    def __init__(self, name: str):
        self.name = name
        self._entries: dict[str, CacheEntry] = {}

    def put(self, key: str, version_id: str, *, at: Optional[str] = None) -> None:
        moment = (now().isoformat() if at is None else at)
        self._entries[key] = CacheEntry(version_id, moment)

    def get(self, key: str) -> Optional[CacheEntry]:
        return self._entries.get(key)

    def invalidate(self, key: str, event_id: str, *, at: Optional[str] = None) -> LayerAck:
        moment = (now().isoformat() if at is None else at)
        entry = self._entries.get(key)
        if entry is None:
            # 未持有该键也算完成传播（没有旧内容需要撤下）。
            return LayerAck(self.name, moment)
        if entry.invalidated_at is not None:
            return LayerAck(self.name, entry.invalidated_at)
        self._entries[key] = CacheEntry(
            entry.version_id, entry.cached_at, moment, event_id)
        return LayerAck(self.name, moment)

    def serving_version(self, key: str) -> Optional[str]:
        """当前该层实际对外提供的版本；已失效返回 None。"""
        entry = self._entries.get(key)
        if entry is None or entry.invalidated_at is not None:
            return None
        return entry.version_id


class CacheCoordinator:
    """跨层协调：一次失效广播到全部层，并保留每层实际失效时间。"""

    def __init__(self, layers: list[CacheLayer]):
        if not layers:
            raise CacheError("至少需要一个缓存层")
        self.layers = list(layers)
        self.events: dict[str, InvalidationEvent] = {}
        # version_id -> 持有它的缓存键集合（发布时登记）
        self._version_keys: dict[str, set[str]] = {}

    def fill(self, key: str, version: PublishedVersion, *, at: Optional[str] = None) -> None:
        for layer in self.layers:
            layer.put(key, version.version_id, at=at)
        self._version_keys.setdefault(version.version_id, set()).add(key)

    def invalidate_version(self, version: PublishedVersion, reason: str, *,
                           event_id: Optional[str] = None,
                           at: Optional[str] = None,
                           layer_times: Optional[dict[str, str]] = None,
                           acknowledged_layers: Optional[set[str]] = None
                           ) -> InvalidationEvent:
        """对所有持有该版本的键执行跨层失效。

        ``layer_times`` 给出各层确认的实际时间（模拟异步回执）；
        ``acknowledged_layers`` 为本次已回执的层集合，缺省为全部层。
        未回执的层不会被伪造确认，事件保持未传播，可用
        :meth:`ack_layer` 补登记该层的实际失效时间。
        """
        if reason not in (CORRECTION, WITHDRAWAL):
            raise CacheError(f"未知的失效原因: {reason}")
        if version.status not in (CORRECTED, WITHDRAWN):
            raise CacheError("只有已更正或已撤回的版本才能触发失效传播")
        moment = (now().isoformat() if at is None else at)
        eid = event_id or f"inv-{version.version_id}-{len(self.events)}"
        keys = self._version_keys.get(version.version_id, set())
        layer_times = layer_times or {}
        all_names = {layer.name for layer in self.layers}
        pending = all_names if acknowledged_layers is None else set(acknowledged_layers)
        unknown = pending - all_names
        if unknown:
            raise CacheError(f"未知缓存层: {', '.join(sorted(unknown))}")

        acks: list[LayerAck] = []
        for layer in self.layers:
            if layer.name not in pending:
                continue  # 该层尚未回执，不登记失效时间
            # 每层对其持有的全部键逐个失效，取最晚时间作为该层实际失效时间。
            times = [
                layer.invalidate(k, eid, at=layer_times.get(layer.name, moment)).acknowledged_at
                for k in keys
            ]
            acks.append(LayerAck(layer.name, max(times, default=layer_times.get(layer.name, moment))))
        event = InvalidationEvent(eid, version.version_id, reason, moment, tuple(acks))
        self.events[eid] = event
        return event

    def ack_layer(self, event: InvalidationEvent, layer_name: str, *,
                  at: Optional[str] = None) -> InvalidationEvent:
        """补记某层对失效事件的实际回执时间。"""
        layer = next((l for l in self.layers if l.name == layer_name), None)
        if layer is None:
            raise CacheError(f"未知缓存层: {layer_name}")
        if any(a.layer == layer_name for a in event.acks):
            return event
        moment = (now().isoformat() if at is None else at)
        keys = self._version_keys.get(event.version_id, set())
        times = [
            layer.invalidate(k, event.event_id, at=moment).acknowledged_at
            for k in keys
        ]
        ack = LayerAck(layer_name, max(times, default=moment))
        updated = InvalidationEvent(
            event.event_id, event.version_id, event.reason,
            event.issued_at, tuple(sorted(event.acks + (ack,), key=lambda a: a.layer)))
        self.events[event.event_id] = updated
        return updated

    def is_propagated(self, event: InvalidationEvent) -> bool:
        """所有层都已确认，且每层都不再提供旧版本。"""
        acked_layers = {a.layer for a in event.acks}
        if acked_layers != {layer.name for layer in self.layers}:
            return False
        for key in self._version_keys.get(event.version_id, set()):
            for layer in self.layers:
                if layer.serving_version(key) == event.version_id:
                    return False
        return True

    def effective_invalidated_at(self, event: InvalidationEvent) -> str:
        """整次传播的实际失效时间 = 各层确认时间的最大值。

        在最慢一层确认之前，旧版本仍可能被看到，因此不能用发起时间冒充。
        """
        if not event.acks:
            raise CacheError("失效事件没有任何层确认")
        return max(a.acknowledged_at for a in event.acks)
