"""公开版本登记处。

核心约束：

- 同一菜单被反复拍摄（不同图片指纹指向同一 ``menu_id``）只能指向
  **一项** 经过确认的公开版本；新图片只登记指纹，不产生新的公开版本。
- 公开投影 :class:`PublicVersion` 只包含游客需要的安全信息；
  ``confidential`` 原料仅保留风险标签，``confidential_recipe`` 整节剥离，
  竞争商家与游客看到同一份公开内容。
- 更正（新的 revision）先令旧版本与其译审单失效再登记新版本；撤回则无新版本。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime

from .caching import CacheBus
from .contracts import MenuClaim
from .workflow import (
    Certainty,
    ReviewCase,
    ReviewState,
    mark_superseded,
    withdraw,
)


@dataclass(frozen=True)
class PublicItem:
    code: str
    label: dict[str, str]
    allergens: tuple[str, ...]
    confidential: bool


@dataclass(frozen=True)
class PublicCrossContact:
    kind: str
    allergens: tuple[str, ...]
    shared_with: tuple[str, ...]
    note: dict[str, str]


@dataclass(frozen=True)
class PublicVersion:
    """对外展示的菜单版本；任何字段都无法反推出机密配方。"""

    menu_id: str
    record_id: str
    revision: int
    dish_id: str
    recipe_version: int
    names: tuple[tuple[str, str, bool], ...]
    ingredients: tuple[PublicItem, ...]
    processing_aids: tuple[PublicItem, ...]
    cross_contact: tuple[PublicCrossContact, ...]
    served_from: str
    served_until: str
    fryer_oil_changed_at: str | None
    certainty: Certainty
    published_at: str
    superseded_at: str | None = None
    withdrawn_at: str | None = None
    image_fingerprints: tuple[str, ...] = ()
    follow_up_open: bool = False

    @property
    def active(self) -> bool:
        return self.superseded_at is None and self.withdrawn_at is None

    def is_current(self, moment: datetime) -> bool:
        """该版本在给定时刻是否仍覆盖当日供应；供应窗口之外即为过期。"""

        if not self.active:
            return False
        now = moment.astimezone()
        return _parse(self.served_from) <= now <= _parse(self.served_until)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


# 机密原料在公开投影中的脱敏标签：只保留风险（过敏原），不暴露任何
# 可反推出配方或商户内部编号的信息，竞争商家与游客看到的内容相同。
REDACTED_LABELS = {
    "zh-Hans": "秘制配料（配方不公开）",
    "en": "house ingredient (recipe not disclosed)",
}


def _public_item(item) -> PublicItem:
    return PublicItem(
        code="<redacted>" if item.confidential else item.code,
        label=dict(REDACTED_LABELS) if item.confidential else dict(item.label.values),
        allergens=item.allergens,
        confidential=item.confidential,
    )


def project_public(
    case: ReviewCase,
    *,
    published_at: str,
    image_fingerprints: tuple[str, ...] = (),
) -> PublicVersion:
    """把签署完成的译审单投影为公开版本；机密节在此被剥离。

    菜名集合由声明原名与译者人工翻译链的产出合并而成；机译候选产出
    不进入公开版本（只保留为翻译链证据）。
    """

    claim: MenuClaim = case.claim
    cross = tuple(
        PublicCrossContact(
            kind=contact.kind,
            allergens=contact.allergens,
            shared_with=contact.shared_with,
            note=dict(contact.note.values),
        )
        for contact in claim.cross_contact
    )
    names = [(name.value, name.lang, name.primary) for name in claim.dish.names]
    seen_langs = {name.lang for name in claim.dish.names}
    for link in case.chain:
        if link.stage == "human" and link.target_lang not in seen_langs:
            names.append((link.output_text, link.target_lang, False))
            seen_langs.add(link.target_lang)
    return PublicVersion(
        menu_id=claim.menu_id,
        record_id=case.envelope.record_id,
        revision=case.envelope.revision,
        dish_id=claim.dish.dish_id,
        recipe_version=claim.dish.recipe_version,
        names=tuple(names),
        ingredients=tuple(_public_item(item) for item in claim.ingredients),
        processing_aids=tuple(_public_item(item) for item in claim.processing_aids),
        cross_contact=cross,
        served_from=claim.operations.served_from,
        served_until=claim.operations.served_until,
        fryer_oil_changed_at=claim.operations.fryer_oil_changed_at,
        certainty=(
            Certainty.CONFIRMED
            if case.state == ReviewState.PUBLISHED
            else Certainty.UNCERTAIN
        ),
        published_at=published_at,
        image_fingerprints=tuple(image_fingerprints) or (case.capture.image_fingerprint,),
        follow_up_open=bool(case.open_follow_ups()),
    )


@dataclass
class MenuCatalog:
    cache: CacheBus
    # menu_id -> 当前在架公开版本
    _versions: dict[str, PublicVersion] = field(default_factory=dict)
    # menu_id -> 产生当前版本的译审单（更正/撤回时用于状态留痕）
    _cases: dict[str, ReviewCase] = field(default_factory=dict)
    # 图片指纹 -> menu_id，反复拍摄归并的依据
    _fingerprint_index: dict[str, str] = field(default_factory=dict)
    # menu_id -> 历次拍摄指纹（保序去重）
    _captures: dict[str, list[str]] = field(default_factory=dict)
    # menu_id -> 历史版本（含已被取代/撤回的版本），供复盘
    _history: dict[str, list[PublicVersion]] = field(default_factory=dict)
    # menu_id -> 历次译审单（翻译链与三方签署的复盘来源）
    _case_log: dict[str, list[ReviewCase]] = field(default_factory=dict)

    def register_capture(self, case: ReviewCase) -> str:
        """登记一次拍摄的指纹归并；返回该指纹指向的 menu_id。

        无论同一菜单被拍多少次，公开版本始终只有一项。
        """

        menu_id = case.menu_id
        existing = self._fingerprint_index.get(case.capture.image_fingerprint)
        if existing is not None and existing != menu_id:
            raise ValueError("图片指纹已归并到另一菜单，拒绝跨菜单拼接")
        self._fingerprint_index[case.capture.image_fingerprint] = menu_id
        captures = self._captures.setdefault(menu_id, [])
        if case.capture.image_fingerprint not in captures:
            captures.append(case.capture.image_fingerprint)
        return menu_id

    def resolve(self, image_fingerprint: str) -> PublicVersion | None:
        """游客拍照后看到的唯一公开版本；不存在则没有可展示的确认版本。"""

        menu_id = self._fingerprint_index.get(image_fingerprint)
        if menu_id is None:
            return None
        current = self._versions.get(menu_id)
        if current is None:
            return None
        return dataclasses.replace(
            current, image_fingerprints=tuple(self._captures.get(menu_id, ()))
        )

    def _retire(self, menu_id: str, retired: PublicVersion) -> None:
        """把历史中该 revision 的在架副本替换为其最终状态（退役/撤回）。

        公开历史每个 revision 只保留一行；挂起期间由译审单事件链留痕。
        """

        log = self._history.setdefault(menu_id, [])
        if (
            log
            and log[-1].revision == retired.revision
            and log[-1].active
        ):
            log[-1] = retired
        else:
            log.append(retired)

    def publish(self, case: ReviewCase, *, at: str) -> PublicVersion:
        if case.state not in (ReviewState.PUBLISHED, ReviewState.HELD_UNCERTAIN):
            raise ValueError("译审单尚未通过发布环节")
        menu_id = case.menu_id
        old = self._versions.get(menu_id)
        if old is not None:
            # 同 revision 的"挂起 → 确认"是同一声明的核实结果，不是更正。
            if case.envelope.revision < old.revision:
                raise ValueError("更正版本的 revision 必须高于现行版本")
            if case.envelope.revision > old.revision:
                # 旧译审单与旧公开版本一并留痕失效，缓存按更正传播。
                mark_superseded(self._cases[menu_id], at=at, by_revision=case.envelope.revision)
                self._retire(menu_id, dataclasses.replace(old, superseded_at=at))
                self.cache.invalidate(menu_id, at=at, reason="correction")
            elif old.certainty is Certainty.CONFIRMED:
                raise ValueError("同 revision 的确定版本不得重复发布，更正需提高 revision")
        version = project_public(
            case,
            published_at=at,
            image_fingerprints=tuple(self._captures.get(menu_id, ())),
        )
        self._versions[menu_id] = version
        self._cases[menu_id] = case
        self.register_capture(case)
        # 同 revision 的挂起→确认：就地替换历史中的待核实行；新 revision：追加。
        log = self._history.setdefault(menu_id, [])
        if log and log[-1].revision == version.revision:
            log[-1] = version
        else:
            log.append(version)
        case_log = self._case_log.setdefault(menu_id, [])
        if case not in case_log:
            case_log.append(case)
        self.cache.publish(menu_id, version.revision, at=at)
        return version

    def emergency_withdraw(
        self,
        menu_id: str,
        *,
        at: str,
        reason: str,
    ) -> str:
        """紧急撤回：状态机置撤回、公开版本下架、各层缓存失效并记录实际失效时间。"""

        current = self._versions.pop(menu_id, None)
        case = self._cases.pop(menu_id, None)
        if current is None or case is None:
            raise ValueError("该菜单没有在架公开版本，无需撤回")
        withdraw(case, at=at, reason=reason)
        self._retire(menu_id, dataclasses.replace(current, withdrawn_at=at))
        return self.cache.invalidate(menu_id, at=at, reason=f"withdraw: {reason}")

    def history(self, menu_id: str) -> tuple[PublicVersion, ...]:
        return tuple(self._history.get(menu_id, ()))
