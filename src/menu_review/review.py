"""三方签署与不确定情形闸门。

角色边界：

* 译者（translator）：对译文语义负责，签署来自
  :mod:`menu_review.translation` 的翻译链。
* 营养专业人员（nutritionist）：复核过敏原、加工助剂与交叉接触风险，
  产出目标语言的风险提示；只对具体配方版本、声明修订号、供应批次与
  当时的经营事实负责，且结论带时效。
* 商户（merchant）：只确认当前经营事实（是否售罄、当天是否换油等），
  不签署专业风险结论。

闸门：季节替换生效、临时售罄、一菜多名或图片识别置信不足时，
不得产出确定的安全结论（``blocked`` / ``sold_out``），只能给出说明性
文案并引导追问。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .claims import DishClaim, MenuClaim
from .identity import RecognitionResult
from .signing import Signature
from .translation import TRANSLATOR_SIGNED, Translation
from .timeutil import now, parse

TRANSLATOR = "translator"
NUTRITIONIST = "nutritionist"
MERCHANT = "merchant"

DRAFT = "draft"
BLOCKED = "blocked"          # 存在不确定情形，不得给出确定安全结论
SOLD_OUT = "sold_out"        # 商户确认临时售罄，只展示售罄，不展示安全结论
READY = "ready"              # 三方齐备且时效有效，可发布为公开版本
STALE = "stale"              # 曾经 READY，但事实/版本已变化

# 三方复核结论的默认时效（秒）：超时必须重新复核
DEFAULT_VALID_SECONDS = 24 * 3600


class ReviewError(ValueError):
    pass


class UncertainCondition(ReviewError):
    """存在不得拼出确定结论的情形。``reasons`` 为机器可读原因码。"""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


def uncertainty_reasons(claim: MenuClaim, dish: DishClaim, *,
                        recognition: Optional[RecognitionResult] = None,
                        at: Optional[str] = None) -> tuple[str, ...]:
    """汇总当前阻止确定结论的原因（售罄单独由调用方处理）。"""
    reasons: list[str] = []
    if recognition is not None:
        if not recognition.is_confident:
            reasons.extend(recognition.reasons or ("recognition_uncertain",))
        if recognition.matched_dish_id is not None and recognition.matched_dish_id != dish.dish_id:
            reasons.append("recognition_dish_mismatch")
    if dish.active_substitution(at) is not None:
        reasons.append("seasonal_substitution_active")
    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class RiskAssessment:
    """营养专业人员的风险复核（带时效，绑定配方版本与经营事实快照）。"""

    assessment_id: str
    dish_id: str
    recipe_ref: str
    claim_revision: int
    target_lang: str
    allergens: tuple[str, ...]                 # 原料+助剂+交叉接触的过敏原并集
    advisories: tuple[str, ...]                # 专业人员撰写的目标语提示，禁止机翻代笔
    reviewed_fact_ids: tuple[str, ...]         # 复核时已纳入的经营事实
    reviewed_batch_ids: tuple[str, ...]
    observed_at: str
    valid_seconds: int
    signature: Signature

    def sign_payload(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "dish_id": self.dish_id,
            "recipe_ref": self.recipe_ref,
            "claim_revision": self.claim_revision,
            "target_lang": self.target_lang,
            "allergens": list(self.allergens),
            "advisories": list(self.advisories),
            "reviewed_fact_ids": list(self.reviewed_fact_ids),
            "reviewed_batch_ids": list(self.reviewed_batch_ids),
            "observed_at": self.observed_at,
            "valid_seconds": self.valid_seconds,
        }

    def fresh_at(self, at: Optional[str] = None) -> bool:
        moment = now() if at is None else parse(at)
        return parse(self.observed_at).timestamp() + self.valid_seconds >= moment.timestamp()

    def verify(self, key: bytes) -> bool:
        return self.signature.verify(self.sign_payload(), key)


@dataclass(frozen=True)
class MerchantConfirmation:
    """商户对当前经营事实的确认（带时效）。"""

    dish_id: str
    recipe_ref: str
    claim_revision: int
    confirmed_fact_ids: tuple[str, ...]
    available: bool
    at: str
    valid_seconds: int
    signature: Signature

    def sign_payload(self) -> dict[str, Any]:
        return {
            "dish_id": self.dish_id,
            "recipe_ref": self.recipe_ref,
            "claim_revision": self.claim_revision,
            "confirmed_fact_ids": list(self.confirmed_fact_ids),
            "available": self.available,
            "at": self.at,
            "valid_seconds": self.valid_seconds,
        }

    def fresh_at(self, moment_iso: Optional[str] = None) -> bool:
        moment = now() if moment_iso is None else parse(moment_iso)
        return parse(self.at).timestamp() + self.valid_seconds >= moment.timestamp()

    def verify(self, key: bytes) -> bool:
        return self.signature.verify(self.sign_payload(), key)


@dataclass(frozen=True)
class ReviewPackage:
    """一次展示的完整复核包，供发布与复盘使用。"""

    package_id: str
    claim: MenuClaim
    dish: DishClaim
    image_fingerprint: str
    recognition: RecognitionResult
    translation: Translation
    risk: Optional[RiskAssessment] = None
    merchant: Optional[MerchantConfirmation] = None
    block_reasons: tuple[str, ...] = ()
    status: str = DRAFT


def start_package(*, package_id: str, claim: MenuClaim,
                  recognition: RecognitionResult, translation: Translation,
                  at: Optional[str] = None) -> ReviewPackage:
    """发起复核包：身份不确定直接建立 BLOCKED 包，后续签署一律拒绝。"""
    if translation.status != TRANSLATOR_SIGNED:
        raise ReviewError("发起复核前译文必须已由译者签署")
    dish_id = recognition.matched_dish_id or translation.dish_id
    dish = claim.dish(dish_id)
    if translation.dish_id != dish.dish_id:
        raise ReviewError("译文与识别结果指向不同菜品")
    if translation.recipe_ref != dish.recipe_ref:
        raise UncertainCondition(("recipe_changed_since_translation",))
    reasons = uncertainty_reasons(claim, dish, recognition=recognition, at=at)
    status = BLOCKED if reasons else DRAFT
    return ReviewPackage(
        package_id=package_id, claim=claim, dish=dish,
        image_fingerprint=recognition.image_fingerprint,
        recognition=recognition, translation=translation,
        block_reasons=reasons, status=status)


def _active_fact_ids(claim: MenuClaim, dish: DishClaim,
                     at: Optional[str]) -> tuple[str, ...]:
    """当前对该菜品生效的经营事实（含菜品级与共用设备级，如换油）。"""
    equipment = {c.equipment_id for c in dish.cross_contact}
    ids = [f.fact_id for f in claim.facts
           if f.active_at(at)
           and (f.dish_id == dish.dish_id
                or (f.equipment_id is not None and f.equipment_id in equipment))]
    return tuple(sorted(ids))


def nutritionist_review(*, package: ReviewPackage, signer: str, key: bytes,
                        advisories: Mapping[str, str],
                        at: Optional[str] = None,
                        valid_seconds: int = DEFAULT_VALID_SECONDS,
                        observed_batch_ids: Sequence[str] = (),
                        assessment_id: Optional[str] = None) -> RiskAssessment:
    """营养专业人员复核并签署风险结论。

    ``advisories`` 为 ``{目标语言: 提示文本}``，必须由专业人员撰写，
    不接受翻译链产物。存在不确定情形时拒绝出具确定结论。
    """
    if package.status == BLOCKED:
        raise UncertainCondition(package.block_reasons)
    moment = (now().isoformat() if at is None else at)
    dish = package.dish

    # 出具结论前再次检查（防止发起复核后情形发生变化）。
    reasons = uncertainty_reasons(package.claim, dish, at=moment)
    if reasons:
        raise UncertainCondition(reasons)

    allergens = set()
    for ing in dish.ingredients:
        allergens.update(ing.allergens)
    for aid in dish.processing_aids:
        allergens.update(aid.allergens)
    for contact in dish.cross_contact:
        allergens.update(contact.shared_allergens)

    fact_ids = _active_fact_ids(package.claim, dish, moment)
    lang = package.translation.target_lang
    if lang not in advisories:
        raise ReviewError(f"缺少目标语言 {lang} 的风险提示")
    if not advisories[lang].strip():
        raise ReviewError("风险提示必须由营养专业人员撰写，不能为空")

    payload = {
        "assessment_id": assessment_id or f"risk-{package.package_id}",
        "dish_id": dish.dish_id,
        "recipe_ref": dish.recipe_ref,
        "claim_revision": package.claim.revision,
        "target_lang": lang,
        "allergens": sorted(allergens),
        "advisories": [advisories[k] for k in sorted(advisories)],
        "reviewed_fact_ids": list(fact_ids),
        "reviewed_batch_ids": list(observed_batch_ids),
        "observed_at": moment,
        "valid_seconds": valid_seconds,
    }
    return RiskAssessment(
        assessment_id=payload["assessment_id"],
        dish_id=payload["dish_id"],
        recipe_ref=payload["recipe_ref"],
        claim_revision=payload["claim_revision"],
        target_lang=lang,
        allergens=tuple(payload["allergens"]),
        advisories=tuple(payload["advisories"]),
        reviewed_fact_ids=fact_ids,
        reviewed_batch_ids=tuple(observed_batch_ids),
        observed_at=moment,
        valid_seconds=valid_seconds,
        signature=Signature.create(NUTRITIONIST, signer, moment, payload, key),
    )


def merchant_confirm(*, package: ReviewPackage, signer: str, key: bytes,
                     at: Optional[str] = None,
                     valid_seconds: int = DEFAULT_VALID_SECONDS) -> MerchantConfirmation:
    """商户确认当前经营事实。

    售罄事实生效时返回 ``available=False``（不得附带安全结论）；
    复核包处于 blocked（识别/替换等不确定）时不接受商户确认。
    """
    if package.status == BLOCKED:
        raise UncertainCondition(package.block_reasons)
    moment = (now().isoformat() if at is None else at)
    dish = package.dish
    fact_ids = _active_fact_ids(package.claim, dish, moment)
    sold_out = bool(package.claim.active_facts(
        "sold_out", dish_id=dish.dish_id, at=moment))
    payload = {
        "dish_id": dish.dish_id,
        "recipe_ref": dish.recipe_ref,
        "claim_revision": package.claim.revision,
        "confirmed_fact_ids": list(fact_ids),
        "available": not sold_out,
        "at": moment,
        "valid_seconds": valid_seconds,
    }
    return MerchantConfirmation(
        dish_id=payload["dish_id"],
        recipe_ref=payload["recipe_ref"],
        claim_revision=payload["claim_revision"],
        confirmed_fact_ids=fact_ids,
        available=payload["available"],
        at=moment,
        valid_seconds=valid_seconds,
        signature=Signature.create(MERCHANT, signer, moment, payload, key),
    )


def finalize(*, package: ReviewPackage, risk: Optional[RiskAssessment] = None,
             merchant: Optional[MerchantConfirmation] = None,
             translator_key: Optional[bytes] = None,
             nutritionist_key: Optional[bytes] = None,
             merchant_key: Optional[bytes] = None,
             at: Optional[str] = None) -> ReviewPackage:
    """汇总三方签署，产出定稿状态。

    * 识别/替换等不确定：保持 ``blocked``；
    * 商户确认售罄：转为 ``sold_out``，不展示安全结论；
    * 任一签署缺失、过期或绑定的配方版本/声明修订不一致：拒绝定稿；
    * 全部通过：``ready``，可发布为唯一公开版本。
    """
    moment = (now() if at is None else parse(at))
    moment_iso = moment.isoformat()
    dish = package.dish

    if package.block_reasons:
        return ReviewPackage(
            package.package_id, package.claim, dish,
            package.image_fingerprint, package.recognition, package.translation,
            risk, merchant, package.block_reasons, BLOCKED)

    if not merchant.available:
        return ReviewPackage(
            package.package_id, package.claim, dish,
            package.image_fingerprint, package.recognition, package.translation,
            risk, merchant, (), SOLD_OUT)

    if translator_key is not None:
        sig = package.translation.translator_signature
        if sig is None or not sig.verify(package.translation.sign_payload(), translator_key):
            raise ReviewError("译者签名校验失败")
    if nutritionist_key is not None and not risk.verify(nutritionist_key):
        raise ReviewError("营养专业人员签名校验失败")
    if merchant_key is not None and not merchant.verify(merchant_key):
        raise ReviewError("商户签名校验失败")

    checks = (
        (risk.dish_id, dish.dish_id, "risk 绑定了不同菜品"),
        (merchant.dish_id, dish.dish_id, "商户确认绑定了不同菜品"),
        (risk.recipe_ref, dish.recipe_ref, "risk 绑定的配方版本已变化"),
        (merchant.recipe_ref, dish.recipe_ref, "商户确认绑定的配方版本已变化"),
        (risk.claim_revision, package.claim.revision, "risk 基于过期的声明修订"),
        (merchant.claim_revision, package.claim.revision, "商户确认基于过期的声明修订"),
        (package.translation.recipe_ref, dish.recipe_ref, "译文绑定的配方版本已变化"),
    )
    for actual, expected, message in checks:
        if actual != expected:
            raise ReviewError(message)

    if not risk.fresh_at(moment_iso):
        raise ReviewError("营养复核结论已过时效，需要重新复核")
    if not merchant.fresh_at(moment_iso):
        raise ReviewError("商户确认已过时效，需要重新确认")

    # 换油等事实若发生在复核纳入快照之后，结论立即失效。
    current_facts = set(_active_fact_ids(package.claim, dish, moment_iso))
    if set(risk.reviewed_fact_ids) != current_facts:
        raise ReviewError("经营事实已变化（如当日换油），风险结论必须重新复核")

    return ReviewPackage(
        package.package_id, package.claim, dish,
        package.image_fingerprint, package.recognition, package.translation,
        risk, merchant, (), READY)


def mark_stale(package: ReviewPackage) -> ReviewPackage:
    """事实/版本变化后把历史定稿标记为 stale（保留记录用于复盘）。"""
    if package.status != READY:
        return package
    return ReviewPackage(
        package.package_id, package.claim, package.dish,
        package.image_fingerprint, package.recognition, package.translation,
        package.risk, package.merchant, package.block_reasons, STALE)
