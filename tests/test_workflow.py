"""译审状态机测试：角色边界、签署顺序、不确定挂起、追问与解除。"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from menu_review import (
    Certainty,
    ReviewState,
    Role,
    UncertaintyReason,
    machine_translate,
    merchant_confirm,
    nutrition_review,
    open_case,
    publish,
    translator_review,
)
from menu_review.workflow import WorkflowError
from _helpers import (
    T_NOON,
    full_signed_case,
    make_capture,
    sample_claim,
    risk_statement,
)


class WorkflowTest(unittest.TestCase):
    def test_machine_translation_is_only_a_candidate(self):
        envelope, claim = sample_claim()
        case = open_case(envelope, claim, make_capture(), set(), at=T_NOON)
        machine_translate(
            case,
            source_lang="zh-Hans",
            source_text="香炸豆腐",
            outputs={"en": "Fried Tofu"},
            engine="nmt-v9",
            at=T_NOON,
        )
        self.assertEqual(case.state, ReviewState.CANDIDATE)
        self.assertNotIn(Role.TRANSLATOR, case.signatures)
        with self.assertRaises(WorkflowError):
            publish(case, at=T_NOON)

    def test_full_three_way_signoff_publishes_confirmed(self):
        case = full_signed_case()
        self.assertEqual(publish(case, at=T_NOON), Certainty.CONFIRMED)
        self.assertEqual(case.state, ReviewState.PUBLISHED)
        self.assertEqual(set(case.signatures), set(Role))

    def test_sign_order_is_semantics_risk_fact(self):
        envelope, claim = sample_claim()
        case = open_case(envelope, claim, make_capture(), set(), at=T_NOON)
        machine_translate(
            case,
            source_lang="zh-Hans",
            source_text="香炸豆腐",
            outputs={"en": "Fried Tofu"},
            engine="nmt-v9",
            at=T_NOON,
        )
        with self.assertRaises(WorkflowError):
            nutrition_review(
                case, signer="n", risk_statement_i18n=risk_statement(), at=T_NOON
            )
        with self.assertRaises(WorkflowError):
            merchant_confirm(case, signer="m", confirmed_operations={}, at=T_NOON)

    def test_nutrition_cannot_sign_guarantee(self):
        envelope, claim = sample_claim()
        case = open_case(envelope, claim, make_capture(), set(), at=T_NOON)
        machine_translate(
            case,
            source_lang="zh-Hans",
            source_text="香炸豆腐",
            outputs={"en": "Fried Tofu"},
            engine="nmt-v9",
            at=T_NOON,
        )
        translator_review(case, signer="t", corrections={"en": "Crispy Tofu"}, at=T_NOON)
        with self.assertRaises(WorkflowError):
            nutrition_review(
                case,
                signer="n",
                risk_statement_i18n={"en": "This dish is safe to eat"},
                at=T_NOON,
            )

    def test_uncertainty_holds_without_safety_conclusion_and_opens_followup(self):
        case = full_signed_case(
            uncertainty={UncertaintyReason.SEASONAL_SUBSTITUTION}
        )
        result = publish(case, at=T_NOON)
        self.assertEqual(result, Certainty.UNCERTAIN)
        self.assertEqual(case.state, ReviewState.HELD_UNCERTAIN)
        self.assertTrue(case.open_follow_ups())
        # 挂起事件文本明确不提供确定安全结论。
        _, state, note = case.events[-1]
        self.assertIs(state, ReviewState.HELD_UNCERTAIN)
        self.assertIn("不提供确定安全结论", note)

    def test_resolve_uncertainty_then_publish_confirmed(self):
        case = full_signed_case(uncertainty={UncertaintyReason.TEMPORARILY_SOLD_OUT})
        self.assertEqual(publish(case, at=T_NOON), Certainty.UNCERTAIN)
        from menu_review import resolve_uncertainty

        resolve_uncertainty(
            case,
            reason=UncertaintyReason.TEMPORARILY_SOLD_OUT,
            resolver=Role.MERCHANT,
            at="2026-09-20T13:00:00+08:00",
            note="菜品恢复供应，经营事实已确认",
        )
        self.assertEqual(
            publish(case, at="2026-09-20T13:05:00+08:00"), Certainty.CONFIRMED
        )
        self.assertEqual(case.state, ReviewState.PUBLISHED)

    def test_followup_can_be_answered_by_responsible_role(self):
        case = full_signed_case(uncertainty={UncertaintyReason.LOW_OCR_CONFIDENCE})
        publish(case, at=T_NOON)
        question = case.open_follow_ups()[0]
        from menu_review import answer_follow_up

        answer_follow_up(
            case,
            question=question,
            answer="Same fryer as shrimp; oil changed at 11:30.",
            answer_lang="en",
            by=Role.MERCHANT,
            at=T_NOON,
        )
        self.assertFalse(case.open_follow_ups())
        # 追问被回答不等于不确定因素解除——OCR 置信不足仍须挂起。
        self.assertEqual(case.state, ReviewState.HELD_UNCERTAIN)


if __name__ == "__main__":
    unittest.main()
