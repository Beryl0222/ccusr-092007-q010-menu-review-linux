"""拍照摄入：把 OCR 结果与菜单声明匹配，在入口判定不确定因素。

任何下列情况都必须挂上不确定标记，后续发布只能进入待核实挂起：

- 图片识别总体置信不足（低于 :data:`OCR_CONFIDENCE_THRESHOLD`）；
- 一菜多名：识别文字同时匹配同一菜单上的多个菜品；
- 季节替换（当日菜单有替换提示）；
- 临时售罄。

匹配不到任何菜品时不得开立译审单——宁可没有结论，也不拼结论。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import DomainRecord, MenuClaim
from .workflow import (
    OCR_CONFIDENCE_THRESHOLD,
    Capture,
    RecognizedName,
    UncertaintyReason,
    fingerprint_image,
)


@dataclass(frozen=True)
class IntakeResult:
    envelope: DomainRecord
    claim: MenuClaim
    capture: Capture
    uncertainty: frozenset[UncertaintyReason]
    matched_dish_ids: tuple[str, ...]


def _normalize(text: str) -> str:
    return "".join(text.casefold().split())


def _match_dishes(
    entries: list[tuple[DomainRecord, MenuClaim]],
    recognized: tuple[RecognizedName, ...],
) -> tuple[str, ...]:
    needles = {_normalize(item.value) for item in recognized if item.value.strip()}
    matched: list[str] = []
    for _, claim in entries:
        names = {_normalize(name.value) for name in claim.dish.names}
        if needles & names:
            matched.append(claim.dish.dish_id)
    return tuple(matched)


def intake_capture(
    entries: list[tuple[DomainRecord, MenuClaim]],
    *,
    image: bytes,
    recognized: tuple[RecognizedName, ...],
    ocr_confidence: float,
    taken_at: str,
    seasonal_substitution: bool = False,
    sold_out: bool = False,
) -> IntakeResult:
    if not entries:
        raise ValueError("没有可匹配的菜单声明")
    restaurant_ids = {claim.restaurant_id for _, claim in entries}
    if len(restaurant_ids) != 1:
        raise ValueError("一次拍摄只能归属于一家餐厅")

    uncertainty: set[UncertaintyReason] = set()
    if ocr_confidence < OCR_CONFIDENCE_THRESHOLD:
        uncertainty.add(UncertaintyReason.LOW_OCR_CONFIDENCE)
    if seasonal_substitution:
        uncertainty.add(UncertaintyReason.SEASONAL_SUBSTITUTION)
    if sold_out:
        uncertainty.add(UncertaintyReason.TEMPORARILY_SOLD_OUT)

    matched = _match_dishes(entries, recognized)
    if not matched:
        raise ValueError("识别文字未匹配到任何菜品，不得生成结论")
    if len(matched) > 1:
        uncertainty.add(UncertaintyReason.MULTIPLE_NAMES_MATCH)

    # 挂起单也绑定一个候选菜品，但因存在不确定标记，绝不会产出确定结论。
    envelope, claim = next(
        (env, claim)
        for env, claim in entries
        if claim.dish.dish_id == matched[0]
    )
    capture = Capture(
        image_fingerprint=fingerprint_image(image),
        restaurant_id=next(iter(restaurant_ids)),
        taken_at=taken_at,
        recognized_names=recognized,
        ocr_confidence=ocr_confidence,
        seasonal_substitution=seasonal_substitution,
        sold_out=sold_out,
    )
    return IntakeResult(
        envelope=envelope,
        claim=claim,
        capture=capture,
        uncertainty=frozenset(uncertainty),
        matched_dish_ids=matched,
    )
