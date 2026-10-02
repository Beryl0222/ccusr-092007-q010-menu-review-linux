import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from menu_review import (BLOCKED, CANDIDATE, MT_ROLE, POST_EDITOR_ROLE, READY,
                         SOLD_OUT, TRANSLATOR_ROLE, TRANSLATOR_SIGNED, STALE,
                         ReviewError, TranslationError, UncertainCondition,
                         load_claim, machine_translate, match_names,
                         merchant_confirm, nutritionist_review, post_edit,
                         sign_translation, start_package, finalize, mark_stale)
from menu_review.review import uncertainty_reasons

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (M_KEY, N_KEY, T_KEY, build_ready_package, fake_engine)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "menu_claim.json"
MORNING = "2026-09-20T10:00:00+08:00"
NOON = "2026-09-20T12:30:00+08:00"
WINTER = "2026-12-21T12:00:00+08:00"


class TranslationChainTest(unittest.TestCase):
    def setUp(self):
        self.claim = load_claim(FIXTURE)
        self.dish = self.claim.dish("d-100")

    def test_machine_translation_is_pending_and_safe_fields_absent(self):
        candidate = machine_translate(
            claim=self.claim, dish=self.dish, target_lang="en",
            engine="mt-demo", engine_version="model-42",
            translate=fake_engine, at=MORNING)
        self.assertEqual(candidate.status, CANDIDATE)
        self.assertEqual(candidate.field_text("name"), "Salt and Pepper Squid")
        # 机翻候选绝不包含过敏原/交叉接触等安全字段
        self.assertIsNone(candidate.field_text("allergens"))
        self.assertTrue(all(h.role == MT_ROLE for h in candidate.hops))

    def test_same_language_rejected(self):
        with self.assertRaises(TranslationError):
            machine_translate(claim=self.claim, dish=self.dish, target_lang="zh",
                              engine="x", engine_version="1",
                              translate=fake_engine, at=MORNING)

    def test_chain_hashes_link_every_hop(self):
        candidate = machine_translate(
            claim=self.claim, dish=self.dish, target_lang="en",
            engine="mt-demo", engine_version="model-42",
            translate=fake_engine, at=MORNING)
        edited = post_edit(candidate=candidate, editor="pe-1", field_id="name",
                           output_text="Salt & Pepper Squid", at=MORNING)
        signed = sign_translation(candidate=edited, translator="translator-1",
                                  key=T_KEY, at=MORNING)
        roles = [h.role for h in signed.hops]
        self.assertEqual(roles, [MT_ROLE, POST_EDITOR_ROLE, TRANSLATOR_ROLE])
        for prev, nxt in zip(signed.hops, signed.hops[1:]):
            self.assertEqual(nxt.prev_hash, prev.hop_hash)
        self.assertEqual(signed.status, TRANSLATOR_SIGNED)

    def test_translator_signature_verifies_and_tampering_fails(self):
        candidate = machine_translate(
            claim=self.claim, dish=self.dish, target_lang="en",
            engine="mt-demo", engine_version="model-42",
            translate=fake_engine, at=MORNING)
        signed = sign_translation(candidate=candidate, translator="translator-1",
                                  key=T_KEY, at=MORNING)
        self.assertTrue(signed.translator_signature.verify(
            signed.sign_payload(), T_KEY))
        self.assertFalse(signed.translator_signature.verify(
            signed.sign_payload(), b"wrong-key"))

    def test_translator_cannot_sign_non_candidate(self):
        candidate = machine_translate(
            claim=self.claim, dish=self.dish, target_lang="en",
            engine="mt-demo", engine_version="model-42",
            translate=fake_engine, at=MORNING)
        signed = sign_translation(candidate=candidate, translator="t",
                                  key=T_KEY, at=MORNING)
        with self.assertRaises(TranslationError):
            sign_translation(candidate=signed, translator="t2", key=T_KEY, at=MORNING)


class GateAndThreePartyTest(unittest.TestCase):
    def setUp(self):
        self.claim = load_claim(FIXTURE)

    def _start(self, dish_id, image, name, confidence, at):
        dish = self.claim.dish(dish_id)
        recognition = match_names(image_bytes=image, observed_name=name,
                                  dishes=self.claim.dishes, confidence=confidence)
        candidate = machine_translate(
            claim=self.claim, dish=dish, target_lang="en",
            engine="mt-demo", engine_version="model-42",
            translate=fake_engine, at=at)
        signed = sign_translation(candidate=candidate, translator="translator-1",
                                  key=T_KEY, at=at)
        return start_package(package_id=f"pkg-{dish_id}", claim=self.claim,
                             recognition=recognition, translation=signed, at=at)

    def test_happy_path_finalizes_ready_with_three_signatures(self):
        package = build_ready_package(self.claim, "d-100", b"photo-ok",
                                      at=MORNING, batch_ids=("b-20260920-squid",))
        self.assertEqual(package.status, READY)
        self.assertEqual(package.risk.allergens,
                         ("fish", "mollusc", "peanut", "soy"))
        self.assertIn("f-oil-0920", package.risk.reviewed_fact_ids)
        self.assertTrue(package.merchant.available)
        self.assertEqual(package.translation.translator_signature.signer,
                         "translator-1")

    def test_low_confidence_photo_blocks_certain_conclusion(self):
        package = self._start("d-100", b"blur", "椒盐鱿鱼", 0.6, MORNING)
        self.assertEqual(package.status, BLOCKED)
        with self.assertRaises(UncertainCondition) as caught:
            nutritionist_review(package=package, signer="n", key=N_KEY,
                                advisories={"en": "x"}, at=MORNING)
        self.assertIn("recognition_below_confident_threshold",
                      caught.exception.reasons)

    def test_multiple_names_ambiguity_blocks(self):
        dish = self.claim.dish("d-100")
        recognition = match_names(
            image_bytes=b"ambig", observed_name="fried tonight",
            dishes=self.claim.dishes, confidence=0.99,
            extra_aliases={"d-100": ["fried tonight"],
                           "d-300": ["fried tonight"]})
        candidate = machine_translate(claim=self.claim, dish=dish, target_lang="en",
                                      engine="mt", engine_version="1",
                                      translate=fake_engine, at=MORNING)
        signed = sign_translation(candidate=candidate, translator="t",
                                  key=T_KEY, at=MORNING)
        package = start_package(package_id="pkg-ambig", claim=self.claim,
                                recognition=recognition, translation=signed,
                                at=MORNING)
        self.assertEqual(package.status, BLOCKED)
        self.assertIn("multiple_dishes_share_name", package.block_reasons)

    def test_seasonal_substitution_blocks_in_winter(self):
        dish = self.claim.dish("d-200")
        reasons = uncertainty_reasons(self.claim, dish, at=WINTER)
        self.assertIn("seasonal_substitution_active", reasons)
        package = self._start("d-200", b"winter", "宫保鸡丁", 0.99, WINTER)
        self.assertEqual(package.status, BLOCKED)

    def test_sold_out_has_no_safety_conclusion(self):
        # 中午走油肉售罄：商户确认 available=False，定稿为 sold_out
        dish = self.claim.dish("d-300")
        recognition = match_names(image_bytes=b"pork", observed_name="本帮走油肉",
                                  dishes=self.claim.dishes, confidence=0.99)
        candidate = machine_translate(claim=self.claim, dish=dish, target_lang="en",
                                      engine="mt", engine_version="1",
                                      translate=fake_engine, at=NOON)
        signed = sign_translation(candidate=candidate, translator="t",
                                  key=T_KEY, at=NOON)
        package = start_package(package_id="pkg-pork", claim=self.claim,
                                recognition=recognition, translation=signed, at=NOON)
        risk = nutritionist_review(package=package, signer="n", key=N_KEY,
                                   advisories={"en": "shared fryer"}, at=NOON)
        merchant = merchant_confirm(package=package, signer="m", key=M_KEY, at=NOON)
        self.assertFalse(merchant.available)
        done = finalize(package=package, risk=risk, merchant=merchant, at=NOON)
        self.assertEqual(done.status, SOLD_OUT)

    def test_sold_out_resumes_after_window(self):
        # 18:00 恢复供应后同一条售罄事实不再生效
        dish = self.claim.dish("d-300")
        recognition = match_names(image_bytes=b"pork2", observed_name="本帮走油肉",
                                  dishes=self.claim.dishes, confidence=0.99)
        candidate = machine_translate(claim=self.claim, dish=dish, target_lang="en",
                                      engine="mt", engine_version="1",
                                      translate=fake_engine, at="2026-09-20T18:00:00+08:00")
        signed = sign_translation(candidate=candidate, translator="t",
                                  key=T_KEY, at="2026-09-20T18:00:00+08:00")
        package = start_package(package_id="pkg-pork2", claim=self.claim,
                                recognition=recognition, translation=signed,
                                at="2026-09-20T18:00:00+08:00")
        merchant = merchant_confirm(package=package, signer="m", key=M_KEY,
                                    at="2026-09-20T18:00:00+08:00")
        self.assertTrue(merchant.available)

    def test_nutritionist_advisory_must_be_in_target_language(self):
        package = self._start("d-100", b"p", "椒盐鱿鱼", 0.99, MORNING)
        with self.assertRaises(ReviewError):
            nutritionist_review(package=package, signer="n", key=N_KEY,
                                advisories={"ja": "日本語のみ"}, at=MORNING)

    def test_stale_recipe_revision_rejects_finalize(self):
        package = build_ready_package(self.claim, "d-100", b"v3", at=MORNING)
        # 模拟声明修订号前进（餐厅换配方版本）：旧签署立即不可定稿
        import json
        from menu_review.claims import MenuClaim
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw["revision"] = 2
        raw["dishes"][0]["recipe_version"] = 4
        new_claim = MenuClaim.from_raw(raw)
        rebuilt = build_ready_package(new_claim, "d-100", b"v4", at=MORNING)
        self.assertEqual(rebuilt.status, READY)
        # 旧包在新事实上定稿必须被拒（claim 被整体替换时）
        from menu_review.review import ReviewPackage
        old_on_new = ReviewPackage(
            package.package_id, new_claim, package.dish,
            package.image_fingerprint, package.recognition,
            package.translation, package.risk, package.merchant,
            package.block_reasons, package.status)
        with self.assertRaises(ReviewError):
            finalize(package=old_on_new, risk=old_on_new.risk,
                     merchant=old_on_new.merchant, at=MORNING)

    def test_oil_change_after_review_forces_re_review(self):
        # 08:00 完成复核（当时当天还没换油），08:30 餐厅换油，
        # 10:00 再定稿必须失败：经营事实快照已落后于当前事实。
        package = self._start("d-100", b"oil", "椒盐鱿鱼", 0.99,
                              "2026-09-20T08:00:00+08:00")
        risk = nutritionist_review(
            package=package, signer="n", key=N_KEY,
            advisories={"en": "shared fryer"}, at="2026-09-20T08:00:00+08:00",
            valid_seconds=86400)
        self.assertNotIn("f-oil-0920", risk.reviewed_fact_ids)
        merchant = merchant_confirm(package=package, signer="m", key=M_KEY,
                                    at="2026-09-20T08:00:00+08:00",
                                    valid_seconds=86400)
        with self.assertRaises(ReviewError) as caught:
            finalize(package=package, risk=risk, merchant=merchant, at=MORNING)
        self.assertIn("经营事实已变化", str(caught.exception))

        # 重新复核纳入换油事实后才能定稿
        risk2 = nutritionist_review(
            package=package, signer="n", key=N_KEY,
            advisories={"en": "Cooked in a shared fryer; oil changed this morning."},
            at=MORNING, valid_seconds=86400)
        merchant2 = merchant_confirm(package=package, signer="m", key=M_KEY, at=MORNING)
        done = finalize(package=package, risk=risk2, merchant=merchant2,
                        translator_key=T_KEY, nutritionist_key=N_KEY,
                        merchant_key=M_KEY, at=MORNING)
        self.assertEqual(done.status, READY)
        self.assertIn("f-oil-0920", done.risk.reviewed_fact_ids)

    def test_ready_becomes_stale_after_fact_change(self):
        package = build_ready_package(self.claim, "d-100", b"stale",
                                      at=MORNING, valid_seconds=86400)
        stale = mark_stale(package)
        self.assertEqual(stale.status, STALE)
        # 非 ready 包标记 stale 是幂等的
        self.assertEqual(mark_stale(stale).status, STALE)

    def test_signature_tamper_rejected_in_finalize(self):
        # 用错误密钥校验三方签名
        package = self._start("d-100", b"pp", "椒盐鱿鱼", 0.99, MORNING)
        risk = nutritionist_review(package=package, signer="n", key=N_KEY,
                                   advisories={"en": "ok"}, at=MORNING)
        merchant = merchant_confirm(package=package, signer="m", key=M_KEY, at=MORNING)
        with self.assertRaises(ReviewError):
            finalize(package=package, risk=risk, merchant=merchant,
                     translator_key=b"other", nutritionist_key=N_KEY,
                     merchant_key=M_KEY, at=MORNING)
        with self.assertRaises(ReviewError):
            finalize(package=package, risk=risk, merchant=merchant,
                     translator_key=T_KEY, nutritionist_key=b"other",
                     merchant_key=M_KEY, at=MORNING)


if __name__ == "__main__":
    unittest.main()
