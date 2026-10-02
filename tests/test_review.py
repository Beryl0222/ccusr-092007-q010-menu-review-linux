"""审核复盘包测试：原图指纹、配方版本、翻译链、三方签署、失效记录一屏可见。"""

import dataclasses
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import menu_review
from menu_review import (
    Certainty,
    ReviewState,
    Role,
    build_review_package,
)
from _helpers import (
    T_NOON,
    catalog_with_cache,
    full_signed_case,
    make_capture,
    sample_claim,
)


class ReviewPackageTest(unittest.TestCase):
    def test_package_shows_full_evidence_chain(self):
        catalog, cache = catalog_with_cache()
        case = full_signed_case()
        menu_review.publish(case, at=T_NOON)
        catalog.publish(case, at=T_NOON)
        pkg = build_review_package(catalog, cache, "sample-010")

        self.assertEqual(pkg.image_fingerprint, case.capture.image_fingerprint)
        self.assertEqual(pkg.recipe_id, "recipe-fried-tofu")
        self.assertEqual(pkg.recipe_version, 4)
        self.assertEqual(pkg.claim_revision, 1)
        self.assertIs(pkg.state, ReviewState.PUBLISHED)
        self.assertIs(pkg.certainty, Certainty.CONFIRMED)
        self.assertTrue(pkg.three_way_signed)
        self.assertEqual(set(pkg.signatures), {r.value for r in Role})

        # 翻译链：先机译候选，后人工修订。
        stages = [link.stage for link in pkg.translation_chain]
        self.assertEqual(stages, ["machine", "human"])
        self.assertIsNone(pkg.translation_chain[0].actor)
        self.assertEqual(pkg.translation_chain[0].engine, "nmt-v9")
        self.assertEqual(pkg.translation_chain[1].actor, "translator-lin")
        self.assertEqual(pkg.translation_chain[1].output_text, "Crispy Fried Tofu")

        # 三方签署各自绑定同一图片指纹与配方版本。
        for sig in pkg.signatures.values():
            self.assertEqual(sig.image_fingerprint, case.capture.image_fingerprint)
            self.assertEqual(sig.recipe_version, 4)

    def test_package_records_cache_invalidation_after_correction(self):
        catalog, cache = catalog_with_cache()
        case = full_signed_case()
        menu_review.publish(case, at=T_NOON)
        catalog.publish(case, at=T_NOON)

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
            capture=make_capture(b"photo-rev2"),
            envelope=new_envelope,
            claim=new_claim,
        )
        menu_review.publish(new_case, at="2026-09-20T15:05:00+08:00")
        catalog.publish(new_case, at="2026-09-20T15:05:00+08:00")

        pkg = build_review_package(catalog, cache, "sample-010")
        self.assertEqual(pkg.claim_revision, 2)
        reasons = {r.reason for r in pkg.invalidations}
        self.assertIn("correction", reasons)
        layers = {r.layer for r in pkg.invalidations}
        self.assertEqual(layers, {"edge_cdn", "app_cache", "saved_store"})

        # 复盘旧版本展示时应能指定旧译审单。
        old_pkg = build_review_package(
            catalog, cache, "sample-010", case=case
        )
        self.assertEqual(old_pkg.claim_revision, 1)
        self.assertIs(old_pkg.state, ReviewState.SUPERSEDED)
        self.assertIsNone(old_pkg.public_version)

    def test_withdraw_visible_in_package(self):
        catalog, cache = catalog_with_cache()
        case = full_signed_case()
        menu_review.publish(case, at=T_NOON)
        catalog.publish(case, at=T_NOON)
        catalog.emergency_withdraw(
            "sample-010", at="2026-09-20T18:00:00+08:00", reason="原料标签错误"
        )
        pkg = build_review_package(catalog, cache, "sample-010", case=case)
        self.assertIs(pkg.state, ReviewState.WITHDRAWN)
        self.assertTrue(
            any("紧急撤回" in note for _, _, note in pkg.event_log)
        )


if __name__ == "__main__":
    unittest.main()
