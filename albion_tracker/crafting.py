from __future__ import annotations

import math
from typing import Any


RESOURCE_FAMILIES: dict[str, dict[str, str]] = {
    "leather": {"label": "皮革", "raw_token": "HIDE", "raw_label": "粗皮", "refined_token": "LEATHER", "refined_label": "皮革"},
    "cloth": {"label": "布料", "raw_token": "FIBER", "raw_label": "纖維", "refined_token": "CLOTH", "refined_label": "布料"},
    "metalbar": {"label": "金屬錠", "raw_token": "ORE", "raw_label": "礦石", "refined_token": "METALBAR", "refined_label": "金屬錠"},
    "planks": {"label": "木板", "raw_token": "WOOD", "raw_label": "原木", "refined_token": "PLANKS", "refined_label": "木板"},
    "stoneblock": {"label": "石塊", "raw_token": "ROCK", "raw_label": "石材", "refined_token": "STONEBLOCK", "refined_label": "石塊"},
}

# First (non-faction) recipe from SBI's current items dump.  All five refining
# families share these raw-resource counts and produce one refined item.
RAW_COUNT_BY_TIER = {2: 1, 3: 2, 4: 2, 5: 3, 6: 4, 7: 5, 8: 5}

# Refining focus per craft follows effective tier (tier + enchantment).
# Values are taken from the same current item recipes.
FOCUS_BY_EFFECTIVE_TIER = {
    2: 18,
    3: 31,
    4: 54,
    5: 94,
    6: 164,
    7: 287,
    8: 503,
    9: 880,
    10: 1539,
    11: 2694,
    12: 4714,
}

# Base silver fees in SBI's current item data and the official Wiki's
# transmutation table.  The game UI may show a different payable amount at a
# particular station, so callers can (and should) override these defaults.
TRANSMUTATION_TIER_FEES = {
    5: {0: 781, 1: 1563, 2: 3125, 3: 6250, 4: 25000},
    6: {0: 1250, 1: 2500, 2: 5000, 3: 16500, 4: 66000},
    7: {0: 2500, 1: 5000, 2: 15750, 3: 51975, 4: 207900},
    8: {0: 5000, 1: 15000, 2: 47250, 3: 155925, 4: 779625},
}
TRANSMUTATION_ENCHANTMENT_FEES = {
    4: {1: 1500, 2: 3000, 3: 6000, 4: 24000},
    5: {1: 2000, 2: 4000, 3: 8000, 4: 32000},
    6: {1: 3000, 2: 6000, 3: 19800, 4: 79200},
    7: {1: 4800, 2: 15120, 3: 49896, 4: 199584},
    8: {1: 14400, 2: 45360, 3: 149688, 4: 784440},
}


class CraftingValidationError(ValueError):
    pass


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise CraftingValidationError(f"{label}必須是整數")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise CraftingValidationError(f"{label}必須是整數") from error
    if result < minimum or result > maximum:
        raise CraftingValidationError(f"{label}必須介於 {minimum} 到 {maximum}")
    return result


def _rate(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CraftingValidationError(f"{label}必須是數字") from error
    if not math.isfinite(result) or result < 0 or result >= 100:
        raise CraftingValidationError(f"{label}必須介於 0（含）到 100（不含）")
    return result


def item_id(tier: int, token: str, enchantment: int = 0) -> str:
    base = f"T{tier}_{token}"
    return base if enchantment == 0 else f"{base}_LEVEL{enchantment}@{enchantment}"


def item_label(family: str, tier: int, enchantment: int, kind: str) -> str:
    definition = RESOURCE_FAMILIES[family]
    label = definition["raw_label" if kind == "raw" else "refined_label"]
    return f"{tier}.{enchantment} {label}"


def icon_url(value: str) -> str:
    return f"https://render.albiononline.com/v1/item/{value}.png?size=96&quality=1"


def catalog() -> dict[str, Any]:
    families = []
    items = []
    for key, definition in RESOURCE_FAMILIES.items():
        families.append({"id": key, "label": definition["label"]})
        for tier in range(2, 9):
            for enchantment in range(0, 5):
                if enchantment and tier < 4:
                    continue
                for kind, token in (("raw", definition["raw_token"]), ("refined", definition["refined_token"])):
                    value = item_id(tier, token, enchantment)
                    items.append({
                        "item_id": value,
                        "name": item_label(key, tier, enchantment, kind),
                        "family": key,
                        "kind": kind,
                        "tier": tier,
                        "enchantment": enchantment,
                        "icon_url": icon_url(value),
                    })
    return {"families": families, "items": items, "tiers": list(range(2, 9)), "enchantments": list(range(0, 5))}


def _net_material(gross: int, focus_gross: int, normal_rate: float, focus_rate: float) -> tuple[int, int]:
    normal_gross = gross - focus_gross
    returned = math.floor(focus_gross * focus_rate / 100) + math.floor(normal_gross * normal_rate / 100)
    return gross - returned, returned


def build_refining_plan(payload: dict[str, Any]) -> dict[str, Any]:
    family = str(payload.get("family") or "leather")
    if family not in RESOURCE_FAMILIES:
        raise CraftingValidationError("不支援的精煉類型")
    target_tier = _integer(payload.get("target_tier", 7), "目標階級", 3, 8)
    start_tier = _integer(payload.get("start_tier", target_tier - 1), "起始成品階級", 2, 8)
    if start_tier > target_tier:
        raise CraftingValidationError("起始階級不可高於目標階級")
    enchantment = _integer(payload.get("enchantment", 0), "附魔等級", 0, 4)
    if enchantment and target_tier < 4:
        raise CraftingValidationError("附魔資源只能從 4 階開始")
    if enchantment and start_tier < 3:
        raise CraftingValidationError("附魔精煉鏈最早只能從 3 階成品開始")
    quantity = _integer(payload.get("quantity", 1), "目標數量", 1, 10_000_000)
    normal_rate = _rate(payload.get("return_rate", 36.7), "一般回報率")
    focus_rate = _rate(payload.get("focus_return_rate", 53.9), "專注回報率")
    use_focus = bool(payload.get("use_focus"))
    available_focus = _integer(payload.get("available_focus", 0), "目前專注點", 0, 2_000_000_000)
    remaining_focus = available_focus if use_focus else 0
    raw_station_fees = payload.get("station_fees") or {}
    if not isinstance(raw_station_fees, dict):
        raise CraftingValidationError("製作台費用格式無效")
    raw_focus_costs = payload.get("focus_costs") or {}
    if not isinstance(raw_focus_costs, dict):
        raise CraftingValidationError("專注消耗格式無效")

    definition = RESOURCE_FAMILIES[family]
    output_id = item_id(target_tier, definition["refined_token"], enchantment)
    needed_output = quantity
    descending_steps: list[dict[str, Any]] = []
    terminal_materials: list[dict[str, Any]] = []
    total_station_cost = 0
    total_focus_used = 0
    starting_material: dict[str, Any] | None = None

    for tier in range(target_tier, start_tier, -1):
        crafts = needed_output
        stage_output_id = item_id(tier, definition["refined_token"], enchantment)
        base_focus_cost = FOCUS_BY_EFFECTIVE_TIER[tier + enchantment]
        try:
            focus_cost = max(1, int(float(raw_focus_costs.get(stage_output_id, base_focus_cost))))
        except (TypeError, ValueError) as error:
            raise CraftingValidationError("每件專注消耗必須是數字") from error
        focus_crafts = min(crafts, remaining_focus // focus_cost) if use_focus else 0
        focus_used = focus_crafts * focus_cost
        remaining_focus -= focus_used
        total_focus_used += focus_used
        normal_crafts = crafts - focus_crafts
        raw_per_craft = RAW_COUNT_BY_TIER[tier]

        raw_gross = crafts * raw_per_craft
        raw_net, raw_returned = _net_material(
            raw_gross, focus_crafts * raw_per_craft, normal_rate, focus_rate
        )
        previous_gross = crafts
        previous_net, previous_returned = _net_material(
            previous_gross, focus_crafts, normal_rate, focus_rate
        )

        raw_id = item_id(tier, definition["raw_token"], enchantment)
        previous_id = item_id(tier - 1, definition["refined_token"], enchantment if tier - 1 >= 4 else 0)
        try:
            fee_per_craft = max(0, int(float(raw_station_fees.get(stage_output_id, 0) or 0)))
        except (TypeError, ValueError) as error:
            raise CraftingValidationError("製作台費用必須是數字") from error
        stage_fee = fee_per_craft * crafts
        total_station_cost += stage_fee

        raw_material = {
            "item_id": raw_id,
            "name": item_label(family, tier, enchantment, "raw"),
            "kind": "raw",
            "gross_quantity": raw_gross,
            "returned_quantity": raw_returned,
            "required_quantity": raw_net,
            "icon_url": icon_url(raw_id),
        }
        previous_material = {
            "item_id": previous_id,
            "name": item_label(family, tier - 1, enchantment if tier - 1 >= 4 else 0, "refined"),
            "kind": "previous_refined",
            "gross_quantity": previous_gross,
            "returned_quantity": previous_returned,
            "required_quantity": previous_net,
            "icon_url": icon_url(previous_id),
        }
        descending_steps.append({
            "tier": tier,
            "output_item_id": stage_output_id,
            "output_name": item_label(family, tier, enchantment, "refined"),
            "output_icon_url": icon_url(stage_output_id),
            "crafts": crafts,
            "raw_per_craft": raw_per_craft,
            "focus_cost_per_craft": focus_cost,
            "base_focus_cost_per_craft": base_focus_cost,
            "focus_crafts": focus_crafts,
            "normal_crafts": normal_crafts,
            "focus_used": focus_used,
            "fee_per_craft": fee_per_craft,
            "station_cost": stage_fee,
            "materials": [raw_material, previous_material],
        })
        terminal_materials.append(raw_material)
        if tier - 1 == start_tier:
            starting_material = previous_material
        needed_output = previous_net

    starting_enchantment = enchantment if start_tier >= 4 else 0
    starting_id = item_id(start_tier, definition["refined_token"], starting_enchantment)
    if starting_material is None:
        starting_material = {
            "item_id": starting_id,
            "name": item_label(family, start_tier, starting_enchantment, "refined"),
            "kind": "starting_refined",
            "gross_quantity": needed_output,
            "returned_quantity": 0,
            "required_quantity": needed_output,
            "icon_url": icon_url(starting_id),
        }
    else:
        starting_material = {**starting_material, "kind": "starting_refined"}
    terminal_materials.append(starting_material)

    terminal_materials.sort(key=lambda value: (int(value["item_id"][1]), value["kind"] != "starting_refined"))
    return {
        "family": family,
        "family_label": definition["label"],
        "target_tier": target_tier,
        "start_tier": start_tier,
        "enchantment": enchantment,
        "quantity": quantity,
        "return_rate": normal_rate,
        "focus_return_rate": focus_rate,
        "use_focus": use_focus,
        "available_focus": available_focus,
        "focus_used": total_focus_used,
        "focus_remaining": remaining_focus,
        "output_item": {
            "item_id": output_id,
            "name": item_label(family, target_tier, enchantment, "refined"),
            "quantity": quantity,
            "icon_url": icon_url(output_id),
        },
        "steps": list(reversed(descending_steps)),
        "materials": terminal_materials,
        "total_station_cost": total_station_cost,
        "calculation": "conservative_expected_net_consumption",
    }


def build_transmutation_options(payload: dict[str, Any]) -> dict[str, Any]:
    family = str(payload.get("family") or "metalbar")
    if family not in RESOURCE_FAMILIES or family == "stoneblock":
        raise CraftingValidationError("轉換比較支援纖維、粗皮、礦石與原木")
    target_tier = _integer(payload.get("target_tier", 6), "目標階級", 4, 8)
    enchantment = _integer(payload.get("enchantment", 1), "目標附魔", 1, 4)
    definition = RESOURCE_FAMILIES[family]
    target_id = item_id(target_tier, definition["raw_token"], enchantment)
    target = {
        "item_id": target_id,
        "name": item_label(family, target_tier, enchantment, "raw"),
        "icon_url": icon_url(target_id),
    }
    routes = []
    if target_tier >= 5:
        source_id = item_id(target_tier - 1, definition["raw_token"], enchantment)
        routes.append({
            "id": "tier",
            "label": "低一階同附魔",
            "description": f"{target_tier - 1}.{enchantment} → {target_tier}.{enchantment}",
            "input_item": {
                "item_id": source_id,
                "name": item_label(family, target_tier - 1, enchantment, "raw"),
                "icon_url": icon_url(source_id),
            },
            "input_per_output": 1,
            "base_silver_fee": TRANSMUTATION_TIER_FEES[target_tier][enchantment],
        })
    source_id = item_id(target_tier, definition["raw_token"], enchantment - 1)
    routes.append({
        "id": "enchantment",
        "label": "同階升附魔",
        "description": f"{target_tier}.{enchantment - 1} → {target_tier}.{enchantment}",
        "input_item": {
            "item_id": source_id,
            "name": item_label(family, target_tier, enchantment - 1, "raw"),
            "icon_url": icon_url(source_id),
        },
        "input_per_output": 1,
        "base_silver_fee": TRANSMUTATION_ENCHANTMENT_FEES[target_tier][enchantment],
    })
    return {
        "family": family,
        "target_tier": target_tier,
        "enchantment": enchantment,
        "target_item": target,
        "routes": routes,
        "calculation": "one_input_plus_station_silver_per_output",
    }
