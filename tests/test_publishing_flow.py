import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from menu_review import (ACTIVE, CORRECTED, CacheCoordinator, CacheLayer,
                         EXPIRED, GuestService, PublishError, PublishRegistry,
                         REMOVED, SUPERSEDED, VALID, WITHDRAWN, load_claim)
from menu_review.caching import CORRECTION, WITHDRAWAL
from menu_review.audit import build_bundle
from menu_review.claims import MenuClaim

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import KEYS, M_KEY, N_KEY, T_KEY, build_ready_package

FIXTURE = Path(__file__).parents[1] / "fixtures" / "menu_claim.json"
MORNING = "2026-09-20T10:00:00+08:00"
NEXT_DAY = "2026-09-21T10:30:00+08:00"
ONE_WEEK_LATER = "2026-09-27T12:00:00+08:00"


def revised_claim(revision, recipe_version, *, oil_lot=None):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["revision"] = revision
    for dish in raw["dishes"]:
        if dish["dish_id"] == "d-100":
            dish["recipe_version"] = recipe_version
    if oil_lot is not None:
        raw["facts"].append({
            "fact_id": f"f-oil-{revision}",
            "kind": "oil_change",
            "at": f"2026-09-{20 + revision - 1:02d}T08:00:00+08:00",
            "equipment_id": "fryer-01",
            "detail": f"更换新一批大豆油（批号 {oil_lot}）",
        })
    return MenuClaim.from_raw(raw)


class PublishingTest(unittest.TestCase):
    def setUp(self):
        self.claim = load_claim(FIXTURE)
        self.registry = PublishRegistry()

    def test_publish_creates_single_public_version_without_recipe_details(self):
        package = build_ready_package(self.claim, "d-100", b"photo-a",
                                      at=MORNING, batch_ids=("b-20260920-squid",))
        version = self.registry.publish(package, at=MORNING)
        self.assertEqual(version.status, ACTIVE)
        payload = asdict(version.payload)
        # 公开载荷只含菜名/故事/过敏原代码/风险提示/时效
        self.assertEqual(set(payload),
                         {"name", "cultural_story", "allergens", "advisories",
                          "valid_until"})
        public_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("r-squid-pepper-salt", public_text)  # 配方标识不公开
        self.assertNotIn("lot-squid-0920", public_text)       # 原料批次不公开
        self.assertNotIn("lot-soyoil", public_text)
        self.assertIn("peanut", payload["allergens"])
        # 对外只有配方指纹，不暴露内部 id
        self.assertTrue(version.recipe_fingerprint.startswith("sha256:"))

    def test_repeated_photos_resolve_to_one_confirmed_version(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-1", at=MORNING)
        v1 = self.registry.publish(p1, at=MORNING)
        p2 = build_ready_package(self.claim, "d-100", b"photo-2", at=MORNING)
        v2 = self.registry.publish(p2, at=MORNING)
        self.assertIs(v1, v2)
        # 两张不同指纹的照片都指向同一个公开版本
        self.assertEqual(self.registry.resolve("sha256:" + __import__("hashlib")
                         .sha256(b"photo-1").hexdigest()).version_id, v1.version_id)
        self.assertEqual(self.registry.resolve("sha256:" + __import__("hashlib")
                         .sha256(b"photo-2").hexdigest()).version_id, v1.version_id)
        self.assertEqual(len(self.registry.resolutions_for(v1.version_id)), 2)

    def test_binding_change_cannot_silently_overwrite(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-1", at=MORNING)
        v1 = self.registry.publish(p1, at=MORNING)
        claim2 = revised_claim(2, 4)
        p2 = build_ready_package(claim2, "d-100", b"photo-new", at=MORNING)
        with self.assertRaises(PublishError):
            self.registry.publish(p2, at=MORNING)

    def test_urgent_correction_replaces_version(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-1", at=MORNING)
        v1 = self.registry.publish(p1, at=MORNING)
        claim2 = revised_claim(2, 4, oil_lot="lot-soyoil-0921")
        p2 = build_ready_package(claim2, "d-100", b"photo-2", at=NEXT_DAY)
        v2 = self.registry.publish(p2, at=NEXT_DAY, replaces=v1)
        old = self.registry.get(v1.version_id)
        self.assertEqual(old.status, CORRECTED)
        self.assertEqual(old.superseded_by, v2.version_id)
        self.assertIs(self.registry.canonical(
            self.claim.menu_id, "d-100", "en"), self.registry.get(v2.version_id))
        self.assertFalse(old.is_valid_at(NEXT_DAY))


class CachePropagationTest(unittest.TestCase):
    def setUp(self):
        self.claim = load_claim(FIXTURE)
        self.registry = PublishRegistry()
        self.cache = CacheCoordinator([
            CacheLayer("app"), CacheLayer("edge"), CacheLayer("device")])
        self.key = f"{self.claim.menu_id}:d-100:en"

    def _publish_and_correct(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-1", at=MORNING)
        v1 = self.registry.publish(p1, at=MORNING)
        self.cache.fill(self.key, v1, at=MORNING)
        claim2 = revised_claim(2, 4, oil_lot="lot-soyoil-0921")
        p2 = build_ready_package(claim2, "d-100", b"photo-2", at=NEXT_DAY)
        v2 = self.registry.publish(p2, at=NEXT_DAY, replaces=v1)
        old = self.registry.get(v1.version_id)
        return v1, v2, old

    def test_correction_propagates_with_actual_layer_times(self):
        v1, v2, old = self._publish_and_correct()
        # app 与 edge 立即回执，游客端 device 延迟到次日 12:00
        event = self.cache.invalidate_version(
            old, CORRECTION, at=NEXT_DAY,
            layer_times={"app": "2026-09-21T10:31:00+08:00",
                         "edge": "2026-09-21T10:35:00+08:00"},
            acknowledged_layers={"app", "edge"})
        self.assertFalse(self.cache.is_propagated(event))
        # device 仍在提供旧版本
        self.assertEqual(self.cache.layers[2].serving_version(self.key),
                         v1.version_id)
        event = self.cache.ack_layer(event, "device",
                                     at="2026-09-21T12:00:00+08:00")
        self.assertTrue(self.cache.is_propagated(event))
        # 实际失效时间取最慢一层，不能用发起时间冒充
        self.assertEqual(self.cache.effective_invalidated_at(event),
                         "2026-09-21T12:00:00+08:00")
        self.assertIsNone(self.cache.layers[2].serving_version(self.key))

    def test_withdrawal_invalidates_all_layers(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-w", at=MORNING)
        v1 = self.registry.publish(p1, at=MORNING)
        self.cache.fill(self.key, v1, at=MORNING)
        withdrawn = self.registry.withdraw(v1, at=NEXT_DAY)
        event = self.cache.invalidate_version(withdrawn, WITHDRAWAL, at=NEXT_DAY)
        self.assertTrue(self.cache.is_propagated(event))
        for layer in self.cache.layers:
            self.assertIsNone(layer.serving_version(self.key))

    def test_only_corrected_or_withdrawn_can_trigger_invalidation(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-x", at=MORNING)
        v1 = self.registry.publish(p1, at=MORNING)
        self.cache.fill(self.key, v1, at=MORNING)
        with self.assertRaises(ValueError):
            self.cache.invalidate_version(v1, CORRECTION, at=NEXT_DAY)


class GuestExperienceTest(unittest.TestCase):
    def setUp(self):
        self.claim = load_claim(FIXTURE)
        self.registry = PublishRegistry()
        self.guest = GuestService(self.registry)

    def test_saved_item_valid_then_expired_then_superseded(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-save",
                                 at=MORNING, valid_seconds=86400 * 7)
        v1 = self.registry.publish(p1, at=MORNING)
        saved = self.guest.save("tourist-7", v1, at=MORNING)

        view = self.guest.check_saved("tourist-7", v1.version_id, at=MORNING)
        self.assertEqual(view.status, VALID)
        self.assertTrue(view.still_valid)

        # 次日仍在时效内 → 有效
        view = self.guest.check_saved("tourist-7", v1.version_id, at=NEXT_DAY)
        self.assertEqual(view.status, VALID)

        # 一周后：先无新版本时只是过期；紧急更正后则显示已被替代
        claim2 = revised_claim(2, 4, oil_lot="lot-soyoil-0927")
        p2 = build_ready_package(claim2, "d-100", b"photo-new",
                                 at=ONE_WEEK_LATER, valid_seconds=86400)
        old = self.registry.get(v1.version_id)
        v2 = self.registry.publish(p2, at=ONE_WEEK_LATER, replaces=old)
        view = self.guest.check_saved("tourist-7", v1.version_id,
                                      at=ONE_WEEK_LATER)
        self.assertEqual(view.status, SUPERSEDED)
        self.assertFalse(view.still_valid)
        self.assertEqual(view.current_version_id, v2.version_id)

    def test_week_old_allergen_note_is_never_shown_as_current(self):
        # 1 小时时效的结论：两小时后收藏页必须标出过期
        p1 = build_ready_package(self.claim, "d-100", b"photo-short",
                                 at=MORNING, valid_seconds=3600)
        v1 = self.registry.publish(p1, at=MORNING)
        self.guest.save("tourist-8", v1, at=MORNING)
        view = self.guest.check_saved(
            "tourist-8", v1.version_id, at="2026-09-20T12:01:00+08:00")
        self.assertEqual(view.status, EXPIRED)
        self.assertFalse(view.still_valid)

    def test_followup_answers_are_time_bounded(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-q",
                                 at=MORNING, valid_seconds=3600)
        v1 = self.registry.publish(p1, at=MORNING)
        ans = self.guest.ask(version_id=v1.version_id, question="allergens",
                             at=MORNING)
        self.assertTrue(ans.certain)
        for code in ("peanut", "fish", "soy", "mollusc"):
            self.assertIn(code, ans.text)

        stale = self.guest.ask(version_id=v1.version_id, question="allergens",
                               at="2026-09-20T12:01:00+08:00")
        self.assertFalse(stale.certain)
        self.assertIsNone(stale.valid_until)

    def test_followup_after_correction_and_withdrawal(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-a2",
                                 at=MORNING, valid_seconds=86400 * 30)
        v1 = self.registry.publish(p1, at=MORNING)
        claim2 = revised_claim(2, 4, oil_lot="lot-soyoil-0921")
        p2 = build_ready_package(claim2, "d-100", b"photo-b2",
                                 at=NEXT_DAY, valid_seconds=86400 * 30)
        v2 = self.registry.publish(p2, at=NEXT_DAY,
                                   replaces=self.registry.get(v1.version_id))
        ans = self.guest.ask(version_id=v1.version_id, question="advisory",
                             at=NEXT_DAY)
        self.assertFalse(ans.certain)
        self.assertEqual(ans.source_version_id, v2.version_id)

        withdrawn = self.registry.withdraw(v2, at=ONE_WEEK_LATER)
        ans = self.guest.ask(version_id=withdrawn.version_id,
                             question="allergens", at=ONE_WEEK_LATER)
        self.assertFalse(ans.certain)
        self.assertIn("撤回", ans.text)

    def test_unknown_question_is_uncertain(self):
        p1 = build_ready_package(self.claim, "d-100", b"photo-u", at=MORNING)
        v1 = self.registry.publish(p1, at=MORNING)
        ans = self.guest.ask(version_id=v1.version_id,
                             question="exact_recipe_amounts", at=MORNING)
        self.assertFalse(ans.certain)  # 不向竞争商家公开配方细节


class AuditBundleTest(unittest.TestCase):
    def test_bundle_shows_fingerprint_recipe_chain_and_three_signatures(self):
        claim = load_claim(FIXTURE)
        registry = PublishRegistry()
        cache = CacheCoordinator([CacheLayer("app"), CacheLayer("edge")])
        package = build_ready_package(claim, "d-100", b"photo-audit",
                                      at=MORNING, batch_ids=("b-20260920-squid",))
        version = registry.publish(package, at=MORNING)
        cache.fill(f"{claim.menu_id}:d-100:en", version, at=MORNING)

        guest = GuestService(registry)
        saved = guest.save("tourist-9", version, at=MORNING)

        bundle = build_bundle(package=package, registry=registry, cache=cache,
                              saved_items=(saved,), at=MORNING, keys=KEYS)
        # 原图指纹
        self.assertEqual(bundle.image_fingerprint,
                         "sha256:" + __import__("hashlib")
                         .sha256(b"photo-audit").hexdigest())
        # 配方版本（仅复盘可见）
        self.assertEqual(bundle.recipe_ref, "r-squid-pepper-salt@v3")
        self.assertTrue(bundle.recipe_fingerprint.startswith("sha256:"))
        # 完整翻译链
        self.assertEqual([h.role for h in bundle.chain],
                         ["mt", "translator"])
        self.assertEqual(bundle.chain_hash, bundle.chain[-1].hop_hash)
        self.assertEqual(bundle.find_hop(bundle.chain[0].hop_hash).actor,
                         "mt-demo")
        # 三方签署且全部验签通过
        roles = {s.role for s in bundle.signers}
        self.assertEqual(roles, {"translator", "nutritionist", "merchant"})
        self.assertTrue(all(s.verified for s in bundle.signers))
        self.assertEqual(bundle.claim_revision, 1)
        self.assertEqual(bundle.published_version_id, version.version_id)
        # 收藏内容当前有效
        self.assertEqual(bundle.saved_views[0].status, VALID)

    def test_bundle_records_effective_invalidation_time(self):
        claim = load_claim(FIXTURE)
        registry = PublishRegistry()
        cache = CacheCoordinator([CacheLayer("app"), CacheLayer("edge"),
                                  CacheLayer("device")])
        p1 = build_ready_package(claim, "d-100", b"photo-i1", at=MORNING)
        v1 = registry.publish(p1, at=MORNING)
        key = f"{claim.menu_id}:d-100:en"
        cache.fill(key, v1, at=MORNING)
        claim2 = revised_claim(2, 4, oil_lot="lot-soyoil-0921")
        p2 = build_ready_package(claim2, "d-100", b"photo-i2", at=NEXT_DAY)
        v2 = registry.publish(p2, at=NEXT_DAY,
                              replaces=registry.get(v1.version_id))
        old = registry.get(v1.version_id)
        event = cache.invalidate_version(
            old, CORRECTION, at=NEXT_DAY,
            layer_times={"app": "2026-09-21T10:31:00+08:00",
                         "edge": "2026-09-21T10:35:00+08:00",
                         "device": "2026-09-21T12:00:00+08:00"})
        bundle = build_bundle(package=p2, registry=registry, cache=cache)
        self.assertEqual(bundle.published_status, ACTIVE)
        self.assertEqual(len(bundle.invalidations), 1)
        self.assertEqual(bundle.effective_invalidated_at,
                         "2026-09-21T12:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
