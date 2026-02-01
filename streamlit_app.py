import streamlit as st
import pandas as pd

# --------------------------------------------------
# Page config
# --------------------------------------------------
st.set_page_config(page_title="Portfolio Dashboard", layout="wide")
st.title("📊 Portfolio & Risk Dashboard")

# --------------------------------------------------
# Sidebar – Upload + Snapshot Month
# --------------------------------------------------
st.sidebar.header("Controls")

uploaded_file = st.sidebar.file_uploader("Upload Portfolio Excel", type=["xlsx"])
if uploaded_file is None:
    st.info("👈 Upload your Excel file to begin")
    st.stop()

@st.cache_data
def load_data(file):
    acct = pd.read_excel(file, sheet_name="Account_Summary")
    port = pd.read_excel(file, sheet_name="Portfolio_Allocation")
    return acct, port

acct, port = load_data(uploaded_file)

# Make Month safe and sortable
acct["Month"] = acct["Month"].astype(str)
port["Month"] = port["Month"].astype(str)

months = sorted(acct["Month"].dropna().unique())

snapshot_month = st.sidebar.selectbox("Snapshot Month", months, index=len(months) - 1)

acct_m = acct[acct["Month"] == snapshot_month].iloc[0]
port_m = port[(port["Month"] == snapshot_month) & (port["Position_Status"] == "Open")]

# --------------------------------------------------
# SECTION 1 — Portfolio Snapshot KPIs
# --------------------------------------------------
st.subheader("📌 Portfolio Snapshot")

row1 = st.columns(5)
row1[0].metric("NAV (End)", f"${acct_m['NAV_End_USD']:,.0f}")
row1[1].metric("Economic P&L", f"${acct_m['Economic_PnL_USD']:,.0f}")
row1[2].metric("Target Achievement", f"{acct_m['Target_Achievement_%']*100:.0f}%")
row1[3].metric("Cash % NAV", f"{acct_m['Cash_%_NAV']*100:.1f}%")
row1[4].metric("Delta Leverage", f"{acct_m['Delta_Leverage_Ratio']:.2f}×")

row2 = st.columns(4)

csp_required = acct_m.get("CSP_Cash_Required_USD", 0)
csp_coverage = acct_m.get("CSP_Coverage_Ratio", None)

row2[0].metric("CSP Cash Required", f"${csp_required:,.0f}")

if pd.isna(csp_coverage) or csp_required == 0:
    row2[1].metric("CSP Coverage", "—")
else:
    row2[1].metric("CSP Coverage", f"{csp_coverage:.2f}×")

row2[2].metric("Notional Exposure", f"${acct_m['Notional_Exposure_USD']:,.0f}")

equity_delta = acct_m.get("Equity_Delta_Notional_USD", 0)
rates_delta = acct_m.get("Rates_Delta_Notional_USD", 0)
total_delta = equity_delta + rates_delta
equity_pct = equity_delta / total_delta * 100 if total_delta > 0 else 0
row2[3].metric("Equity Risk Share", f"{equity_pct:.0f}%")

# --------------------------------------------------
# SECTION 2 — Allocation & Exposure
# --------------------------------------------------
st.subheader("📊 Allocation & Exposure")

left, right = st.columns(2)

alloc = port_m.groupby("Asset_Class")["Market_Value_USD"].sum().reset_index()

with left:
    st.markdown("**Allocation (Market Value)**")
    st.plotly_chart(
        {
            "data": [{
                "labels": alloc["Asset_Class"],
                "values": alloc["Market_Value_USD"],
                "type": "pie",
                "hole": 0.4,
            }],
            "layout": {"height": 350}
        },
        use_container_width=True
    )

expo = port_m.groupby("Exposure_Class")["Delta_Notional_USD"].sum().reset_index()
expo = expo[expo["Delta_Notional_USD"] > 0]

with right:
    st.markdown("**Risk Exposure (Delta-Notional)**")
    st.plotly_chart(
        {
            "data": [{
                "labels": expo["Exposure_Class"],
                "values": expo["Delta_Notional_USD"],
                "type": "pie",
                "hole": 0.4,
            }],
            "layout": {"height": 350}
        },
        use_container_width=True
    )

# --------------------------------------------------
# SECTION 3 — Trend Explorer (Local Month Range)
# --------------------------------------------------
st.subheader("📈 Trends Over Time")

metric_map = {
    "NAV_End_USD": "NAV (End)",
    "Economic_PnL_USD": "Economic P&L",
    "Target_Achievement_%": "Target Achievement %",
    "Delta_Leverage_Ratio": "Delta Leverage",
    "Cash_%_NAV": "Cash % NAV",
    "CSP_Cash_Required_USD": "CSP Cash Required",
    "CSP_Coverage_Ratio": "CSP Coverage Ratio",
    "Equity_Delta_Notional_USD": "Equity Delta Exposure",
    "Rates_Delta_Notional_USD": "Rates Delta Exposure",
}

metric = st.selectbox("Metric", list(metric_map.keys()), format_func=lambda x: metric_map[x])
smooth = st.selectbox("Smoothing", ["None", "3-Month", "6-Month"])

start_m, end_m = st.select_slider("Month Range", options=months, value=(months[0], months[-1]))

trend_df = acct[(acct["Month"] >= start_m) & (acct["Month"] <= end_m)].sort_values("Month")

series = trend_df[metric]
if smooth == "3-Month":
    series = series.rolling(3).mean()
elif smooth == "6-Month":
    series = series.rolling(6).mean()

trend_df["Plot"] = series
trend_df = trend_df.dropna(subset=["Plot"])

st.line_chart(trend_df.set_index("Month")["Plot"], height=350)

# --------------------------------------------------
# SECTION 4 — Open Positions Table
# --------------------------------------------------
st.subheader("📋 Open Positions")

display_cols = [
    "Ticker",
    "Asset_Class",
    "Exposure_Class",
    "Quantity",
    "Market_Value_USD",
    "Notional_Value_USD",
    "Delta_Notional_USD",
    "Unrealized_PnL_USD",
    "CSP_Cash_Required_USD",
    "Strategy",
]

# Only show columns that exist (safer if older files are uploaded)
display_cols = [c for c in display_cols if c in port_m.columns]

st.dataframe(
    port_m[display_cols].sort_values("Market_Value_USD", key=abs, ascending=False),
    use_container_width=True,
    height=420
)

st.caption("Excel is the source of truth. Risk flags (amber/red) are computed in Excel and reflected in the workbook.")
