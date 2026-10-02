"""测试用：把一份声明走到 ready 定稿的最小流水线。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from menu_review import (machine_translate, match_names, merchant_confirm,
                         nutritionist_review, finalize, sign_translation,
                         start_package, post_edit)

T_KEY = b"translator-secret"
N_KEY = b"nutritionist-secret"
M_KEY = b"merchant-secret"

KEYS = {"translator": T_KEY, "nutritionist": N_KEY, "merchant": M_KEY}


def fake_engine(text, src, dst):
    table = {"椒盐鱿鱼": "Salt and Pepper Squid",
             "宫保鸡丁": "Kung Pao Chicken",
             "本帮走油肉": "Shanghai Braised Fried Pork"}
    return table.get(text, text + f" [{dst}]")


def build_ready_package(claim, dish_id, image, *, at, observed_name=None,
                        confidence=0.99, advisories=None, story=None,
                        edited_name=None, valid_seconds=24 * 3600,
                        batch_ids=(), verify=True):
    dish = claim.dish(dish_id)
    observed_name = observed_name or dish.names[0]
    recognition = match_names(
        image_bytes=image, observed_name=observed_name,
        dishes=claim.dishes, confidence=confidence)
    candidate = machine_translate(
        claim=claim, dish=dish, target_lang="en",
        engine="mt-demo", engine_version="model-42",
        translate=fake_engine, at=at)
    if edited_name is not None:
        candidate = post_edit(candidate=candidate, editor="pe-1",
                              field_id="name", output_text=edited_name, at=at)
    signed = sign_translation(candidate=candidate, translator="translator-1",
                              key=T_KEY, cultural_story_target=story, at=at)
    package = start_package(package_id=f"pkg-{dish_id}-{image.hex()[:8]}",
                            claim=claim, recognition=recognition,
                            translation=signed, at=at)
    advisories = advisories or {"en": "Cooked in a shared fryer: may contain peanut and fish. Fried in soybean oil."}
    risk = nutritionist_review(
        package=package, signer="nutritionist-1", key=N_KEY,
        advisories=advisories, at=at, valid_seconds=valid_seconds,
        observed_batch_ids=batch_ids)
    merchant = merchant_confirm(package=package, signer="merchant-1",
                                key=M_KEY, at=at, valid_seconds=valid_seconds)
    kw = dict(at=at)
    if verify:
        kw.update(translator_key=T_KEY, nutritionist_key=N_KEY, merchant_key=M_KEY)
    return finalize(package=package, risk=risk, merchant=merchant, **kw)
