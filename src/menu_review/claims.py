"""餐厅以 ``menu_claim.json`` 做出的声明：菜品与配方标识、原料、
加工助剂、交叉接触、供应批次，以及当日经营事实。

合同约定：

* 配方由 ``(recipe_id, recipe_version)`` 标识；配方任何变化都必须提升版本，
  旧版本声明不可被当作当前版本展示。
* 过敏原只允许使用规范代码（:data:`ALLERGEN_CODES`）。
* 本模块只承载事实，不产出安全结论；安全结论由营养专业人员在
  :mod:`menu_review.review` 中复核后签署。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from .timeutil import now, parse

ALLERGEN_CODES = frozenset({
    "gluten", "crustacean", "egg", "fish", "peanut", "soy",
    "milk", "tree_nut", "celery", "mustard", "sesame",
    "sulphite", "lupin", "mollusc",
})


class ClaimError(ValueError):
    """声明数据违反合同。"""


def _allergens(raw: Sequence[str], *, where: str) -> tuple[str, ...]:
    values = tuple(raw or ())
    unknown = sorted(set(values) - ALLERGEN_CODES)
    if unknown:
        raise ClaimError(f"{where} 使用了未登记的过敏原代码: {', '.join(unknown)}")
    return values


@dataclass(frozen=True)
class Ingredient:
    ingredient_id: str
    name: str
    allergens: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Ingredient":
        return cls(
            ingredient_id=str(raw["ingredient_id"]),
            name=str(raw["name"]),
            allergens=_allergens(raw.get("allergens", ()), where=f"原料 {raw.get('ingredient_id')}"),
        )


@dataclass(frozen=True)
class ProcessingAid:
    """加工助剂，例如炸制用油。"""

    aid_id: str
    name: str
    allergens: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "ProcessingAid":
        return cls(
            aid_id=str(raw["aid_id"]),
            name=str(raw["name"]),
            allergens=_allergens(raw.get("allergens", ()), where=f"加工助剂 {raw.get('aid_id')}"),
        )


@dataclass(frozen=True)
class CrossContact:
    """交叉接触：共用设备及其可能带入的过敏原（例如共用炸锅）。"""

    equipment_id: str
    kind: str
    shared_allergens: tuple[str, ...]
    detail: str = ""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "CrossContact":
        return cls(
            equipment_id=str(raw["equipment_id"]),
            kind=str(raw["kind"]),
            shared_allergens=_allergens(
                raw.get("shared_allergens", ()),
                where=f"设备 {raw.get('equipment_id')}"),
            detail=str(raw.get("detail", "")),
        )


@dataclass(frozen=True)
class Substitution:
    """季节替换：在 [valid_from, valid_to) 时间窗内用替代原料替换原原料。"""

    reason: str
    replaces: str
    replacement: Ingredient
    valid_from: str
    valid_to: Optional[str] = None

    def active_at(self, at: Union[str, Any, None] = None) -> bool:
        moment = now() if at is None else parse(at)
        if parse(self.valid_from) > moment:
            return False
        if self.valid_to is not None and parse(self.valid_to) <= moment:
            return False
        return True

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Substitution":
        return cls(
            reason=str(raw["reason"]),
            replaces=str(raw["replaces"]),
            replacement=Ingredient.from_raw(raw["replacement"]),
            valid_from=str(raw["valid_from"]),
            valid_to=(str(raw["valid_to"]) if raw.get("valid_to") else None),
        )


@dataclass(frozen=True)
class DishClaim:
    dish_id: str
    names: tuple[str, ...]
    recipe_id: str
    recipe_version: int
    ingredients: tuple[Ingredient, ...]
    processing_aids: tuple[ProcessingAid, ...] = ()
    cross_contact: tuple[CrossContact, ...] = ()
    substitutions: tuple[Substitution, ...] = ()

    @property
    def recipe_ref(self) -> str:
        """配方标识：配方身份只由 id+version 决定。"""
        return f"{self.recipe_id}@v{self.recipe_version}"

    def known_name(self, candidate: str) -> bool:
        return candidate.strip() in self.names

    def active_substitution(self, at: Union[str, Any, None] = None) -> Optional[Substitution]:
        active = [s for s in self.substitutions if s.active_at(at)]
        if len(active) > 1:
            raise ClaimError(f"菜品 {self.dish_id} 在同一时刻存在多个生效的替换")
        return active[0] if active else None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "DishClaim":
        names = tuple(raw.get("names") or [raw["name"]])
        if not names:
            raise ClaimError(f"菜品 {raw.get('dish_id')} 至少需要一个名称")
        return cls(
            dish_id=str(raw["dish_id"]),
            names=names,
            recipe_id=str(raw["recipe_id"]),
            recipe_version=int(raw["recipe_version"]),
            ingredients=tuple(Ingredient.from_raw(i) for i in raw.get("ingredients", ())),
            processing_aids=tuple(ProcessingAid.from_raw(i) for i in raw.get("processing_aids", ())),
            cross_contact=tuple(CrossContact.from_raw(i) for i in raw.get("cross_contact", ())),
            substitutions=tuple(Substitution.from_raw(i) for i in raw.get("substitutions", ())),
        )


@dataclass(frozen=True)
class SupplyBatch:
    """供应批次：某菜品在某个供餐日使用的批次，绑定具体原料批次。"""

    batch_id: str
    dish_id: str
    served_on: str
    ingredient_batch_ids: tuple[str, ...]
    produced_at: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "SupplyBatch":
        date.fromisoformat(str(raw["served_on"]))
        return cls(
            batch_id=str(raw["batch_id"]),
            dish_id=str(raw["dish_id"]),
            served_on=str(raw["served_on"]),
            ingredient_batch_ids=tuple(raw.get("ingredient_batch_ids", ())),
            produced_at=(str(raw["produced_at"]) if raw.get("produced_at") else None),
        )


@dataclass(frozen=True)
class OperatingFact:
    """商户确认的当日经营事实，例如当天更换炸锅油、临时售罄。"""

    fact_id: str
    kind: str  # oil_change | sold_out | equipment_change | note
    at: str
    dish_id: Optional[str] = None
    equipment_id: Optional[str] = None
    detail: str = ""
    resume_at: Optional[str] = None

    def active_at(self, at: Union[str, Any, None] = None) -> bool:
        moment = now() if at is None else parse(at)
        if parse(self.at) > moment:
            return False
        if self.resume_at is not None and parse(self.resume_at) <= moment:
            return False
        return True

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "OperatingFact":
        kind = str(raw["kind"])
        if kind not in {"oil_change", "sold_out", "equipment_change", "note"}:
            raise ClaimError(f"未知的经营事实类型: {kind}")
        return cls(
            fact_id=str(raw["fact_id"]),
            kind=kind,
            at=str(raw["at"]),
            dish_id=(str(raw["dish_id"]) if raw.get("dish_id") else None),
            equipment_id=(str(raw["equipment_id"]) if raw.get("equipment_id") else None),
            detail=str(raw.get("detail", "")),
            resume_at=(str(raw["resume_at"]) if raw.get("resume_at") else None),
        )


@dataclass(frozen=True)
class MenuClaim:
    """一整份菜单声明。``revision`` 提升表示声明内容发生变化。"""

    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str
    restaurant_id: str
    menu_id: str
    dishes: tuple[DishClaim, ...]
    batches: tuple[SupplyBatch, ...] = ()
    facts: tuple[OperatingFact, ...] = ()

    def dish(self, dish_id: str) -> DishClaim:
        for dish in self.dishes:
            if dish.dish_id == dish_id:
                return dish
        raise ClaimError(f"菜单 {self.menu_id} 中没有菜品 {dish_id}")

    def find_by_name(self, name: str) -> tuple[DishClaim, ...]:
        """一菜多名 / 一名多菜时可能返回多个候选，调用方必须按不确定处理。"""
        wanted = name.strip()
        return tuple(d for d in self.dishes if wanted in d.names)

    def batches_for(self, dish_id: str, served_on: str) -> tuple[SupplyBatch, ...]:
        return tuple(b for b in self.batches
                     if b.dish_id == dish_id and b.served_on == served_on)

    def active_facts(self, kind: str, *, dish_id: Optional[str] = None,
                     at: Union[str, Any, None] = None) -> tuple[OperatingFact, ...]:
        return tuple(
            f for f in self.facts
            if f.kind == kind and f.active_at(at)
            and (dish_id is None or f.dish_id == dish_id)
        )

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "MenuClaim":
        if raw.get("domain") != "menu_review":
            raise ClaimError("domain 必须为 menu_review")
        if not raw.get("restaurant_id") or not raw.get("menu_id"):
            raise ClaimError("菜单声明必须包含 restaurant_id 与 menu_id")
        dishes = tuple(DishClaim.from_raw(d) for d in raw.get("dishes", ()))
        batches = tuple(SupplyBatch.from_raw(b) for b in raw.get("batches", ()))
        facts = tuple(OperatingFact.from_raw(f) for f in raw.get("facts", ()))
        known_dishes = {d.dish_id for d in dishes}
        for b in batches:
            if b.dish_id not in known_dishes:
                raise ClaimError(f"批次 {b.batch_id} 引用了不存在的菜品 {b.dish_id}")
        for f in facts:
            if f.dish_id is not None and f.dish_id not in known_dishes:
                raise ClaimError(f"经营事实 {f.fact_id} 引用了不存在的菜品 {f.dish_id}")
        return cls(
            schema_version=int(raw["schema_version"]),
            record_id=str(raw["record_id"]),
            domain=str(raw["domain"]),
            occurred_at=str(raw["occurred_at"]),
            revision=int(raw["revision"]),
            source=str(raw["source"]),
            restaurant_id=str(raw["restaurant_id"]),
            menu_id=str(raw["menu_id"]),
            dishes=dishes,
            batches=batches,
            facts=facts,
        )


def load_claim(path: Path) -> MenuClaim:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return MenuClaim.from_raw(payload)
