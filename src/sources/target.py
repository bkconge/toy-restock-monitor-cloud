"""Target.com source.

Routes a URL to RedSky (PDP -> product_summary_with_fulfillment_v1,
PLP/search -> plp_search_v2) and falls back to JSON-LD on the PDP HTML
when RedSky returns non-200, JSON decode fails, or the response shape
doesn't match the expected schema (the three explicit triggers from
plan.md task #6).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

from src.sources._target_key import REDSKY_KEY
from src.sources.base import (
    HttpClient,
    HttpResponse,
    StockSnapshot,
    _MissingLocationError,
    snapshot_from_missing_location,
    snapshot_from_network_error,
)


log = logging.getLogger(__name__)


_REDSKY_HOST = "https://redsky.target.com"
_PDP_PATH = "/redsky_aggregations/v1/web/product_summary_with_fulfillment_v1"
_PLP_PATH = "/redsky_aggregations/v1/web/plp_search_v2"

# Plan/spec called for store_id=3991, but the live RedSky API (verified
# 2026-05-23) rejects 3991 with "store_id cannot be digital store 3991".
# 2776 (LA Central) is a real fulfillment store close to Brian's region;
# availability for the watched products is national-shipping-driven anyway,
# so this only affects local-store fields in the response we don't read.
_STORE_ID = "2776"

_PDP_TCIN_RE = re.compile(r"/-/A-(\d+)")
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _is_pdp(url: str) -> bool:
    return "/p/" in url and "/-/A-" in url


def _is_search(url: str) -> bool:
    return "/s?" in url or "/s/?" in url or url.endswith("/s")


def _tcin_from_url(url: str) -> str | None:
    m = _PDP_TCIN_RE.search(url)
    return m.group(1) if m else None


def _search_term_from_url(url: str) -> str | None:
    qs = urllib.parse.urlsplit(url).query
    params = urllib.parse.parse_qs(qs)
    for k in ("searchTerm", "term", "q"):
        if k in params and params[k]:
            return params[k][0]
    return None


class TargetSource:
    """Source impl for target.com URLs (PDP + search)."""

    def fetch(
        self,
        url: str,
        *,
        http_client: HttpClient,
        user_agent: str,
    ) -> StockSnapshot:
        http_client.ensure_target_warmup(user_agent)
        if _is_pdp(url):
            return self._fetch_pdp(url, http_client=http_client, user_agent=user_agent)
        if _is_search(url):
            return self._fetch_search(url, http_client=http_client, user_agent=user_agent)
        return StockSnapshot(
            in_stock=False,
            price=None,
            title=None,
            sku=None,
            http_status=None,
            error=f"unrecognized Target URL shape: {url}",
            parse_error=False,
        )

    def _redsky_get(
        self,
        path: str,
        params: dict[str, str],
        *,
        http_client: HttpClient,
        user_agent: str,
        referer: str,
    ) -> HttpResponse | StockSnapshot:
        qs = urllib.parse.urlencode(params)
        full = f"{_REDSKY_HOST}{path}?{qs}"
        try:
            return http_client.request(
                full,
                ua=user_agent,
                extra_headers={
                    "Referer": referer,
                    "Origin": "https://www.target.com",
                    "Accept": "application/json",
                },
            )
        except _MissingLocationError as e:
            return snapshot_from_missing_location(e)
        except Exception as e:
            return snapshot_from_network_error(e)

    def _fetch_pdp(
        self,
        url: str,
        *,
        http_client: HttpClient,
        user_agent: str,
    ) -> StockSnapshot:
        tcin = _tcin_from_url(url)
        if tcin is None:
            return StockSnapshot(
                in_stock=False,
                price=None,
                title=None,
                sku=None,
                http_status=None,
                error=f"could not extract tcin from PDP url: {url}",
                parse_error=False,
            )
        params = {
            "key": REDSKY_KEY,
            # Plan called for "tcin" (singular); the live endpoint requires
            # "tcins" (plural) and rejects "tcin" with a GraphQL Null error.
            "tcins": tcin,
            "store_id": _STORE_ID,
            "pricing_store_id": _STORE_ID,
            "has_pricing_store_id": "true",
            "visitor_id": http_client.visitor_id,
            "channel": "WEB",
            "page": f"/p/A-{tcin}",
        }
        resp_or_snap = self._redsky_get(
            _PDP_PATH, params,
            http_client=http_client, user_agent=user_agent, referer=url,
        )
        if isinstance(resp_or_snap, StockSnapshot):
            return self._html_fallback(url, http_client=http_client, user_agent=user_agent, tcin=tcin)

        resp = resp_or_snap

        if resp.status != 200:
            log.info("target redsky pdp non-200 status=%d; trying HTML fallback", resp.status)
            return self._html_fallback(
                url, http_client=http_client, user_agent=user_agent, tcin=tcin,
                upstream_status=resp.status,
            )

        try:
            data = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.info("target redsky pdp JSON decode failed; trying HTML fallback")
            return self._html_fallback(url, http_client=http_client, user_agent=user_agent, tcin=tcin)

        parsed = self._parse_pdp_payload(data, tcin=tcin)
        if parsed is None:
            log.info("target redsky pdp shape mismatch; trying HTML fallback")
            return self._html_fallback(url, http_client=http_client, user_agent=user_agent, tcin=tcin)

        return parsed

    def _parse_pdp_payload(self, data: Any, *, tcin: str) -> StockSnapshot | None:
        # Live endpoint shape (2026-05): data.product_summaries[<n>]. The
        # plan referenced data.product (singular) which doesn't match the
        # current GraphQL aggregation; we accept either to remain tolerant
        # if Target rolls a new shape under the same endpoint name.
        if not isinstance(data, dict):
            return None
        wrapper = data.get("data")
        if not isinstance(wrapper, dict):
            return None
        product: dict[str, Any] | None = None
        summaries = wrapper.get("product_summaries")
        if isinstance(summaries, list) and summaries and isinstance(summaries[0], dict):
            product = summaries[0]
        elif isinstance(wrapper.get("product"), dict):
            product = wrapper["product"]
        if product is None:
            return None

        item = product.get("item")
        title = None
        if isinstance(item, dict):
            desc = item.get("product_description")
            if isinstance(desc, dict):
                title = desc.get("title")

        price = None
        price_block = product.get("price")
        if isinstance(price_block, dict):
            for k in ("current_retail", "current_retail_min", "reg_retail"):
                v = price_block.get(k)
                if isinstance(v, (int, float)):
                    price = float(v)
                    break

        in_stock = self._pdp_in_stock(product)
        if in_stock is None:
            return None

        return StockSnapshot(
            in_stock=in_stock,
            price=price,
            title=title,
            sku=tcin,
            http_status=200,
            error=None,
            parse_error=False,
        )

    def _pdp_in_stock(self, product: dict[str, Any]) -> bool | None:
        # Availability lives under fulfillment.shipping_options.availability_status
        # in product_summary_with_fulfillment_v1 (the only call we make for the
        # PDP) — values observed: IN_STOCK, OUT_OF_STOCK, LIMITED_STOCK.
        fulfillment = product.get("fulfillment")
        if isinstance(fulfillment, dict):
            shipping = fulfillment.get("shipping_options")
            if isinstance(shipping, dict):
                avail = shipping.get("availability_status") or shipping.get("availability")
                if isinstance(avail, str):
                    return avail.upper() in ("IN_STOCK", "AVAILABLE", "LIMITED_STOCK", "PRE_ORDER_SELLABLE")
            avail = fulfillment.get("availability_status")
            if isinstance(avail, str):
                return avail.upper() in ("IN_STOCK", "AVAILABLE", "LIMITED_STOCK", "PRE_ORDER_SELLABLE")
        return None

    def _fetch_search(
        self,
        url: str,
        *,
        http_client: HttpClient,
        user_agent: str,
    ) -> StockSnapshot:
        term = _search_term_from_url(url)
        if not term:
            return StockSnapshot(
                in_stock=False, price=None, title=None, sku=None,
                http_status=None,
                error=f"could not extract searchTerm from {url}",
                parse_error=False,
            )
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
            "visitor_id": http_client.visitor_id,
        }
        resp_or_snap = self._redsky_get(
            _PLP_PATH, params,
            http_client=http_client, user_agent=user_agent, referer=url,
        )
        if isinstance(resp_or_snap, StockSnapshot):
            return resp_or_snap
        resp = resp_or_snap
        if resp.status != 200:
            return StockSnapshot(
                in_stock=False, price=None, title=None, sku=None,
                http_status=resp.status,
                error=f"target plp non-200 status={resp.status}",
                parse_error=False,
            )
        try:
            data = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return StockSnapshot(
                in_stock=False, price=None, title=None, sku=None,
                http_status=200,
                error=f"plp JSON decode failed: {e}",
                parse_error=True,
            )
        parsed = self._parse_plp_payload(data)
        if parsed is None:
            return StockSnapshot(
                in_stock=False, price=None, title=None, sku=None,
                http_status=200,
                error="plp shape mismatch",
                parse_error=True,
            )
        return parsed

    def _parse_plp_payload(self, data: Any) -> StockSnapshot | None:
        search = (
            data.get("data", {}).get("search") if isinstance(data, dict) else None
        )
        if not isinstance(search, dict):
            return None
        products = search.get("products")
        if not isinstance(products, list):
            return None
        for entry in products:
            if not isinstance(entry, dict):
                continue
            tcin = entry.get("tcin")
            item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
            title = None
            desc = item.get("product_description") if isinstance(item, dict) else None
            if isinstance(desc, dict):
                title = desc.get("title")
            price = None
            price_block = entry.get("price")
            if isinstance(price_block, dict):
                for k in ("current_retail", "current_retail_min", "reg_retail"):
                    v = price_block.get(k)
                    if isinstance(v, (int, float)):
                        price = float(v)
                        break
            availability = entry.get("availability_status")
            if availability is None:
                fulfillment = entry.get("fulfillment")
                if isinstance(fulfillment, dict):
                    availability = fulfillment.get("availability_status") or fulfillment.get("availability")
            in_stock = isinstance(availability, str) and availability.upper() in (
                "IN_STOCK", "AVAILABLE", "LIMITED_STOCK", "PRE_ORDER_SELLABLE",
            )
            if in_stock:
                return StockSnapshot(
                    in_stock=True,
                    price=price,
                    title=title,
                    sku=str(tcin) if tcin is not None else None,
                    http_status=200,
                    error=None,
                    parse_error=False,
                )
        # No in-stock match — surface the first product (if any) as OOS so the
        # caller still records something useful. If products list is empty,
        # also OOS with no sku.
        first = products[0] if products else {}
        first_tcin = first.get("tcin") if isinstance(first, dict) else None
        return StockSnapshot(
            in_stock=False,
            price=None,
            title=None,
            sku=str(first_tcin) if first_tcin is not None else None,
            http_status=200,
            error=None,
            parse_error=False,
        )

    def _html_fallback(
        self,
        url: str,
        *,
        http_client: HttpClient,
        user_agent: str,
        tcin: str,
        upstream_status: int | None = None,
    ) -> StockSnapshot:
        try:
            resp = http_client.request(url, ua=user_agent)
        except _MissingLocationError as e:
            return snapshot_from_missing_location(e)
        except Exception as e:
            return snapshot_from_network_error(e, http_status=upstream_status)

        if resp.status != 200:
            return StockSnapshot(
                in_stock=False, price=None, title=None, sku=tcin,
                http_status=resp.status,
                error=f"html fallback non-200 status={resp.status} (upstream={upstream_status})",
                parse_error=False,
            )

        try:
            html = resp.body.decode("utf-8", errors="replace")
        except Exception as e:
            return StockSnapshot(
                in_stock=False, price=None, title=None, sku=tcin,
                http_status=200,
                error=f"html fallback decode error: {e}",
                parse_error=True,
            )

        for m in _JSONLD_RE.finditer(html):
            blob = m.group(1).strip()
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                continue
            extracted = self._extract_from_jsonld(data, tcin=tcin)
            if extracted is not None:
                return extracted

        return StockSnapshot(
            in_stock=False, price=None, title=None, sku=tcin,
            http_status=200,
            error="html fallback: no parseable JSON-LD offers block",
            parse_error=True,
        )

    def _extract_from_jsonld(self, data: Any, *, tcin: str) -> StockSnapshot | None:
        candidates: list[dict[str, Any]] = []
        if isinstance(data, dict):
            candidates.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                for g in graph:
                    if isinstance(g, dict):
                        candidates.append(g)
        elif isinstance(data, list):
            for d in data:
                if isinstance(d, dict):
                    candidates.append(d)

        for c in candidates:
            offers = c.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if not isinstance(offers, dict):
                continue
            avail = offers.get("availability")
            price = offers.get("price")
            if avail is None and price is None:
                continue
            in_stock = isinstance(avail, str) and "instock" in avail.lower()
            try:
                price_f = float(price) if price is not None else None
            except (TypeError, ValueError):
                price_f = None
            title = c.get("name")
            return StockSnapshot(
                in_stock=in_stock,
                price=price_f,
                title=title if isinstance(title, str) else None,
                sku=tcin,
                http_status=200,
                error=None,
                parse_error=False,
            )
        return None
