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
import matplotlib.pyplot as plt

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
            # reset second-based tokens
            self.semaphore = Semaphore(self.calls_per_second)
            # reset daily counter at UTC midnight
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
                # Price Over Time
                # -------------------
                st.subheader("📈 Price Trend Over Time")
                fig, ax = plt.subplots(figsize=(8, 4))
                df_sorted = df.sort_values("SoldDate")
                ax.plot(df_sorted["SoldDate"], df_sorted["Price"], marker="o", linestyle="-")
                ax.set_xlabel("Date")
                ax.set_ylabel("Price")
                ax.set_title("Sold Price Over Time")
                st.pyplot(fig)

                # -------------------
                # Price Distribution
                # -------------------
                st.subheader("📊 Price Distribution")
                fig2, ax2 = plt.subplots(figsize=(8, 4))
                ax2.hist(df["Price"], bins=20, edgecolor="black")
                ax2.set_xlabel("Price")
                ax2.set_ylabel("Frequency")
                ax2.set_title("Histogram of Sold Prices")
                st.pyplot(fig2)

        except Exception as e:
            st.error(f"Error fetching results: {e}")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

def make_cache_key(query: str, filters: dict) -> str:
    payload = {"q": query.strip().lower(), "filters": filters or {}}
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()

@contextmanager
def sqlite_conn(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    try:
        yield conn
    finally:
        conn.close()

# --------------- RATE LIMITER ---------------
class RateLimiter:
    """
    Token-bucket style limiter for per-second and per-day budgets.
    """
    def __init__(self, calls_per_second: int, daily_budget: int):
        self.calls_per_second = max(1, calls_per_second)
        self.daily_budget = daily_budget
        self.per_second_semaphore = Semaphore(self.calls_per_second)
        self.shutdown = Event()
        self.daily_count = 0
        self.day_start = datetime.utcnow().date()

        # Refill tokens each second
        Thread(target=self._refill_loop, daemon=True).start()

    def _refill_loop(self):
        while not self.shutdown.is_set():
            # reset per-second tokens
            self.per_second_semaphore = Semaphore(self.calls_per_second)
            # reset daily budget at UTC midnight
            if datetime.utcnow().date() != self.day_start:
                self.daily_count = 0
                self.day_start = datetime.utcnow().date()
            time.sleep(1)

    def acquire(self):
        # enforce daily budget
        if self.daily_count >= self.daily_budget:
            raise RuntimeError("Daily API budget reached. Try again tomorrow or lower requests.")
        # take a per-second token (blocks until available)
        self.per_second_semaphore.acquire()
        self.daily_count += 1

# --------------- HTTP SESSION WITH RETRIES ---------------
def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

# --------------- DB INIT ---------------
def init_db(db_path: str):
    with sqlite_conn(db_path) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            filters_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_rows (
            cache_key TEXT NOT NULL,
            item_id TEXT,
            title TEXT,
            price REAL,
            currency TEXT,
            sold_date TEXT,
            url TEXT,
            raw_json TEXT,
            PRIMARY KEY (cache_key, item_id)
        );
        """)
        conn.commit()

# --------------- CACHE API ---------------
def cache_get(conn: sqlite3.Connection, cache_key: str, max_age_hours: int):
    cur = conn.cursor()
    cur.execute("SELECT fetched_at FROM search_cache WHERE cache_key = ?", (cache_key,))
    row = cur.fetchone()
    if not row:
        return None, None  # (df, fetched_at)

    fetched_at = datetime.fromisoformat(row[0])
    cur.execute("""
        SELECT item_id, title, price, currency, sold_date, url, raw_json
        FROM sales_rows WHERE cache_key = ?
        ORDER BY datetime(sold_date) DESC
    """, (cache_key,))
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["item_id","title","price","currency","sold_date","url","raw_json"])
    is_fresh = datetime.utcnow() - fetched_at <= timedelta(hours=max_age_hours)
    return df, (fetched_at if is_fresh else None)

def cache_put(conn: sqlite3.Connection, cache_key: str, query: str, filters: dict, items: list):
    fetched_at = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO search_cache(cache_key, query, filters_json, fetched_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET fetched_at=excluded.fetched_at, query=excluded.query, filters_json=excluded.filters_json;
    """, (cache_key, query, stable_json_dumps(filters), fetched_at))
    # upsert items
    for it in items:
        conn.execute("""
            INSERT INTO sales_rows(cache_key,item_id,title,price,currency,sold_date,url,raw_json)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key, item_id) DO UPDATE SET
                title=excluded.title,
                price=excluded.price,
                currency=excluded.currency,
                sold_date=excluded.sold_date,
                url=excluded.url,
                raw_json=excluded.raw_json;
        """, (
            cache_key,
            it.get("itemId", ""),
            it.get("title", ""),
            float(it.get("price", 0.0)) if it.get("price") is not None else None,
            it.get("currency", ""),
            it.get("soldDate", ""),
            it.get("url", ""),
            json.dumps(it, ensure_ascii=False)
        ))
    conn.commit()

# --------------- EBAY API CALL ---------------
def ebay_find_completed_items(session: requests.Session, rate_limiter: RateLimiter, query: str, page: int, filters: dict):
    """
    Example call to eBay Finding API for completed items.
    You may need to adjust 'params' depending on your exact API flavor/headers.
    """
    if not EBAY_APP_ID:
        raise RuntimeError("Set EBAY_APP_ID environment variable for eBay API access.")

    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT": "JSON",
        "paginationInput.entriesPerPage": PAGE_SIZE,
        "paginationInput.pageNumber": page,
        "keywords": query,
        "sortOrder": "EndTimeSoonest"
    }

    # Apply simple filter examples; extend as needed
    if filters:
        if "categoryId" in filters:
            params["categoryId"] = filters["categoryId"]
        if "minPrice" in filters:
            params["itemFilter.name"] = "MinPrice"
            params["itemFilter.value"] = str(filters["minPrice"])
        if "maxPrice" in filters:
            params["itemFilter(1).name"] = "MaxPrice"
            params["itemFilter(1).value"] = str(filters["maxPrice"])

    headers = {"X-EBAY-SOA-SECURITY-APPNAME": EBAY_APP_ID}

    rate_limiter.acquire()
    resp = session.get(EBAY_FINDING_ENDPOINT, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    # Parse response into a normalized list of items
    try:
        items = data["findCompletedItemsResponse"][0]["searchResult"][0].get("item", [])
    except Exception:
        items = []

    normalized = []
    for it in items:
        selling_status = it.get("sellingStatus", [{}])[0]
        current_price = selling_status.get("currentPrice", [{}])[0]
        converted_price = selling_status.get("convertedCurrentPrice", [{}])[0]
        price = None
        currency = None
        if "value" in current_price:
            price = float(current_price["value"])
            currency = current_price.get("@currencyId", None)
        elif "value" in converted_price:
            price = float(converted_price["value"])
            currency = converted_price.get("@currencyId", None)

        item_id = it.get("itemId", [None])[0]
        title = it.get("title", [""])[0]
        view_url = it.get("viewItemURL", [""])[0]
        # endTime is the "sold" time for completed items
        sold_date = it.get("listingInfo", [{}])[0].get("endTime", [""])[0]

        normalized.append({
            "itemId": item_id,
            "title": title,
            "price": price,
            "currency": currency,
            "soldDate": sold_date,
            "url": view_url,
            "raw": it
        })
    return normalized

# --------------- BACKGROUND REFRESH WORKER ---------------
class RefreshWorker:
    def __init__(self, db_path: str, session: requests.Session, rate_limiter: RateLimiter):
        self.db_path = db_path
        self.session = session
        self.rate_limiter = rate_limiter
        self.q = queue.Queue()
        self.stop = Event()
        Thread(target=self._run, daemon=True).start()

    def submit(self, cache_key: str, query: str, filters: dict):
        self.q.put((cache_key, query, filters))

    def _run(self):
        while not self.stop.is_set():
            try:
                cache_key, query, filters = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                all_items = []
                for page in range(1, MAX_PAGES + 1):
                    items = ebay_find_completed_items(self.session, self.rate_limiter, query, page, filters)
                    all_items.extend(items)
                    # stop early if fewer than a full page returned
                    if len(items) < PAGE_SIZE:
                        break
                with sqlite_conn(st.session_state["db_path"]) as conn:
                    cache_put(conn, cache_key, query, filters, all_items)
            except Exception as e:
                # Log to Streamlit for visibility (optional)
                st.sidebar.warning(f"Background refresh failed: {e}")
            finally:
                self.q.task_done()

# --------------- STREAMLIT APP ---------------
st.set_page_config(page_title="CardSight (Refactor)", layout="wide")

# Initialize shared resources once
@st.cache_resource
def _init_resources():
    db_path = os.path.join(os.getcwd(), "cardsight_cache.sqlite")
    init_db(db_path)
    session = build_session()
    limiter = RateLimiter(CALLS_PER_SECOND, CALLS_PER_DAY_BUDGET)
    worker = RefreshWorker(db_path, session, limiter) if BACKGROUND_REFRESH else None
    return db_path, session, limiter, worker

db_path, session, limiter, worker = _init_resources()
st.session_state["db_path"] = db_path

st.title("CardSight – Efficient eBay Past Sales")
st.caption("SQLite caching • rate-limited requests • optional background refresh")

with st.sidebar:
    st.header("Search")
    query = st.text_input("Keywords", "pokemon charizard psa 10")
    category = st.text_input("Category ID (optional)", "")
    min_price = st.number_input("Min Price", min_value=0.0, value=0.0, step=1.0)
    max_price = st.number_input("Max Price", min_value=0.0, value=0.0, step=1.0)
    max_pages_ui = st.slider("Max Pages", 1, MAX_PAGES, min(MAX_PAGES, 3))
    refresh_now = st.button("Force Refresh")

filters = {}
if category:
    filters["categoryId"] = category
if min_price > 0:
    filters["minPrice"] = min_price
if max_price > 0:
    filters["maxPrice"] = max_price

cache_key = make_cache_key(query, filters)

# Read cache
with sqlite_conn(db_path) as conn:
    cached_df, fresh_ts = cache_get(conn, cache_key, CACHE_STALE_HOURS)

# If user forces refresh, or cache is missing/stale, refresh
needs_refresh = refresh_now or (fresh_ts is None)
if needs_refresh:
    if BACKGROUND_REFRESH and cached_df is not None and not cached_df.empty:
        # Return cached data immediately and kick off background refresh
        if worker:
            worker.submit(cache_key, query, filters)
        st.info("Showing cached results while refreshing in the background…")
    else:
        # Synchronous refresh (blocks UI until done)
        st.info("Refreshing from eBay…")
        all_items = []
        for page in range(1, max_pages_ui + 1):
            try:
                items = ebay_find_completed_items(session, limiter, query, page, filters)
            except Exception as e:
                st.error(f"API error on page {page}: {e}")
                break
            all_items.extend(items)
            if len(items) < PAGE_SIZE:
                break
        with sqlite_conn(db_path) as conn:
            cache_put(conn, cache_key, query, filters, all_items)
        with sqlite_conn(db_path) as conn:
            cached_df, fresh_ts = cache_get(conn, cache_key, CACHE_STALE_HOURS)

# Display results
if cached_df is None or cached_df.empty:
    st.warning("No results yet. Try a different query or wait for the refresh to complete.")
else:
    st.subheader("Results")
    st.write(f"Last updated: **{fresh_ts.isoformat() if fresh_ts else 'stale (refreshing)'}**")
    show_cols = ["title", "price", "currency", "sold_date", "url"]
    st.dataframe(cached_df[show_cols])

    # Simple aggregates from local cache (no extra API calls)
    st.subheader("Summary from cache")
    with st.container():
        try:
            m_price = cached_df["price"].dropna()
            if not m_price.empty:
                st.metric("Median price", f"{m_price.median():.2f}")
                st.metric("Mean price", f"{m_price.mean():.2f}")
                st.metric("Count", f"{len(m_price)}")
        except Exception:
            pass

st.caption("This refactor keeps you within rate limits via caching, throttling, and background refresh.")
'''

path = "/mnt/data/CardSight_refactor.py"
with open(path, "w") as f:
    f.write(refactored_code)

print(f"Refactored file written to: {path}")

                .get("searchResult", [{}])[0]
                .get("item", [])
        )
        results = []
        for item in items:
            price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
            results.append({
                "Title": item.get("title", ""),
                "Price": float(price_info.get("__value__", 0)),
                "Currency": price_info.get("@currencyId", ""),
                "End Date": item.get("listingInfo", [{}])[0].get("endTime", ""),
                "URL": item.get("viewItemURL", "")
            })
        return results
    else:
        st.error(
            f"Error fetching from eBay API: {response.status_code}. "
            "This may be due to rate limits. Try again later."
        )
        return []

# ==============================
# Streamlit App
# ==============================
st.set_page_config(page_title="CardSight Sold Listings", page_icon="🃏", layout="wide")
st.title("CardSight: Sold Listings Lookup")
st.info("Search for a sports card to view past sold eBay sales.")

search_term = st.text_input("Enter a card/player name:", "")

if search_term:
    with st.spinner("Fetching sold listings from eBay (cached results may appear instantly)..."):
        results = search_sold_ebay_cards(search_term, limit=10)

    if results:
        df = pd.DataFrame(results)

        # Sidebar filters
        st.sidebar.header("🔎 Filters")
        min_price, max_price = st.sidebar.slider(
            "Price Range ($)",
            0, int(df["Price"].max() + 10),
            (0, int(df["Price"].max()))
        )
        df = df[(df["Price"] >= min_price) & (df["Price"] <= max_price)]

        # Summary stats
        col1, col2 = st.columns(2)
        col1.metric("Total Sold Listings", len(df))
        col2.metric("Average Price", f"${df['Price'].mean():.2f}")

        # Price distribution chart
        st.subheader("📊 Sold Price Distribution")
        price_counts = df["Price"].value_counts().sort_index()
        st.bar_chart(price_counts)

        # Results table
        st.subheader("📋 Sold Listings")
        for _, row in df.iterrows():
            with st.expander(row["Title"], expanded=False):
                st.write(f"💲 Price: {row['Price']} {row['Currency']}")
                st.write(f"📅 End Date: {row['End Date']}")
                st.markdown(f"🔗 [View on eBay]({row['URL']})")

        # Download CSV
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download CSV",
            data=csv,
            file_name=f"{search_term.replace(' ', '_')}_sold.csv",
            mime="text/csv"
        )
    else:
        st.warning("No sold listings found or API limit reached. Try again later.")
