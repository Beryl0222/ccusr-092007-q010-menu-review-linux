"""审核复盘材料。

复盘一次展示时，审核人员应在同一处看到：

1. 原图指纹（照片是否为原图）；
2. 配方版本（仅供审核，不对竞争商家公开）；
3. 完整翻译链（每一跳角色、工具版本、前后哈希、译者签名）；
4. 三方签署（译者 / 营养专业人员 / 商户，含签名与绑定的版本）；
5. 发布版本与历次紧急更正/撤回的实际失效时间；
6. 游客已保存内容的当前有效性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .caching import CacheCoordinator, InvalidationEvent
from .guest import SavedItem, SavedItemView
from .publishing import PublishRegistry, PublishedVersion
from .review import ReviewPackage


@dataclass(frozen=True)
class HopRecord:
    seq: int
    role: str
    actor: str
    tool_version: str
    at: str
    prev_hash: str
    hop_hash: str


@dataclass(frozen=True)
class SignerRecord:
    role: str
    signer: str
    at: str
    value: str
    bound_recipe_ref: str
    claim_revision: int
    verified: bool


@dataclass(frozen=True)
class AuditBundle:
    image_fingerprint: str
    recipe_ref: str
    recipe_fingerprint: str
    claim_record_id: str
    claim_revision: int
    chain: tuple[HopRecord, ...]
    chain_hash: str
    signers: tuple[SignerRecord, ...]
    published_version_id: Optional[str]
    published_status: Optional[str]
    invalidations: tuple[InvalidationEvent, ...]
    effective_invalidated_at: Optional[str]
    saved_views: tuple[SavedItemView, ...]

    def find_hop(self, hop_hash: str) -> Optional[HopRecord]:
        return next((h for h in self.chain if h.hop_hash == hop_hash), None)


def build_bundle(*, package: ReviewPackage,
                 registry: Optional[PublishRegistry] = None,
                 cache: Optional[CacheCoordinator] = None,
                 saved_items: tuple[SavedItem, ...] = (),
                 at: Optional[str] = None,
                 keys: Optional[dict[str, bytes]] = None) -> AuditBundle:
    """组装复盘材料。

    ``keys`` 形如 ``{"translator": b"..", "nutritionist": b"..",
    "merchant": b".."}``；提供时三方签名会被实际校验。
    """
    keys = keys or {}
    translation = package.translation
    chain = tuple(
        HopRecord(h.seq, h.role, h.actor, h.tool_version, h.at,
                  h.prev_hash, h.hop_hash)
        for h in translation.hops)

    signers: list[SignerRecord] = []
    tsig = translation.translator_signature
    if tsig is not None:
        verified = tsig.verify(translation.sign_payload(), keys["translator"]) \
            if "translator" in keys else True
        signers.append(SignerRecord(
            tsig.role, tsig.signer, tsig.at, tsig.value,
            translation.recipe_ref, translation.claim_revision, verified))

    if package.risk is not None:
        risk = package.risk
        verified = risk.verify(keys["nutritionist"]) if "nutritionist" in keys else True
        signers.append(SignerRecord(
            "nutritionist", risk.signature.signer, risk.observed_at,
            risk.signature.value, risk.recipe_ref, risk.claim_revision, verified))

    if package.merchant is not None:
        merchant = package.merchant
        verified = merchant.verify(keys["merchant"]) if "merchant" in keys else True
        signers.append(SignerRecord(
            "merchant", merchant.signature.signer, merchant.at,
            merchant.signature.value, merchant.recipe_ref,
            merchant.claim_revision, verified))

    published_version_id = None
    published_status = None
    if registry is not None:
        published = registry.canonical(
            package.claim.menu_id, package.dish.dish_id, translation.target_lang)
        if published is not None:
            published_version_id = published.version_id
            published_status = published.status

    invalidations = tuple(cache.events.values()) if cache is not None else ()
    effective: Optional[str] = None
    if invalidations:
        effective = max(
            (cache.effective_invalidated_at(e) for e in invalidations),
            default=None)

    saved_views: tuple[SavedItemView, ...] = ()
    if registry is not None:
        saved_views = tuple(item.view(registry, at=at) for item in saved_items)

    return AuditBundle(
        image_fingerprint=package.image_fingerprint,
        recipe_ref=package.dish.recipe_ref,
        recipe_fingerprint=_fp(package.dish.recipe_ref),
        claim_record_id=package.claim.record_id,
        claim_revision=package.claim.revision,
        chain=chain,
        chain_hash=translation.chain_hash,
        signers=tuple(signers),
        published_version_id=published_version_id,
        published_status=published_status,
        invalidations=invalidations,
        effective_invalidated_at=effective,
        saved_views=saved_views,
    )


def _fp(recipe_ref: str) -> str:
    from .publishing import recipe_fingerprint
    return recipe_fingerprint(recipe_ref)
