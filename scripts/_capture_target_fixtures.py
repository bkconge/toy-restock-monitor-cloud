"""One-shot fixture capture from live Target.

Goes through HttpClient (UA rotation + cookies + warmup) and writes raw
RedSky JSON bodies into tests/fixtures/. Synthesizes OOS / malformed / 429
fixtures from the in-stock capture so we never need to repeat the live
fetch for derived shapes.

Run: .venv/bin/python scripts/_capture_target_fixtures.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.parse

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.sources._target_key import REDSKY_KEY
from src.sources.base import HttpClient
from src.sources.target import _REDSKY_HOST, _PDP_PATH, _PLP_PATH, _STORE_ID


FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


def _redsky_pdp_url(tcin: str, visitor_id: str) -> str:
    params = {
        "key": REDSKY_KEY,
        "tcins": tcin,
        "store_id": _STORE_ID,
        "pricing_store_id": _STORE_ID,
        "has_pricing_store_id": "true",
        "visitor_id": visitor_id,
        "channel": "WEB",
        "page": f"/p/A-{tcin}",
    }
    return f"{_REDSKY_HOST}{_PDP_PATH}?{urllib.parse.urlencode(params)}"


def _redsky_plp_url(term: str, visitor_id: str) -> str:
    params = {
        "key": REDSKY_KEY,
        "channel": "WEB",
        "count": "24",
        "default_purchasability_filter": "true",
        "include_sponsored": "false",
        "keyword": term,
        "offset": "0",
        "page": f"/s/{term}",
        "platform": "desktop",
        "pricing_store_id": _STORE_ID,
        "store_ids": _STORE_ID,
        "visitor_id": visitor_id,
    }
    return f"{_REDSKY_HOST}{_PLP_PATH}?{urllib.parse.urlencode(params)}"


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    client = HttpClient(PROJECT_ROOT / "data")
    ua = client.select_ua()
    print(f"using UA: {ua[:60]}...")

    referer_strawberry = "https://www.target.com/p/sunny-days-squeezy-strawberry/-/A-94757072"
    referer_cheese = "https://www.target.com/p/sunny-days-squeezy-cheese-block/-/A-1003785284"
    referer_neeoh = "https://www.target.com/s?searchTerm=nee+doh"

    client.ensure_target_warmup(ua)

    pdp_strawberry_url = _redsky_pdp_url("94757072", client.visitor_id)
    pdp_cheese_url = _redsky_pdp_url("1003785284", client.visitor_id)
    plp_neeoh_url = _redsky_plp_url("nee doh", client.visitor_id)

    fixtures: list[tuple[str, str, dict[str, str], pathlib.Path]] = [
        (
            "strawberry PDP",
            pdp_strawberry_url,
            {"Referer": referer_strawberry, "Origin": "https://www.target.com", "Accept": "application/json"},
            FIXTURES_DIR / "target_redsky_in_stock.json",
        ),
        (
            "cheese PDP",
            pdp_cheese_url,
            {"Referer": referer_cheese, "Origin": "https://www.target.com", "Accept": "application/json"},
            FIXTURES_DIR / "target_redsky_cheese_pdp.json",
        ),
        (
            "nee doh PLP",
            plp_neeoh_url,
            {"Referer": referer_neeoh, "Origin": "https://www.target.com", "Accept": "application/json"},
            FIXTURES_DIR / "target_plp_search_v2.json",
        ),
    ]

    captures: dict[str, dict] = {}
    for label, url, extra, path in fixtures:
        print(f"\nfetching {label}: {url[:120]}...")
        try:
            resp = client.request(url, ua=ua, extra_headers=extra)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        print(f"  status={resp.status} body_len={len(resp.body)}")
        if resp.status == 200 and resp.body:
            path.write_bytes(resp.body)
            print(f"  wrote {path}")
            try:
                captures[label] = json.loads(resp.body.decode("utf-8"))
            except Exception as e:
                print(f"  JSON decode warning: {e}")
        else:
            print(f"  skipping write (non-200 or empty)")

    # Strawberry is currently OOS live. Save the raw OOS capture as the OOS
    # fixture, then synthesize an in-stock fixture by flipping availability
    # to IN_STOCK and injecting a price block (the endpoint doesn't return
    # price for OOS items, but the parser expects price under
    # product_summaries[0].price.current_retail = 5.99 per the plan).
    captured_raw = captures.get("strawberry PDP")
    if captured_raw is not None:
        oos_path = FIXTURES_DIR / "target_redsky_oos.json"
        oos_path.write_text(json.dumps(captured_raw, indent=2))
        print(f"\nwrote raw OOS capture as OOS fixture")

        in_stock = json.loads(json.dumps(captured_raw))
        try:
            summary = in_stock["data"]["product_summaries"][0]
            ff = summary.setdefault("fulfillment", {})
            shipping = ff.setdefault("shipping_options", {})
            shipping["availability_status"] = "IN_STOCK"
            ff["is_out_of_stock_in_all_store_locations"] = False
            ff["sold_out"] = False
            summary["price"] = {
                "current_retail": 5.99,
                "formatted_current_price": "$5.99",
                "formatted_current_price_type": "reg",
                "reg_retail": 5.99,
            }
            (FIXTURES_DIR / "target_redsky_in_stock.json").write_text(
                json.dumps(in_stock, indent=2)
            )
            print(f"wrote synthesized in-stock fixture (avail=IN_STOCK, price=5.99)")
        except (KeyError, TypeError, IndexError) as e:
            print(f"could not synthesize in-stock fixture: {e}")

        raw = json.dumps(captured_raw)
        truncated = raw[: max(40, len(raw) // 2)]
        (FIXTURES_DIR / "target_redsky_malformed.json").write_text(truncated)
        print(f"wrote synthesized malformed fixture (truncated mid-JSON)")

        synthetic_429 = {
            "errors": [
                {
                    "code": "RATE_LIMIT",
                    "message": "Too many requests",
                }
            ]
        }
        (FIXTURES_DIR / "target_429.json").write_text(json.dumps(synthetic_429, indent=2))
        print(f"wrote synthesized 429 fixture")
    else:
        print(f"\nno strawberry capture; cannot synthesize OOS/in-stock/malformed/429")

    jsonld_html = (
        "<!doctype html><html><head>"
        '<script type="application/ld+json">'
        "{"
        '"@context":"https://schema.org",'
        '"@type":"Product",'
        '"name":"Sunny Days Squeezy Strawberry",'
        '"sku":"94757072",'
        '"offers":{'
        '"@type":"Offer",'
        '"availability":"https://schema.org/InStock",'
        '"price":"5.99",'
        '"priceCurrency":"USD"'
        "}}"
        "</script>"
        "</head><body></body></html>"
    )
    (FIXTURES_DIR / "target_pdp_jsonld.html").write_text(jsonld_html)
    print(f"wrote hand-crafted JSON-LD HTML fixture")

    return 0


if __name__ == "__main__":
    sys.exit(main())
