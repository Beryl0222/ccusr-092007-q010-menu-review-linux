"""收藏页测试：保存内容必须标出是否仍有效（解决"一周前过敏原说明仍在展示"）。"""

import dataclasses
import unittest
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from menu_review import (
    SavedCollection,
    SavedStatus,
    render_saved,
)
from _helpers import (
    T_NOON,
    catalog_with_cache,
    full_signed_case,
    make_capture,
    sample_claim,
)
import menu_review


def at_noon(day: str = "2026-09-20") -> datetime:
    return datetime.fromisoformat(f"{day}T12:30:00+08:00")


class SavedTest(unittest.TestCase):
    def _publish(self):
        catalog, cache = catalog_with_cache()
        case = full_signed_case()
        menu_review.publish(case, at=T_NOON)
        version = catalog.publish(case, at=T_NOON)
        return catalog, version

    def test_fresh_save_is_current(self):
        catalog, version = self._publish()
        saved = SavedCollection(catalog)
        saved.save("tourist-1", version, at=T_NOON)
        view = saved.view("tourist-1", "sample-010", now=at_noon())
        self.assertIs(view.status, SavedStatus.CURRENT)
        self.assertTrue(view.still_valid)

    def test_week_old_allergen_sheet_is_marked_expired_or_superseded(self):
        # 投诉场景：收藏页还在展示一周前的过敏原说明。
        catalog, version = self._publish()
        saved = SavedCollection(catalog)
        saved.save("tourist-1", version, at="2026-09-13T12:30:00+08:00")
        # 同一天稍后看仍是 current（版本在架且在供应窗口内）；
        # 一周后再看：供应窗口已过 → EXPIRED，提示重新确认。
        view = saved.view("tourist-1", "sample-010", now=at_noon("2026-09-27"))
        self.assertIn(
            view.status,
            (SavedStatus.EXPIRED, SavedStatus.SUPERSEDED),
        )
        page = render_saved(
            saved, "tourist-1", "sample-010", lang="en", now=at_noon("2026-09-27")
        )
        self.assertEqual(page.status, "expired")
        self.assertIn("重新确认", page.validity_note)

    def test_correction_marks_save_superseded(self):
        catalog, version = self._publish()
        saved = SavedCollection(catalog)
        saved.save("tourist-2", version, at=T_NOON)
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
        view = saved.view("tourist-2", "sample-010", now=at_noon())
        self.assertIs(view.status, SavedStatus.SUPERSEDED)
        self.assertFalse(view.still_valid)
        self.assertEqual(view.current_revision, 2)

    def test_withdrawn_save_marked(self):
        catalog, version = self._publish()
        saved = SavedCollection(catalog)
        saved.save("tourist-3", version, at=T_NOON)
        catalog.emergency_withdraw(
            "sample-010", at="2026-09-20T18:00:00+08:00", reason="标签错误"
        )
        view = saved.view("tourist-3", "sample-010", now=at_noon())
        self.assertIs(view.status, SavedStatus.WITHDRAWN)
        page = render_saved(
            saved, "tourist-3", "sample-010", lang="zh-Hans", now=at_noon()
        )
        self.assertEqual(page.status, "withdrawn")

    def test_pending_save_shown_without_safety_conclusion(self):
        from menu_review import UncertaintyReason

        catalog, cache = catalog_with_cache()
        case = full_signed_case(
            uncertainty={UncertaintyReason.TEMPORARILY_SOLD_OUT}
        )
        menu_review.publish(case, at=T_NOON)
        version = catalog.publish(case, at=T_NOON)
        saved = SavedCollection(catalog)
        saved.save("tourist-4", version, at=T_NOON)
        view = saved.view("tourist-4", "sample-010", now=at_noon())
        self.assertIs(view.status, SavedStatus.PENDING)
        page = render_saved(
            saved, "tourist-4", "sample-010", lang="en", now=at_noon()
        )
        self.assertEqual(page.status, "pending")
        self.assertEqual(page.allergens, ())


if __name__ == "__main__":
    unittest.main()
