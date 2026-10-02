import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from menu_review import (ALLERGEN_CODES, ClaimError, load_claim, load_record,
                         match_names, fingerprint)
from menu_review.claims import MenuClaim

FIXTURE = Path(__file__).parents[1] / "fixtures" / "menu_claim.json"

# 复核时间窗：2026-09-20 上午（开店换油 08:30 已发生，午市售罄 11:20 未发生）
MORNING = "2026-09-20T10:00:00+08:00"
NOON = "2026-09-20T12:30:00+08:00"
WINTER = "2026-12-21T12:00:00+08:00"


class ContractTest(unittest.TestCase):
    def test_minimal_contract_still_loads_extended_fixture(self):
        item = load_record(FIXTURE)
        self.assertEqual(item.domain, "menu_review")
        self.assertGreater(item.revision, 0)

    def test_claim_loads_fixture(self):
        claim = load_claim(FIXTURE)
        self.assertEqual(claim.menu_id, "menu-2026-09-20")
        self.assertEqual(len(claim.dishes), 3)


class ClaimRulesTest(unittest.TestCase):
    def setUp(self):
        self.claim = load_claim(FIXTURE)

    def test_dish_multiple_names(self):
        squid = self.claim.dish("d-100")
        self.assertEqual(squid.names, ("椒盐鱿鱼", "香炸鲜鱿"))
        self.assertTrue(squid.known_name("香炸鲜鱿"))
        self.assertEqual(squid.recipe_ref, "r-squid-pepper-salt@v3")

    def test_shared_fryer_and_oil_are_declared(self):
        squid = self.claim.dish("d-100")
        aid_allergens = {a for aid in squid.processing_aids for a in aid.allergens}
        contact_allergens = {a for c in squid.cross_contact for a in c.shared_allergens}
        self.assertEqual(aid_allergens, {"soy"})           # 炸制用大豆油
        self.assertEqual(contact_allergens, {"peanut", "fish"})  # 共用炸锅
        fryer = squid.cross_contact[0]
        self.assertEqual(fryer.kind, "shared_fryer")

    def test_oil_change_fact_is_active_and_tied_to_shared_equipment(self):
        squid = self.claim.dish("d-100")
        equipment = {c.equipment_id for c in squid.cross_contact}
        facts = [f for f in self.claim.facts
                if f.equipment_id in equipment and f.active_at(MORNING)]
        self.assertEqual([f.fact_id for f in facts], ["f-oil-0920"])
        self.assertIn("lot-soyoil-0920-morning", facts[0].detail)
        # 换油发生在 08:30，08:00 时尚不存在该事实
        self.assertFalse(facts[0].active_at("2026-09-20T08:00:00+08:00"))

    def test_sold_out_fact_has_window(self):
        pork = self.claim.dish("d-300")
        active = self.claim.active_facts("sold_out", dish_id=pork.dish_id, at=NOON)
        self.assertEqual(len(active), 1)
        # 17:00 恢复后事实不再生效
        self.assertFalse(active[0].active_at("2026-09-20T18:00:00+08:00"))
        self.assertFalse(active[0].active_at("2026-09-20T10:00:00+08:00"))

    def test_seasonal_substitution_only_active_in_window(self):
        chicken = self.claim.dish("d-200")
        self.assertIsNone(chicken.active_substitution(MORNING))
        sub = chicken.active_substitution(WINTER)
        self.assertIsNotNone(sub)
        self.assertEqual(sub.replacement.ingredient_id, "i-bamboo-shoot")

    def test_batch_binds_dish_and_ingredient_lots(self):
        batches = self.claim.batches_for("d-100", "2026-09-20")
        self.assertEqual(len(batches), 1)
        self.assertIn("lot-soyoil-0920-morning", batches[0].ingredient_batch_ids)

    def test_unknown_allergen_code_rejected(self):
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw["dishes"][0]["ingredients"][0]["allergens"] = ["shellfish"]
        with self.assertRaises(ClaimError):
            MenuClaim.from_raw(raw)

    def test_batch_referencing_missing_dish_rejected(self):
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw["batches"][0]["dish_id"] = "d-nope"
        with self.assertRaises(ClaimError):
            MenuClaim.from_raw(raw)

    def test_recipe_version_change_is_visible(self):
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw["revision"] = 2
        raw["dishes"][0]["recipe_version"] = 4
        claim2 = MenuClaim.from_raw(raw)
        self.assertNotEqual(claim2.dish("d-100").recipe_ref,
                            self.claim.dish("d-100").recipe_ref)

    def test_claim_with_extras_stays_readable_by_minimal_contract(self):
        # 扩展后的样例必须仍能被旧的最小合同读取（忽略未知键）。
        self.assertEqual(load_record(FIXTURE).record_id, "sample-010")


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.claim = load_claim(FIXTURE)

    def test_fingerprint_stable_and_distinct(self):
        a = fingerprint(b"photo-bytes-1")
        b = fingerprint(b"photo-bytes-1")
        c = fingerprint(b"photo-bytes-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("sha256:"))

    def test_confident_match_including_alias_name(self):
        result = match_names(
            image_bytes=b"img1", observed_name="香炸鲜鱿",
            dishes=self.claim.dishes, confidence=0.97)
        self.assertTrue(result.is_confident)
        self.assertEqual(result.matched_dish_id, "d-100")
        self.assertFalse(result.candidates[0].via_alias)

    def test_low_confidence_blocks_certain_match(self):
        result = match_names(
            image_bytes=b"img2", observed_name="椒盐鱿鱼",
            dishes=self.claim.dishes, confidence=0.4)
        self.assertFalse(result.is_confident)
        self.assertIn("recognition_low_confidence", result.reasons)
        self.assertEqual(result.candidates, ())

    def test_below_confident_threshold_is_uncertain(self):
        result = match_names(
            image_bytes=b"img3", observed_name="椒盐鱿鱼",
            dishes=self.claim.dishes, confidence=0.75)
        self.assertIsNone(result.matched_dish_id)
        self.assertIn("recognition_below_confident_threshold", result.reasons)

    def test_one_name_multiple_dishes_is_ambiguous(self):
        # 同一外语写法在两个菜品上都登记（一菜多名的歧义面）。
        result = match_names(
            image_bytes=b"img4", observed_name="fried tonight",
            dishes=self.claim.dishes, confidence=0.98,
            extra_aliases={"d-100": ["fried tonight"],
                           "d-300": ["fried tonight"]})
        self.assertIsNone(result.matched_dish_id)
        self.assertIn("multiple_dishes_share_name", result.reasons)
        self.assertEqual(len(result.candidates), 2)

    def test_name_not_on_menu(self):
        result = match_names(
            image_bytes=b"img5", observed_name="不存在的菜",
            dishes=self.claim.dishes, confidence=0.99)
        self.assertFalse(result.is_confident)
        self.assertIn("name_not_on_menu", result.reasons)


if __name__ == "__main__":
    unittest.main()
