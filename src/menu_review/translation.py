"""翻译链：自动翻译只能生成待审候选，语义由译者负责。

链条上的每一跳都记录角色、引擎/模型版本、输入输出与时间，并对前一跳
做哈希链接；整条链的哈希进入审核复盘材料，无法在事后悄悄替换某一跳。

角色约定：

``mt``
    机器翻译，产物只能是 ``pending_review`` 候选；不得包含过敏原/安全结论。
``post_editor``
    译后编辑，修改候选文本，仍不能替代译者签署。
``translator``
    人工译者，只有译者签署后的译文才能进入三方签署。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .claims import DishClaim, MenuClaim
from .signing import Signature
from .timeutil import now

MT_ROLE = "mt"
POST_EDITOR_ROLE = "post_editor"
TRANSLATOR_ROLE = "translator"

CANDIDATE = "pending_review"
TRANSLATOR_SIGNED = "translator_signed"


class TranslationError(ValueError):
    pass


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TranslationHop:
    seq: int
    role: str
    actor: str           # 人员账号，或 mt 的引擎标识
    tool_version: str    # 引擎/模型版本；人工环节为编辑工具版本或 "manual"
    source_lang: str
    target_lang: str
    input_text: str
    output_text: str
    at: str
    prev_hash: str
    hop_hash: str

    @classmethod
    def make(cls, *, seq: int, role: str, actor: str, tool_version: str,
             source_lang: str, target_lang: str, input_text: str,
             output_text: str, at: str, prev_hash: str) -> "TranslationHop":
        if not output_text or not output_text.strip():
            raise TranslationError("翻译输出不能为空")
        body = canonical_json({
            "seq": seq, "role": role, "actor": actor, "tool_version": tool_version,
            "source_lang": source_lang, "target_lang": target_lang,
            "input_text": input_text, "output_text": output_text,
            "at": at, "prev_hash": prev_hash,
        })
        return cls(seq, role, actor, tool_version, source_lang, target_lang,
                   input_text, output_text, at, prev_hash, _hash(body))


@dataclass(frozen=True)
class LocalizedText:
    """一段可译内容。``safety`` 标记的字段禁止机翻直出安全结论。"""

    field_id: str
    text: str
    safety: bool = False


@dataclass(frozen=True)
class Translation:
    """一条译文及其完整翻译链。"""

    menu_id: str
    dish_id: str
    recipe_ref: str          # 译文绑定的具体配方版本
    claim_revision: int      # 译文绑定的菜单声明修订号
    target_lang: str
    fields: tuple[LocalizedText, ...]
    hops: tuple[TranslationHop, ...]
    status: str
    translator_signature: Optional[Signature] = None

    @property
    def chain_hash(self) -> str:
        return self.hops[-1].hop_hash if self.hops else ""

    def sign_payload(self) -> dict[str, Any]:
        """译者签名覆盖的内容：业务字段 + 整条链哈希。"""
        return {
            "menu_id": self.menu_id,
            "dish_id": self.dish_id,
            "recipe_ref": self.recipe_ref,
            "claim_revision": self.claim_revision,
            "target_lang": self.target_lang,
            "fields": [[f.field_id, f.text, f.safety] for f in self.fields],
            "chain_hash": self.chain_hash,
        }

    def field_text(self, field_id: str) -> Optional[str]:
        for f in self.fields:
            if f.field_id == field_id:
                return f.text
        return None

    def _rebind_fields(self, fields: Sequence[LocalizedText]) -> "Translation":
        return Translation(
            self.menu_id, self.dish_id, self.recipe_ref, self.claim_revision,
            self.target_lang, tuple(fields), self.hops, self.status)


def _source_fields(dish: DishClaim) -> tuple[LocalizedText, ...]:
    # 名称与文化故事可自动翻译；过敏原/交叉接触字段标记 safety，
    # 只能由营养专业人员复核后填充，机翻候选中一律不产出。
    return (
        LocalizedText("name", dish.names[0], safety=False),
    )


def machine_translate(*, claim: MenuClaim, dish: DishClaim, target_lang: str,
                      engine: str, engine_version: str,
                      translate, source_lang: str = "zh",
                      at: Optional[str] = None) -> Translation:
    """生成机器翻译候选。

    ``translate(text, source_lang, target_lang)`` 由调用方接入实际引擎；
    返回的译文状态恒为 ``pending_review``，且不包含任何 safety 字段。
    """
    if target_lang == source_lang:
        raise TranslationError("目标语言不能与源语言相同")
    moment = (now().isoformat() if at is None else at)
    source = _source_fields(dish)
    fields: list[LocalizedText] = []
    hops: list[TranslationHop] = []
    for local in source:
        assert not local.safety  # safety 字段绝不进入机翻
        output = translate(local.text, source_lang, target_lang)
        prev = hops[-1].hop_hash if hops else ""
        hops.append(TranslationHop.make(
            seq=len(hops), role=MT_ROLE, actor=engine, tool_version=engine_version,
            source_lang=source_lang, target_lang=target_lang, input_text=local.text,
            output_text=output, at=moment, prev_hash=prev))
        fields.append(LocalizedText(local.field_id, output, safety=False))
    return Translation(
        menu_id=claim.menu_id, dish_id=dish.dish_id, recipe_ref=dish.recipe_ref,
        claim_revision=claim.revision, target_lang=target_lang,
        fields=tuple(fields), hops=tuple(hops), status=CANDIDATE)


def post_edit(*, candidate: Translation, editor: str, field_id: str,
              output_text: str, at: Optional[str] = None) -> Translation:
    """译后编辑：在链条上追加一跳，候选仍待译者签署。"""
    if candidate.status != CANDIDATE:
        raise TranslationError("只有待审候选可以被译后编辑")
    current = candidate.field_text(field_id)
    if current is None:
        raise TranslationError(f"候选中不存在字段 {field_id}")
    moment = (now().isoformat() if at is None else at)
    hop = TranslationHop.make(
        seq=len(candidate.hops), role=POST_EDITOR_ROLE, actor=editor,
        tool_version="manual", source_lang=candidate.target_lang,
        target_lang=candidate.target_lang, input_text=current,
        output_text=output_text, at=moment, prev_hash=candidate.chain_hash)
    fields = tuple(
        LocalizedText(field_id, output_text, f.safety) if f.field_id == field_id else f
        for f in candidate.fields)
    return Translation(
        candidate.menu_id, candidate.dish_id, candidate.recipe_ref,
        candidate.claim_revision, candidate.target_lang, fields,
        candidate.hops + (hop,), CANDIDATE)


def sign_translation(*, candidate: Translation, translator: str, key: bytes,
                     cultural_story_target: Optional[str] = None,
                     at: Optional[str] = None) -> Translation:
    """译者对语义负责：追加译者签署跳并签名，译文转为 translator_signed。

    文化故事（非安全内容）可在此一并交付；译者必须确认链条上的全部文本。
    """
    if candidate.status != CANDIDATE:
        raise TranslationError("译者只能签署待审候选")
    if not any(h.role in (MT_ROLE, POST_EDITOR_ROLE) for h in candidate.hops):
        raise TranslationError("译者签署前必须存在待审翻译链")
    moment = (now().isoformat() if at is None else at)
    fields = list(candidate.fields)
    if cultural_story_target is not None:
        source_story = "（文化故事由商户提供，见声明附件）"
        hop = TranslationHop.make(
            seq=len(candidate.hops), role=TRANSLATOR_ROLE, actor=translator,
            tool_version="manual", source_lang="zh",
            target_lang=candidate.target_lang, input_text=source_story,
            output_text=cultural_story_target, at=moment,
            prev_hash=candidate.chain_hash)
        fields.append(LocalizedText("cultural_story", cultural_story_target, safety=False))
        hops = candidate.hops + (hop,)
    else:
        sign_hop = TranslationHop.make(
            seq=len(candidate.hops), role=TRANSLATOR_ROLE, actor=translator,
            tool_version="manual", source_lang=candidate.target_lang,
            target_lang=candidate.target_lang,
            input_text="\n".join(f.text for f in candidate.fields),
            output_text="\n".join(f.text for f in candidate.fields),
            at=moment, prev_hash=candidate.chain_hash)
        hops = candidate.hops + (sign_hop,)
    unsigned = Translation(
        candidate.menu_id, candidate.dish_id, candidate.recipe_ref,
        candidate.claim_revision, candidate.target_lang, tuple(fields),
        hops, TRANSLATOR_SIGNED)
    signature = Signature.create(
        TRANSLATOR_ROLE, translator, moment, unsigned.sign_payload(), key)
    return Translation(
        unsigned.menu_id, unsigned.dish_id, unsigned.recipe_ref,
        unsigned.claim_revision, unsigned.target_lang, unsigned.fields,
        unsigned.hops, unsigned.status, signature)
