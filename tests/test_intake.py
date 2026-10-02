"""拍照摄入测试：低置信、一菜多名、季节替换、售罄均挂起；无匹配不得出结论。"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from menu_review import OCR_CONFIDENCE_THRESHOLD, UncertaintyReason, intake_capture
from menu_review.workflow import RecognizedName
from _helpers import sample_claim, T_NOON


def names(*values: str, confidence: float = 0.97) -> tuple[RecognizedName, ...]:
    return tuple(
        RecognizedName(value=value, lang="zh-Hans", confidence=confidence)
        for value in values
    )


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.entries = [sample_claim()]

    def test_clean_capture_has_no_uncertainty(self):
        result = intake_capture(
            self.entries,
            image=b"photo-clean",
            recognized=names("香炸豆腐"),
            ocr_confidence=0.97,
            taken_at=T_NOON,
        )
        self.assertEqual(result.uncertainty, frozenset())
        self.assertEqual(result.matched_dish_ids, ("dish-fried-tofu",))

    def test_alias_name_matches_same_dish(self):
        # 一菜多名：别名"炸豆腐"也应归并到同一菜品，不产生第二个匹配。
        result = intake_capture(
            self.entries,
            image=b"photo-alias",
            recognized=names("炸豆腐"),
            ocr_confidence=0.97,
            taken_at=T_NOON,
        )
        self.assertEqual(result.matched_dish_ids, ("dish-fried-tofu",))
        self.assertEqual(result.uncertainty, frozenset())

    def test_low_confidence_is_uncertain(self):
        result = intake_capture(
            self.entries,
            image=b"photo-blurry",
            recognized=names("香炸豆腐"),
            ocr_confidence=OCR_CONFIDENCE_THRESHOLD - 0.01,
            taken_at=T_NOON,
        )
        self.assertIn(UncertaintyReason.LOW_OCR_CONFIDENCE, result.uncertainty)

    def test_seasonal_and_soldout_flagged(self):
        result = intake_capture(
            self.entries,
            image=b"photo-winter",
            recognized=names("香炸豆腐"),
            ocr_confidence=0.99,
            taken_at=T_NOON,
            seasonal_substitution=True,
            sold_out=True,
        )
        self.assertEqual(
            set(result.uncertainty),
            {
                UncertaintyReason.SEASONAL_SUBSTITUTION,
                UncertaintyReason.TEMPORARILY_SOLD_OUT,
            },
        )

    def test_no_match_refuses_any_conclusion(self):
        with self.assertRaises(ValueError):
            intake_capture(
                self.entries,
                image=b"photo-other",
                recognized=names("宫保鸡丁"),
                ocr_confidence=0.99,
                taken_at=T_NOON,
            )

    def test_multiple_dish_matches_flagged(self):
        # 同一识别结果命中两条声明（模拟一菜多名横跨两菜的歧义）。
        envelope, claim = sample_claim()
        import dataclasses
        from menu_review import Dish

        second_dish = dataclasses.replace(
            claim,
            dish=Dish(
                dish_id="dish-other-tofu",
                recipe_id="recipe-other",
                recipe_version=1,
                names=claim.dish.names,
            ),
        )
        result = intake_capture(
            [(envelope, claim), (envelope, second_dish)],
            image=b"photo-ambiguous",
            recognized=names("香炸豆腐"),
            ocr_confidence=0.99,
            taken_at=T_NOON,
        )
        self.assertIn(UncertaintyReason.MULTIPLE_NAMES_MATCH, result.uncertainty)
        self.assertEqual(len(result.matched_dish_ids), 2)

    def test_same_image_has_same_fingerprint(self):
        first = intake_capture(
            self.entries,
            image=b"same-bytes",
            recognized=names("香炸豆腐"),
            ocr_confidence=0.97,
            taken_at=T_NOON,
        )
        second = intake_capture(
            self.entries,
            image=b"same-bytes",
            recognized=names("香炸豆腐"),
            ocr_confidence=0.95,
            taken_at=T_NOON,
        )
        self.assertEqual(
            first.capture.image_fingerprint, second.capture.image_fingerprint
        )


if __name__ == "__main__":
    unittest.main()
