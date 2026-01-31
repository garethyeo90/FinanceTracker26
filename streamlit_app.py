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
html, body {background-color:#0e1117;}
.block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
.card {
  background: #121417;
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 14px;
  padding: 16px;
}
.card h3 {margin: 0 0 0.25rem 0; font-size: 0.95rem; color: rgba(255,255,255,0.75);}
.big {font-size: 1.9rem; font-weight: 700; margin: 0.15rem 0;}
.sub {color: rgba(255,255,255,0.55); font-size: 0.85rem;}
.small {color: rgba(255,255,255,0.55); font-size: 0.78rem;}
</style>
""", unsafe_allow_html=True)

# ============================================================
# Helpers
# ============================================================
def money_to_float(s: str) -> float:
    """Convert money-ish strings to float (handles commas, - 123, (123))."""
    if s is None:
        return 0.0
    s = str(s).strip()
    s = s.replace(",", "").replace("−", "-").replace("- ", "-")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if s in {"", "-"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else 0.0


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


def direction_to_side(direction: str) -> str:
    d = (direction or "").strip().lower()
    if "sell" in d:
        return "SELL"
    if "buy" in d:
        return "BUY"
    return "UNKNOWN"


def is_options_symbol(code: str) -> bool:
    """Option codes often look like SPY260123P665000."""
    if not code:
        return False
    s = str(code).strip().upper().replace(" ", "")
    return bool(re.match(r"^[A-Z]+(\d{6})[CP]\d+$", s))


def parse_option_code(code: str) -> Dict[str, Optional[object]]:
    """Parse option symbol: SPY260123P665000 -> underlying=SPY, expiry=2026-01-23, type=P, strike=665.0"""
    code = (code or "").strip().upper().replace(" ", "")
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d+)$", code)
    if not m:
        return {"underlying": None, "expiry": None, "call_put": None, "strike": None}
    und, yymmdd, cp, strike_digits = m.groups()

    yy = int(yymmdd[0:2])
    year = 2000 + yy
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])

    strike = int(strike_digits) / 1000.0 if strike_digits.isdigit() else None

    return {
        "underlying": und,
        "expiry": pd.Timestamp(year=year, month=month, day=day),
        "call_put": cp,
        "strike": strike,
    }


def extract_account_summary(all_text: str) -> Dict[str, float]:
    """
    Pull out key figures from page text.
    Uses looser regex to handle newlines/spaces.
    """
    out: Dict[str, float] = {}

    m = re.search(r"Net Asset Value[\s:]*([\d,]+\.\d+)", all_text, flags=re.IGNORECASE)
    if m:
        out["nav_end"] = money_to_float(m.group(1))

    m = re.search(r"Portfolio Value[\s:]*([\d,]+\.\d+)", all_text, flags=re.IGNORECASE)
    if m:
        out["portfolio_value"] = money_to_float(m.group(1))

    m = re.search(r"Cash Balance[\s:]*([\d,]+\.\d+)", all_text, flags=re.IGNORECASE)
    if m:
        out["cash_balance"] = money_to_float(m.group(1))

    # Starting NAV (SGD equal)
    m = re.search(
        r"Starting Net Asset Value\s*\d+.*?Equal to\(SGD\)\s*([\d,]+\.\d+)",
        all_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        out["nav_start"] = money_to_float(m.group(1))

    # Ending NAV (SGD equal)
    m = re.search(
        r"Ending Net Asset Value\s*\d+.*?Equal to\(SGD\)\s*([\d,]+\.\d+)",
        all_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        out["nav_end_sgd_equal"] = money_to_float(m.group(1))

    return out


# ============================================================
# Trade parsing (text-line scanner)
# ============================================================
@dataclass
class ParsedTrade:
    direction: str
    symbol_code: str
    exchange: str
    currency: str
    datetime: pd.Timestamp
    price: float
    quantity: float
    amount: float
    fees_alloc: float
    group_id: int


def _extract_datetime_from_block(block_text: str) -> Optional[pd.Timestamp]:
    """
    Fix: moomoo PDF often has DATE and TIME on different lines.
    Our block_text joins lines with ' | ', so we support:
      - 'YYYY/MM/DD HH:MM:SS'
      - 'YYYY/MM/DD | HH:MM:SS'
      - date anywhere + time anywhere
    """
    # 1) same line
    m = re.search(r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})", block_text)
    if m:
        return pd.to_datetime(m.group(1) + " " + m.group(2), format="%Y/%m/%d %H:%M:%S", errors="coerce")

    # 2) date | time
    m = re.search(r"(\d{4}/\d{2}/\d{2})\s*\|\s*(\d{2}:\d{2}:\d{2})", block_text)
    if m:
        return pd.to_datetime(m.group(1) + " " + m.group(2), format="%Y/%m/%d %H:%M:%S", errors="coerce")

    # 3) date anywhere + time anywhere
    d = re.search(r"(\d{4}/\d{2}/\d{2})", block_text)
    t = re.search(r"(\d{2}:\d{2}:\d{2})", block_text)
    if d and t:
        return pd.to_datetime(d.group(1) + " " + t.group(1), format="%Y/%m/%d %H:%M:%S", errors="coerce")

    return None


def parse_trades_from_text_lines(lines: List[str]) -> Tuple[List[ParsedTrade], List[Dict[str, float]]]:
    trades: List[ParsedTrade] = []
    fee_groups: List[Dict[str, float]] = []

    dir_pat = re.compile(r"^(Buy|Sell)\s+to\s+(Open|Close)$", re.IGNORECASE)
    subtotal_pat = re.compile(r"^Subtotal:\s*([\d,]+\.\d+)", re.IGNORECASE)

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

    current_group_id = 0
    current_group_trade_idx: List[int] = []

    i = 0
    while i < len(lines):
        line = (lines[i] or "").strip()

        # Fee subtotal block ends a group
        sm = subtotal_pat.match(line)
        if sm:
            subtotal_fee = money_to_float(sm.group(1))

            lookahead = " ".join(lines[i : min(i + 7, len(lines))])
            component_sum = 0.0
            for f in fee_fields:
                fm = re.search(rf"{re.escape(f)}:\s*([\d,]+\.\d+)", lookahead, flags=re.IGNORECASE)
                if fm:
                    component_sum += money_to_float(fm.group(1))

            fee_total = component_sum if component_sum > 0 else subtotal_fee
            fee_groups.append({"group_id": current_group_id, "fee_total": fee_total, "n_trades": len(current_group_trade_idx)})

            if current_group_trade_idx:
                per_trade_fee = fee_total / len(current_group_trade_idx)
                for idx in current_group_trade_idx:
                    trades[idx].fees_alloc = per_trade_fee

            current_group_id += 1
            current_group_trade_idx = []
            i += 1
            continue

        # Trade direction start
        dm = dir_pat.match(line)
        if dm:
            direction = f"{dm.group(1).title()} to {dm.group(2).title()}"

            # Build a block (next lines belong to this trade)
            block = [line]
            for j in range(1, 12):
                if i + j >= len(lines):
                    break
                nxt = (lines[i + j] or "").strip()
                if dir_pat.match(nxt) or subtotal_pat.match(nxt) or nxt.startswith("Direction "):
                    break
                block.append(nxt)

            block_text = " | ".join(block)

            dt = _extract_datetime_from_block(block_text)
            if dt is None or pd.isna(dt):
                i += len(block)
                continue

            # Currency
            cur = ""
            curm = re.search(r"\b(USD|SGD|HKD|CNH|JPY)\b", block_text)
            if curm:
                cur = curm.group(1)

            # Exchange (often "US")
            exch = ""
            exchm = re.search(r"\b(US|SG|HK)\b", block_text)
            if exchm:
                exch = exchm.group(1)

            # Prefer option code; else ticker
            # Note: we remove spaces for option code matching (PDF sometimes inserts spaces)
            opt_codes = re.findall(r"\b[A-Z]+(?:\d{6})[CP]\d+\b", block_text.replace(" ", ""))
            symbol_code = opt_codes[0] if opt_codes else ""

            if not symbol_code:
                # fallback ticker (first ALLCAPS token length 1-6)
                tm = re.search(r"\b([A-Z]{1,6})\b", block_text)
                symbol_code = tm.group(1) if tm else ""

            # Extract last numeric triplet: price qty amount
            # Example: "0.2000 9 180.00"
            triplets = re.findall(r"(-?\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+([\d,]+\.\d+)", block_text)
            if not triplets:
                i += len(block)
                continue
            p, q, a = triplets[-1]
            price = float(p)
            qty = float(q)
            amt = money_to_float(a)

            if not symbol_code:
                i += len(block)
                continue

            t = ParsedTrade(
                direction=direction,
                symbol_code=symbol_code,
                exchange=exch,
                currency=cur,
                datetime=dt,
                price=price,
                quantity=qty,
                amount=amt,
                fees_alloc=0.0,
                group_id=current_group_id,
            )
            trades.append(t)
            current_group_trade_idx.append(len(trades) - 1)

            i += len(block)
            continue

        i += 1

    return trades, fee_groups


@st.cache_data(show_spinner=False)
def parse_moomoo_monthly_pdf(uploaded_bytes: bytes) -> Dict[str, pd.DataFrame]:
    all_text_pages: List[str] = []
    all_lines: List[str] = []

    with pdfplumber.open(io.BytesIO(uploaded_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            all_text_pages.append(txt)
            all_lines.extend(txt.splitlines())

    all_text = "\n".join(all_text_pages)

    acct = extract_account_summary(all_text)
    account_summary_df = pd.DataFrame([acct]) if acct else pd.DataFrame([{}])

    parsed_trades, fee_groups = parse_trades_from_text_lines(all_lines)

    # Normalize trades
    rows = []
    for t in parsed_trades:
        side = direction_to_side(t.direction)

        gross = float(t.amount)
        gross = -abs(gross) if side == "BUY" else abs(gross) if side == "SELL" else gross

        if is_options_symbol(t.symbol_code):
            opt = parse_option_code(t.symbol_code)
            underlying = opt.get("underlying")
            expiry = opt.get("expiry")
            call_put = opt.get("call_put")
            strike = opt.get("strike")
            instrument_type = "option"
        else:
            underlying = t.symbol_code
            expiry = None
            call_put = None
            strike = None
            instrument_type = "equity_or_fund"

        fees = float(t.fees_alloc or 0.0)
        net = gross - fees

        rows.append({
            "datetime": t.datetime,
            "direction": t.direction,
            "side": side,
            "symbol_code": t.symbol_code,
            "underlying": underlying,
            "call_put": call_put,
            "expiry": expiry,
            "strike": strike,
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

    if not trades_df.empty:
        trades_df["datetime"] = pd.to_datetime(trades_df["datetime"], errors="coerce")
        trades_df = trades_df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        trades_df["expiry"] = pd.to_datetime(trades_df["expiry"], errors="coerce")

    # Include fee group info in a tiny df for debug
    fee_groups_df = pd.DataFrame(fee_groups)

    return {
        "account_summary": account_summary_df,
        "trades": trades_df,
        "fee_groups": fee_groups_df,
    }


# ============================================================
# UI - Upload
# ============================================================
st.sidebar.header("Upload")
uploaded = st.sidebar.file_uploader("Upload moomoo Monthly Statement PDF", type=["pdf"])

if uploaded is None:
    st.markdown(
        """
        <div class="card">
          <h3>Upload your PDF</h3>
          <div class="sub">Use the sidebar to upload your moomoo monthly statement PDF.</div>
          <div class="sub">After upload, the dashboard updates instantly.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()

data = parse_moomoo_monthly_pdf(uploaded.getvalue())
account_summary = data["account_summary"]
trades = data["trades"]
fee_groups = data["fee_groups"]

# quick visible sanity check
st.sidebar.write("Parsed trades:", int(len(trades)))
st.sidebar.write("Parsed option trades:", int((trades["instrument_type"] == "option").sum()) if not trades.empty else 0)

# ============================================================
# Compute KPIs
# ============================================================
options = trades[trades["instrument_type"] == "option"].copy()

# Harden datetime before .dt usage (prevents .dt crash)
if not options.empty:
    options["datetime"] = pd.to_datetime(options["datetime"], errors="coerce")
    options = options.dropna(subset=["datetime"]).copy()

if options.empty:
    latest_month = None
    this_month_net = 0.0
    week_net = 0.0
    ytd_net = 0.0
else:
    options["date"] = options["datetime"].dt.date
    options["week"] = options["datetime"].dt.to_period("W").astype(str)
    options["month"] = options["datetime"].dt.to_period("M").astype(str)
    options["year"] = options["datetime"].dt.year

    latest_month = options["month"].max()
    this_month_net = float(options.loc[options["month"] == latest_month, "net_amount"].sum())

    now = pd.Timestamp.now()
    week_cutoff = now - pd.Timedelta(days=7)
    week_net = float(options.loc[options["datetime"] >= week_cutoff, "net_amount"].sum())

    ytd_cutoff = pd.Timestamp(year=now.year, month=1, day=1)
    ytd_net = float(options.loc[options["datetime"] >= ytd_cutoff, "net_amount"].sum())

# Account summary metrics
def _get_float(df: pd.DataFrame, key: str) -> float:
    if df.empty:
        return np.nan
    v = df.iloc[0].get(key, np.nan)
    try:
        return float(v)
    except Exception:
        return np.nan

nav_end = _get_float(account_summary, "nav_end")
portfolio_value = _get_float(account_summary, "portfolio_value")
cash_balance = _get_float(account_summary, "cash_balance")
nav_start = _get_float(account_summary, "nav_start")

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
        unsafe_allow_html=True,
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
        unsafe_allow_html=True,
    )

with c3:
    year_label = str(pd.Timestamp.now().year) + "-01-01"
    st.markdown(
        f"""
        <div class="card">
          <h3>Options Net (YTD)</h3>
          <div class="big">${ytd_net:,.2f}</div>
          <div class="sub">From {year_label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with c4:
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
          <div class="small">Tip: if trades still show 0, expand Debug and check parsed lines.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

left, mid, right = st.columns([1.2, 1.0, 1.0], gap="large")

# ==========================
# Table: Options trades
# ==========================
with left:
    st.markdown("<div class='card'><h3>Parsed Options Trades</h3></div>", unsafe_allow_html=True)
    if options.empty:
        st.info("No option trades detected in this PDF (yet). See Debug section.")
    else:
        show_cols = [
            "datetime", "direction", "symbol_code", "underlying", "call_put",
            "expiry", "strike", "qty", "price", "gross_amount", "fees", "net_amount"
        ]
        show_df = options[show_cols].copy()
        show_df["datetime"] = pd.to_datetime(show_df["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
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

# ============================================================
# Debug / diagnostics
# ============================================================
with st.expander("Debug: Fee allocation groups + raw stats"):
    st.write("Total parsed trades:", int(len(trades)))
    if not trades.empty:
        st.write("Instrument types:", trades["instrument_type"].value_counts(dropna=False))
        st.write("Directions seen:", sorted(trades["direction"].dropna().unique().tolist()))
        st.write("Example rows (first 10):")
        st.dataframe(trades.head(10), use_container_width=True)

    st.write("Fee allocation groups (from Subtotal blocks):")
    st.dataframe(fee_groups, use_container_width=True)

    st.write("If you still see 0 option trades:")
    st.write("- Your PDF text extraction might differ (some PDFs are image-based).")
    st.write("- Try downloading the statement again (some exports are cleaner).")
    st.write("- If you want, upload another month and we can adjust patterns for consistency.")
