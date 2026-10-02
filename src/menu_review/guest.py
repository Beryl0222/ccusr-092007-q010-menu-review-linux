"""游客侧：已保存内容的有效性标注与可追问回答。

游客收藏时保存的是当时公开版本的**快照**。事后再次查看时，必须清楚标出
它是否仍有效（仍在时效内 / 已更正 / 已撤回 / 已过期），并指向当前公开
版本；绝不让游客把一周前的过敏原说明当作今天的结论。

追问回答只能引用已确认公开版本中由专业人员撰写的内容，并且同样带时效；
时效之外或版本已失效时，不给出确定回答。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .publishing import (CORRECTED, PublicPayload, PublishRegistry,
                         PublishedVersion, WITHDRAWN)
from .timeutil import now, parse

VALID = "valid"                       # 版本生效且仍在时效内
EXPIRED = "expired"                   # 版本仍 active，但风险结论已过时效
SUPERSEDED = "superseded"             # 已被紧急更正替代
REMOVED = "removed"                   # 已撤回

# 允许追问的问题码
QUESTION_ALLERGENS = "allergens"
QUESTION_ADVISORY = "advisory"
_ALLOWED_QUESTIONS = frozenset({QUESTION_ALLERGENS, QUESTION_ADVISORY})


@dataclass(frozen=True)
class SavedItem:
    tourist_id: str
    version_id: str
    snapshot: PublicPayload
    saved_at: str

    def view(self, registry: PublishRegistry, *, at: Optional[str] = None) -> "SavedItemView":
        moment = (now() if at is None else at)
        try:
            current = registry.get(self.version_id)
        except KeyError:
            return SavedItemView(self, REMOVED, False, None, moment)

        canonical = registry.canonical(current.menu_id, current.dish_id, current.target_lang)
        canonical_id = canonical.version_id if canonical else None
        if current.status == WITHDRAWN:
            return SavedItemView(self, REMOVED, False, None, moment)
        if current.status == CORRECTED:
            return SavedItemView(self, SUPERSEDED, False, current.superseded_by, moment)
        if parse(self.snapshot.valid_until) < parse(moment):
            return SavedItemView(self, EXPIRED, False, canonical_id, moment)
        return SavedItemView(self, VALID, True, canonical_id, moment)


@dataclass(frozen=True)
class SavedItemView:
    item: SavedItem
    status: str
    still_valid: bool
    current_version_id: Optional[str]
    checked_at: str


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    certain: bool           # False 时为说明性回答，不得作为安全结论
    valid_until: Optional[str]
    source_version_id: str
    answered_at: str


def _saved_key(tourist_id: str, version_id: str) -> str:
    return f"{tourist_id}:{version_id}"


class GuestService:
    def __init__(self, registry: PublishRegistry):
        self.registry = registry
        self._saved: dict[str, SavedItem] = {}

    def save(self, tourist_id: str, version: PublishedVersion, *,
             at: Optional[str] = None) -> SavedItem:
        moment = (now().isoformat() if at is None else at)
        item = SavedItem(tourist_id, version.version_id, version.payload, moment)
        self._saved[_saved_key(tourist_id, version.version_id)] = item
        return item

    def check_saved(self, tourist_id: str, version_id: str,
                    *, at: Optional[str] = None) -> SavedItemView:
        item = self._saved[_saved_key(tourist_id, version_id)]
        return item.view(self.registry, at=at)

    def ask(self, *, version_id: str, question: str,
            at: Optional[str] = None) -> Answer:
        """对已发布版本追问；证据不足或失效时返回不确定回答。"""
        moment = (now().isoformat() if at is None else at)
        if question not in _ALLOWED_QUESTIONS:
            return Answer(question,
                          "该问题无法依据已确认信息回答，请向餐厅工作人员确认。",
                          False, None, version_id, moment)
        try:
            version = self.registry.get(version_id)
        except KeyError:
            return Answer(question, "该信息已不可用，请向餐厅工作人员确认。",
                          False, None, version_id, moment)

        if version.status == WITHDRAWN:
            return Answer(question, "该菜品信息已被撤回，请向餐厅工作人员确认。",
                          False, None, version_id, moment)
        if version.status == CORRECTED:
            return Answer(question,
                          "该信息已被紧急更正，请查看最新版本后再作判断。",
                          False, None, version.superseded_by, moment)
        if parse(version.payload.valid_until) < parse(moment):
            return Answer(question,
                          "过敏原与风险信息已过时效，需要餐厅与专业人员重新确认，"
                          "请向餐厅工作人员确认。",
                          False, None, version_id, moment)

        if question == QUESTION_ALLERGENS:
            text = ("含过敏原: " + "、".join(version.payload.allergens)
                    if version.payload.allergens else "未申报过敏原，仍请以现场确认为准。")
        else:
            text = " ".join(version.payload.advisories) or "暂无更多风险提示。"
        return Answer(question, text, True, version.payload.valid_until,
                      version_id, moment)
