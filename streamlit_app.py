import re
import io
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import pdfplumber
import plotly.express as px
import streamlit as st

# ============================================================
# Page config + styling
# ============================================================
st.set_page_config(page_title="Moomoo Options Dashboard", layout="wide")

st.markdown("""
<style>
html, body {
    background-color: #0e1117;
}
.block-container {
    padding-top: 1.2rem;
    padding-bottom: 2rem;
}
.card {
    background: #121417;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 16px;
}
.card h3 {
    margin: 0 0 0.25rem 0;
    font-size: 0.95rem;
    color: rgba(255,255,255,0.75);
}
.big {
    font-size: 1.9rem;
    font-weight: 700;
    margin: 0.15rem 0;
}
.sub {
    color: rgba(255,255,255,0.55);
    font-size: 0.85rem;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# Helpers
# ============================================================
def money_to_float(s: str) -> float:
    """
    Converts strings like "533,599.86" or "- 529,600.00" or "(1,234.56)" to float.
    """
    if s is None:
        return 0.0
    s = str(s).strip()
    s = s.replace(",", "")
    s = s.replace("−", "-")  # unicode minus
    # handle "- 529,600.00" style
    s = s.replace("- ", "-")
    # parentheses negative
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    # allow empty
    if s in {"", "-"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        # try to extract first float-like
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else 0.0


def parse_option_code(code: str) -> Dict[str, Optional[object]]:
    """
    Parse option symbol like:
      SPY260123P665000  -> underlying=SPY, expiry=2026-01-23, type=P, strike=665.0
      VT260821C125000   -> strike=125.0
    """
    code = (code or "").strip().upper().replace(" ", "")
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d+)$", code)
    if not m:
        return {"underlying": None, "expiry": None, "call_put": None, "strike": None}
    und, yymmdd, cp, strike_digits = m.groups()

    # yymmdd -> 20yy-mm-dd (reasonable for your use case)
    yy = int(yymmdd[0:2])
    year = 2000 + yy
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])

    # strike digits appear to be scaled by 1000 in your statement codes (e.g. 665000 -> 665.0)
    strike = int(strike_digits) / 1000.0 if strike_digits.isdigit() else None

    return {
        "underlying": und,
        "expiry": pd.Timestamp(year=year, month=month, day=day),
        "call_put": cp,
        "strike": strike,
    }


def direction_to_side(direction: str) -> str:
    """
    Map statement direction to a simplified side.
    """
    d = (direction or "").strip().lower()
    if "sell" in d:
        return "SELL"
    if "buy" in d:
        return "BUY"
    return "UNKNOWN"


def is_options_symbol(symbol_text: str) -> bool:
    """
    Heuristic: options codes are like SPY260123P665000 or TLT260116C89500, etc.
    """
    s = (symbol_text or "").strip().upper().replace(" ", "")
    return bool(re.match(r"^[A-Z]+(\d{6})[CP]\d+$", s))


# ============================================================
# Parsing moomoo PDF
# ============================================================
@dataclass
class ParsedTrade:
    direction: str
    symbol_name: str     # long name or underlying name line
    symbol_code: str     # e.g. SPY260123P665000 or SPY
    exchange: str
    currency: str
    datetime: pd.Timestamp
    price: float
    quantity: float
    amount: float        # statement "Amount" (positive number shown)
    fees_alloc: float    # allocated fees for this trade-group
    group_id: int


def extract_account_summary(all_text: str) -> Dict[str, float]:
    """
    Pull out NAV / Portfolio Value / Cash Balance from the first pages text.
    Works off patterns visible in your statement.
    """
    out = {}

    # Net Asset Value: 270,563.28
    m = re.search(r"Net Asset Value:\s*([\d,]+\.\d+)", all_text)
    if m:
        out["nav_end"] = money_to_float(m.group(1))

    # Portfolio Value 270,555.07
    m = re.search(r"Portfolio Value\s*([\d,]+\.\d+)", all_text)
    if m:
        out["portfolio_value"] = money_to_float(m.group(1))

    # Cash Balance 8.21
    m = re.search(r"Cash Balance\s*([\d,]+\.\d+)", all_text)
    if m:
        out["cash_balance"] = money_to_float(m.group(1))

    # Starting/Ending NAV in SGD section:
    m = re.search(r"Starting Net Asset Value\s*\d+\s*Equal to\(SGD\)\s*([\d,]+\.\d+)", all_text)
    if m:
        out["nav_start"] = money_to_float(m.group(1))

    m = re.search(r"Ending Net Asset Value\s*\d+\s*Equal to\(SGD\)\s*([\d,]+\.\d+)", all_text)
    if m:
        out["nav_end_sgd_equal"] = money_to_float(m.group(1))

    return out


def parse_trades_from_text_lines(lines: List[str]) -> Tuple[List[ParsedTrade], List[Dict[str, float]]]:
    """
    Parse the "Trades - Securities" section by scanning text lines.
    We also capture Subtotal blocks to allocate fees.

    Returns:
      trades, fee_groups
    """
    trades: List[ParsedTrade] = []
    fee_groups: List[Dict[str, float]] = []

    # Patterns seen in your PDF:
    # Direction line: "Buy to Open" / "Sell to Close" etc
    dir_pat = re.compile(r"^(Buy|Sell)\s+to\s+(Open|Close)$", re.IGNORECASE)

    # A trade line chunk looks like (spanning lines):
    # Direction
    # Symbol long name (optional)
    # Symbol code (e.g. SPY260123P665000) + Exchange + ... + Currency
    # Date/Time
    # Price Quantity Amount
    #
    # But sometimes the statement prints in a more compressed way; we handle both.

    # Fee subtotal blocks:
    # "Subtotal: 29.79 Number of Transactions: 2 Transaction Amount: 510.00 Net Transaction Amount: 320.21
    #  Commission: 13.00 Platform Fees: 6.00 Trading Activity Fees: 0.03 ..."
    subtotal_pat = re.compile(r"^Subtotal:\s*([\d,]+\.\d+)", re.IGNORECASE)

    # fee fields:
    fee_fields = [
        "Commission",
        "Platform Fees",
        "Trading Activity Fees",
        "Options Regulatory Fees",
        "OCC Fees",
        "Option Settlement Fees",
        "Consolidated Audit Trail Fees",
        "Consumption Tax",
        "Total of Transaction Fee",
    ]

    # temp state
    current_group_id = 0
    current_group_trades_idx: List[int] = []
    fee_accum = 0.0

    i = 0
    while i < len(lines):
        line = (lines[i] or "").strip()

        # Detect Subtotal block -> finalize fee allocation for the current group
        sm = subtotal_pat.match(line)
        if sm:
            # Subtotal sometimes represents total fee for that block (often yes).
            # We will ALSO scan subsequent lines for explicit fee components and sum them.
            # We'll take the more reliable of:
            #   a) Subtotal value (first number after Subtotal:)
            #   b) Sum of components if found
            subtotal_fee = money_to_float(sm.group(1))

            # Scan next few lines for fee components
            component_sum = 0.0
            lookahead = " ".join(lines[i : min(i + 6, len(lines))])
            for f in fee_fields:
                fm = re.search(rf"{re.escape(f)}:\s*([\d,]+\.\d+)", lookahead, flags=re.IGNORECASE)
                if fm:
                    component_sum += money_to_float(fm.group(1))

            # Choose fee estimate:
            # - If component_sum > 0, use it (more detailed).
            # - else use subtotal_fee.
            fee_total = component_sum if component_sum > 0 else subtotal_fee

            # Save fee group info
            fee_groups.append(
                {
                    "group_id": current_group_id,
                    "fee_total": fee_total,
                    "n_trades": len(current_group_trades_idx),
                }
            )

            # Allocate fees equally across the trades in this group
            if current_group_trades_idx:
                per_trade_fee = fee_total / len(current_group_trades_idx)
                for t_idx in current_group_trades_idx:
                    trades[t_idx].fees_alloc = per_trade_fee

            # Start a new group
            current_group_id += 1
            current_group_trades_idx = []
            i += 1
            continue

        # Detect trade direction
        dm = dir_pat.match(line)
        if dm:
            direction = f"{dm.group(1).title()} to {dm.group(2).title()}"
            # Gather nearby lines to extract details
            # We'll look ahead up to ~8 lines and try to find:
            # - option/equity symbol code
            # - exchange
            # - currency
            # - datetime
            # - price, quantity, amount
            block = [line]
            for j in range(1, 10):
                if i + j >= len(lines):
                    break
                nxt = (lines[i + j] or "").strip()
                # Stop if a new trade begins
                if dir_pat.match(nxt) or subtotal_pat.match(nxt) or nxt.startswith("Direction "):
                    break
                block.append(nxt)

            block_text = " | ".join(block)

            # Extract datetime (YYYY/MM/DD HH:MM:SS)
            dt = None
            dtm = re.search(r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})", block_text)
            if dtm:
                dt = pd.to_datetime(dtm.group(1) + " " + dtm.group(2), format="%Y/%m/%d %H:%M:%S", errors="coerce")

            # Extract currency (USD/SGD etc.)
            cur = None
            curm = re.search(r"\b(USD|SGD|HKD|CNH|JPY)\b", block_text)
            if curm:
                cur = curm.group(1)

            # Extract exchange (US, SG, etc.) – statement uses "US" often
            exch = None
            exchm = re.search(r"\b(US|SG|HK)\b", block_text)
            if exchm:
                exch = exchm.group(1)

            # Extract possible symbol codes (options + equities)
            # Options code is best; else fallback to uppercase ticker
            option_codes = re.findall(r"\b[A-Z]+(?:\d{6})[CP]\d+\b", block_text.replace(" ", ""))
            symbol_code = option_codes[0] if option_codes else None

            # Underlying ticker might appear as separate word (SPY, VT, TLT, BRKB)
            # We'll try to pick the first ALLCAPS token of len 1-6
            ticker = None
            tickm = re.search(r"\b([A-Z]{1,6})\b", block_text)
            if tickm:
                ticker = tickm.group(1)

            if symbol_code is None and ticker is not None:
                symbol_code = ticker

            # Extract numbers: price, quantity, amount
            # In your statement, a common sequence is: "price qty amount"
            # Example: "0.2000 9 180.00"
            num_trip = re.findall(r"(-?\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+([\d,]+\.\d+)", block_text)
            price = qty = amt = None
            if num_trip:
                # choose last triplet in block (usually the main row)
                p, q, a = num_trip[-1]
                price = float(p)
                qty = float(q)
                amt = money_to_float(a)

            # Symbol display name (optional)
            # We'll pick the second line if it looks like a name and not like metadata
            symbol_name = ""
            if len(block) >= 2 and not re.search(r"\b(US|SG|HK|Agency|USD|SGD)\b", block[1]):
                symbol_name = block[1]
            else:
                symbol_name = symbol_code or ""

            # Only add if we have minimally useful data
            if dt is not None and symbol_code is not None and price is not None and qty is not None and amt is not None:
                t = ParsedTrade(
                    direction=direction,
                    symbol_name=symbol_name,
                    symbol_code=symbol_code,
                    exchange=exch or "",
                    currency=cur or "",
                    datetime=dt,
                    price=price,
                    quantity=qty,
                    amount=amt,
                    fees_alloc=0.0,
                    group_id=current_group_id,
                )
                trades.append(t)
                current_group_trades_idx.append(len(trades) - 1)

            i += len(block)  # skip block
            continue

        i += 1

    return trades, fee_groups


@st.cache_data(show_spinner=False)
def parse_moomoo_monthly_pdf(uploaded_bytes: bytes) -> Dict[str, pd.DataFrame]:
    """
    Main parser. Returns:
      - account_summary: one-row df
      - trades: normalized trades df (options + equity)
    """
    all_text_pages: List[str] = []
    trade_lines: List[str] = []

    with pdfplumber.open(io.BytesIO(uploaded_bytes)) as pdf:
        for p in pdf.pages:
            txt = p.extract_text() or ""
            all_text_pages.append(txt)

            # Collect lines from anywhere; we will focus on "Trades - Securities" sections
            lines = txt.splitlines()
            for ln in lines:
                trade_lines.append(ln)

    all_text = "\n".join(all_text_pages)

    # Account summary (NAV / cash / portfolio)
    acct = extract_account_summary(all_text)
    account_summary_df = pd.DataFrame([acct]) if acct else pd.DataFrame([{}])

    # Trades parsing
    trades_parsed, fee_groups = parse_trades_from_text_lines(trade_lines)

    if not trades_parsed:
        trades_df = pd.DataFrame(columns=[
            "datetime","direction","side","symbol_code","underlying","call_put","expiry","strike",
            "currency","exchange","qty","price","gross_amount","fees","net_amount","instrument_type"
        ])
        return {"account_summary": account_summary_df, "trades": trades_df}

    # Normalize to dataframe
    rows = []
    for t in trades_parsed:
        side = direction_to_side(t.direction)

        # Determine gross sign:
        # - SELL directions are credits (positive)
        # - BUY directions are debits (negative)
        gross = float(t.amount)
        if side == "BUY":
            gross = -abs(gross)
        elif side == "SELL":
            gross = abs(gross)

        opt_meta = parse_option_code(t.symbol_code) if is_options_symbol(t.symbol_code) else {
            "underlying": t.symbol_code, "expiry": None, "call_put": None, "strike": None
        }

        instrument_type = "option" if is_options_symbol(t.symbol_code) else "equity_or_fund"

        fees = float(t.fees_alloc or 0.0)
        net = gross - fees  # fees reduce outcome

        rows.append({
            "datetime": t.datetime,
            "direction": t.direction,
            "side": side,
            "symbol_code": t.symbol_code,
            "underlying": opt_meta.get("underlying"),
            "call_put": opt_meta.get("call_put"),
            "expiry": opt_meta.get("expiry"),
            "strike": opt_meta.get("strike"),
            "currency": t.currency,
            "exchange": t.exchange,
            "qty": t.quantity,
            "price": t.price,
            "gross_amount": gross,
            "fees": fees,
            "net_amount": net,
            "instrument_type": instrument_type,
            "group_id": t.group_id,
        })

    trades_df = pd.DataFrame(rows)
    trades_df["datetime"] = pd.to_datetime(trades_df["datetime"], errors="coerce")
    trades_df = trades_df.sort_values("datetime").reset_index(drop=True)

    return {"account_summary": account_summary_df, "trades": trades_df}


def dark_plot(fig):
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="rgba(255,255,255,0.85)"),
        margin=dict(l=10, r=10, t=30, b=10),
    )
    fig.update_xaxes(gridcolor="rgba(255,255,255,0.08)")
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.08)")
    return fig


# ============================================================
# UI - Upload
# ============================================================
st.sidebar.header("Upload")
uploaded = st.sidebar.file_uploader(
    "Upload moomoo Monthly Statement PDF",
    type=["pdf"]
)

if uploaded is None:
    st.markdown(
        """
        <div class="card">
          <h3>Upload your PDF</h3>
          <div class="sub">Use the sidebar to upload your moomoo monthly statement PDF.</div>
          <div class="sub">After upload, the dashboard updates instantly.</div>
        </div>
        """,
        unsafe_allow_html=True
    )
    st.stop()

data = parse_moomoo_monthly_pdf(uploaded.getvalue())
account_summary = data["account_summary"]
trades = data["trades"]

# ============================================================
# Compute KPIs
# ============================================================
# Filter options only (you can relax this later)
options = trades[trades["instrument_type"] == "option"].copy()

# Premium logic:
# Selling options (SELL) produces positive gross credits
# Buying to close/open produces negative debits
# "Net premium" here is net_amount (gross - fees)
options["date"] = options["datetime"].dt.date
options["week"] = options["datetime"].dt.to_period("W").astype(str)
options["month"] = options["datetime"].dt.to_period("M").astype(str)
options["year"] = options["datetime"].dt.year

latest_month = options["month"].max() if not options.empty else None
this_month_net = options.loc[options["month"] == latest_month, "net_amount"].sum() if latest_month else 0.0

today = pd.Timestamp.today()
week_cutoff = today - pd.Timedelta(days=7)
week_net = options.loc[options["datetime"] >= week_cutoff, "net_amount"].sum() if not options.empty else 0.0

ytd_cutoff = pd.Timestamp(year=today.year, month=1, day=1)
ytd_net = options.loc[options["datetime"] >= ytd_cutoff, "net_amount"].sum() if not options.empty else 0.0

# NAV/cash from account summary if available
nav_end = float(account_summary.iloc[0].get("nav_end", np.nan)) if not account_summary.empty else np.nan
portfolio_value = float(account_summary.iloc[0].get("portfolio_value", np.nan)) if not account_summary.empty else np.nan
cash_balance = float(account_summary.iloc[0].get("cash_balance", np.nan)) if not account_summary.empty else np.nan
nav_start = float(account_summary.iloc[0].get("nav_start", np.nan)) if not account_summary.empty else np.nan
nav_change = (nav_end - nav_start) if np.isfinite(nav_end) and np.isfinite(nav_start) else np.nan
nav_change_pct = (nav_change / nav_start * 100.0) if np.isfinite(nav_change) and np.isfinite(nav_start) and nav_start != 0 else np.nan

# ============================================================
# Layout
# ============================================================
c1, c2, c3, c4 = st.columns([1.1, 1.1, 1.1, 1.2], gap="large")

with c1:
    st.markdown(
        f"""
        <div class="card">
          <h3>Options Net (7d)</h3>
          <div class="big">${week_net:,.2f}</div>
          <div class="sub">Net = credits - debits - allocated fees</div>
        </div>
        """,
        unsafe_allow_html=True
    )

with c2:
    st.markdown(
        f"""
        <div class="card">
          <h3>Options Net (Month)</h3>
          <div class="big">${this_month_net:,.2f}</div>
          <div class="sub">Month: {latest_month or "—"}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

with c3:
    st.markdown(
        f"""
        <div class="card">
          <h3>Options Net (YTD)</h3>
          <div class="big">${ytd_net:,.2f}</div>
          <div class="sub">From {today.year}-01-01</div>
        </div>
        """,
        unsafe_allow_html=True
    )

with c4:
    # NAV / Portfolio summary
    nav_line = f"${nav_end:,.2f}" if np.isfinite(nav_end) else "—"
    pv_line = f"${portfolio_value:,.2f}" if np.isfinite(portfolio_value) else "—"
    cash_line = f"${cash_balance:,.2f}" if np.isfinite(cash_balance) else "—"
    chg_line = (
        f"{nav_change:+,.2f} ({nav_change_pct:+.2f}%)"
        if np.isfinite(nav_change) and np.isfinite(nav_change_pct)
        else "—"
    )

    st.markdown(
        f"""
        <div class="card">
          <h3>Account Summary</h3>
          <div class="sub">NAV (end): <b>{nav_line}</b></div>
          <div class="sub">NAV change: <b>{chg_line}</b></div>
          <div class="sub">Portfolio value: <b>{pv_line}</b></div>
          <div class="sub">Cash balance: <b>{cash_line}</b></div>
        </div>
        """,
        unsafe_allow_html=True
    )

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

left, mid, right = st.columns([1.2, 1.0, 1.0], gap="large")

# ==========================
# Table: Options trades
# ==========================
with left:
    st.markdown("<div class='card'><h3>Parsed Options Trades</h3></div>", unsafe_allow_html=True)

    show_cols = [
        "datetime", "direction", "symbol_code", "underlying", "call_put", "expiry", "strike",
        "qty", "price", "gross_amount", "fees", "net_amount"
    ]
    show_df = options[show_cols].copy()
    show_df["datetime"] = show_df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    show_df["expiry"] = pd.to_datetime(show_df["expiry"], errors="coerce").dt.strftime("%Y-%m-%d")

    st.dataframe(show_df, use_container_width=True, hide_index=True)

# ==========================
# Chart: Net by week
# ==========================
with mid:
    st.markdown("<div class='card'><h3>Net Options (Weekly)</h3></div>", unsafe_allow_html=True)
    if options.empty:
        st.info("No option trades detected in this PDF.")
    else:
        weekly = options.groupby("week", as_index=False)["net_amount"].sum()
        fig = px.bar(weekly, x="week", y="net_amount")
        st.plotly_chart(dark_plot(fig), use_container_width=True)

# ==========================
# Chart: Net by underlying
# ==========================
with right:
    st.markdown("<div class='card'><h3>Net by Underlying</h3></div>", unsafe_allow_html=True)
    if options.empty:
        st.info("No option trades detected in this PDF.")
    else:
        by_und = options.groupby("underlying", as_index=False)["net_amount"].sum().sort_values("net_amount", ascending=False)
        fig = px.bar(by_und, x="underlying", y="net_amount")
        st.plotly_chart(dark_plot(fig), use_container_width=True)

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

# ============================================================
# Debug section (helps you verify parsing quality)
# ============================================================
with st.expander("Debug: Fee allocation groups + raw stats"):
    st.write("Total parsed trades:", len(trades))
    st.write("Parsed option trades:", len(options))
    st.write("Currencies seen:", sorted(trades["currency"].dropna().unique().tolist()))
    st.write("Directions seen:", sorted(trades["direction"].dropna().unique().tolist()))
    st.write("Note: fees are allocated per 'Subtotal' group in the statement.")