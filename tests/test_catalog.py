"""公开版本登记处测试：反复拍摄归一、唯一公开版本、机密剥离、更正与撤回。"""

import unittest
import sys
import dataclasses
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from menu_review import Certainty, ReviewState
from _helpers import (
    T_NOON,
    catalog_with_cache,
    full_signed_case,
    make_capture,
    sample_claim,
)


class CatalogTest(unittest.TestCase):
    def _publish(self, at=T_NOON):
        catalog, cache = catalog_with_cache()
        case = full_signed_case()
        self.assertEqual(__import__("menu_review").publish(case, at=at), Certainty.CONFIRMED)
        catalog.register_capture(case)
        version = catalog.publish(case, at=at)
        return catalog, cache, case, version

    def test_repeated_photos_resolve_to_single_public_version(self):
        catalog, _cache, case, version = self._publish()
        fp1 = make_capture(b"second-photo").image_fingerprint
        fp2 = make_capture(b"third-photo").image_fingerprint
        catalog.register_capture(
            dataclasses.replace(case, capture=dataclasses.replace(case.capture, image_fingerprint=fp1))
        )
        catalog.register_capture(
            dataclasses.replace(case, capture=dataclasses.replace(case.capture, image_fingerprint=fp2))
        )
        for fp in (case.capture.image_fingerprint, fp1, fp2):
            resolved = catalog.resolve(fp)
            self.assertEqual(resolved.menu_id, "sample-010")
            self.assertEqual(resolved.revision, version.revision)
        self.assertEqual(len({catalog.resolve(fp).revision for fp in (fp1, fp2)}), 1)

    def test_confidential_recipe_never_enters_public_version(self):
        catalog, _cache, _case, version = self._publish()
        payload = repr(version)
        self.assertNotIn("proportions", payload)
        self.assertNotIn("method_steps", payload)
        self.assertNotIn("Z-07 配比封存", payload)
        # 机密原料保留风险标签（过敏原仍须提示），但不泄露用量。
        secret = next(item for item in version.ingredients if item.confidential)
        self.assertIn("peanut", secret.allergens)
        self.assertNotIn("配比", repr(secret))

    def test_correction_retires_old_version_and_invalidates_cache(self):
        catalog, cache, old_case, v1 = self._publish()
        # 新 revision：当日换油时间变化（投诉后的紧急事实更正）。
        envelope, claim = sample_claim()
        new_envelope = dataclasses.replace(envelope, revision=2)
        new_claim = dataclasses.replace(
            claim,
            operations=dataclasses.replace(
                claim.operations,
                fryer_oil_changed_at="2026-09-20T15:00:00+08:00",
            ),
        )
        new_case = full_signed_case(
            capture=make_capture(b"correction-photo"),
            envelope=new_envelope,
            claim=new_claim,
        )
        __import__("menu_review").publish(new_case, at="2026-09-20T15:05:00+08:00")
        v2 = catalog.publish(new_case, at="2026-09-20T15:05:00+08:00")

        self.assertEqual(v2.revision, 2)
        self.assertEqual(v2.fryer_oil_changed_at, "2026-09-20T15:00:00+08:00")
        self.assertEqual(old_case.state, ReviewState.SUPERSEDED)
        # 更正先把旧 revision 从各层清掉（有失效记录），随后回填新 revision。
        records = cache.invalidation_log("sample-010")
        self.assertTrue(records)
        self.assertTrue(all(r.revision_before == 1 for r in records))
        self.assertEqual(
            {layer.get("sample-010") for layer in cache.layers}, {2}
        )
        history = catalog.history("sample-010")
        # 每个 revision 一行：v1 退役、v2 在架。
        self.assertEqual([v.revision for v in history], [1, 2])
        self.assertIsNotNone(history[0].superseded_at)
        self.assertIsNone(history[1].superseded_at)
        # 反复拍摄的指纹在更正后仍指向唯一的新版本。
        self.assertEqual(
            catalog.resolve(new_case.capture.image_fingerprint).revision, 2
        )

    def test_correction_requires_higher_revision(self):
        catalog, _cache, _case, _v = self._publish()
        another = full_signed_case(capture=make_capture(b"dup"))
        with self.assertRaises(ValueError):
            catalog.publish(another, at="2026-09-20T16:00:00+08:00")

    def test_emergency_withdraw_pulls_version_and_records_invalidation(self):
        catalog, cache, _case, version = self._publish()
        event_id = catalog.emergency_withdraw(
            "sample-010",
            at="2026-09-20T18:00:00+08:00",
            reason="原料标签错误",
        )
        self.assertTrue(event_id.startswith("inv:sample-010"))
        self.assertIsNone(catalog.resolve(version.image_fingerprints[0]))
        self.assertTrue(cache.all_layers_cleared("sample-010"))
        records = cache.invalidation_log("sample-010")
        self.assertEqual([r.layer for r in records[-3:]],
                         ["edge_cdn", "app_cache", "saved_store"])
        self.assertTrue(all(r.reason.startswith("withdraw") for r in records[-3:]))

    def test_pending_version_publishes_as_uncertain_then_confirmed(self):
        from menu_review import (
            Certainty as C,
            UncertaintyReason,
            publish as do_publish,
            resolve_uncertainty,
            Role,
        )
        catalog, cache = catalog_with_cache()
        case = full_signed_case(uncertainty={UncertaintyReason.SEASONAL_SUBSTITUTION})
        do_publish(case, at=T_NOON)
        held = catalog.publish(case, at=T_NOON)
        self.assertEqual(held.certainty, C.UNCERTAIN)
        self.assertTrue(held.follow_up_open)
        # 同 revision 解除不确定后转正式，不算更正，不触发失效传播。
        resolve_uncertainty(
            case,
            reason=UncertaintyReason.SEASONAL_SUBSTITUTION,
            resolver=Role.MERCHANT,
            at="2026-09-20T13:00:00+08:00",
            note="当季原料声明已补交确认",
        )
        do_publish(case, at="2026-09-20T13:05:00+08:00")
        confirmed = catalog.publish(case, at="2026-09-20T13:05:00+08:00")
        self.assertEqual(confirmed.certainty, C.CONFIRMED)
        self.assertEqual(confirmed.revision, held.revision)
        self.assertEqual(cache.invalidation_log("sample-010"), ())


if __name__ == "__main__":
    unittest.main()
