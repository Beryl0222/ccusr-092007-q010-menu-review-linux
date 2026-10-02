"""审核复盘：一次展示背后的全部证据链。

审核人员复盘任意一次游客侧展示时，应在同一个复盘包里看到：

- 原图指纹（:meth:`Capture.image_fingerprint`）；
- 配方版本（``recipe_id`` + ``recipe_version``）；
- 翻译链（机译候选与人工修订的每一环）；
- 三方签署（译者语义、营养风险、商户事实，各自时间与绑定的版本）；
- 状态轨迹与不确定因素；
- 该菜单的缓存实际失效记录。

复盘包由登记处留痕的译审单构建，不接受任何事后补造的签署。
"""

from __future__ import annotations

from dataclasses import dataclass

from .caching import CacheBus, InvalidationRecord
from .catalog import MenuCatalog, PublicVersion
from .workflow import (
    Certainty,
    ReviewCase,
    ReviewState,
    Role,
    Signature,
    TranslationLink,
)


@dataclass(frozen=True)
class SignatureView:
    role: str
    signer: str
    at: str
    claim_revision: int
    recipe_version: int
    image_fingerprint: str
    detail: dict


@dataclass(frozen=True)
class ReviewPackage:
    menu_id: str
    image_fingerprint: str
    recipe_id: str
    recipe_version: int
    state: ReviewState
    certainty: Certainty
    claim_revision: int
    translation_chain: tuple[TranslationLink, ...]
    signatures: dict[str, SignatureView]
    uncertainty: tuple[str, ...]
    event_log: tuple[tuple[str, ReviewState, str], ...]
    invalidations: tuple[InvalidationRecord, ...]
    public_version: PublicVersion | None

    @property
    def three_way_signed(self) -> bool:
        return {Role.TRANSLATOR, Role.NUTRITION, Role.MERCHANT}.issubset(
            {self._role_from_key(key) for key in self.signatures}
        )

    @staticmethod
    def _role_from_key(key: str) -> Role:
        return Role(key)


def _signature_view(sig: Signature) -> SignatureView:
    return SignatureView(
        role=sig.role.value,
        signer=sig.signer,
        at=sig.at,
        claim_revision=sig.claim_revision,
        recipe_version=sig.recipe_version,
        image_fingerprint=sig.image_fingerprint,
        detail=dict(sig.detail),
    )


def build_review_package(
    catalog: MenuCatalog,
    cache: CacheBus,
    menu_id: str,
    *,
    case: ReviewCase | None = None,
) -> ReviewPackage:
    """聚合一次展示的证据链。

    ``case`` 缺省时取登记处为该菜单留痕的最新译审单（含当前在架版本；
    更正后的旧单可从 ``case_log`` 中指定以复盘历史展示）。
    """

    if case is None:
        log = catalog._case_log.get(menu_id, ())
        if not log:
            raise ValueError("该菜单没有已留痕的译审单")
        case = log[-1]
    signatures = {
        role.value: _signature_view(sig) for role, sig in case.signatures.items()
    }
    current = catalog._versions.get(menu_id)
    public = current if current is not None and case is catalog._cases.get(menu_id) else None
    return ReviewPackage(
        menu_id=menu_id,
        image_fingerprint=case.capture.image_fingerprint,
        recipe_id=case.claim.dish.recipe_id,
        recipe_version=case.claim.dish.recipe_version,
        state=case.state,
        certainty=(
            Certainty.CONFIRMED if case.state == ReviewState.PUBLISHED
            else Certainty.UNCERTAIN
        ),
        claim_revision=case.envelope.revision,
        translation_chain=tuple(case.chain),
        signatures=signatures,
        uncertainty=tuple(sorted(item.value for item in case.uncertainty)),
        event_log=tuple(case.events),
        invalidations=cache.invalidation_log(menu_id),
        public_version=public,
    )
