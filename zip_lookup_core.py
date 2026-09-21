#!/usr/bin/env python3
"""
zip_lookup_core.py — Shared school-ZIP-code geocoding logic.

Used by both:
  - zip_lookup.py     (batch script: processes a whole spreadsheet)
  - streamlit_app.py  (single-lookup web form)

Keeping this logic in one place means the two tools can never quietly
drift apart — a fix or a new territory mapping here applies to both.

IMPORTANT — Nominatim usage policy:
    The public Nominatim API is free but rate-limited to ~1 request/second
    and is explicitly NOT intended for heavy bulk geocoding. This module
    respects that limit (MIN_DELAY_SECONDS). For large datasets or
    production use, consider a bulk/commercial geocoder instead (e.g. the
    free US Census Geocoder for US addresses, Google Geocoding API,
    Mapbox, or a self-hosted Nominatim instance).

    Also set USER_AGENT below to something that identifies your project
    and includes a real contact email/URL, per Nominatim's usage policy.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd
from geopy.exc import GeocoderServiceError, GeocoderTimedOut, GeocoderUnavailable
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

log = logging.getLogger("zip_lookup_core")

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

MIN_DELAY_SECONDS = 1  # Nominatim policy: max ~1 request/second
GEOCODE_TIMEOUT = 10

# >>> Replace with a real contact per Nominatim's usage policy <<<
USER_AGENT = "school_zip_lookup (contact: your-email@example.com)"

NOT_FOUND_MARKER = "NOT_FOUND"

# Territory -> plausible US state abbreviations, used as extra query
# attempts when a school's country looks like the US and the direct
# name search doesn't resolve.
TERRITORY_STATES = {
    "Mid-Atlantic": ["PA", "NJ", "DE", "MD", "DC"],
    "Midwest": ["IL", "WI", "MN"],
    "Great Lakes": ["MI", "OH", "IN"],
    "Great Plains": ["MO", "KS", "NE", "IA"],
    "Heartland": ["IN", "KY", "OH"],
    "Frontier": ["OK", "NM", "CO"],
    "Southeast": ["NC", "SC", "GA", "TN"],
    "Florida-Georgia": ["FL", "GA"],
    "Southwest": ["AZ", "NV", "HI", "UT"],
    "California": ["CA"],
    "Northwest": ["WA", "OR", "ID"],
    "New England": ["MA", "VT", "NH", "ME", "RI", "CT"],
    "South Central": ["TX", "LA", "AR"],
}

US_COUNTRY_ALIASES = {"us", "usa", "united states", "united states of america"}

# Common school-name abbreviations expanded to help geocoding hit a match.
# Applied with word boundaries + case-insensitive so "SD" doesn't corrupt
# unrelated substrings (e.g. a school literally named "...SDN...").
NAME_EXPANSIONS = {
    r"\bISD\b": "Independent School District",
    r"\bUSD\b": "Unified School District",
    r"\bCSD\b": "Central School District",
    r"\bSD\b": "School District",
    r"\bDIST\b": "District",
    r"\bELEM\b": "Elementary",
    r"\bJR\b": "Junior",
    r"\bSR\b": "Senior",
}


# ----------------------------------------------------------------------
# ON-DISK CACHE (so a school already looked up is never re-queried,
# across runs of the batch script, across Streamlit sessions, or between
# the two tools if they share a cache file)
# ----------------------------------------------------------------------

def load_cache(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read cache file %s (%s) — starting fresh.", path, exc)
    return {}


def save_cache(cache: dict, path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except OSError as exc:
        log.warning("Could not save cache file %s: %s", path, exc)


# ----------------------------------------------------------------------
# SCHOOL NAME CLEANING
# ----------------------------------------------------------------------

def clean_name(name) -> str:
    if pd.isna(name):
        return ""
    text = str(name).strip()
    text = text.replace("#", "").replace("/", " ")
    for pattern, replacement in NAME_EXPANSIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ----------------------------------------------------------------------
# GEOCODER SETUP
# ----------------------------------------------------------------------

def build_geocoder():
    geolocator = Nominatim(user_agent=USER_AGENT, timeout=GEOCODE_TIMEOUT)
    geocode = RateLimiter(
        geolocator.geocode,
        min_delay_seconds=MIN_DELAY_SECONDS,
        max_retries=2,
        error_wait_seconds=5.0,
        swallow_exceptions=True,
    )
    reverse = RateLimiter(
        geolocator.reverse,
        min_delay_seconds=MIN_DELAY_SECONDS,
        max_retries=2,
        error_wait_seconds=5.0,
        swallow_exceptions=True,
    )
    return geocode, reverse


def is_us(country) -> bool:
    if pd.isna(country):
        return False
    return str(country).strip().lower() in US_COUNTRY_ALIASES


def extract_zip_from_address(address: dict) -> Optional[str]:
    zipcode = address.get("postcode")
    if zipcode:
        zipcode = str(zipcode).split("-")[0].strip()
        if zipcode:
            return zipcode
    return None


def build_queries(school_name: str, country, territory) -> list[str]:
    """Ordered, de-duplicated list of search strings to try."""
    queries: list[str] = []
    country_str = "" if pd.isna(country) else str(country).strip()

    if country_str:
        queries.append(f"{school_name}, {country_str}")
    queries.append(school_name)

    if is_us(country) and isinstance(territory, str) and territory.strip():
        territory_name = territory.replace("- Territory", "").replace("Territory", "").strip()
        for state in TERRITORY_STATES.get(territory_name, []):
            queries.append(f"{school_name}, {state}, USA")

    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


def lookup_zip(school_name_raw, country, territory, geocode, reverse, cache: dict) -> Optional[str]:
    """
    Look up a single school's ZIP code, using and updating `cache` in place.

    `geocode` / `reverse` are callables with the same signature as
    geopy's RateLimiter-wrapped Nominatim methods (see build_geocoder()) —
    pass fakes here for testing without hitting the network.
    """
    school_name = clean_name(school_name_raw)
    if not school_name:
        return None

    country_key = "" if pd.isna(country) else str(country).strip()
    territory_key = "" if pd.isna(territory) else str(territory).strip()
    cache_key = f"{school_name}|{country_key}|{territory_key}"

    if cache_key in cache:
        return cache[cache_key] or None

    result_zip = None

    for query in build_queries(school_name, country, territory):
        try:
            result = geocode(query, addressdetails=True)
        except (GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable) as exc:
            log.warning("Geocode error for %r: %s", query, exc)
            continue

        if not result:
            continue

        zipcode = extract_zip_from_address(result.raw.get("address", {}))

        # The forward search sometimes matches a point with no postcode
        # tag in OSM. Only in that case, fall back to a reverse lookup on
        # the matched coordinates (this halves the API calls vs. always
        # reverse-geocoding, which matters for the 1 req/sec budget).
        if not zipcode:
            try:
                rev = reverse((result.latitude, result.longitude), exactly_one=True)
                if rev:
                    zipcode = extract_zip_from_address(rev.raw.get("address", {}))
            except (GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable) as exc:
                log.warning("Reverse geocode error for %r: %s", query, exc)

        if zipcode:
            result_zip = zipcode
            break

    cache[cache_key] = result_zip or ""
    return result_zip


def cell_needs_lookup(value) -> bool:
    """
    True if a 'Zip Code' value hasn't been successfully filled yet.

    Distinguishes "never attempted" (blank/NaN) and "attempted, not found"
    (NOT_FOUND_MARKER) from an already-found zip — both of the former
    should be retried, but a real zip code should never be overwritten.
    """
    if pd.isna(value):
        return True
    text = str(value).strip()
    return text == "" or text == NOT_FOUND_MARKER
