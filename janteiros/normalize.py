"""Normalization helpers: numbers in de-DE / en-US, dates to UTC, ids, units and attribute values."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

ZW_RE = re.compile(r"[​-‏⁠﻿­]")
CENT = Decimal("0.01")


def strip_invisible(s: str) -> str:
    return ZW_RE.sub("", s or "")


def norm_id(s: str | None) -> str:
    """Canonical form for references, keys and material numbers: no invisible chars, no spaces, upper case."""
    if s is None:
        return ""
    s = strip_invisible(str(s))
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("‐", "-").replace("‑", "-").replace("‒", "-").replace("–", "-").replace("−", "-")
    return re.sub(r"\s+", "", s).upper()


def loose_id(s: str | None) -> str:
    """Looser form for matching ids that drop separators (A3 == A-3); never used for the tender reference."""
    return re.sub(r"[^0-9A-Z]", "", norm_id(s))


def round_cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def to_decimal(value, fmt: str | None = None) -> Decimal | None:
    """Parse a number given as text in de-DE (1.234,56) or en-US (1,234.56) or as a plain number."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    s = strip_invisible(str(value)).strip()
    s = s.replace(" ", " ").replace(" ", " ")
    s = re.sub(r"(?i)(eur|usd|chf|gbp|€|\$|%|stk\.?|stück|st\b|mm|bar|kg|g\b)", " ", s)
    s = s.replace(" ", "").replace("'", "")
    m = re.search(r"[-+]?[\d.,]+", s)
    if not m:
        return None
    s = m.group(0)
    f = (fmt or "").lower()
    if f.startswith("de"):
        s = s.replace(".", "").replace(",", ".")
    elif f.startswith("en"):
        s = s.replace(",", "")
    else:
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            parts = s.split(",")
            if len(parts) == 2 and len(parts[1]) != 3:
                s = s.replace(",", ".")
            elif len(parts) > 2:
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")  # ambiguous 1,234: treated as decimal comma (German sites)
        elif s.count(".") > 1:
            s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def to_int(value) -> int | None:
    d = to_decimal(value)
    if d is None:
        return None
    if d != d.to_integral_value():
        return None
    return int(d)


_DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?"), "iso"),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "isodate"),
    (re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:,?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?"), "de"),
]


def parse_datetime(value, tz: str | None = None) -> datetime | None:
    """Parse ISO or German dates; naive times are read in `tz` (default UTC). Returns aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = strip_invisible(str(value)).strip()
    for rx, kind in _DATE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        g = m.groups()
        try:
            if kind == "iso":
                dt = datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]), int(g[5] or 0))
                if g[6]:
                    if g[6] == "Z":
                        return dt.replace(tzinfo=timezone.utc)
                    sign = 1 if g[6][0] == "+" else -1
                    hh, mm = int(g[6][1:3]), int(g[6][-2:])
                    return (dt - sign * timedelta(hours=hh, minutes=mm)).replace(tzinfo=timezone.utc)
                return _localize(dt, tz)
            if kind == "isodate":
                return _localize(datetime(int(g[0]), int(g[1]), int(g[2])), tz)
            if kind == "de":
                dt = datetime(int(g[2]), int(g[1]), int(g[0]), int(g[3] or 0), int(g[4] or 0), int(g[5] or 0))
                return _localize(dt, tz)
        except ValueError:
            continue
    return None


def _localize(dt: datetime, tz: str | None) -> datetime:
    if tz:
        try:
            return dt.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
        except Exception:  # noqa: BLE001 - unknown zone names fall back to UTC
            pass
    return dt.replace(tzinfo=timezone.utc)


def parse_day(value) -> date | None:
    dt = parse_datetime(value)
    return dt.date() if dt else None


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


SET_UNITS = re.compile(r"^(satz|sätze|saetze|set|sets|garnitur|kit)$", re.I)
PIECE_UNITS = re.compile(r"^(stk\.?|stück|stueck|st|pcs|pc|piece|pieces|einheit|ea)$", re.I)


def unit_kind(unit: str | None) -> str:
    u = strip_invisible(unit or "").strip().lower().rstrip(".")
    if SET_UNITS.match(u):
        return "set"
    if PIECE_UNITS.match(u) or u in ("stk", "st"):
        return "piece"
    if "satz" in u or "set" in u:
        return "set"
    return "piece" if not u else u


def norm_text(s: str | None) -> str:
    s = strip_invisible(s or "")
    s = unicodedata.normalize("NFKC", s).strip().lower()
    s = s.replace("ß", "ss")
    return re.sub(r"[\s\-_/]+", " ", s).strip()


NONE_CERT = {"", "keine", "keine gefordert", "none", "ohne", "ohne zertifizierung", "nicht gefordert", "n a", "na", "-", "—", "kein", "no", "keine zertifizierung"}


def norm_cert(s: str | None) -> str:
    t = norm_text(s)
    if t in NONE_CERT or t.startswith("keine") or t.startswith("ohne"):
        return "none"
    return re.sub(r"\s+", "", t)


def norm_mounting(s: str | None) -> str:
    return re.sub(r"\s+", "", norm_text(s))


def num_equal(a, b) -> bool:
    da, db = to_decimal(a), to_decimal(b)
    return da is not None and db is not None and da == db
