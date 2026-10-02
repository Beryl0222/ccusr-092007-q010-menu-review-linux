"""端到端：国际游客因"共用炸锅"译文缺失投诉后的完整处置链路。"""

import unittest
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import dataclasses
import menu_review
from menu_review import (
    Certainty,
    SavedCollection,
    SavedStatus,
    UncertaintyReason,
    build_review_package,
    view_for_capture,
)
from _helpers import (
    T_NOON,
    catalog_with_cache,
    full_signed_case,
    make_capture,
    sample_claim,
)

NOON = datetime.fromisoformat("2026-09-20T12:30:00+08:00")


class ComplaintScenarioTest(unittest.TestCase):
    def test_shared_fryer_complaint_full_lifecycle(self):
        # ── 1. 投诉前的旧做法：revision 1 在开店前提交，
        # 只译了菜名与文化故事，未记录换油时间，也没有交叉接触的时效复核。
        catalog, cache = catalog_with_cache()
        envelope, claim = sample_claim()
        morning_claim = dataclasses.replace(
            claim,
            operations=dataclasses.replace(
                claim.operations, fryer_oil_changed_at=None
            ),
        )
        case = full_signed_case(envelope=envelope, claim=morning_claim)
        menu_review.publish(case, at=T_NOON)
        catalog.register_capture(case)
        v1 = catalog.publish(case, at=T_NOON)
        self.assertEqual(v1.revision, 1)
        self.assertIsNone(v1.fryer_oil_changed_at)

        tourist = "intl-tourist-en"
        saved = SavedCollection(catalog)
        saved.save(tourist, v1, at=T_NOON)

        # ── 2. 投诉后核查发现：当天 11:30 换过食用油，
        # 商户提交 revision 2，三方按新声明重新签署。
        new_envelope = dataclasses.replace(envelope, revision=2)
        case2 = full_signed_case(
            capture=make_capture(b"photo-recheck"),
            envelope=new_envelope,
            claim=claim,
        )
        menu_review.publish(case2, at="2026-09-20T15:05:00+08:00")
        v2 = catalog.publish(case2, at="2026-09-20T15:05:00+08:00")
        self.assertEqual(v2.fryer_oil_changed_at, "2026-09-20T11:30:00+08:00")

        # ── 3. 游客反复拍摄同一菜单，始终只指向 revision 2 这一项公开版本。
        for raw in (b"photo-a", b"photo-b", b"photo-c"):
            fp = make_capture(raw).image_fingerprint
            catalog.register_capture(
                dataclasses.replace(
                    case2,
                    capture=dataclasses.replace(case2.capture, image_fingerprint=fp),
                )
            )
            self.assertEqual(catalog.resolve(fp).menu_id, "sample-010")
            self.assertEqual(catalog.resolve(fp).revision, 2)

        # ── 4. 英文视图明确提示共用炸锅与换油时间。
        view = view_for_capture(
            catalog,
            make_capture(b"photo-a").image_fingerprint,
            lang="en",
            now=NOON,
        )
        self.assertEqual(view.status, "current")
        self.assertIn("crustacean", view.allergens)
        self.assertTrue(
            any("same fryer" in n.casefold() for n in view.cross_contact_notices)
        )
        self.assertEqual(view.fryer_oil_changed_at, "2026-09-20T11:30:00+08:00")
        self.assertTrue(view.can_follow_up)

        # ── 5. 游客此前保存的内容被清楚标出已被更正，不再显示旧说明。
        status = saved.view(tourist, "sample-010", now=NOON)
        self.assertIs(status.status, SavedStatus.SUPERSEDED)
        self.assertFalse(status.still_valid)

        # ── 6. 更正传播到各层缓存，记录每层实际失效时间，随后回填 revision 2。
        invalidations = cache.invalidation_log("sample-010")
        self.assertEqual(len(invalidations), 3)
        self.assertEqual(
            [r.layer for r in invalidations],
            ["edge_cdn", "app_cache", "saved_store"],
        )
        self.assertTrue(all(r.effective_at for r in invalidations))
        self.assertEqual(
            {layer.get("sample-010") for layer in cache.layers}, {2}
        )

        # ── 7. 审核复盘一次展示：指纹、配方版本、翻译链、三方签署齐备。
        pkg = build_review_package(catalog, cache, "sample-010")
        self.assertTrue(pkg.three_way_signed)
        self.assertEqual(pkg.recipe_version, 4)
        self.assertEqual([l.stage for l in pkg.translation_chain], ["machine", "human"])
        self.assertEqual(pkg.image_fingerprint, case2.capture.image_fingerprint)
        self.assertTrue(pkg.invalidations)

        # ── 8. 竞争商家视角同样看不到配方机密。
        competitor = view_for_capture(
            catalog,
            make_capture(b"photo-a").image_fingerprint,
            lang="zh-Hans",
            now=NOON,
        )
        self.assertNotIn("配比", repr(competitor.as_dict()))
        self.assertNotIn("method", repr(competitor.as_dict()).casefold())

    def test_blurry_photo_seasonal_dish_ends_pending_with_followup(self):
        # 低置信 + 季节替换：即使三方签署齐全也只能挂起，不拼安全结论。
        catalog, cache = catalog_with_cache()
        case = full_signed_case(
            capture=make_capture(b"blurry", confidence=0.62),
            uncertainty={
                UncertaintyReason.LOW_OCR_CONFIDENCE,
                UncertaintyReason.SEASONAL_SUBSTITUTION,
            },
        )
        self.assertEqual(
            menu_review.publish(case, at=T_NOON), Certainty.UNCERTAIN
        )
        version = catalog.publish(case, at=T_NOON)
        self.assertEqual(version.certainty, Certainty.UNCERTAIN)
        view = view_for_capture(
            catalog, case.capture.image_fingerprint, lang="en", now=NOON
        )
        self.assertEqual(view.status, "pending")
        self.assertEqual(view.allergens, ())
        self.assertTrue(view.can_follow_up)


if __name__ == "__main__":
    unittest.main()
