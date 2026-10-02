"""测试用构造工厂：组装菜单声明、拍照与完整三方签署流程。"""

from __future__ import annotations

from pathlib import Path

from menu_review import (
    CacheBus,
    MenuCatalog,
    MenuClaim,
    DomainRecord,
    fingerprint_image,
    machine_translate,
    merchant_confirm,
    nutrition_review,
    open_case,
    translator_review,
)
from menu_review.workflow import Capture, RecognizedName, ReviewCase

FIXTURE = Path(__file__).parents[1] / "fixtures" / "menu_claim.json"

DAY = "2026-09-20"
T_OPEN = f"{DAY}T11:00:00+08:00"
T_OIL = f"{DAY}T11:30:00+08:00"
T_NOON = f"{DAY}T12:30:00+08:00"
T_CLOSE = f"{DAY}T21:00:00+08:00"
T_NEXT_WEEK = "2026-09-27T10:00:00+08:00"


def sample_claim() -> tuple[DomainRecord, MenuClaim]:
    from menu_review import load_claim

    return load_claim(FIXTURE)


def make_capture(
    image: bytes = b"menu-photo-010",
    *,
    confidence: float = 0.97,
    names: tuple[str, ...] = ("香炸豆腐",),
    taken_at: str = T_NOON,
    seasonal_substitution: bool = False,
    sold_out: bool = False,
) -> Capture:
    return Capture(
        image_fingerprint=fingerprint_image(image),
        restaurant_id="rest-anon-07",
        taken_at=taken_at,
        recognized_names=tuple(
            RecognizedName(value=name, lang="zh-Hans", confidence=confidence)
            for name in names
        ),
        ocr_confidence=confidence,
        seasonal_substitution=seasonal_substitution,
        sold_out=sold_out,
    )


def risk_statement() -> dict[str, str]:
    return {
        "zh-Hans": "含大豆、小麦、花生；与虾类、鱿鱼类共用炸锅，可能有甲壳类残留",
        "en": (
            "Contains soy, wheat and peanut. Same fryer as shrimp and squid: "
            "crustacean cross-contact is possible"
        ),
    }


def full_signed_case(
    *,
    uncertainty=None,
    capture: Capture | None = None,
    at: str = T_NOON,
    envelope: DomainRecord | None = None,
    claim: MenuClaim | None = None,
) -> ReviewCase:
    base_envelope, base_claim = sample_claim()
    envelope = envelope or base_envelope
    claim = claim or base_claim
    case = open_case(
        envelope,
        claim,
        capture or make_capture(),
        set(uncertainty or ()),
        at=at,
    )
    machine_translate(
        case,
        source_lang="zh-Hans",
        source_text="香炸豆腐",
        outputs={"en": "Fried Tofu"},
        engine="nmt-v9",
        at=at,
    )
    translator_review(
        case,
        signer="translator-lin",
        corrections={"en": "Crispy Fried Tofu"},
        at=at,
    )
    nutrition_review(
        case,
        signer="nutrition-zhao",
        risk_statement_i18n=risk_statement(),
        at=at,
    )
    merchant_confirm(
        case,
        signer="merchant-owner",
        confirmed_operations={"fryer_oil_changed_at": T_OIL},
        at=at,
    )
    return case


def catalog_with_cache() -> tuple[MenuCatalog, CacheBus]:
    cache = CacheBus.default()
    return MenuCatalog(cache=cache), cache
