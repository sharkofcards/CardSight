import requests
import base64
import time
import pandas as pd
import streamlit as st

# ==============================
# 🔑 eBay API credentials via Streamlit Secrets
# ==============================
EBAY_APP_ID = st.secrets["ebay"]["app_id"]
EBAY_CERT_ID = st.secrets["ebay"]["cert_id"]
EBAY_REDIRECT_URI = st.secrets["ebay"]["redirect_uri"]

# Token cache
token_cache = {"access_token": None, "expires_at": 0}

# ==============================
# OAuth Token Handling
# ==============================
def get_ebay_oauth_token():
    global token_cache
    if token_cache["access_token"] and time.time() < token_cache["expires_at"]:
        return token_cache["access_token"]

    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }

    response = requests.post(url, headers=headers, data=data)

    if response.status_code == 200:
        json_resp = response.json()
        token_cache["access_token"] = json_resp["access_token"]
        token_cache["expires_at"] = time.time() + int(json_resp["expires_in"]) - 60
        return token_cache["access_token"]
    else:
        st.error(f"Failed to get OAuth token: {response.text}")
        return None

# ==============================
# eBay Browse API Search
# ==============================
def search_ebay_cards(query, limit=10):
    token = get_ebay_oauth_token()
    if not token:
        return []

    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {"q": query, "limit": limit}

    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        return response.json().get("itemSummaries", [])
    else:
        st.error(f"Error fetching from eBay API: {response.status_code}, {response.text}")
        return []

# ==============================
# Generate Grading Pop Report Links
# ==============================
def get_pop_report_links(query):
    q = query.replace(" ", "+")
    return {
        "PSA": f"https://www.psacard.com/pop/tcg?query={q}",
        "BGS": f"https://www.beckett.com/pop-report?q={q}",
        "SGC": f"https://www.gosgc.com/pop-report/{q}"
    }

# ==============================
# Streamlit App - CardSight
# ==============================
st.set_page_config(page_title="CardSight", page_icon="🃏", layout="wide")

st.title("CardSight: Sports Card Comp & Pop Lookup")
st.info("Search for a sports card to view past eBay sales and grading population reports.")

# Search input
search_term = st.text_input("Enter a card/player name:", "")

if search_term:
    with st.spinner("Searching eBay and fetching data..."):
        results = search_ebay_cards(search_term, limit=25)

    if results:
        data = []
        for item in results:
            data.append({
                "Title": item.get("title"),
                "Price": float(item.get("price", {}).get("value", 0)),
                "Currency": item.get("price", {}).get("currency"),
                "End Date": item.get("itemEndDate", "N/A"),
                "URL": item.get("itemWebUrl")
            })

        df = pd.DataFrame(data)

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
        col1.metric("Total Sales Found", len(df))
        col2.metric("Average Price", f"${df['Price'].mean():.2f}")

        # Results
        st.subheader("📋 Sales Results")
        pop_links = get_pop_report_links(search_term)
        for _, row in df.iterrows():
            with st.expander(row["Title"], expanded=False):
                st.write(f"💲 Price: {row['Price']} {row['Currency']}")
                st.write(f"📅 End Date: {row['End Date']}")
                st.markdown(f"🔗 [View on eBay]({row['URL']})")

                # Pop report links
                st.write("📊 Population Reports:")
                st.markdown(f"- [PSA]({pop_links['PSA']})")
                st.markdown(f"- [BGS]({pop_links['BGS']})")
                st.markdown(f"- [SGC]({pop_links['SGC']})")

        # Download option
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download CSV",
            data=csv,
            file_name=f"{search_term.replace(' ', '_')}_sales.csv",
            mime="text/csv"
        )
    else:
        st.warning("No results found.")
