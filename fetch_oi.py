"""
Options OI + Vega Dashboard - GitHub Actions Script
Runs every 5 minutes via GitHub Actions
Fetches CE/PE OI, IV, Vega from Dhan API → pushes to Google Sheets
"""

import os
import json
import math
import requests
import gspread
from datetime import datetime, date
from google.oauth2.service_account import Credentials

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
    """Asks Dhan for the live list of valid expiries and picks the nearest one,
    so we never have to hardcode/update a date manually again."""
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
    nearest = expiries[0]   # Dhan returns them sorted, nearest first
    print(f"   📅 Using nearest expiry: {nearest}")
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
    now = datetime.now().strftime("%d-%b-%Y %H:%M")

    total_ce_oi = sum(r["ce_oi"] for r in rows)
    total_pe_oi = sum(r["pe_oi"] for r in rows)
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0
    signal = "🟢 BULLISH" if pcr > 1.2 else ("🔴 BEARISH" if pcr < 0.8 else "🟡 NEUTRAL")

    header_block = [
        [f"NIFTY Options OI Dashboard — {now}", "", "", "", "", "", "", ""],
        [f"Spot: {spot:.0f}", f"ATM: {atm}", f"Expiry: {expiry}", "", f"OI PCR: {pcr}", f"Signal: {signal}", "", ""],
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
    return pcr, signal

def write_vega_table(sheet, rows, spot, atm):
    ws = get_or_create_tab(sheet, "Vega Table")
    now = datetime.now().strftime("%d-%b %H:%M")

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
    now = datetime.now().strftime("%d-%b %H:%M")

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
    print(f"🚀 Starting OI fetch — {datetime.now().strftime('%H:%M:%S')}")

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
