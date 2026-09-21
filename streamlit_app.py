#!/usr/bin/env python3
"""
streamlit_app.py — Single-school ZIP code lookup form.

Run with:
    streamlit run streamlit_app.py

Collects a requester's name and email, plus a school's name, country, and
sales territory, then geocodes the school via Nominatim (OpenStreetMap —
the same engine zip_lookup.py uses for batch jobs, via the shared logic
in zip_lookup_core.py) and displays the resulting ZIP code.

Each submission is appended to a local CSV log (request_log.csv) next to
this file, so there's a record of who requested what. Remove the
log_request() call below if you don't want that.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from zip_lookup_core import (
    TERRITORY_STATES,
    build_geocoder,
    load_cache,
    lookup_zip,
    save_cache,
)

CACHE_FILE = "geocode_cache.json"
LOG_FILE = "request_log.csv"
LOG_COLUMNS = ["timestamp_utc", "name", "email", "school_name", "territory", "country", "zip_code"]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

st.set_page_config(page_title="School ZIP Code Lookup", page_icon="📍")


@st.cache_resource(show_spinner=False)
def get_geocoder():
    """Built once per server process and reused across all requests/sessions."""
    return build_geocoder()


def get_cache() -> dict:
    """
    The geocode result cache lives on disk (shared across all users of this
    app) but is loaded into session state once per session so we're not
    re-reading the file on every rerun.
    """
    if "geocode_cache" not in st.session_state:
        st.session_state.geocode_cache = load_cache(CACHE_FILE)
    return st.session_state.geocode_cache


def log_request(name: str, email: str, school_name: str, territory: str, country: str, zip_code: str | None) -> None:
    is_new = not Path(LOG_FILE).exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(LOG_COLUMNS)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            name, email, school_name, territory, country, zip_code or "NOT_FOUND",
        ])


st.title("📍 School ZIP Code Lookup")
st.caption(
    "Enter a school's name, country, and sales territory to look up its ZIP "
    "code. Lookups are powered by OpenStreetMap (Nominatim); your name and "
    "email are saved with each request."
)

territory_options = ["(none / unknown)"] + sorted(TERRITORY_STATES.keys())

with st.form("lookup_form", clear_on_submit=False):
    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Your name *")
    with col2:
        email = st.text_input("Your email *")

    school_name = st.text_input("School name *", placeholder="e.g. Springfield Elementary School")

    col3, col4 = st.columns(2)
    with col3:
        country = st.text_input("Country *", value="USA")
    with col4:
        territory = st.selectbox("Sales territory", territory_options)

    submitted = st.form_submit_button("Look up ZIP code", width="stretch")

if submitted:
    errors = []
    if not name.strip():
        errors.append("Your name is required.")
    if not email.strip() or not EMAIL_RE.match(email.strip()):
        errors.append("A valid email address is required.")
    if not school_name.strip():
        errors.append("School name is required.")
    if not country.strip():
        errors.append("Country is required.")

    if errors:
        for e in errors:
            st.error(e)
    else:
        territory_value = "" if territory == "(none / unknown)" else territory
        geocode, reverse = get_geocoder()
        cache = get_cache()

        with st.spinner("Looking up ZIP code…"):
            zip_code = lookup_zip(school_name, country, territory_value, geocode, reverse, cache)
            save_cache(cache, CACHE_FILE)

        log_request(name.strip(), email.strip(), school_name.strip(), territory_value, country.strip(), zip_code)

        if zip_code:
            st.success(f"ZIP code for **{school_name.strip()}**: **{zip_code}**")
        else:
            st.warning(
                f"Couldn't find a ZIP code for **{school_name.strip()}**. "
                "Try checking the spelling, or add more detail (e.g. a city) to the school name."
            )

with st.expander("Recent lookups"):
    if Path(LOG_FILE).exists():
        history = pd.read_csv(LOG_FILE, dtype=str)
        st.dataframe(history.sort_values("timestamp_utc", ascending=False), width="stretch", hide_index=True)
    else:
        st.write("No lookups yet.")
