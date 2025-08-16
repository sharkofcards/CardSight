import requests
import pandas as pd
import streamlit as st

# ==============================
# eBay API Credentials (from secrets)
# ==============================
EBAY_APP_ID = st.secrets["ebay"]["app_id"]

# ==============================
# Cached function to query sold listings
# ==============================
@st.cache_data(ttl=600)  # cache results for 10 minutes
def search_sold_ebay_cards(query, limit=25):
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

    response = requests.get(url, params=params)
    if response.status_code == 200:
        data = response.json()
        items = data.get("findCompletedItemsResponse", [])[0].get("searchResult", [])[0].get("item", [])
        results = []
        for item in items:
            results.append({
                "Title": item.get("title", ""),
                "Price": float(item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0].get("__value__", 0)),
                "Currency": item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0].get("@currencyId", ""),
                "End Date": item.get("listingInfo", [{}])[0].get("endTime", ""),
                "URL": item.get("viewItemURL", "")
            })
        return results
    else:
        st.error(f"Error fetching from eBay API: {response.status_code}")
        return []

# ==============================
# Streamlit App
# ==============================
st.set_page_config(page_title="CardSight Sold Listings", page_icon="🃏", layout="wide")
st.title("CardSight: Sold Listings Lookup")
st.info("Search for a sports card to view past sold eBay sales.")

search_term = st.text_input("Enter a card/player name:", "")

if search_term:
    with st.spinner("Fetching sold listings from eBay..."):
        results = search_sold_ebay_cards(search_term, limit=50)

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
        st.warning("No sold listings found.")
