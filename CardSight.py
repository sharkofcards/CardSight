import os
import sqlite3
import time
import hashlib
import queue
from datetime import datetime, timedelta
from threading import Semaphore, Thread, Event

import requests
import pandas as pd
import streamlit as st
import altair as alt

# =========================================================
# CONFIG
# =========================================================
EBAY_APP_ID = st.secrets["ebay"]["app_id"]  # put your eBay API app ID in .streamlit/secrets.toml
DB_PATH = "cardsight_cache.sqlite"
CALLS_PER_SECOND = 4          # adjust to your allowed rate
CALLS_PER_DAY_BUDGET = 8000   # daily API budget
CACHE_STALE_HOURS = 6


# =========================================================
# Rate Limiter
# =========================================================
class RateLimiter:
    def __init__(self, calls_per_second, daily_budget):
        self.calls_per_second = calls_per_second
        self.daily_budget = daily_budget
        self.semaphore = Semaphore(calls_per_second)
        self.daily_count = 0
        self.day_start = datetime.utcnow().date()
        self.stop = Event()
        Thread(target=self._refill, daemon=True).start()

    def _refill(self):
        while not self.stop.is_set():
            self.semaphore = Semaphore(self.calls_per_second)
            if datetime.utcnow().date() != self.day_start:
                self.daily_count = 0
                self.day_start = datetime.utcnow().date()
            time.sleep(1)

    def acquire(self):
        if self.daily_count >= self.daily_budget:
            raise RuntimeError("Daily API budget reached")
        self.semaphore.acquire()
        self.daily_count += 1


limiter = RateLimiter(CALLS_PER_SECOND, CALLS_PER_DAY_BUDGET)


# =========================================================
# SQLite Helpers
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS search_cache ("
        " key TEXT PRIMARY KEY,"
        " query TEXT,"
        " fetched_at TEXT,"
        " data_json TEXT)"
    )
    conn.commit()
    conn.close()


def make_key(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()


def get_cache(query: str):
    key = make_key(query)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT fetched_at, data_json FROM search_cache WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, None
    fetched_at_str, data_json = row
    fetched_at = datetime.fromisoformat(fetched_at_str)
    if datetime.utcnow() - fetched_at > timedelta(hours=CACHE_STALE_HOURS):
        return None, None
    return pd.read_json(data_json), fetched_at


def put_cache(query: str, df: pd.DataFrame):
    key = make_key(query)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO search_cache (key, query, fetched_at, data_json)"
        " VALUES (?, ?, ?, ?)",
        (key, query, datetime.utcnow().isoformat(), df.to_json())
    )
    conn.commit()
    conn.close()


# =========================================================
# Background Worker
# =========================================================
class RefreshWorker:
    def __init__(self):
        self.q = queue.Queue()
        self.stop = Event()
        Thread(target=self._run, daemon=True).start()

    def submit(self, query: str, limit: int):
        self.q.put((query, limit))

    def _run(self):
        while not self.stop.is_set():
            try:
                query, limit = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                df, _ = _fetch_from_ebay(query, limit)
                put_cache(query, df)
            except Exception as e:
                print(f"[Worker] Refresh failed: {e}")
            finally:
                self.q.task_done()


worker = RefreshWorker()


# =========================================================
# Core Fetch (no cache)
# =========================================================
def _fetch_from_ebay(query: str, limit: int):
    limiter.acquire()
    url = "https://svcs.ebay.com/services/search/FindingService/v1"
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.0.0",
        "SECURITY-APPNAME": EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "true",
        "keywords": query,
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "paginationInput.entriesPerPage": limit
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("findCompletedItemsResponse", [{}])[0].get("searchResult", [{}])[0].get("item", [])

    results = []
    for item in items:
        price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
        results.append({
            "Title": item.get("title", [""])[0],
            "Price": float(price_info.get("__value__", 0)),
            "Currency": price_info.get("@currencyId", ""),
            "SoldDate": item.get("listingInfo", [{}])[0].get("endTime", [""])[0],
            "URL": item.get("viewItemURL", [""])[0]
        })

    return pd.DataFrame(results), datetime.utcnow()


# =========================================================
# Public API
# =========================================================
def search_sold_ebay_cards(query, limit=20, force_refresh=False):
    init_db()
    cached, ts = get_cache(query)

    if cached is not None and not force_refresh:
        worker.submit(query, limit)
        return cached, ts

    df, ts = _fetch_from_ebay(query, limit)
    put_cache(query, df)
    return df, ts


# =========================================================
# Streamlit UI
# =========================================================
st.set_page_config(page_title="CardSight", layout="wide")
st.title("📊 CardSight — eBay Sold Listings")

query = st.text_input("Search eBay sold items", "")
limit = st.slider("Results per page", 10, 100, 20)

if st.button("Search") and query:
    with st.spinner("Fetching results..."):
        try:
            df, ts = search_sold_ebay_cards(query, limit=limit)

            st.success(f"Showing results (cached at {ts.strftime('%Y-%m-%d %H:%M:%S')} UTC)")
            st.dataframe(df)

            if not df.empty:
                # Convert SoldDate to datetime
                df["SoldDate"] = pd.to_datetime(df["SoldDate"], errors="coerce")

                # -------------------
                # Summary Stats
                # -------------------
                st.subheader("📌 Price Summary")
                st.write({
                    "Min": df["Price"].min(),
                    "Max": df["Price"].max(),
                    "Mean": df["Price"].mean(),
                    "Median": df["Price"].median(),
                    "Count": len(df)
                })

                # -------------------
                # Price Over Time
                # -------------------
                st.subheader("📈 Price Trend Over Time")
                df_sorted = df.sort_values("SoldDate")

                price_trend = (
                    alt.Chart(df_sorted)
                    .mark_line(point=True)
                    .encode(
                        x=alt.X("SoldDate:T", title="Date"),
                        y=alt.Y("Price:Q", title="Price"),
                        tooltip=["Title", "Price", "SoldDate:T", "URL"]
                    )
                    .interactive()
                )
                st.altair_chart(price_trend, use_container_width=True)

                # -------------------
                # Price Distribution
                # -------------------
                st.subheader("📊 Price Distribution")

                hist = (
                    alt.Chart(df)
                    .mark_bar()
                    .encode(
                        alt.X("Price:Q", bin=alt.Bin(maxbins=20), title="Price"),
                        y=alt.Y("count():Q", title="Frequency"),
                        tooltip=[alt.Tooltip("count()", title="Count")]
                    )
                )
                st.altair_chart(hist, use_container_width=True)

        except Exception as e:
            st.error(f"Error fetching results: {e}")
