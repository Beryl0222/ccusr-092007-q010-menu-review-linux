"""原图指纹与拍照识别身份。

同一菜单被反复拍摄时，照片本身不同（指纹不同），但只能指向同一项
经过确认的公开版本；识别置信度不足时返回不确定结果，调用方不得据此
拼出确定的安全结论。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

# 低于该置信度只给候选，不允许生成确定结论
CONFIDENT_MATCH_THRESHOLD = 0.9
# 低于该置信度连候选都不采用
MIN_CANDIDATE_THRESHOLD = 0.5

_NORMALIZE = re.compile(r"[\s　·・\-—–()（）]+")


def fingerprint(image_bytes: bytes) -> str:
    """SHA-256 原图指纹，复盘时可核对照片是否为原图。"""
    return "sha256:" + hashlib.sha256(image_bytes).hexdigest()


def normalize_name(name: str) -> str:
    return _NORMALIZE.sub("", name).casefold()


@dataclass(frozen=True)
class NameCandidate:
    dish_id: str
    confidence: float
    via_alias: bool


@dataclass(frozen=True)
class RecognitionResult:
    """一次拍照识别的结果。

    ``matched_dish_id`` 为 None 即表示置信不足或一菜多名存在歧义，
    此时 ``reasons`` 说明不能给出确定结论的原因；候选仍可用于追问澄清。
    """

    image_fingerprint: str
    matched_dish_id: Optional[str]
    confidence: float
    candidates: tuple[NameCandidate, ...]
    reasons: tuple[str, ...]

    @property
    def is_confident(self) -> bool:
        return self.matched_dish_id is not None and not self.reasons


def match_names(
    *,
    image_bytes: bytes,
    observed_name: str,
    dishes: Sequence["object"],
    confidence: float,
    extra_aliases: Optional[Mapping[str, Sequence[str]]] = None,
) -> RecognitionResult:
    """根据 OCR 得到的菜名与置信度做身份匹配。

    ``dishes`` 为 :class:`~menu_review.claims.DishClaim` 序列；
    ``extra_aliases`` 提供菜名的其他写法/外语写法（一菜多名）。
    置信度由识别方给出，本模块不会自行抬高它。
    """
    fp = fingerprint(image_bytes)
    reasons: list[str] = []

    exact: list[str] = []
    alias_hits: dict[str, bool] = {}
    key = normalize_name(observed_name)
    for dish in dishes:
        names = list(dish.names)
        if extra_aliases and dish.dish_id in extra_aliases:
            names.extend(extra_aliases[dish.dish_id])
        normalized = [normalize_name(n) for n in names]
        if key in normalized:
            exact.append(dish.dish_id)
            alias_hits[dish.dish_id] = (normalize_name(observed_name)
                                        not in [normalize_name(n) for n in dish.names])

    candidates = tuple(
        NameCandidate(dish_id=dish_id, confidence=confidence,
                      via_alias=alias_hits.get(dish_id, False))
        for dish_id in dict.fromkeys(exact)
    )

    if confidence < MIN_CANDIDATE_THRESHOLD:
        reasons.append("recognition_low_confidence")
        return RecognitionResult(fp, None, confidence, (), tuple(reasons))

    if confidence < CONFIDENT_MATCH_THRESHOLD:
        reasons.append("recognition_below_confident_threshold")

    if len(candidates) > 1:
        reasons.append("multiple_dishes_share_name")
    elif not candidates:
        reasons.append("name_not_on_menu")

    matched = None
    if len(candidates) == 1 and confidence >= CONFIDENT_MATCH_THRESHOLD:
        matched = candidates[0].dish_id

    return RecognitionResult(fp, matched, confidence, candidates, tuple(reasons))
