"""游客侧展示：带时效、可追问的多语菜单信息。

游客（无论是否竞争商家）只能拿到公开投影：

- 确定版本：多语菜名、含过敏原的原料与加工助剂、交叉接触提示
  （"本菜品与虾类共用炸锅"）、当日换油时间与供应窗口；
- 待核实版本：只展示不确定原因与追问入口，**没有**确定安全结论；
- 撤回/过期：明确提示内容失效；
- 机密配方节在任何情况下都不出现在视图中。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .catalog import MenuCatalog, PublicVersion, PublicCrossContact, PublicItem
from .saved import SavedCollection, SavedStatus
from .workflow import Certainty


@dataclass(frozen=True)
class VisitorView:
    menu_id: str
    lang: str
    status: str
    names: tuple[str, ...]
    allergens: tuple[str, ...]
    items: tuple[str, ...]
    cross_contact_notices: tuple[str, ...]
    fryer_oil_changed_at: str | None
    served_from: str
    served_until: str
    uncertainty: tuple[str, ...]
    can_follow_up: bool
    validity_note: str

    def as_dict(self) -> dict:
        return {
            "menu_id": self.menu_id,
            "lang": self.lang,
            "status": self.status,
            "names": list(self.names),
            "allergens": list(self.allergens),
            "items": list(self.items),
            "cross_contact_notices": list(self.cross_contact_notices),
            "fryer_oil_changed_at": self.fryer_oil_changed_at,
            "served_from": self.served_from,
            "served_until": self.served_until,
            "uncertainty": list(self.uncertainty),
            "can_follow_up": self.can_follow_up,
            "validity_note": self.validity_note,
        }


def _label(item: PublicItem, lang: str) -> str:
    return item.label.get(lang) or item.label.get("zh-Hans") or item.code


def _contact_notice(contact: PublicCrossContact, lang: str) -> str:
    note = contact.note.get(lang) or contact.note.get("zh-Hans")
    if note:
        return note
    if contact.kind == "shared_fryer":
        return f"共用炸锅，可能存在交叉接触：{', '.join(contact.allergens)}"
    return f"交叉接触（{contact.kind}）：{', '.join(contact.allergens)}"


def view_for_capture(
    catalog: MenuCatalog,
    image_fingerprint: str,
    *,
    lang: str,
    now: datetime,
) -> VisitorView | None:
    """按原图指纹取唯一公开版本；无确认版本时返回 None（调用方提示待译审）。"""

    version = catalog.resolve(image_fingerprint)
    if version is None:
        return None
    return render(version, lang=lang, now=now)


def render(version: PublicVersion, *, lang: str, now: datetime) -> VisitorView:
    names = tuple(
        value for value, name_lang, _primary in version.names if name_lang == lang
    ) or tuple(value for value, _name_lang, primary in version.names if primary)
    allergens = tuple(
        sorted(
            {
                allergen
                for item in (*version.ingredients, *version.processing_aids)
                for allergen in item.allergens
            }
            | {
                allergen
                for contact in version.cross_contact
                for allergen in contact.allergens
            }
        )
    )
    items = tuple(_label(item, lang) for item in version.ingredients)
    notices = tuple(_contact_notice(contact, lang) for contact in version.cross_contact)

    if not version.active:
        status = "withdrawn" if version.withdrawn_at else "superseded"
        return VisitorView(
            menu_id=version.menu_id,
            lang=lang,
            status=status,
            names=names,
            allergens=allergens,
            items=items,
            cross_contact_notices=notices,
            fryer_oil_changed_at=version.fryer_oil_changed_at,
            served_from=version.served_from,
            served_until=version.served_until,
            uncertainty=(),
            can_follow_up=False,
            validity_note="该版本已失效，请以当前在架版本为准",
        )

    if version.certainty is Certainty.UNCERTAIN:
        return VisitorView(
            menu_id=version.menu_id,
            lang=lang,
            status="pending",
            names=names,
            allergens=(),
            items=(),
            cross_contact_notices=(),
            fryer_oil_changed_at=None,
            served_from=version.served_from,
            served_until=version.served_until,
            uncertainty=("pending_verification",),
            can_follow_up=version.follow_up_open,
            validity_note="信息待核实，暂不提供确定的过敏原与安全结论，可向餐厅追问",
        )

    if not version.is_current(now):
        return VisitorView(
            menu_id=version.menu_id,
            lang=lang,
            status="expired",
            names=names,
            allergens=allergens,
            items=items,
            cross_contact_notices=notices,
            fryer_oil_changed_at=version.fryer_oil_changed_at,
            served_from=version.served_from,
            served_until=version.served_until,
            uncertainty=(),
            can_follow_up=False,
            validity_note="已过当日供应时段，原料批次可能不同，请重新确认",
        )

    return VisitorView(
        menu_id=version.menu_id,
        lang=lang,
        status="current",
        names=names,
        allergens=allergens,
        items=items,
        cross_contact_notices=notices,
        fryer_oil_changed_at=version.fryer_oil_changed_at,
        served_from=version.served_from,
        served_until=version.served_until,
        uncertainty=(),
        can_follow_up=True,
        validity_note="当前有效",
    )


def render_saved(
    saved: SavedCollection,
    tourist_id: str,
    menu_id: str,
    *,
    lang: str,
    now: datetime,
) -> VisitorView:
    """收藏页视图：顶部清楚标出保存内容是否仍有效。"""

    view = saved.view(tourist_id, menu_id, now=now)
    base = render(view.item.snapshot, lang=lang, now=now)
    status_map = {
        SavedStatus.CURRENT: "current",
        SavedStatus.PENDING: "pending",
        SavedStatus.SUPERSEDED: "superseded",
        SavedStatus.WITHDRAWN: "withdrawn",
        SavedStatus.EXPIRED: "expired",
    }
    return VisitorView(
        menu_id=base.menu_id,
        lang=lang,
        status=status_map[view.status],
        names=base.names,
        allergens=base.allergens,
        items=base.items,
        cross_contact_notices=base.cross_contact_notices,
        fryer_oil_changed_at=base.fryer_oil_changed_at,
        served_from=base.served_from,
        served_until=base.served_until,
        uncertainty=base.uncertainty,
        can_follow_up=base.can_follow_up and view.still_valid,
        validity_note=view.detail,
    )
