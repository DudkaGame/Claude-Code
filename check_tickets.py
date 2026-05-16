#!/usr/bin/env python3
"""Aeroflot subsidized tickets monitor.

Strategy:
1. Open the subsidized search SPA in headless Chromium via Playwright to
   pass Aeroflot's JS bot-challenge and obtain a valid browser context.
2. Call the internal JSON API
   ``POST /se/api/app/flight/subsidized/search/v3``
   via ``page.evaluate(fetch(...))`` so the request runs inside the page's
   fingerprinted DOM context (direct requests from Python are blocked 503).
3. For each configured outbound and return date (one-way requests), parse
   ``data.route_itineraries`` — a non-empty array means at least ``qty``
   subsidized seats are currently available on that leg.
4. Notify via Telegram using a proxy chain (first SOCKS5 that passes a live
   ``getMe`` healthcheck is used; if all proxies fail, a last-resort direct
   connection is attempted). Persist matches in ``state.json`` to avoid
   duplicate notifications.
5. Once per day (on the first successful run of that day) a heartbeat is
   sent to Telegram so the user sees the agent is alive even when no new
   flights appear.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "state.json"
# Snapshot of the previous run's flights, used to compute per-run diffs
# ("appeared / disappeared since last check"). Stored next to state.json
# because it's state, not a log. Cleared by --reset-state.
LAST_FLIGHTS_PATH = SCRIPT_DIR / "last_flights.json"
LOG_DIR = Path(os.environ.get("AEROFLOT_LOG_DIR", SCRIPT_DIR / "logs"))
SCANS_PATH = LOG_DIR / "scans.jsonl"

SPA_URL = "https://www.aeroflot.ru/sb/subsidized/app/ru-ru#/search"
API_PATH = "/se/api/app/flight/subsidized/search/v3"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class FoundFlight:
    leg_date: str
    direction: str
    flight_no: str
    seats: int | None
    price: int | None
    departure: str | None
    arrival: str | None
    # Operating carrier when it differs from the marketing carrier (SU).
    # E.g. "FV Россия" for SU6293 → operated by Rossiya as FV6293, or
    # "iFly (франшиза)" for SU58xx. None means "real Aeroflot metal".
    operator: str | None = None
    # Number of physical segments in the itinerary. 1 = direct flight,
    # >1 = at least one connection. Direct-only is the default filter.
    segments: int = 1

    def key(self) -> str:
        return f"{self.leg_date}|{self.direction}|{self.flight_no}"


def _ddmmyyyy(iso_date: str) -> str:
    """2026-08-31 -> 31/08/2026."""
    try:
        y, m, d = iso_date.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return iso_date


def _hhmm(ts: str | None) -> str:
    """'2026-08-29 01:20' -> '01:20'. Returns '' for unparseable input."""
    if not ts or " " not in ts:
        return ""
    return ts.split(" ", 1)[1][:5]


def format_flights_message(flights: list["FoundFlight"]) -> str:
    """One line per flight with enough detail to verify on the site.

    Previously this only showed a count per (date, direction). That was
    too lossy: when the user opened the Aeroflot site to book, they
    couldn't tell which of the N flights the bot meant, and any
    inaccuracy (codeshare counted as real, e.g.) was invisible.

    New format:
        21/08/2026 MOW→VVO SU1700 15:40  9 мест, 37 000 ₽
        29/08/2026 MOW→VVO SU1708 01:20  9 мест, 37 000 ₽

    Codeshare/franchise flights, if ever surfaced (off by default), get an
    "(оператор: …)" suffix so they're visually distinct from real SU
    flights.
    """
    lines: list[str] = []
    for f in sorted(flights, key=lambda x: (x.leg_date, x.direction, x.departure or "")):
        time_str = _hhmm(f.departure)
        seats_str = f"{f.seats} мест" if f.seats is not None else "места есть"
        if f.price:
            price_str = f", {f.price:,} ₽".replace(",", " ")
        else:
            price_str = ""
        op_str = f"  (оператор: {f.operator})" if f.operator else ""
        seg_str = f"  ({f.segments} сегмента)" if f.segments > 1 else ""
        lines.append(
            f"{_ddmmyyyy(f.leg_date)} {f.direction} {f.flight_no} {time_str}  "
            f"{seats_str}{price_str}{op_str}{seg_str}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Diff vs previous run
# --------------------------------------------------------------------------- #


def load_last_flights() -> tuple[set[str], bool]:
    """Load the previous run's flight keys.

    Returns ``(keys, exists)`` — ``exists`` is False on the very first run
    (no snapshot file yet), which the caller uses to suppress the diff
    section so the first message doesn't claim every flight just appeared.
    """
    if not LAST_FLIGHTS_PATH.exists():
        return set(), False
    try:
        with LAST_FLIGHTS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set(), False
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return set(), False
    return set(str(k) for k in keys), True


def save_last_flights(flights: list["FoundFlight"]) -> None:
    """Persist the current flight set as the next run's reference snapshot."""
    payload = {
        "ts": datetime.now(MSK).isoformat(timespec="seconds"),
        "keys": sorted(f.key() for f in flights),
    }
    LAST_FLIGHTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _format_flight_key(key: str) -> str:
    """``2026-08-23|MOW→VVO|1701`` -> ``23/08/2026 MOW→VVO рейс 1701``."""
    parts = key.split("|", 2)
    if len(parts) != 3:
        return key
    return f"{_ddmmyyyy(parts[0])} {parts[1]} рейс {parts[2]}"


def format_diff(
    current: list["FoundFlight"],
    previous_keys: set[str],
    previous_exists: bool,
) -> str:
    """Build the "diff since last check" block appended to every message.

    Format:
      * first ever run → ``Отличий по рейсам - 0 (первая проверка)``
      * no change      → ``Отличий по рейсам - 0``
      * change         → header + one ``+`` line per new flight + one
                         ``-`` line per disappeared flight, sorted.
    """
    if not previous_exists:
        return "Отличий по рейсам - 0 (первая проверка)"

    current_keys = {f.key() for f in current}
    appeared = sorted(current_keys - previous_keys)
    disappeared = sorted(previous_keys - current_keys)

    if not appeared and not disappeared:
        return "Отличий по рейсам - 0"

    lines = ["Отличия от прошлой проверки:"]
    for k in appeared:
        lines.append(f"+ появился {_format_flight_key(k)}")
    for k in disappeared:
        lines.append(f"- пропал {_format_flight_key(k)}")
    return "\n".join(lines)


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"check_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return log_path


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        sys.exit(f"config.json not found at {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Telegram with proxy-chain failover
# --------------------------------------------------------------------------- #


def _telegram_proxy_chain(tg: dict[str, Any]) -> list[str | None]:
    """Return the ordered list of SOCKS5 proxies to try (last None = direct)."""
    chain: list[str | None] = []
    explicit = tg.get("proxy_chain")
    if isinstance(explicit, list):
        for item in explicit:
            if isinstance(item, str) and item.strip():
                chain.append(item.strip())
            elif item is None:
                chain.append(None)
    legacy = tg.get("proxy_socks5")
    if isinstance(legacy, str) and legacy.strip() and legacy.strip() not in chain:
        chain.append(legacy.strip())
    if not chain:
        chain.append(None)
    # Always end with a direct attempt as last resort if not already present.
    if None not in chain:
        chain.append(None)
    return chain


def _telegram_proxies(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _proxy_label(proxy: str | None) -> str:
    return proxy if proxy else "direct"


def _try_get_me(token: str, proxy: str | None, timeout: float = 8.0) -> bool:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            proxies=_telegram_proxies(proxy),
            timeout=timeout,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True
        logging.warning(
            "telegram getMe via %s: http=%s body=%s",
            _proxy_label(proxy),
            r.status_code,
            r.text[:200],
        )
        return False
    except Exception as e:
        logging.warning("telegram getMe via %s failed: %s", _proxy_label(proxy), e)
        return False


def pick_working_proxy(tg: dict[str, Any]) -> tuple[str | None, list[tuple[str, bool]]]:
    """Try each entry in the proxy chain, return the first that works with getMe.

    Returns a tuple ``(proxy_or_None, results)`` where ``results`` is a list of
    ``(label, ok)`` suitable for heartbeat reporting. If none work, returns
    ``(None, results)`` — the caller can still attempt the send but should
    expect it to fail.
    """
    token = tg["bot_token"]
    chain = _telegram_proxy_chain(tg)
    results: list[tuple[str, bool]] = []
    working: str | None = None
    for proxy in chain:
        ok = _try_get_me(token, proxy)
        results.append((_proxy_label(proxy), ok))
        if ok and working is None:
            working = proxy if proxy else ""  # "" means direct but confirmed
            # Don't short-circuit — keep probing so heartbeat sees full picture.
    # Translate sentinel back: first successful proxy (or None if only "direct" worked).
    if working is None:
        return None, results
    return (working if working != "" else None), results


def send_telegram(cfg: dict[str, Any], text: str) -> str:
    """Send a Telegram message, trying each proxy in the chain until one works.

    Returns the label of the proxy that ultimately delivered the message
    (useful for logs / heartbeat). Raises the last exception if every
    attempt fails.
    """
    tg = cfg["telegram"]
    token = tg["bot_token"]
    chain = _telegram_proxy_chain(tg)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": tg["chat_id"],
        "text": text,
        "disable_web_page_preview": True,
    }
    last_err: Exception | None = None
    for proxy in chain:
        # Quick healthcheck before sending, so we don't blindly hit a dead tunnel.
        if not _try_get_me(token, proxy, timeout=8.0):
            continue
        try:
            resp = requests.post(
                url,
                json=payload,
                proxies=_telegram_proxies(proxy),
                timeout=25,
            )
            resp.raise_for_status()
            label = _proxy_label(proxy)
            logging.info("telegram sent via %s", label)
            return label
        except Exception as e:
            last_err = e
            logging.warning(
                "telegram send via %s failed: %s", _proxy_label(proxy), e
            )
    if last_err:
        raise last_err
    raise RuntimeError("no working telegram proxy in chain")


# --------------------------------------------------------------------------- #
# Flight extraction
# --------------------------------------------------------------------------- #


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.replace(" ", "").replace(",", ".")
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def extract_flights(
    payload: Any,
    direction: str,
    leg_date: str,
    min_seats: int,
    *,
    airline_code: str = "SU",
    passenger_type: str = "youth",
    passenger_qty: int = 1,
    allow_codeshare: bool = False,
    allow_connections: bool = False,
) -> list[FoundFlight]:
    """Parse the Aeroflot subsidized-search response into bookable flights.

    Response shape (as of May 2026):
        data.route_itineraries[0]  ->  list of itineraries for the queried route
        itinerary.legs[i].segments[j]
            -> the actual flight: marketing_flight_number,
               operating_flight_number, marketing_airline_code,
               operating_airline_code, departure, arrival, …
        itinerary.offers[k]
            -> fare offer: seat_quantity, price_total_amount,
               passenger_prices[*].passenger_type_code, …

    The old implementation walked the whole tree looking for any node with
    a "flight_number"-ish key, then read seats off the SAME node. That was
    wrong on two counts:

    1. seats live on `offers`, NOT on segments — so the min_seats filter
       was effectively bypassed (seats came back as None, which the old
       code treated as "pass").

    2. it counted every segment as a separate match — so codeshare flights
       operated by Rossiya (FVxxxx as SU6xxx) and iFly franchise flights
       (SU58xx) showed up as bookable Aeroflot rows, when in fact they're
       either invisible on the Aeroflot site or only bookable under the
       partner's code. This is the "рейсы которых нет на сайте" bug.

    The rewrite walks the documented structure, applies meaningful filters
    (codeshare-off and direct-only by default), and reads seats / price
    from the right place.
    """
    found: list[FoundFlight] = []

    # Navigate to the inner itinerary list. Tolerate a few legacy shapes
    # in case the API ever changes: payload may already be the inner list,
    # or it may be the outer list-of-lists, or it may be wrapped in {data: …}.
    inner: list[Any] = []
    if isinstance(payload, list):
        if payload and isinstance(payload[0], list):
            inner = payload[0]
        elif payload and isinstance(payload[0], dict):
            inner = payload
    elif isinstance(payload, dict):
        ri = (payload.get("data") or payload).get("route_itineraries")
        if isinstance(ri, list) and ri and isinstance(ri[0], list):
            inner = ri[0]

    for itin in inner:
        if not isinstance(itin, dict):
            continue
        legs = itin.get("legs") or []
        offers = itin.get("offers") or []
        if not legs or not offers:
            continue

        # ---- Walk segments to characterise the physical flight chain. ----
        segments_meta: list[dict[str, Any]] = []
        for leg in legs:
            for seg in leg.get("segments") or []:
                segments_meta.append(seg)

        if not segments_meta:
            continue

        # Direct-only filter: a single segment with no stops.
        is_direct = (len(segments_meta) == 1
                     and not _as_int(segments_meta[0].get("stop_quantity") or 0))
        if not is_direct and not allow_connections:
            continue

        # Codeshare / franchise filter:
        #   - franchise_info on the leg means "operated by partner under SU code"
        #     (e.g. iFly for SU5800–5949)
        #   - marketing != operating airline code means classic codeshare
        #     (e.g. SU6293 actually flown as FV6293 by Rossiya)
        # Both look identical on Aeroflot's site (you may not see them at all
        # under the SU code, or you'll see them with a partner badge that
        # confuses the user), so by default we exclude them.
        first_seg = segments_meta[0]
        mk_code = first_seg.get("marketing_airline_code")
        op_code = first_seg.get("operating_airline_code")
        op_name = first_seg.get("operating_airline_name")
        franchise = any((leg.get("franchise_info") or []) for leg in legs)
        is_codeshare = mk_code != op_code or franchise
        if is_codeshare and not allow_codeshare:
            continue

        # Wrong marketing airline (the bot is configured for SU). Defensive —
        # subsidized API shouldn't return non-SU, but keep the guard.
        if mk_code and mk_code != airline_code and not allow_codeshare:
            continue

        # ---- Compose the human-facing flight identifier across segments ----
        flight_no_parts: list[str] = []
        for seg in segments_meta:
            fn = (seg.get("marketing_flight_number")
                  or seg.get("operating_flight_number"))
            code = seg.get("marketing_airline_code") or airline_code
            if fn:
                flight_no_parts.append(f"{code}{fn}")
        flight_no_str = "+".join(flight_no_parts) if flight_no_parts else "?"

        # Operator note when not pure Aeroflot — kept on the dataclass so
        # downstream code (Telegram message, state.json key) can disambiguate.
        operator_note: str | None = None
        if franchise:
            operator_note = "франшиза"
        elif mk_code != op_code:
            operator_note = f"{op_code} {op_name}".strip() if op_code else None

        # ---- Pick the best offer for our passenger group ----
        # The subsidized API returns one offer per fare brand. We want the
        # one that:
        #   1. covers our passenger type (youth/senior/etc.)
        #   2. has enough seats for our group
        #   3. is the cheapest among the candidates
        def offer_covers(o: dict) -> bool:
            pps = o.get("passenger_prices") or []
            for p in pps:
                if (p.get("passenger_type_code") == passenger_type
                    and _as_int(p.get("passenger_quantity") or 0)
                        and (_as_int(p.get("passenger_quantity")) or 0)
                            >= passenger_qty):
                    return True
            # If passenger_prices is empty or doesn't list a quantity, fall
            # back to the offer-level seat_quantity check below.
            return not pps

        candidate_offers = [o for o in offers if offer_covers(o)]
        if not candidate_offers:
            candidate_offers = offers  # last-resort fallback

        max_seats = max((_as_int(o.get("seat_quantity")) or 0
                         for o in candidate_offers), default=0)
        if max_seats < min_seats:
            continue  # not enough seats — skip

        min_price = None
        for o in candidate_offers:
            p = _as_int(o.get("price_total_amount")
                        or o.get("price_base_amount"))
            if p is None:
                continue
            if min_price is None or p < min_price:
                min_price = p

        dep_raw = first_seg.get("departure")
        arr_raw = segments_meta[-1].get("arrival")

        found.append(FoundFlight(
            leg_date=leg_date,
            direction=direction,
            flight_no=flight_no_str,
            seats=max_seats,
            price=min_price,
            departure=str(dep_raw) if dep_raw else None,
            arrival=str(arr_raw) if arr_raw else None,
            operator=operator_note,
            segments=len(segments_meta),
        ))

    return found


# --------------------------------------------------------------------------- #
# Aeroflot API via Playwright fetch
# --------------------------------------------------------------------------- #


async def fetch_itineraries(page, body: dict) -> tuple[int, Any]:
    resp = await page.evaluate(
        """async (body) => {
            const r = await fetch('%s', {
                method: 'POST',
                headers: {'Content-Type':'application/json','Accept':'application/json'},
                body: JSON.stringify(body),
                credentials: 'include'
            });
            const text = await r.text();
            return {status: r.status, text: text};
        }"""
        % API_PATH,
        body,
    )
    try:
        return resp["status"], json.loads(resp["text"])
    except Exception:
        return resp["status"], {"_raw": resp.get("text", "")[:2000]}


# --------------------------------------------------------------------------- #
# Per-scan history (scans.jsonl)
# --------------------------------------------------------------------------- #


def append_scan_record(
    flights: list["FoundFlight"],
    min_prices: dict[str, int | None],
    probed_legs: list[tuple[str, str]] | None = None,
) -> None:
    """Append one JSON line with this run's per-(date,direction) results.

    ``probed_legs`` is the full list of ``(leg_date, direction)`` pairs the
    run attempted to fetch — including ones that returned 0 matches. We
    record all of them so historical stats can answer "how many times did
    the bot check date X" honestly (not just "how many times did X have
    available seats").

    File: ``$AEROFLOT_LOG_DIR/scans.jsonl`` — one record per run, e.g.::

        {"ts": "2026-04-28T03:20:54+07:00", "msk": "2026-04-27T23:20:54+03:00",
         "total": 27,
         "by_leg": [{"date": "2026-08-31", "direction": "VVO→MOW",
                     "count": 6, "min_price": 37000}, ...]}
    """
    counts: dict[tuple[str, str], int] = {}
    for f in flights:
        counts[(f.leg_date, f.direction)] = counts.get((f.leg_date, f.direction), 0) + 1

    # Union of probed legs and legs that had matches — so a leg the run
    # attempted but got zero matches on still appears as count=0.
    all_keys: set[tuple[str, str]] = set(counts.keys())
    if probed_legs:
        all_keys.update(probed_legs)

    by_leg = []
    for (leg_date, direction) in sorted(all_keys):
        by_leg.append({
            "date": leg_date,
            "direction": direction,
            "count": counts.get((leg_date, direction), 0),
            "min_price": min_prices.get(f"{direction} {leg_date}"),
        })
    now_local = datetime.now().astimezone()
    record = {
        "ts": now_local.isoformat(timespec="seconds"),
        "msk": datetime.now(MSK).isoformat(timespec="seconds"),
        "total": len(flights),
        "by_leg": by_leg,
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with SCANS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_scan_history(target_date: str) -> list[dict[str, Any]]:
    """Read scans.jsonl, return matches for ``target_date`` (ISO YYYY-MM-DD).

    Each returned item is ``{"ts", "msk", "direction", "count", "min_price"}``.
    """
    if not SCANS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    with SCANS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for leg in rec.get("by_leg", []):
                if leg.get("date") == target_date:
                    rows.append({
                        "ts": rec.get("ts"),
                        "msk": rec.get("msk"),
                        "direction": leg.get("direction"),
                        "count": leg.get("count"),
                        "min_price": leg.get("min_price"),
                    })
    return rows


def _read_scans_jsonl() -> list[dict[str, Any]]:
    if not SCANS_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    with SCANS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def build_stats(cfg: dict[str, Any]) -> str:
    """Aggregate scans.jsonl into a per-(date, direction) summary.

    For every (date, direction) pair currently configured, count how many
    times the bot probed it, how many of those probes saw ≥1 subsidized
    seat, the maximum match count, the lowest subsidized price observed,
    and the most recent probe timestamp. Dates the bot never scanned
    (e.g. just added to config) appear as "(нет данных)".
    """
    records = _read_scans_jsonl()

    # Build aggregate per (date, direction). Source of truth for *which*
    # pairs to show is the current config, not whatever happens to be in
    # scans.jsonl — that way newly added dates appear as "no data yet".
    origin = cfg.get("origin", "MOW")
    destination = cfg.get("destination", "VVO")
    expected: list[tuple[str, str]] = []
    for d in cfg.get("dates_outbound", []):
        expected.append((d, f"{origin}→{destination}"))
    for d in cfg.get("dates_return", []):
        expected.append((d, f"{destination}→{origin}"))

    agg: dict[tuple[str, str], dict[str, Any]] = {
        key: {"probes": 0, "with_match": 0, "max_count": 0,
              "min_price": None, "last_ts": None, "last_count": 0}
        for key in expected
    }

    for rec in records:
        ts = rec.get("msk") or rec.get("ts")
        for leg in rec.get("by_leg", []):
            d = leg.get("date")
            direction = leg.get("direction")
            if (d, direction) not in agg:
                continue
            count = int(leg.get("count") or 0)
            mp = leg.get("min_price")
            row = agg[(d, direction)]
            row["probes"] += 1
            if count > 0:
                row["with_match"] += 1
            if count > row["max_count"]:
                row["max_count"] = count
            if isinstance(mp, int) and mp > 0:
                if row["min_price"] is None or mp < row["min_price"]:
                    row["min_price"] = mp
            if ts and (row["last_ts"] is None or ts > row["last_ts"]):
                row["last_ts"] = ts
                row["last_count"] = count

    # Pretty print
    if not records:
        return "Истории сканирований ещё нет (файл scans.jsonl пуст)."

    lines = [
        f"📊 Статистика появлений билетов "
        f"(всего прогонов: {len(records)})",
        "",
        f"{'Дата':<12} {'Направление':<10} {'Скан.':>6} {'≥1 место':>9} {'Макс.':>6} {'Мин.цена':>10} {'Посл. скан':<17}",
    ]
    for key in expected:
        d, direction = key
        row = agg[key]
        probes = row["probes"]
        if probes == 0:
            lines.append(f"{d:<12} {direction:<10} {'(нет данных — дата только что добавлена)':<60}")
            continue
        last_ts = (row["last_ts"] or "").replace("T", " ")[:16]
        price = row["min_price"]
        price_s = f"{price:>7,} ₽".replace(",", " ") if price else "       —"
        lines.append(
            f"{d:<12} {direction:<10} {probes:>6} {row['with_match']:>9} "
            f"{row['max_count']:>6} {price_s:>10} {last_ts:<17}"
        )
    lines.append("")
    lines.append("Колонки:")
    lines.append("  Скан.    — сколько раз бот опрашивал API по этой дате")
    lines.append("  ≥1 место — в скольки из них нашлось хоть одно субсидированное место")
    lines.append("  Макс.    — наибольшее число найденных рейсов за один прогон")
    lines.append("  Мин.цена — самая низкая зафиксированная цена обычного тарифа (₽)")
    return "\n".join(lines)


def format_history_text(target_date: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"История по {target_date}: записей нет."
    lines = [f"История совпадений на {_ddmmyyyy(target_date)}:"]
    for r in rows:
        ts = (r.get("msk") or r.get("ts") or "").replace("T", " ")[:16]
        price = r.get("min_price")
        price_s = f", от {price:,} ₽".replace(",", " ") if price else ""
        lines.append(f"{ts} {r['direction']}: {r['count']}{price_s}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #


def _today_msk_iso() -> str:
    return datetime.now(MSK).date().isoformat()


def build_heartbeat_text(
    cfg: dict[str, Any],
    state: dict[str, str],
    probe_results: list[tuple[str, bool]],
    total_matches: int,
) -> str:
    tracked = sum(1 for k in state.keys() if not k.startswith("_"))
    now_msk = datetime.now(MSK).strftime("%Y-%m-%d %H:%M МСК")
    lines = [
        "✅ Агент Аэрофлот жив",
        f"Время: {now_msk}",
        f"Текущих совпадений: {total_matches}",
        f"В state.json: {tracked} рейсов",
    ]
    out_from = cfg.get("origin", "?")
    out_to = cfg.get("destination", "?")
    dates_o = cfg.get("dates_outbound", [])
    dates_r = cfg.get("dates_return", [])
    if dates_o and dates_r:
        lines.append(
            f"Маршрут: {out_from}→{out_to} {dates_o[0]}…{dates_o[-1]}, "
            f"обратно {dates_r[0]}…{dates_r[-1]}"
        )
    if probe_results:
        probe_text = ", ".join(f"{l}={'OK' if ok else 'FAIL'}" for l, ok in probe_results)
        lines.append(f"Прокси Telegram: {probe_text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #


def self_check(cfg: dict[str, Any]) -> dict[str, Any]:
    """Quick offline-ish diagnostic. No flight search, no Telegram send."""
    report: dict[str, Any] = {}
    tg = cfg.get("telegram") or {}
    chain = _telegram_proxy_chain(tg)
    token = tg.get("bot_token", "")
    probes: list[tuple[str, bool]] = []
    if token:
        for proxy in chain:
            probes.append((_proxy_label(proxy), _try_get_me(token, proxy)))
    report["telegram_probes"] = probes
    # Aeroflot front-page reachability (no JS challenge bypass, but confirms
    # the VPS has a route to aeroflot.ru at all).
    try:
        r = requests.get("https://www.aeroflot.ru/", timeout=15)
        report["aeroflot_http"] = r.status_code
    except Exception as e:
        report["aeroflot_http"] = f"error: {e}"
    report["state_tracked"] = sum(
        1 for k in load_state().keys() if not k.startswith("_")
    )
    report["msk_now"] = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S %Z")
    return report


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #


async def run_check(cfg: dict[str, Any], dry_run: bool) -> int:
    origin = cfg["origin"]
    destination = cfg["destination"]
    passengers = int(cfg["passengers"])
    min_seats = int(cfg.get("min_seats", passengers))
    program_id = int(cfg.get("program_id", 40))
    passenger_type = cfg.get("passenger_type", "youth")
    outbound_dates = cfg["dates_outbound"]
    return_dates = cfg["dates_return"]
    # Accuracy filters (defaults match "what's actually visible/bookable
    # on aeroflot.ru for an ordinary passenger"). Override in config.json
    # only if you intentionally want codeshare / connecting itineraries
    # included in alerts.
    airline_code = cfg.get("airline_code", "SU")
    allow_codeshare = bool(cfg.get("allow_codeshare", False))
    allow_connections = bool(cfg.get("allow_connections", False))

    state = load_state()
    all_found: list[FoundFlight] = []
    min_prices: dict[str, int | None] = {}
    probed_legs: list[tuple[str, str]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 820},
        )
        page = await context.new_page()
        try:
            logging.info("loading SPA %s", SPA_URL)
            await page.goto(SPA_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception as e:
                logging.warning("networkidle timeout (continuing): %s", e)
            await page.wait_for_timeout(2500)

            legs: list[tuple[str, str, str, str]] = [
                (origin, destination, d, f"{origin}→{destination}")
                for d in outbound_dates
            ] + [
                (destination, origin, d, f"{destination}→{origin}")
                for d in return_dates
            ]

            for o, d, leg_date, direction in legs:
                probed_legs.append((leg_date, direction))
                body = {
                    "program_id": program_id,
                    "routes": [
                        {"origin": o, "destination": d, "departure_date": leg_date}
                    ],
                    "passengers": [
                        {"passenger_type": passenger_type, "quantity": passengers}
                    ],
                    "lang": "ru",
                }
                try:
                    status, parsed = await fetch_itineraries(page, body)
                except Exception as e:
                    logging.error("fetch %s %s failed: %s", direction, leg_date, e)
                    continue

                if status != 200 or not parsed.get("success", False):
                    logging.warning(
                        "bad response %s %s: http=%s, body=%s",
                        direction,
                        leg_date,
                        status,
                        str(parsed)[:400],
                    )
                    continue

                data = parsed.get("data") or {}
                itins = data.get("route_itineraries") or []

                route_prices = data.get("route_min_prices") or []
                if route_prices:
                    for d_entry in route_prices[0] or []:
                        dd = d_entry.get("departure_date")
                        pa = _as_int(d_entry.get("price_amount"))
                        if dd == leg_date and pa is not None:
                            min_prices[f"{direction} {leg_date}"] = pa

                leg_found = extract_flights(
                    itins,
                    direction,
                    leg_date,
                    min_seats,
                    airline_code=airline_code,
                    passenger_type=passenger_type,
                    passenger_qty=passengers,
                    allow_codeshare=allow_codeshare,
                    allow_connections=allow_connections,
                )
                # itineraries=N is the count returned by the API (everything
                # subsidized for this date), matches=M is what survived the
                # accuracy filters (direct + non-codeshare + enough seats).
                # The gap between the two is the most useful debug signal.
                inner_len = (len(itins[0]) if (itins and isinstance(itins[0], list))
                             else 0)
                logging.info(
                    "%s %s: http=%s, itineraries=%d, matches=%d, min_price=%s",
                    direction,
                    leg_date,
                    status,
                    inner_len,
                    len(leg_found),
                    min_prices.get(f"{direction} {leg_date}"),
                )
                all_found.extend(leg_found)
        finally:
            await context.close()
            await browser.close()

    logging.info("total matching flights across all legs: %d", len(all_found))
    if min_prices:
        logging.info("min subsidized prices observed: %s", min_prices)

    # Persistent per-scan history (one JSON object per line) for offline
    # querying: how many matches were there for a given date over time.
    # Includes zero-match legs too, so stats can answer "how many times
    # did we probe date X".
    append_scan_record(all_found, min_prices, probed_legs=probed_legs)

    # Refresh state.json — kept in sync with reality even though delivery
    # no longer depends on it (every run sends a summary).
    today = date.today().isoformat()
    new_state: dict[str, str] = {
        k: v for k, v in state.items() if k.startswith("_")
    }
    for f in all_found:
        new_state[f.key()] = today
    if not dry_run:
        save_state(new_state)

    # Compose the compact summary — one line per (date, direction) with the
    # match count. If nothing matches at all, send a short "no matches" note
    # so the user can see the bot still ran.
    if all_found:
        summary = format_flights_message(all_found)
    else:
        summary = "Совпадений нет."

    # Diff against the previous run's flight set, so each message tells the
    # user at a glance what's new since last check. Computed before saving
    # the new snapshot below.
    prev_keys, prev_exists = load_last_flights()
    diff_block = format_diff(all_found, prev_keys, prev_exists)
    message = f"{summary}\n\n{diff_block}"

    if dry_run:
        logging.info("[dry-run] would send:\n%s", message)
        return 0

    send_telegram(cfg, message)
    # Snapshot the current flight set so the *next* run can diff against it.
    save_last_flights(all_found)
    logging.info("summary sent (%d flights across %d legs)",
                 len(all_found),
                 len({(f.leg_date, f.direction) for f in all_found}))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="parse but don't send Telegram")
    ap.add_argument("--test-notify", action="store_true", help="send a test message and exit")
    ap.add_argument("--reset-state", action="store_true", help="clear the antidup state file")
    ap.add_argument("--heartbeat", action="store_true", help="send a heartbeat now and exit")
    ap.add_argument("--self-check", action="store_true", help="run offline diagnostic and exit")
    ap.add_argument(
        "--history",
        metavar="YYYY-MM-DD",
        help="print scan history for a specific departure date and exit",
    )
    ap.add_argument(
        "--stats",
        action="store_true",
        help="print aggregate statistics across all configured dates and exit",
    )
    args = ap.parse_args()

    log_path = setup_logging()
    logging.info("=== run start; log=%s", log_path)

    cfg = load_config()

    if args.reset_state:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        if LAST_FLIGHTS_PATH.exists():
            LAST_FLIGHTS_PATH.unlink()
        logging.info("state reset")
        return 0

    if args.history:
        rows = load_scan_history(args.history)
        text = format_history_text(args.history, rows)
        print(text)
        return 0

    if args.stats:
        print(build_stats(cfg))
        return 0

    if args.self_check:
        report = self_check(cfg)
        logging.info("self-check report: %s", json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # Exit non-zero only if no proxy works AND aeroflot unreachable.
        any_proxy_ok = any(ok for _, ok in report.get("telegram_probes", []))
        aero_ok = isinstance(report.get("aeroflot_http"), int) and report["aeroflot_http"] < 500
        return 0 if (any_proxy_ok and aero_ok) else 1

    if args.heartbeat:
        state = load_state()
        _, probe_results = pick_working_proxy(cfg.get("telegram", {}))
        hb_text = build_heartbeat_text(cfg, state, probe_results, total_matches=0)
        send_telegram(cfg, hb_text)
        state["_heartbeat_date"] = _today_msk_iso()
        save_state(state)
        logging.info("manual heartbeat sent")
        return 0

    if args.test_notify:
        send_telegram(
            cfg, "🧪 Тест уведомления: агент Аэрофлот подключён и шлёт в Telegram."
        )
        logging.info("test notification sent")
        return 0

    try:
        return asyncio.run(run_check(cfg, dry_run=args.dry_run))
    except Exception as e:
        logging.error("run failed: %s\n%s", e, traceback.format_exc())
        try:
            send_telegram(cfg, f"⚠️ Проверка Аэрофлот не удалась: {e!s}")
        except Exception as tg_e:
            logging.error("telegram fallback failed: %s", tg_e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
