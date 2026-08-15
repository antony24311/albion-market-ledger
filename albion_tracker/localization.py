from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable


ITEM_API = "https://gameinfo.albiononline.com/api/gameinfo/items/{item_id}/data"
SAFE_ITEM_ID = re.compile(r"^[A-Z0-9_@.-]+$")

# Albion market cluster IDs.  Keep the original code visible in the UI tooltip,
# while presenting the place name people actually recognize.
LOCATION_NAMES = {
    "0007": "塞特福德 (Thetford)",
    "7": "塞特福德 (Thetford)",
    "1002": "林姆赫斯特 (Lymhurst)",
    "2004": "橋望城 (Bridgewatch)",
    "3005": "卡利昂 (Caerleon)",
    "3008": "馬特洛克 (Martlock)",
    "4002": "斯特靈堡 (Fort Sterling)",
    "5003": "布雷西利恩 (Brecilien)",
}

RESOURCE_NAMES = {
    "HIDE": "獸皮",
    "LEATHER": "皮革",
    "ORE": "礦石",
    "METALBAR": "金屬錠",
    "WOOD": "原木",
    "PLANKS": "木板",
    "FIBER": "纖維",
    "CLOTH": "布料",
    "ROCK": "石材",
    "STONEBLOCK": "石塊",
}

ITEM_TOKEN_NAMES = {
    **RESOURCE_NAMES,
    "BAG": "背包",
    "CAPE": "披風",
    "HEAD": "頭部裝備",
    "ARMOR": "護甲",
    "SHOES": "鞋子",
    "POTION": "藥水",
    "MEAL": "食物",
    "MOUNT": "坐騎",
    "SWORD": "劍",
    "AXE": "斧",
    "HAMMER": "錘",
    "BOW": "弓",
    "CROSSBOW": "弩",
    "STAFF": "法杖",
    "SHIELD": "盾牌",
}


def location_name(location_id: str | None) -> str:
    if not location_id:
        return "—"
    value = str(location_id).strip()
    if value in LOCATION_NAMES:
        return LOCATION_NAMES[value]
    base = value.split("-", 1)[0]
    if base in LOCATION_NAMES:
        return LOCATION_NAMES[base]
    if value.startswith("BLACKBANK-"):
        return "黑市銀行 (Black Market Bank)"
    if any(character.isalpha() for character in value):
        return value
    return f"未知地點 ({value})"


def item_category(item_id: str) -> str:
    value = item_id.upper()
    if any(f"_{token}" in value for token in RESOURCE_NAMES):
        return "資源"
    if any(token in value for token in ("POTION", "MEAL", "FOOD", "FISH", "JOURNAL")):
        return "消耗品"
    if any(token in value for token in ("MOUNT", "MAMMOTH", "HORSE", "OX")):
        return "坐騎"
    if any(token in value for token in ("BAG", "CAPE", "HEAD", "ARMOR", "SHOES", "MAIN_", "2H_", "OFF_")):
        return "裝備"
    return "其他"


def fallback_item_name(item_id: str) -> str:
    """Return a readable local fallback while the official name is unavailable."""
    if item_id == "T1_HIDE":
        return "零碎獸皮"
    tier_match = re.match(r"^T(\d+)_", item_id)
    tier = f"T{tier_match.group(1)} " if tier_match else ""
    body = item_id.split("@", 1)[0]
    for token, translated in ITEM_TOKEN_NAMES.items():
        if f"_{token}" in body or body.endswith(token):
            enchantment = ""
            if "@" in item_id:
                enchantment = f" +{item_id.rsplit('@', 1)[1]}"
            return f"{tier}{translated}{enchantment}"
    return item_id


def _fetch_item_name(item_id: str, timeout: float) -> tuple[str, str | None]:
    if not SAFE_ITEM_ID.fullmatch(item_id):
        return item_id, None
    url = ITEM_API.format(item_id=urllib.parse.quote(item_id, safe="@._-"))
    request = urllib.request.Request(url, headers={"User-Agent": "albion-local-ledger/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
        names = payload.get("localizedNames") or {}
        name = names.get("ZH-TW") or names.get("ZH-CN") or names.get("EN-US")
        if isinstance(name, str) and name.strip():
            return item_id, name.strip()
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        pass
    return item_id, None


def resolve_item_names(item_ids: Iterable[str], *, timeout: float = 3.0) -> dict[str, str]:
    unique = list(dict.fromkeys(item_id for item_id in item_ids if item_id))[:30]
    if not unique:
        return {}
    resolved: dict[str, str] = {}
    workers = min(6, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fetch_item_name, item_id, timeout) for item_id in unique]
        for future in as_completed(futures):
            item_id, name = future.result()
            if name:
                resolved[item_id] = name
    return resolved
