"""分层缓存测试：更正/撤回逐层传播，记录每层实际失效时间。"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from menu_review import CacheBus


class CacheBusTest(unittest.TestCase):
    def test_publish_fills_all_layers(self):
        bus = CacheBus.default()
        bus.publish("m1", 1, at="2026-09-20T12:00:00+08:00")
        self.assertEqual(bus.layer("edge_cdn").get("m1"), 1)
        self.assertEqual(bus.layer("app_cache").get("m1"), 1)
        self.assertEqual(bus.layer("saved_store").get("m1"), 1)

    def test_invalidation_propagates_to_every_layer_with_effective_time(self):
        bus = CacheBus.default()
        bus.publish("m1", 1, at="2026-09-20T12:00:00+08:00")
        at = "2026-09-20T18:00:00+08:00"
        event_id = bus.invalidate("m1", at=at, reason="withdraw: test")
        self.assertTrue(bus.all_layers_cleared("m1"))
        records = bus.invalidation_log("m1")
        self.assertEqual(len(records), 3)
        self.assertEqual([r.layer for r in records],
                         ["edge_cdn", "app_cache", "saved_store"])
        # 同步层：实际失效时间等于发起时间。
        self.assertTrue(all(r.effective_at == at for r in records))
        self.assertTrue(all(r.requested_at == at for r in records))
        self.assertTrue(all(r.revision_before == 1 for r in records))
        self.assertTrue(event_id)

    def test_delayed_layer_records_later_effective_time(self):
        # 游客设备离线：saved_store 层到下次同步（更晚）才真正丢弃副本。
        bus = CacheBus.default()
        synced = "2026-09-21T08:00:00+08:00"
        bus.clocks["saved_store"] = lambda _at: synced
        bus.publish("m1", 1, at="2026-09-20T12:00:00+08:00")
        requested = "2026-09-20T18:00:00+08:00"
        bus.invalidate("m1", at=requested, reason="correction")
        by_layer = {r.layer: r for r in bus.invalidation_log("m1")}
        self.assertEqual(by_layer["edge_cdn"].effective_at, requested)
        self.assertEqual(by_layer["saved_store"].effective_at, synced)
        # 实际记账更晚，但失效动作已经落实。
        self.assertIsNone(bus.layer("saved_store").get("m1"))


if __name__ == "__main__":
    unittest.main()
