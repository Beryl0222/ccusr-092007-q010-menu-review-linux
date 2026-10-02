"""领域数据合同：商户以 ``menu_claim.json`` 提交的菜品与配方标识声明。

v1 仅含六个标识字段（``schema_version`` / ``record_id`` / ``domain`` /
``occurred_at`` / ``revision`` / ``source``），含义保持不变：

- ``record_id`` —— 声明所针对的菜单标识（同一菜单的反复拍摄都归并到它）；
- ``occurred_at`` —— 商户作出本条声明的业务时间（不是拍摄时间，也不是发布时间）；
- ``revision`` —— 声明版本号，更正产生新的 ``revision``，旧版本不就地改写。

v2 在信封内嵌 ``menu`` 节，描述原料、加工助剂、交叉接触与供应批次。
读取器自动把 v1 迁移为 v2（无内嵌声明时 ``menu`` 为 ``None``）；后续若再新增
schema 版本，必须在 :func:`migrate_envelope` 中说明迁移方式，不得重解释旧字段。

``confidential_recipe`` 是配方机密节（用量、步骤），仅供三方签署留痕，
不会进入任何公开投影，竞争商家看到的内容与游客相同。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# v1 信封的六个既有字段，顺序与含义均不得改变。
ENVELOPE_FIELDS = (
    "schema_version",
    "record_id",
    "domain",
    "occurred_at",
    "revision",
    "source",
)

CURRENT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class DomainRecord:
    """v1 最小合同；:func:`load_record` 对任何 schema 版本都只返回这六项。"""

    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


@dataclass(frozen=True)
class LocalizedText:
    """多语文案，键为 BCP-47 语言标签。"""

    values: dict[str, str] = field(default_factory=dict)

    def get(self, lang: str) -> str | None:
        if lang in self.values:
            return self.values[lang]
        return self.values.get("zh-Hans")

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "LocalizedText":
        return cls(dict(payload or {}))


@dataclass(frozen=True)
class DishName:
    value: str
    lang: str
    primary: bool = False


@dataclass(frozen=True)
class DeclaredItem:
    """原料或加工助剂的申报项。

    ``confidential`` 为真的条目（如秘制配料代号）只保留风险标签，
    公开投影不显示可反推出配方的描述。
    """

    code: str
    label: LocalizedText
    allergens: tuple[str, ...] = ()
    confidential: bool = False


@dataclass(frozen=True)
class CrossContact:
    """交叉接触声明，例如与虾、花生制品共用一口炸锅。"""

    kind: str
    allergens: tuple[str, ...]
    shared_with: tuple[str, ...] = ()
    note: LocalizedText = field(default_factory=LocalizedText)


@dataclass(frozen=True)
class SupplyBatch:
    batch_id: str
    ingredient_code: str
    served_from: str
    served_until: str


@dataclass(frozen=True)
class Operations:
    """当天经营事实：供应时段与炸锅换油时间，由商户签署确认。"""

    served_from: str
    served_until: str
    fryer_oil_changed_at: str | None = None


@dataclass(frozen=True)
class ConfidentialRecipe:
    """配方机密：用量与做法只用于签署绑定，不进入公开投影。"""

    recipe_id: str
    recipe_version: int
    proportions: dict[str, str]
    method_steps: tuple[str, ...]


@dataclass(frozen=True)
class Dish:
    dish_id: str
    recipe_id: str
    recipe_version: int
    names: tuple[DishName, ...]


@dataclass(frozen=True)
class MenuClaim:
    """一条菜品声明（信封 v2 的 ``menu`` 节）。"""

    menu_id: str
    restaurant_id: str
    dish: Dish
    ingredients: tuple[DeclaredItem, ...]
    processing_aids: tuple[DeclaredItem, ...]
    cross_contact: tuple[CrossContact, ...]
    supply_batches: tuple[SupplyBatch, ...]
    operations: Operations
    confidential_recipe: ConfidentialRecipe | None

    @property
    def declared_allergens(self) -> frozenset[str]:
        allergens: set[str] = set()
        for item in (*self.ingredients, *self.processing_aids):
            allergens.update(item.allergens)
        for contact in self.cross_contact:
            allergens.update(contact.allergens)
        return frozenset(allergens)


def migrate_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """把任意已支持版本的信封迁移到当前版本。

    迁移规则：
    v1 → v2：六个标识字段原样保留；v1 不含菜品声明，``menu`` 置空，
    声明方需在业务侧补交 v2 菜单节，旧字段不被重新解释。
    """

    version = payload.get("schema_version", 1)
    if version == 1:
        migrated = {key: payload[key] for key in ENVELOPE_FIELDS if key in payload}
        migrated["schema_version"] = 2
        migrated["menu"] = None
        return migrated
    if version == CURRENT_SCHEMA_VERSION:
        return payload
    raise ValueError(f"不支持的 schema_version: {version}")


def _localized(payload: dict[str, Any], key: str) -> LocalizedText:
    return LocalizedText.from_dict(payload.get(key) or payload.get(f"{key}_i18n"))


def _parse_menu(payload: dict[str, Any]) -> MenuClaim:
    dish_payload = payload["dish"]
    dish = Dish(
        dish_id=dish_payload["dish_id"],
        recipe_id=dish_payload["recipe_id"],
        recipe_version=dish_payload["recipe_version"],
        names=tuple(
            DishName(
                value=name["value"],
                lang=name["lang"],
                primary=name.get("primary", False),
            )
            for name in dish_payload.get("names", ())
        ),
    )

    def _items(key: str) -> tuple[DeclaredItem, ...]:
        return tuple(
            DeclaredItem(
                code=item["code"],
                label=_localized(item, "label"),
                allergens=tuple(item.get("allergens", ())),
                confidential=item.get("confidential", False),
            )
            for item in payload.get(key, ())
        )

    cross_contact = tuple(
        CrossContact(
            kind=contact["kind"],
            allergens=tuple(contact.get("allergens", ())),
            shared_with=tuple(contact.get("shared_with", ())),
            note=_localized(contact, "note"),
        )
        for contact in payload.get("cross_contact", ())
    )
    batches = tuple(
        SupplyBatch(
            batch_id=batch["batch_id"],
            ingredient_code=batch["ingredient_code"],
            served_from=batch["served_from"],
            served_until=batch["served_until"],
        )
        for batch in payload.get("supply_batches", ())
    )
    operations_payload = payload["operations"]
    operations = Operations(
        served_from=operations_payload["served_from"],
        served_until=operations_payload["served_until"],
        fryer_oil_changed_at=operations_payload.get("fryer_oil_changed_at"),
    )
    secret = payload.get("confidential_recipe")
    confidential = None
    if secret:
        confidential = ConfidentialRecipe(
            recipe_id=secret["recipe_id"],
            recipe_version=secret["recipe_version"],
            proportions=dict(secret.get("proportions", {})),
            method_steps=tuple(secret.get("method_steps", ())),
        )
    return MenuClaim(
        menu_id=payload["menu_id"],
        restaurant_id=payload["restaurant_id"],
        dish=dish,
        ingredients=_items("ingredients"),
        processing_aids=_items("processing_aids"),
        cross_contact=cross_contact,
        supply_batches=batches,
        operations=operations,
        confidential_recipe=confidential,
    )


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_record(path: Path) -> DomainRecord:
    """读取信封的六个标识字段；v1/v2 样例均可读，多余字段被忽略。"""

    payload = _read(path)
    envelope = {key: payload[key] for key in ENVELOPE_FIELDS if key in payload}
    return DomainRecord(**envelope)


def load_claim(path: Path) -> tuple[DomainRecord, MenuClaim | None]:
    """读取完整声明，返回 (信封, 菜单节)；v1 样例的菜单节为 ``None``。"""

    payload = migrate_envelope(_read(path))
    envelope = DomainRecord(**{key: payload[key] for key in ENVELOPE_FIELDS})
    menu = _parse_menu(payload["menu"]) if payload.get("menu") else None
    return envelope, menu
