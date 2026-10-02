"""译审状态机：拍照 → 机译候选 → 三方签署 → 发布 / 挂起待核。

角色边界（流程的硬约束）：

- 机译产出只能停留在 ``CANDIDATE`` 待审候选，不能自行发布；
- 译者（TRANSLATOR）只对语义负责；
- 营养专业人员（NUTRITION）复核过敏原与交叉接触风险；
- 商户（MERCHANT）确认当前经营事实（批次、换油、供餐时段）；
- 季节替换、临时售罄、一菜多名、OCR 置信不足任一存在时，
  只能挂起为 ``HELD_UNCERTAIN``，不附带任何确定的安全结论，
  必须保留一条可追问（FollowUp）通道。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from .contracts import DomainRecord, MenuClaim

# OCR 总体置信度阈值；低于该值不得自动归并菜单，也不得生成确定结论。
OCR_CONFIDENCE_THRESHOLD = 0.90


class ReviewState(str, Enum):
    CAPTURED = "captured"                    # 仅登记原图指纹
    CANDIDATE = "candidate"                  # 机译待审候选（自动翻译的终点）
    IN_REVIEW = "in_review"                  # 已有人工签署，签署未齐
    PUBLISHED = "published"                  # 三方签署齐全、结论确定的公开版本
    HELD_UNCERTAIN = "held_uncertain"        # 不确定：仅可展示"待核实+追问"
    SUPERSEDED = "superseded"                # 被更高 revision 的更正版本取代
    WITHDRAWN = "withdrawn"                  # 撤回


class Role(str, Enum):
    TRANSLATOR = "translator"                # 语义
    NUTRITION = "nutrition"                  # 风险
    MERCHANT = "merchant"                    # 当前经营事实


class Certainty(str, Enum):
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"


class UncertaintyReason(str, Enum):
    SEASONAL_SUBSTITUTION = "seasonal_substitution"
    TEMPORARILY_SOLD_OUT = "temporarily_sold_out"
    MULTIPLE_NAMES_MATCH = "multiple_names_match"
    LOW_OCR_CONFIDENCE = "low_ocr_confidence"


# 唯一合法的签署顺序：语义 → 风险 → 事实。
SIGN_ORDER = (Role.TRANSLATOR, Role.NUTRITION, Role.MERCHANT)


class WorkflowError(ValueError):
    """违反译审流程约束。"""


def fingerprint_image(data: bytes) -> str:
    """原图指纹（sha256 十六进制）；同一图片重复拍摄得到同一指纹。"""

    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RecognizedName:
    value: str
    lang: str
    confidence: float


@dataclass(frozen=True)
class Capture:
    """一次菜单拍照；指纹是后续所有复盘的锚点。"""

    image_fingerprint: str
    restaurant_id: str
    taken_at: str
    recognized_names: tuple[RecognizedName, ...]
    ocr_confidence: float
    seasonal_substitution: bool = False
    sold_out: bool = False


@dataclass(frozen=True)
class TranslationLink:
    """翻译链中的一环；机译环 actor 为 None，人工环必须有译者。"""

    stage: str            # "machine" | "human"
    source_lang: str
    target_lang: str
    source_text: str
    output_text: str
    engine: str | None
    actor: str | None
    at: str


@dataclass(frozen=True)
class Signature:
    role: Role
    signer: str
    at: str
    claim_revision: int
    recipe_version: int
    image_fingerprint: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FollowUp:
    """游客追问；挂起版本必须保持至少一条开放追问。"""

    question: str
    lang: str
    asked_at: str
    answer: str | None = None
    answer_lang: str | None = None
    answered_by: Role | None = None
    answered_at: str | None = None

    @property
    def open(self) -> bool:
        return self.answer is None


@dataclass
class ReviewCase:
    envelope: DomainRecord
    claim: MenuClaim
    capture: Capture
    state: ReviewState
    chain: list[TranslationLink] = field(default_factory=list)
    signatures: dict[Role, Signature] = field(default_factory=dict)
    uncertainty: set[UncertaintyReason] = field(default_factory=set)
    follow_ups: list[FollowUp] = field(default_factory=list)
    events: list[tuple[str, ReviewState, str]] = field(default_factory=list)

    def _transition(self, state: ReviewState, at: str, note: str) -> None:
        self.state = state
        self.events.append((at, state, note))

    @property
    def menu_id(self) -> str:
        return self.claim.menu_id

    def open_follow_ups(self) -> tuple[FollowUp, ...]:
        return tuple(item for item in self.follow_ups if item.open)


def open_case(
    envelope: DomainRecord,
    claim: MenuClaim,
    capture: Capture,
    uncertainty: set[UncertaintyReason],
    at: str,
) -> ReviewCase:
    """从一次拍照开立译审单；不确定因素在入口即登记，不得事后悄悄清除。"""

    case = ReviewCase(
        envelope=envelope,
        claim=claim,
        capture=capture,
        state=ReviewState.CAPTURED,
        uncertainty=set(uncertainty),
    )
    note = "开立译审单"
    if uncertainty:
        note += "；不确定因素：" + ",".join(sorted(item.value for item in uncertainty))
    case.events.append((at, ReviewState.CAPTURED, note))
    return case


def machine_translate(
    case: ReviewCase,
    *,
    source_lang: str,
    outputs: dict[str, str],
    source_text: str,
    engine: str,
    at: str,
) -> None:
    """自动翻译：写入翻译链并停在 CANDIDATE，不产生任何签署。"""

    if case.state not in (ReviewState.CAPTURED, ReviewState.CANDIDATE):
        raise WorkflowError("仅拍照后的译审单可以接受机译候选")
    for target_lang, text in sorted(outputs.items()):
        case.chain.append(
            TranslationLink(
                stage="machine",
                source_lang=source_lang,
                target_lang=target_lang,
                source_text=source_text,
                output_text=text,
                engine=engine,
                actor=None,
                at=at,
            )
        )
    case._transition(ReviewState.CANDIDATE, at, f"机译候选写入（{engine}），等待人工译审")


def _require_state(case: ReviewCase, *allowed: ReviewState) -> None:
    if case.state not in allowed:
        raise WorkflowError(f"当前状态 {case.state.value} 不允许该操作")


def translator_review(
    case: ReviewCase,
    *,
    signer: str,
    corrections: dict[str, str],
    at: str,
) -> None:
    """译者修订译语文案并对语义签署；没有机译候选不得签署。"""

    _require_state(case, ReviewState.CANDIDATE, ReviewState.IN_REVIEW)
    if not case.chain:
        raise WorkflowError("缺少机译候选，译者无从译审")
    if Role.TRANSLATOR in case.signatures:
        raise WorkflowError("语义签署已存在，不得重复签署")
    machine_links = {
        link.target_lang: link for link in case.chain if link.stage == "machine"
    }
    for target_lang, text in sorted(corrections.items()):
        source = machine_links.get(target_lang)
        case.chain.append(
            TranslationLink(
                stage="human",
                source_lang=source.source_lang if source else "zh-Hans",
                target_lang=target_lang,
                source_text=source.source_text if source else text,
                output_text=text,
                engine=None,
                actor=signer,
                at=at,
            )
        )
    case.signatures[Role.TRANSLATOR] = Signature(
        role=Role.TRANSLATOR,
        signer=signer,
        at=at,
        claim_revision=envelope_revision(case),
        recipe_version=case.claim.dish.recipe_version,
        image_fingerprint=case.capture.image_fingerprint,
        detail={"languages": sorted(corrections)},
    )
    case._transition(ReviewState.IN_REVIEW, at, f"译者 {signer} 完成语义签署")


def nutrition_review(
    case: ReviewCase,
    *,
    signer: str,
    risk_statement_i18n: dict[str, str],
    at: str,
) -> None:
    """营养专业人员复核原料、加工助剂与交叉接触风险并签署。

    风险陈述只允许陈述事实（含哪些过敏原、与何种食品共用设备），
    不得出现"可安全食用 / allergen-free"式保证结论。
    """

    _require_state(case, ReviewState.IN_REVIEW)
    if Role.TRANSLATOR not in case.signatures:
        raise WorkflowError("风险签署必须先有语义签署")
    if Role.NUTRITION in case.signatures:
        raise WorkflowError("风险签署已存在，不得重复签署")
    forbidden = ("safe to eat", "allergen-free", "无过敏原", "绝对安全", "可安全食用")
    for lang, text in risk_statement_i18n.items():
        lowered = text.casefold()
        if any(word in lowered for word in forbidden):
            raise WorkflowError("风险陈述不得给出确定性安全保证")
    case.signatures[Role.NUTRITION] = Signature(
        role=Role.NUTRITION,
        signer=signer,
        at=at,
        claim_revision=envelope_revision(case),
        recipe_version=case.claim.dish.recipe_version,
        image_fingerprint=case.capture.image_fingerprint,
        detail={"risk_statement_i18n": dict(risk_statement_i18n)},
    )
    case._transition(ReviewState.IN_REVIEW, at, f"营养专业人员 {signer} 完成风险签署")


def merchant_confirm(
    case: ReviewCase,
    *,
    signer: str,
    confirmed_operations: dict[str, str],
    at: str,
) -> None:
    """商户确认当前经营事实（供餐时段、炸锅换油时间等）并签署。"""

    _require_state(case, ReviewState.IN_REVIEW)
    if Role.NUTRITION not in case.signatures:
        raise WorkflowError("事实签署必须先有风险签署")
    if Role.MERCHANT in case.signatures:
        raise WorkflowError("事实签署已存在，不得重复签署")
    ops = case.claim.operations
    confirmed = dict(confirmed_operations)
    confirmed.setdefault("served_from", ops.served_from)
    confirmed.setdefault("served_until", ops.served_until)
    if ops.fryer_oil_changed_at:
        confirmed.setdefault("fryer_oil_changed_at", ops.fryer_oil_changed_at)
    case.signatures[Role.MERCHANT] = Signature(
        role=Role.MERCHANT,
        signer=signer,
        at=at,
        claim_revision=envelope_revision(case),
        recipe_version=case.claim.dish.recipe_version,
        image_fingerprint=case.capture.image_fingerprint,
        detail={"confirmed_operations": confirmed},
    )
    case._transition(ReviewState.IN_REVIEW, at, f"商户 {signer} 确认当前经营事实")


def ask_follow_up(case: ReviewCase, *, question: str, lang: str, at: str) -> FollowUp:
    item = FollowUp(question=question, lang=lang, asked_at=at)
    case.follow_ups.append(item)
    return item


def answer_follow_up(
    case: ReviewCase,
    *,
    question: FollowUp,
    answer: str,
    answer_lang: str,
    by: Role,
    at: str,
) -> None:
    idx = case.follow_ups.index(question)
    case.follow_ups[idx] = FollowUp(
        question=question.question,
        lang=question.lang,
        asked_at=question.asked_at,
        answer=answer,
        answer_lang=answer_lang,
        answered_by=by,
        answered_at=at,
    )


def resolve_uncertainty(
    case: ReviewCase,
    *,
    reason: UncertaintyReason,
    resolver: Role,
    at: str,
    note: str,
) -> None:
    """清除一项不确定因素。

    只允许在事实已被确认（如临时售罄恢复、季节替换后的新声明到位）时调用；
    任何改变原料/配方的更正都应以新的 ``revision`` 重新开单，而不是就地清除。
    """

    if reason not in case.uncertainty:
        raise WorkflowError(f"不确定因素 {reason.value} 不存在或已清除")
    case.uncertainty.remove(reason)
    case.events.append(
        (at, case.state, f"{resolver.value} 解除 {reason.value}：{note}")
    )


def publish(case: ReviewCase, *, at: str) -> Certainty:
    """发布。返回确定性级别；不确定时挂起而不是拼出安全结论。

    已挂起的单子在不确定因素解除、签署补齐后可再次调用，转为正式发布；
    仍有不确定因素时保持挂起（每次尝试都留痕）。
    """

    _require_state(
        case,
        ReviewState.CANDIDATE,
        ReviewState.IN_REVIEW,
        ReviewState.HELD_UNCERTAIN,
    )
    if not case.signatures:
        raise WorkflowError("自动翻译候选不得直接发布")
    if case.uncertainty:
        if not case.open_follow_ups():
            ask_follow_up(
                case,
                question="该菜品信息尚待核实，请向餐厅确认当日原料与设备情况。",
                lang="zh-Hans",
                at=at,
            )
        if case.state is not ReviewState.HELD_UNCERTAIN:
            case._transition(
                ReviewState.HELD_UNCERTAIN,
                at,
                "存在不确定因素，挂起为待核实版本，不提供确定安全结论",
            )
        else:
            case.events.append(
                (at, ReviewState.HELD_UNCERTAIN, "不确定因素仍在，继续挂起")
            )
        return Certainty.UNCERTAIN
    missing = [role.value for role in SIGN_ORDER if role not in case.signatures]
    if missing:
        raise WorkflowError(f"三方签署不齐，缺少：{','.join(missing)}")
    case._transition(ReviewState.PUBLISHED, at, "不确定因素已解除且三方签署齐全，发布为确定公开版本")
    return Certainty.CONFIRMED


def mark_superseded(case: ReviewCase, *, at: str, by_revision: int) -> None:
    if case.state not in (ReviewState.PUBLISHED, ReviewState.HELD_UNCERTAIN):
        raise WorkflowError("只有已发布版本可被更正取代")
    case._transition(
        ReviewState.SUPERSEDED, at, f"被 revision {by_revision} 的更正版本取代"
    )


def withdraw(case: ReviewCase, *, at: str, reason: str) -> None:
    if case.state not in (ReviewState.PUBLISHED, ReviewState.HELD_UNCERTAIN):
        raise WorkflowError("只有已发布版本可以撤回")
    case._transition(ReviewState.WITHDRAWN, at, f"紧急撤回：{reason}")


def envelope_revision(case: ReviewCase) -> int:
    return case.envelope.revision
