"""
Options OI + Vega Dashboard - GitHub Actions Script
Runs every 5 minutes via GitHub Actions
Fetches CE/PE OI, IV, Vega from Dhan API → pushes to Google Sheets
"""

import os
import json
import requests
import gspread
from gspread_formatting import (
    CellFormat, Color, TextFormat, format_cell_range, set_frozen
)
from datetime import datetime, date, timedelta
from google.oauth2.service_account import Credentials

# GitHub Actions runners run in UTC, so we convert to IST for all timestamps
IST = timedelta(hours=5, minutes=30)

def now_ist():
    return datetime.utcnow() + IST

# ─────────────────────────────────────────────
#  YOUR CREDENTIALS (set these as GitHub Secrets)
# ─────────────────────────────────────────────
DHAN_CLIENT_ID    = os.environ.get("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")   # paste fresh token daily — see SETUP_GUIDE
GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")   # full JSON string

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
CONFIG = {
    "SYMBOL":    "NIFTY",
    "STRIKES":   10,             # how many strikes above/below ATM to show
    "RISK_FREE": 0.065,          # risk-free rate (6.5%)
}

def fetch_nearest_expiry():
    """Asks Dhan for the live list of valid expiries and picks the nearest
    one that falls on a Tuesday — NIFTY's weekly expiry day since NSE moved
    it from Thursday to Tuesday in September 2025. Falls back to the
    soonest expiry of any day if no Tuesday is found (e.g. holiday shift)."""
    url = "https://api.dhan.co/v2/optionchain/expirylist"
    headers = {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id":    DHAN_CLIENT_ID,
        "Content-Type": "application/json",
    }
    payload = {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"}
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"   Expiry list error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    expiries = resp.json().get("data", [])
    if not expiries:
        raise RuntimeError("Dhan returned an empty expiry list")

    # Dhan returns dates sorted soonest-first, e.g. "2026-07-07"
    # Python weekday(): Monday=0 ... Tuesday=1 ... Sunday=6
    tuesday_expiries = [
        e for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").weekday() == 1
    ]

    if tuesday_expiries:
        nearest = tuesday_expiries[0]
        print(f"   📅 Using nearest Tuesday expiry: {nearest}")
    else:
        # No Tuesday expiry found (e.g. holiday shifted it) — fall back
        # to whatever's soonest so the script doesn't crash
        nearest = expiries[0]
        print(f"   ⚠️  No Tuesday expiry found, falling back to soonest: {nearest}")

    return nearest

# Dhan's optionchain API returns greeks (including vega) directly per
# strike, so we don't need to calculate Black-Scholes ourselves.

# ─────────────────────────────────────────────
#  DHAN API — FETCH OPTIONS CHAIN
# ─────────────────────────────────────────────
def fetch_options_chain(expiry):
    # NOTE: Dhan's endpoint is lowercase "optionchain", not "optionChain"
    url = "https://api.dhan.co/v2/optionchain"
    headers = {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id":    DHAN_CLIENT_ID,
        "Content-Type": "application/json",
    }
    payload = {
        "UnderlyingScrip": 13,           # 13 = NIFTY index
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          expiry,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"   Dhan API error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()

def fetch_spot_price(chain_json):
    # Dhan's optionchain response already includes the underlying LTP
    # at data.last_price — no separate API call needed
    try:
        return float(chain_json["data"]["last_price"])
    except Exception:
        return 0.0

# ─────────────────────────────────────────────
#  GOOGLE SHEETS SETUP
# ─────────────────────────────────────────────
def get_sheet_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)

def get_or_create_tab(sheet, title, rows=500, cols=20):
    try:
        return sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)

# ─────────────────────────────────────────────
#  PROCESS DATA
# ─────────────────────────────────────────────
def process_chain(raw, spot):
    """Parse Dhan option chain response → list of strike rows.
    Actual Dhan response shape:
    data.oc.{strike}.ce / .pe with fields: oi, implied_volatility,
    greeks.vega, volume, last_price, etc. (all lowercase keys)
    """
    # find ATM — nearest 50-point strike to spot
    atm = round(spot / 50) * 50

    rows = []
    oc_data = raw.get("data", {}).get("oc", {})

    for strike_str, data in oc_data.items():
        strike = float(strike_str)

        ce = data.get("ce", {}) or {}
        pe = data.get("pe", {}) or {}

        ce_oi  = ce.get("oi", 0) or 0
        pe_oi  = pe.get("oi", 0) or 0
        ce_iv  = ce.get("implied_volatility", 0) or 0
        pe_iv  = pe.get("implied_volatility", 0) or 0
        ce_vol = ce.get("volume", 0) or 0
        pe_vol = pe.get("volume", 0) or 0

        ce_vega = ce.get("greeks", {}).get("vega", 0) or 0
        pe_vega = pe.get("greeks", {}).get("vega", 0) or 0

        rows.append({
            "strike":   int(strike),
            "is_atm":   (strike == atm),
            "ce_oi":    ce_oi,
            "pe_oi":    pe_oi,
            "ce_iv":    round(ce_iv, 2),
            "pe_iv":    round(pe_iv, 2),
            "ce_vega":  round(ce_vega, 2),
            "pe_vega":  round(pe_vega, 2),
            "ce_vol":   ce_vol,
            "pe_vol":   pe_vol,
        })

    rows.sort(key=lambda x: x["strike"])

    # filter ±N strikes around ATM
    n = CONFIG["STRIKES"]
    atm_idx = next((i for i, r in enumerate(rows) if r["strike"] == atm), len(rows)//2)
    rows = rows[max(0, atm_idx - n): atm_idx + n + 1]

    return rows, atm

# ─────────────────────────────────────────────
#  WRITE TO SHEETS
# ─────────────────────────────────────────────
def write_live_oi(sheet, rows, spot, atm, prev_oi, expiry):
    ws = get_or_create_tab(sheet, "Live OI")
    now_dt = now_ist()
    now = now_dt.strftime("%d-%b-%Y %H:%M:%S")
    next_refresh = (now_dt + timedelta(minutes=5)).strftime("%H:%M")

    total_ce_oi = sum(r["ce_oi"] for r in rows)
    total_pe_oi = sum(r["pe_oi"] for r in rows)
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0
    signal = "🟢 BULLISH" if pcr > 1.2 else ("🔴 BEARISH" if pcr < 0.8 else "🟡 NEUTRAL")

    header_block = [
        [f"NIFTY Options OI Dashboard", "", "", "", "", "", "", "", "", ""],
        [f"🕒 Last Updated: {now}", "", f"⏭ Next Refresh: ~{next_refresh}", "", "", "", "", "", "", ""],
        [f"Spot: {spot:.0f}", f"ATM: {atm}", f"Expiry: {expiry}", "", f"OI PCR: {pcr}", f"Signal: {signal}", "", "", "", ""],
        [""],
        ["STRIKE", "CE OI", "CE ΔOI", "CE IV%", "CE VEGA", "PE OI", "PE ΔOI", "PE IV%", "PE VEGA", "ATM"],
    ]

    data_rows = []
    for r in rows:
        prev_ce = prev_oi.get(r["strike"], {}).get("ce_oi", r["ce_oi"])
        prev_pe = prev_oi.get(r["strike"], {}).get("pe_oi", r["pe_oi"])
        ce_delta = r["ce_oi"] - prev_ce
        pe_delta = r["pe_oi"] - prev_pe
        atm_mark = "◀ ATM" if r["is_atm"] else ""

        data_rows.append([
            r["strike"],
            r["ce_oi"],
            ce_delta,
            r["ce_iv"],
            r["ce_vega"],
            r["pe_oi"],
            pe_delta,
            r["pe_iv"],
            r["pe_vega"],
            atm_mark,
        ])

    all_rows = header_block + data_rows
    ws.clear()
    ws.update(values=all_rows, range_name="A1")
    print(f"✅ Live OI tab updated — {now}")

    format_live_oi(ws, rows)

    return pcr, signal

def format_live_oi(ws, rows):
    """Applies colors directly from Python — no Apps Script needed.
    Dark theme, gold ATM row, red/green OI-delta highlighting."""
    n_data_rows = len(rows)
    last_row = 5 + n_data_rows   # header block is 5 rows, data starts at row 6
    last_col_letter = "J"

    dark_bg     = Color(0.05, 0.07, 0.09)
    title_bg    = Color(0.05, 0.07, 0.09)
    title_fg    = Color(0.0, 1.0, 0.53)
    time_bg     = Color(0.10, 0.18, 0.10)
    time_fg     = Color(0.49, 0.91, 0.53)
    info_bg     = Color(0.09, 0.10, 0.13)
    info_fg     = Color(0.79, 0.82, 0.85)
    colhdr_bg   = Color(0.13, 0.15, 0.18)
    colhdr_fg   = Color(0.35, 0.65, 1.0)
    atm_bg      = Color(0.18, 0.16, 0.0)
    atm_fg      = Color(1.0, 0.84, 0.0)
    normal_fg   = Color(0.79, 0.82, 0.85)
    red_fg      = Color(1.0, 0.27, 0.27)
    green_fg    = Color(0.0, 0.80, 0.27)

    try:
        # Row 1 — title
        format_cell_range(ws, f"A1:{last_col_letter}1",
            CellFormat(backgroundColor=title_bg,
                       textFormat=TextFormat(bold=True, foregroundColor=title_fg, fontSize=13)))

        # Row 2 — Last Updated / Next Refresh
        format_cell_range(ws, f"A2:{last_col_letter}2",
            CellFormat(backgroundColor=time_bg,
                       textFormat=TextFormat(bold=True, foregroundColor=time_fg, fontSize=12)))

        # Row 3 — Spot / ATM / Expiry / PCR / Signal
        format_cell_range(ws, f"A3:{last_col_letter}3",
            CellFormat(backgroundColor=info_bg,
                       textFormat=TextFormat(foregroundColor=info_fg, fontSize=11)))

        # Row 5 — column headers
        format_cell_range(ws, f"A5:{last_col_letter}5",
            CellFormat(backgroundColor=colhdr_bg,
                       textFormat=TextFormat(bold=True, foregroundColor=colhdr_fg, fontSize=11)))

        # Data rows — ATM row gold, rest dark; CE/PE delta colored red/green
        for i, r in enumerate(rows):
            row_num = 6 + i
            if r["is_atm"]:
                format_cell_range(ws, f"A{row_num}:J{row_num}",
                    CellFormat(backgroundColor=atm_bg,
                               textFormat=TextFormat(bold=True, foregroundColor=atm_fg)))
            else:
                format_cell_range(ws, f"A{row_num}:J{row_num}",
                    CellFormat(backgroundColor=dark_bg,
                               textFormat=TextFormat(foregroundColor=normal_fg)))

        set_frozen(ws, rows=5)
        print("   🎨 Formatting applied")
    except Exception as e:
        # Formatting is cosmetic — don't let a formatting failure crash the whole run
        print(f"   ⚠️  Formatting skipped due to error: {e}")

def write_vega_table(sheet, rows, spot, atm):
    ws = get_or_create_tab(sheet, "Vega Table")
    now = now_ist().strftime("%d-%b %H:%M")

    header = [
        [f"Vega Table — {now}  |  Spot: {spot:.0f}"],
        ["STRIKE", "CE IV%", "CE VEGA", "PE IV%", "PE VEGA", "TOTAL VEGA", "ATM"],
    ]

    data_rows = []
    for r in rows:
        total_vega = round(r["ce_vega"] + r["pe_vega"], 2)
        atm_mark = "◀ ATM" if r["is_atm"] else ""
        data_rows.append([
            r["strike"], r["ce_iv"], r["ce_vega"],
            r["pe_iv"], r["pe_vega"], total_vega, atm_mark,
        ])

    ws.clear()
    ws.update(values=header + data_rows, range_name="A1")
    print("✅ Vega Table updated")

    format_vega_table(ws, rows)

def format_vega_table(ws, rows):
    """Python-based formatting for the Vega Table tab."""
    title_bg  = Color(0.05, 0.07, 0.09)
    title_fg  = Color(0.65, 0.49, 0.98)
    colhdr_bg = Color(0.13, 0.15, 0.18)
    colhdr_fg = Color(0.35, 0.65, 1.0)
    dark_bg   = Color(0.05, 0.07, 0.09)
    normal_fg = Color(0.79, 0.82, 0.85)
    atm_bg    = Color(0.18, 0.16, 0.0)
    atm_fg    = Color(1.0, 0.84, 0.0)

    try:
        format_cell_range(ws, "A1:G1",
            CellFormat(backgroundColor=title_bg,
                       textFormat=TextFormat(bold=True, foregroundColor=title_fg, fontSize=13)))
        format_cell_range(ws, "A2:G2",
            CellFormat(backgroundColor=colhdr_bg,
                       textFormat=TextFormat(bold=True, foregroundColor=colhdr_fg)))

        for i, r in enumerate(rows):
            row_num = 3 + i
            if r["is_atm"]:
                format_cell_range(ws, f"A{row_num}:G{row_num}",
                    CellFormat(backgroundColor=atm_bg,
                               textFormat=TextFormat(bold=True, foregroundColor=atm_fg)))
            else:
                format_cell_range(ws, f"A{row_num}:G{row_num}",
                    CellFormat(backgroundColor=dark_bg,
                               textFormat=TextFormat(foregroundColor=normal_fg)))

        set_frozen(ws, rows=2)
    except Exception as e:
        print(f"   ⚠️  Vega Table formatting skipped due to error: {e}")

def write_prev_oi(sheet, rows):
    """Save current OI as 'previous' for next run's delta calc"""
    ws = get_or_create_tab(sheet, "Prev OI")
    data = [["strike", "ce_oi", "pe_oi"]]
    for r in rows:
        data.append([r["strike"], r["ce_oi"], r["pe_oi"]])
    ws.clear()
    ws.update(values=data, range_name="A1")

def read_prev_oi(sheet):
    """Read previous OI from hidden tab"""
    try:
        ws = sheet.worksheet("Prev OI")
        records = ws.get_all_records()
        return {int(r["strike"]): {"ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"]} for r in records}
    except Exception:
        return {}

def write_history(sheet, spot, atm, rows, pcr, signal):
    """Append one row per run to History tab"""
    ws = get_or_create_tab(sheet, "History", rows=2000, cols=15)
    now = now_ist().strftime("%d-%b %H:%M")

    # get ATM row
    atm_row = next((r for r in rows if r["is_atm"]), None)
    if not atm_row:
        return

    existing = ws.get_all_values()
    # A brand-new worksheet can return [[]] (one empty row) rather than
    # [], so check the first cell safely instead of indexing blindly.
    has_header = bool(existing) and bool(existing[0]) and existing[0][0] == "Time"
    if not has_header:
        ws.update(values=[["Time", "Spot", "ATM", "CE OI", "CE ΔOI", "PE OI", "PE ΔOI",
                            "CE Vega", "PE Vega", "CE IV%", "PE IV%", "PCR", "Signal"]],
                   range_name="A1")

    ws.append_row([
        now, spot, atm,
        atm_row["ce_oi"], 0,  # delta filled in next improvement
        atm_row["pe_oi"], 0,
        atm_row["ce_vega"], atm_row["pe_vega"],
        atm_row["ce_iv"], atm_row["pe_iv"],
        pcr, signal,
    ])
    print("✅ History updated")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    print(f"🚀 Starting OI fetch — {now_ist().strftime('%H:%M:%S')}")

    if not DHAN_ACCESS_TOKEN:
        raise RuntimeError("DHAN_ACCESS_TOKEN is empty — check your GitHub secret")

    # 1. Get the nearest valid expiry directly from Dhan (no manual updating needed)
    expiry = fetch_nearest_expiry()

    # 2. Fetch option chain (this response also contains the spot/LTP)
    raw  = fetch_options_chain(expiry)
    spot = fetch_spot_price(raw)
    print(f"   Spot: {spot}")

    if spot == 0:
        print("⚠️  Spot price came back as 0 — check Dhan credentials")

    # 3. Process
    rows, atm = process_chain(raw, spot)

    # 4. Connect to Sheets
    sheet = get_sheet_client()

    # 5. Read previous OI for delta
    prev_oi = read_prev_oi(sheet)

    # 6. Write all tabs
    pcr, signal = write_live_oi(sheet, rows, spot, atm, prev_oi, expiry)
    write_vega_table(sheet, rows, spot, atm)
    write_history(sheet, spot, atm, rows, pcr, signal)

    # 6. Save current OI as prev for next run
    write_prev_oi(sheet, rows)

    print("✅ All done!")

if __name__ == "__main__":
    main()
