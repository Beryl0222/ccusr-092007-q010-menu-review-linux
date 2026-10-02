"""游客视图测试：多语、带时效、可追问；待核实无确定结论；机密不泄露。"""

import unittest
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import menu_review
from menu_review import UncertaintyReason, view_for_capture
from _helpers import (
    T_NOON,
    catalog_with_cache,
    full_signed_case,
    make_capture,
)


def noon(day: str = "2026-09-20") -> datetime:
    return datetime.fromisoformat(f"{day}T12:30:00+08:00")


class VisitorViewTest(unittest.TestCase):
    def _publish_confirmed(self):
        catalog, _cache = catalog_with_cache()
        case = full_signed_case()
        menu_review.publish(case, at=T_NOON)
        catalog.register_capture(case)
        version = catalog.publish(case, at=T_NOON)
        return catalog, case, version

    def test_english_view_shows_shared_fryer_notice(self):
        # 投诉根因：译文没有提示共用炸锅。确定版本必须把该交叉接触说清楚。
        catalog, case, _v = self._publish_confirmed()
        view = view_for_capture(
            catalog, case.capture.image_fingerprint, lang="en", now=noon()
        )
        self.assertEqual(view.status, "current")
        self.assertIn("Crispy Fried Tofu", view.names)
        self.assertIn("crustacean", view.allergens)
        notice = " ".join(view.cross_contact_notices).casefold()
        self.assertIn("same fryer", notice)
        self.assertIn("shrimp", notice)
        # 当天换过油：时效事实展示给游客。
        self.assertEqual(view.fryer_oil_changed_at, "2026-09-20T11:30:00+08:00")
        self.assertTrue(view.can_follow_up)

    def test_chinese_view_matches(self):
        catalog, case, _v = self._publish_confirmed()
        view = view_for_capture(
            catalog, case.capture.image_fingerprint, lang="zh-Hans", now=noon()
        )
        self.assertIn("香炸豆腐", view.names)
        self.assertTrue(any("炸锅" in n for n in view.cross_contact_notices))

    def test_pending_view_has_no_allergen_conclusion(self):
        catalog, _cache = catalog_with_cache()
        case = full_signed_case(uncertainty={UncertaintyReason.LOW_OCR_CONFIDENCE})
        menu_review.publish(case, at=T_NOON)
        catalog.publish(case, at=T_NOON)
        view = view_for_capture(
            catalog, case.capture.image_fingerprint, lang="en", now=noon()
        )
        self.assertEqual(view.status, "pending")
        self.assertEqual(view.allergens, ())
        self.assertEqual(view.cross_contact_notices, ())
        self.assertTrue(view.can_follow_up)
        self.assertIn("待核实", view.validity_note)

    def test_expired_view_after_service_window(self):
        catalog, case, _v = self._publish_confirmed()
        view = view_for_capture(
            catalog,
            case.capture.image_fingerprint,
            lang="en",
            now=noon("2026-09-21"),
        )
        self.assertEqual(view.status, "expired")
        self.assertFalse(view.can_follow_up)

    def test_unseen_photo_returns_none(self):
        catalog, _case, _v = self._publish_confirmed()
        unknown = make_capture(b"never-seen").image_fingerprint
        self.assertIsNone(
            view_for_capture(catalog, unknown, lang="en", now=noon())
        )

    def test_public_view_never_leaks_recipe(self):
        catalog, case, _v = self._publish_confirmed()
        view = view_for_capture(
            catalog, case.capture.image_fingerprint, lang="en", now=noon()
        )
        blob = repr(view.as_dict())
        self.assertNotIn("proportions", blob)
        self.assertNotIn("method_steps", blob)
        self.assertNotIn("Z-07", blob)


if __name__ == "__main__":
    unittest.main()
