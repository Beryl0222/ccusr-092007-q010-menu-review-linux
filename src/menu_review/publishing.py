"""发布注册表：同一菜单反复拍摄只指向唯一的公开版本。

竞争商家隔离：公开载荷只含译者签署的名称/文化故事与营养专业人员撰写的
过敏原代码和风险提示，**不包含**配方明细、原料批次与内部配方标识；
配方以指纹（SHA-256 摘要）形式参与绑定，供审核复盘核对，不对外可读。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .review import READY, ReviewPackage
from .timeutil import now, parse

ACTIVE = "active"
CORRECTED = "corrected"   # 被紧急更正替代
WITHDRAWN = "withdrawn"   # 撤回，不再展示任何安全内容


class PublishError(ValueError):
    pass


def recipe_fingerprint(recipe_ref: str) -> str:
    return "sha256:" + hashlib.sha256(recipe_ref.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PublicPayload:
    """对外公开的最小内容，配方明细与批次一律不在其中。"""

    name: str
    cultural_story: Optional[str]
    allergens: tuple[str, ...]
    advisories: tuple[str, ...]
    valid_until: str


@dataclass(frozen=True)
class PublishedVersion:
    version_id: str
    menu_id: str
    dish_id: str
    target_lang: str
    recipe_fingerprint: str
    claim_revision: int
    payload: PublicPayload
    published_at: str
    status: str = ACTIVE
    superseded_by: Optional[str] = None

    def is_valid_at(self, at: Optional[str] = None) -> bool:
        if self.status != ACTIVE:
            return False
        moment = now() if at is None else parse(at)
        return parse(self.payload.valid_until) >= moment


@dataclass(frozen=True)
class Resolution:
    """一次拍摄到公开版本的指向记录（反复拍摄都落到同一版本）。"""

    image_fingerprint: str
    version_id: str
    at: str


class PublishRegistry:
    """维护每个 (menu, dish, lang) 的唯一公开版本与拍摄指向记录。"""

    def __init__(self) -> None:
        self._versions: dict[str, PublishedVersion] = {}
        self._canonical: dict[tuple[str, str, str], str] = {}
        self._resolutions: dict[str, tuple[Resolution, ...]] = {}

    def publish(self, package: ReviewPackage, *, at: Optional[str] = None,
                replaces: Optional[PublishedVersion] = None) -> PublishedVersion:
        """发布定稿包。

        同菜单、同绑定的反复拍摄不会产生新版本，只增加一条指向记录；
        绑定已变化时必须以 ``replaces`` 指明要更正的旧版本（紧急更正），
        不允许悄悄覆盖。
        """
        if package.status != READY:
            raise PublishError(f"只有 ready 的复核包可以发布，当前状态 {package.status}")
        assert package.risk is not None and package.merchant is not None
        moment = (now().isoformat() if at is None else at)
        key = (package.claim.menu_id, package.dish.dish_id,
               package.translation.target_lang)
        existing_id = self._canonical.get(key)
        if replaces is not None:
            if existing_id != replaces.version_id:
                raise PublishError("要更正的版本不是当前公开版本")
        elif existing_id is not None:
            existing = self._versions[existing_id]
            same_binding = (
                existing.recipe_fingerprint == recipe_fingerprint(package.dish.recipe_ref)
                and existing.claim_revision == package.claim.revision
                and existing.is_valid_at(moment)
            )
            if same_binding:
                # 同菜单反复拍摄：不创建新版本，只记录指向。
                self._record_resolution(existing, package.image_fingerprint, moment)
                return existing
            # 绑定已变化：调用方应先紧急更正，不允许悄悄覆盖公开版本。
            raise PublishError("已存在公开版本且绑定发生变化，请走紧急更正流程")

        name = package.translation.field_text("name") or package.dish.names[0]
        story = package.translation.field_text("cultural_story")
        payload = PublicPayload(
            name=name,
            cultural_story=story,
            allergens=package.risk.allergens,
            advisories=package.risk.advisories,
            valid_until=_iso_after(package.risk.observed_at,
                                   package.risk.valid_seconds),
        )
        version_id = "ver-" + hashlib.sha256(
            "|".join([*key, package.dish.recipe_ref, str(package.claim.revision),
                      moment]).encode("utf-8")).hexdigest()[:16]
        version = PublishedVersion(
            version_id=version_id, menu_id=key[0], dish_id=key[1],
            target_lang=key[2],
            recipe_fingerprint=recipe_fingerprint(package.dish.recipe_ref),
            claim_revision=package.claim.revision, payload=payload,
            published_at=moment)
        self._versions[version_id] = version
        if replaces is not None:
            # 紧急更正：旧版本保留记录并指向新版本，canonical 一并切换。
            self.correct(replaces, version, at=moment)
        else:
            self._canonical[key] = version_id
        self._record_resolution(version, package.image_fingerprint, moment)
        return version

    def resolve(self, image_fingerprint: str) -> Optional[PublishedVersion]:
        for versions in self._resolutions.values():
            for resolution in versions:
                if resolution.image_fingerprint == image_fingerprint:
                    return self._versions[resolution.version_id]
        return None

    def resolutions_for(self, version_id: str) -> tuple[Resolution, ...]:
        return self._resolutions.get(version_id, ())

    def canonical(self, menu_id: str, dish_id: str, target_lang: str) -> Optional[PublishedVersion]:
        version_id = self._canonical.get((menu_id, dish_id, target_lang))
        return self._versions[version_id] if version_id else None

    def correct(self, old: PublishedVersion, replacement: PublishedVersion,
                *, at: Optional[str] = None) -> None:
        """紧急更正：旧版本标记 corrected 并指向新版本。"""
        moment = (now().isoformat() if at is None else at)
        self._versions[old.version_id] = PublishedVersion(
            old.version_id, old.menu_id, old.dish_id, old.target_lang,
            old.recipe_fingerprint, old.claim_revision, old.payload,
            old.published_at, CORRECTED, replacement.version_id)
        self._versions[replacement.version_id] = replacement
        self._canonical[(old.menu_id, old.dish_id, old.target_lang)] = replacement.version_id

    def withdraw(self, version: PublishedVersion, *, at: Optional[str] = None) -> PublishedVersion:
        """撤回：公开版本不再展示任何安全内容。"""
        moment = (now().isoformat() if at is None else at)
        withdrawn = PublishedVersion(
            version.version_id, version.menu_id, version.dish_id, version.target_lang,
            version.recipe_fingerprint, version.claim_revision, version.payload,
            version.published_at, WITHDRAWN, version.superseded_by)
        self._versions[version.version_id] = withdrawn
        return withdrawn

    def get(self, version_id: str) -> PublishedVersion:
        return self._versions[version_id]

    def _record_resolution(self, version: PublishedVersion, image_fingerprint: str,
                           moment: str) -> None:
        log = self._resolutions.get(version.version_id, ())
        self._resolutions[version.version_id] = log + (
            Resolution(image_fingerprint, version.version_id, moment),)


def _iso_after(observed_at: str, valid_seconds: int) -> str:
    from datetime import timedelta
    return (parse(observed_at) + timedelta(seconds=valid_seconds)).isoformat()
