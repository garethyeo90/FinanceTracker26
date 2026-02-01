import streamlit as st
import pandas as pd

from datetime import date

def parse_expiry_yymmdd_to_date(x):
    """
    Parses expiry values like 260214 (YYMMDD) into a python date (2026-02-14).
    Handles strings/numbers, blanks, invalid values safely.
    """
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return pd.NaT

    s = str(x).strip()

    # Some Excel values may come in like '260214.0'
    if s.endswith(".0"):
        s = s[:-2]

    # Keep digits only
    s = "".join(ch for ch in s if ch.isdigit())

    if len(s) != 6:
        return pd.NaT

    yy = int(s[:2])
    mm = int(s[2:4])
    dd = int(s[4:6])

    # Choose century rule: 00-79 -> 2000s, 80-99 -> 1900s (common convention)
    year = 2000 + yy if yy <= 79 else 1900 + yy

    try:
        return date(year, mm, dd)
    except ValueError:
        return pd.NaT


def compute_days_to_expiry_from_yymmdd(df: pd.DataFrame) -> pd.Series:
    if "Expiry" not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index)

    today = date.today()
    exp_dates = df["Expiry"].apply(parse_expiry_yymmdd_to_date)

    return exp_dates.apply(lambda d: (d - today).days if pd.notna(d) else pd.NA)

# --------------------------------------------------
# Page config
# --------------------------------------------------
st.set_page_config(page_title="Portfolio & Risk Dashboard", layout="wide")
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
    # Explicit engine avoids ambiguity in some environments
    acct = pd.read_excel(file, sheet_name="Account_Summary", engine="openpyxl")
    port = pd.read_excel(file, sheet_name="Portfolio_Allocation", engine="openpyxl")
    return acct, port

acct, port = load_data(uploaded_file)

# Standardize Month as string
acct["Month"] = acct["Month"].astype(str)
port["Month"] = port["Month"].astype(str)

months = sorted(acct["Month"].dropna().unique())
if not months:
    st.error("No months found in Account_Summary[Month].")
    st.stop()

snapshot_month = st.sidebar.selectbox("Snapshot Month", months, index=len(months) - 1)

# Snapshot frames
acct_m = acct[acct["Month"] == snapshot_month].iloc[0]
port_m = port[(port["Month"] == snapshot_month) & (port["Position_Status"] == "Open")].copy()

month_label = str(snapshot_month)

# --------------------------------------------------
# Helper: safe numeric fetch
# --------------------------------------------------
def num(x, default=0.0):
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default

# --------------------------------------------------
# SECTION 1 — Portfolio Snapshot
# --------------------------------------------------
st.subheader("📌 Portfolio Snapshot")

row1 = st.columns(5)
row1[0].metric(f"NAV (End) — {month_label}", f"${num(acct_m.get('NAV_End_USD')):,.0f}")
row1[1].metric(f"Economic P&L — {month_label}", f"${num(acct_m.get('Economic_PnL_USD')):,.0f}")
row1[2].metric("Target Achievement", f"{num(acct_m.get('Target_Achievement_%'))*100:.0f}%")
row1[3].metric("Cash % NAV", f"{num(acct_m.get('Cash_%_NAV'))*100:.1f}%")
row1[4].metric("Delta Leverage", f"{num(acct_m.get('Delta_Leverage_Ratio')):.2f}×")

# Second KPI row: CSP + Delta Notional Exposure
row2 = st.columns(3)

cash_usd = num(acct_m.get("Cash_USD"))
csp_required = num(acct_m.get("CSP_Cash_Required_USD"))

row2[0].metric("Cash (USD)", f"${cash_usd:,.0f}")
row2[1].metric("CSP Cash Required", f"${csp_required:,.0f}")

# CSP Utilization = CSP required / Cash
if cash_usd <= 0 or csp_required <= 0:
    row2[2].metric("CSP Utilization", "—")
else:
    csp_util = csp_required / cash_usd
    row2[2].metric("CSP Utilization", f"{csp_util:.2f}×")
    if csp_util > 1.0:
        st.error(f"⚠️ CSP Utilization {csp_util:.2f}× — NOT fully cash-secured (CSP required > cash)")
    elif csp_util > 0.8:
        st.warning(f"⚠️ CSP Utilization {csp_util:.2f}× — elevated cash usage")

# Delta Notional Exposure KPI (replaces Notional Exposure)
equity_delta = num(acct_m.get("Equity_Delta_Notional_USD"))
rates_delta = num(acct_m.get("Rates_Delta_Notional_USD"))
delta_total = equity_delta + rates_delta
st.metric("Delta Notional Exposure", f"${delta_total:,.0f}")

# --------------------------------------------------
# SECTION 2 — Allocation & Exposure
# --------------------------------------------------
st.subheader("📊 Allocation & Exposure")

left, right = st.columns(2)

# Allocation by Asset Class
if "Asset_Class" in port_m.columns and "Market_Value_USD" in port_m.columns and not port_m.empty:
    alloc = port_m.groupby("Asset_Class")["Market_Value_USD"].sum().reset_index()
else:
    alloc = pd.DataFrame(columns=["Asset_Class", "Market_Value_USD"])

with left:
    st.markdown("**Allocation (Market Value)**")
    if alloc.empty:
        st.info("No allocation data for this month.")
    else:
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

# Risk Exposure by Exposure_Class (Delta Notional)
if "Exposure_Class" in port_m.columns and "Delta_Notional_USD" in port_m.columns and not port_m.empty:
    expo = port_m.groupby("Exposure_Class")["Delta_Notional_USD"].sum().reset_index()
    expo = expo[expo["Delta_Notional_USD"] > 0]
else:
    expo = pd.DataFrame(columns=["Exposure_Class", "Delta_Notional_USD"])

with right:
    st.markdown("**Risk Exposure (Delta-Notional)**")
    if expo.empty:
        st.info("No delta exposure data for this month.")
    else:
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
# SECTION 3 — Trend Explorer (Month range only here)
# --------------------------------------------------
st.subheader("📈 Trends Over Time")

metric_map = {
    "NAV_End_USD": "NAV (End)",
    "Economic_PnL_USD": "Economic P&L",
    "Target_Achievement_%": "Target Achievement %",
    "Delta_Leverage_Ratio": "Delta Leverage",
    "Cash_%_NAV": "Cash % NAV",
    "CSP_Cash_Required_USD": "CSP Cash Required",
    "Cash_USD": "Cash (USD)",
}

metric = st.selectbox("Metric", list(metric_map.keys()), format_func=lambda x: metric_map[x])
smooth = st.selectbox("Smoothing", ["None", "3-Month", "6-Month"])

start_m, end_m = st.select_slider("Month Range", options=months, value=(months[0], months[-1]))

trend_df = acct[(acct["Month"] >= start_m) & (acct["Month"] <= end_m)].sort_values("Month").copy()

# If selected metric missing, show warning
if metric not in trend_df.columns:
    st.warning(f"Metric '{metric}' not found in Account_Summary.")
else:
    series = trend_df[metric]

    if smooth == "3-Month":
        series = series.rolling(3).mean()
    elif smooth == "6-Month":
        series = series.rolling(6).mean()

    trend_df["Plot"] = series
    trend_df = trend_df.dropna(subset=["Plot"])

    # IMPORTANT: guard against empty chart data (prevents JS RangeError)
    if trend_df.empty:
        st.info("Not enough data to display this metric for the selected range/smoothing.")
    else:
        st.line_chart(trend_df.set_index("Month")["Plot"], height=350)


        # --------------------------------------------------
# SECTION 3.5 — NAV Projection (next 12 months)
# --------------------------------------------------
st.subheader("🔮 NAV Projection (Next 12 Months)")

base_nav = num(acct_m.get("NAV_End_USD"))
if base_nav <= 0:
    st.info("NAV projection requires a positive NAV_End_USD in Account_Summary.")
else:
    c1, c2, c3 = st.columns([1.2, 1, 1])

    with c1:
        monthly_flow = st.number_input(
            "Monthly net flow (USD) — +contribution / -withdrawal",
            value=0.0,
            step=10_000.0,
            format="%.0f",
            help="Assumed net cash added/removed at the end of each month."
        )

    with c2:
        annual_return = st.number_input(
            "Assumed annual return (%)",
            value=0.0,
            step=0.5,
            format="%.2f",
            help="Optional. Set to 0 for a straight-line projection with flows only."
        )

    with c3:
        flow_timing = st.selectbox(
            "Flow timing",
            ["End of month", "Start of month"],
            index=0,
            help="Whether monthly_flow is applied before or after the return each month."
        )

    # Convert annual return to monthly (simple comp; keep it simple/transparent)
    r_m = (1 + annual_return / 100.0) ** (1/12) - 1

    # Build next 12 months labels
    try:
        start_dt = pd.to_datetime(month_label + "-01")
    except Exception:
        # Fallback if month_label isn't YYYY-MM
        start_dt = pd.Timestamp.today().normalize().replace(day=1)

    proj_months = pd.date_range(start=start_dt, periods=13, freq="MS")  # include start + 12 ahead
    nav = base_nav
    nav_path = [nav]

    for _ in range(12):
        if flow_timing == "Start of month":
            nav = nav + monthly_flow
            nav = nav * (1 + r_m)
        else:  # End of month
            nav = nav * (1 + r_m)
            nav = nav + monthly_flow
        nav_path.append(nav)

    proj_df = pd.DataFrame({
        "Month": proj_months.strftime("%Y-%m"),
        "Projected_NAV_USD": nav_path
    })

    # Show KPI + chart
    end_nav = proj_df["Projected_NAV_USD"].iloc[-1]
    total_change = end_nav - base_nav

    k1, k2, k3 = st.columns(3)
    k1.metric("Starting NAV", f"${base_nav:,.0f}")
    k2.metric("Projected NAV (12M)", f"${end_nav:,.0f}")
    k3.metric("Change (12M)", f"${total_change:,.0f}")

    st.line_chart(proj_df.set_index("Month")["Projected_NAV_USD"], height=350)

    with st.expander("Show projection table"):
        st.dataframe(proj_df, use_container_width=True, height=320)

# --------------------------------------------------
# SECTION 4 — Risk Alerts (2% rule) + Open Positions Table
# --------------------------------------------------
st.subheader("🛑 Risk Alerts & Open Positions")

# 2% rule: hard stop & soft warning
# Hard: Unrealized_PnL_USD < -2% NAV_End
# Soft: Unrealized_PnL_USD < -(2% NAV_End / #Open non-cash positions)

nav_end = num(acct_m.get("NAV_End_USD"))

# Only compute if the needed columns exist
required_cols = {"Unrealized_PnL_USD", "Asset_Class", "Position_Status"}
can_flag = required_cols.issubset(set(port_m.columns)) and nav_end > 0

if can_flag and not port_m.empty:
    df = port_m.copy()
    df["Unrealized_PnL_USD"] = pd.to_numeric(df["Unrealized_PnL_USD"], errors="coerce").fillna(0.0)

    open_non_cash = df[df["Asset_Class"] != "Cash"]
    open_count = max(len(open_non_cash), 1)

    hard_threshold = -0.02 * nav_end
    soft_threshold = -0.02 * nav_end / open_count

    df["Hard_Breach"] = (df["Unrealized_PnL_USD"] < hard_threshold) & (df["Asset_Class"] != "Cash")
    df["Soft_Breach"] = (df["Unrealized_PnL_USD"] < soft_threshold) & (df["Asset_Class"] != "Cash")

    hard_df = df[df["Hard_Breach"]].copy()
    soft_df = df[(df["Soft_Breach"]) & (~df["Hard_Breach"])].copy()

    if len(hard_df) > 0:
        st.error(f"🔴 Hard 2% rule breached: {len(hard_df)} position(s) worse than -2% of NAV.")
        show_cols = [c for c in ["Ticker", "Unrealized_PnL_USD", "Market_Value_USD", "Delta_Notional_USD", "Strategy"] if c in hard_df.columns]
        st.dataframe(hard_df[show_cols].sort_values("Unrealized_PnL_USD"), use_container_width=True, height=180)

    if len(soft_df) > 0:
        st.warning(f"🟠 Soft risk budget breached: {len(soft_df)} position(s) worse than -(2% NAV / #open positions).")
        show_cols = [c for c in ["Ticker", "Unrealized_PnL_USD", "Market_Value_USD", "Delta_Notional_USD", "Strategy"] if c in soft_df.columns]
        st.dataframe(soft_df[show_cols].sort_values("Unrealized_PnL_USD"), use_container_width=True, height=180)

    if len(hard_df) == 0 and len(soft_df) == 0:
        st.success("✅ No 2% risk breaches for this month.")
else:
    st.info("Risk flags require Unrealized_PnL_USD, Asset_Class, and NAV_End_USD to be present.")

# Open positions table
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
display_cols = [c for c in display_cols if c in port_m.columns]

# Add Days_to_Expiry (Expiry is YYMMDD)
port_m["Days_to_Expiry"] = compute_days_to_expiry_from_yymmdd(port_m)

# Recompute thresholds for this month (same as alerts)
nav_end = num(acct_m.get("NAV_End_USD"))

tmp = port_m.copy()
tmp["Unrealized_PnL_USD"] = pd.to_numeric(tmp.get("Unrealized_PnL_USD", 0), errors="coerce").fillna(0.0)

# Hard/Soft breach logic only applies to non-cash positions
open_non_cash = tmp[tmp.get("Asset_Class", "") != "Cash"]
open_count = max(len(open_non_cash), 1)

hard_threshold = -0.02 * nav_end if nav_end > 0 else float("-inf")
soft_threshold = (-0.02 * nav_end / open_count) if nav_end > 0 else float("-inf")

tmp["Hard_Breach"] = (tmp.get("Asset_Class", "") != "Cash") & (tmp["Unrealized_PnL_USD"] < hard_threshold)
tmp["Soft_Breach"] = (tmp.get("Asset_Class", "") != "Cash") & (tmp["Unrealized_PnL_USD"] < soft_threshold)

# Style rows: RED if hard breach OR DTE < 14; AMBER if soft breach (optional)
def style_row(row):
    dte = row.get("Days_to_Expiry", pd.NA)
    hard = bool(row.get("Hard_Breach", False))
    soft = bool(row.get("Soft_Breach", False))

    if hard or (pd.notna(dte) and dte < 14):
        return ["background-color: #ffcccc"] * len(row)  # red

    if soft:
        return ["background-color: #fff2cc"] * len(row)  # amber

    return [""] * len(row)

# Add DTE column into display columns
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
    "Expiry",
    "Days_to_Expiry",
    "Strategy",
]
display_cols = [c for c in display_cols if c in tmp.columns]

# Optional: show a banner count for soon-to-expire positions
soon = tmp[pd.to_numeric(tmp["Days_to_Expiry"], errors="coerce") < 14]
if len(soon) > 0:
    st.warning(f"⏳ {len(soon)} open position(s) expiring within 14 days.")

st.dataframe(
    tmp[display_cols]
      .sort_values("Market_Value_USD", key=abs, ascending=False)
      .style.apply(style_row, axis=1),
    use_container_width=True,
    height=420
)

st.caption("Note: Excel conditional formatting colors do not carry into Streamlit tables. "
           "The dashboard reproduces the 2% rules as alert sections above.")