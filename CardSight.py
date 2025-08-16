import requests
import pandas as pd
import streamlit as st
from datetime import timedelta

# ==============================
# 🔑 eBay API credentials from Streamlit secrets
# ==============================
EBAY_APP_ID = st.secrets["ebay"]["app_id"]

# ==============================
# eBay Finding API
# ==============================
FINDING_API_URL = "https://svcs.ebay.com/services/search/FindingService/v1"

# ==============================
# Search completed/sold items (cached)
# ==============================
@st.cache_data(ttl=600)  # Cache results for 10 minutes
def search_sold_ebay_cards(query, limit=25):
    headers = {
        "X-EBAY-SOA-OPERATION-NAME": "findCompletedItems",
        "X-EBAY-SOA-SERVICE-VERSION": "1.13.0",
        "X-EBAY-SOA-REQUEST-DATA-FORMAT": "JSON",
        "X-EBAY-SOA-SECURITY-APPNAME": EBAY_APP_ID,
        "Content-Type": "application/json"
    }

    payload = {
        "keywords": query,
        "paginationInput": {"entriesPerPage": limit, "pageNumber": 1},
        "itemFilter": [
            {"name": "SoldItemsOnly", "value": "true"},
            {"name": "ListingType", "value": "FixedPrice"}
        ]
    }

    try:
        response = requests.post(FINDING_API_URL, headers=headers, json=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        st.error(f"Error fetching from eBay API: {e}")
        return []

    data = response.json()
    items = (
        data.get("findCompletedItemsResponse", [{}])[0]
        .get("searchResult", [{}])[0]
        .get("item", [])
    )

    results = []
    for item in items:
        price = float(item.get("sellingStatus", {}).get("currentPrice", {}).get("__value__", 0))
        results.append({
            "Title": item.get("title"),
            "Price": price,
            "Currency": item.get("sellingStatus", {}).get("currentPrice", {}).get("@currencyId"),
            "End Date": item.get("listingInfo", {}).get("endTime"),
            "URL": item.get("viewItemURL")
        })
    return results

# ==============================
# Streamlit App - CardSight Sold Listings
# ==============================
st.set_page_config(page_title="CardSight Sold", page_icon="🃏", layout="wide")
st.title("CardSight: Sold Sports Card Lookup")
st.info("Search for a sports card to view past sold eBay listings.")

# Search input
search_term = st.text_input("Enter a card/player name:", "")

if search_term:
    with st.spinner("Fetching sold listings..."):
        results = search_sold_ebay_cards(search_term, limit=25)

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

        # Results
        st.subheader("📋 Sold Listings")
        for _, row in df.iterrows():
            with st.expander(row["Title"], expanded=False):
                st.write(f"💲 Price: {row['Price']} {row['Currency']}")
                st.write(f"📅 End Date: {row['End Date']}")
                st.markdown(f"🔗 [View on eBay]({row['URL']})")

        # Download option
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download CSV",
            data=csv,
            file_name=f"{search_term.replace(' ', '_')}_sold.csv",
            mime="text/csv"
        )
    else:
        st.warning("No sold listings found or API limit reached.")
