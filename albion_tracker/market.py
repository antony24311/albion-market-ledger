from __future__ import annotations

import gzip
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


MARKET_CLUSTERS = {
    "west": {"label": "美洲", "host": "west.albion-online-data.com"},
    "east": {"label": "亞洲", "host": "east.albion-online-data.com"},
    "europe": {"label": "歐洲", "host": "europe.albion-online-data.com"},
}
MARKET_LOCATIONS = (
    "Bridgewatch",
    "Caerleon",
    "Fort Sterling",
    "Lymhurst",
    "Martlock",
    "Thetford",
    "Brecilien",
    "Black Market",
)
SAFE_ITEM_ID = re.compile(r"^[A-Z0-9_@.-]+$")


class MarketError(RuntimeError):
    pass


def _ssl_context() -> ssl.SSLContext:
    for certificate_file in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem"):
        try:
            return ssl.create_default_context(cafile=certificate_file)
        except (FileNotFoundError, ssl.SSLError):
            continue
    return ssl.create_default_context()


def market_config() -> dict[str, Any]:
    return {
        "clusters": [{"id": key, "label": value["label"]} for key, value in MARKET_CLUSTERS.items()],
        "locations": list(MARKET_LOCATIONS),
    }


def _request_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "albion-local-ledger/0.7", "Accept": "application/json", "Accept-Encoding": "gzip"},
    )
    last_error: Exception | None = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
                body = response.read(2_000_000)
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
            return json.loads(body)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as error:
            last_error = error
    raise MarketError("市場價格服務暫時無法連線") from last_error


def _history_prices(item_ids: list[str], cluster: str, location: str, timeout: float) -> dict[str, dict[str, Any]]:
    encoded_ids = urllib.parse.quote(",".join(item_ids), safe=",@._-")
    query = urllib.parse.urlencode({"locations": location, "qualities": "1", "time-scale": "24"})
    url = f"https://{MARKET_CLUSTERS[cluster]['host']}/api/v2/stats/history/{encoded_ids}.json?{query}"
    payload = _request_json(url, timeout)
    if not isinstance(payload, list):
        raise MarketError("市場歷史價格格式無效")
    result: dict[str, dict[str, Any]] = {}
    for raw in payload:
        if not isinstance(raw, dict) or raw.get("item_id") not in item_ids or not isinstance(raw.get("data"), list):
            continue
        samples = [sample for sample in raw["data"] if isinstance(sample, dict) and int(sample.get("avg_price") or 0) > 0][-14:]
        if not samples:
            continue
        weighted_count = sum(max(0, int(sample.get("item_count") or 0)) for sample in samples)
        if weighted_count:
            average = round(sum(int(sample["avg_price"]) * max(0, int(sample.get("item_count") or 0)) for sample in samples) / weighted_count)
        else:
            average = round(sum(int(sample["avg_price"]) for sample in samples) / len(samples))
        result[str(raw["item_id"])] = {
            "history_avg_price": max(0, average),
            "history_avg_date": samples[-1].get("timestamp"),
            "history_sample_days": len(samples),
        }
    return result


def fetch_market_prices(item_ids: list[str], cluster: str, location: str, *, timeout: float = 6.0) -> list[dict[str, Any]]:
    unique = list(dict.fromkeys(str(value).strip() for value in item_ids if str(value).strip()))[:40]
    if not unique or any(not SAFE_ITEM_ID.fullmatch(value) for value in unique):
        raise MarketError("物品代碼格式無效")
    if cluster not in MARKET_CLUSTERS:
        raise MarketError("市場伺服器無效")
    if location not in MARKET_LOCATIONS:
        raise MarketError("市場城市無效")
    encoded_ids = urllib.parse.quote(",".join(unique), safe=",@._-")
    query = urllib.parse.urlencode({"locations": location, "qualities": "1"})
    url = f"https://{MARKET_CLUSTERS[cluster]['host']}/api/v2/stats/prices/{encoded_ids}.json?{query}"
    current_error: MarketError | None = None
    try:
        payload = _request_json(url, timeout)
    except MarketError as error:
        payload = []
        current_error = error
    if not isinstance(payload, list):
        raise MarketError("市場價格服務回傳格式無效")
    current_by_id = {str(raw.get("item_id")): raw for raw in payload if isinstance(raw, dict) and raw.get("item_id") in unique}
    missing = [item_id for item_id in unique if not int((current_by_id.get(item_id) or {}).get("sell_price_min") or 0)]
    history: dict[str, dict[str, Any]] = {}
    if missing:
        try:
            history = _history_prices(missing, cluster, location, timeout)
        except MarketError:
            if current_error and not current_by_id:
                raise current_error
    result = []
    for item_id in unique:
        raw = current_by_id.get(item_id) or {}
        result.append({
            "item_id": item_id,
            "city": raw.get("city") or location,
            "quality": int(raw.get("quality") or 1),
            "sell_price_min": max(0, int(raw.get("sell_price_min") or 0)),
            "sell_price_min_date": raw.get("sell_price_min_date"),
            "buy_price_max": max(0, int(raw.get("buy_price_max") or 0)),
            "buy_price_max_date": raw.get("buy_price_max_date"),
            "source": "albion_online_data",
            **history.get(item_id, {"history_avg_price": 0, "history_avg_date": None, "history_sample_days": 0}),
        })
    return result
