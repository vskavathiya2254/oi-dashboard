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
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")   # full JSON string

# ─────────────────────────────────────────────
#  CONFIG — update EXPIRY every week (nearest Thursday)
# ─────────────────────────────────────────────
CONFIG = {
    "SYMBOL":    "NIFTY",
    "EXPIRY":    "2025-07-03",   # ← change this every week to nearest Thursday
    "STRIKES":   10,             # how many strikes above/below ATM to show
    "RISK_FREE": 0.065,          # risk-free rate (6.5%)
}

# ─────────────────────────────────────────────
#  BLACK-SCHOLES VEGA CALCULATOR
# ─────────────────────────────────────────────
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def calc_vega(S, K, T, r, sigma):
    """Returns vega per 1% IV move"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        vega = S * norm_pdf(d1) * math.sqrt(T)
        return round(vega * 0.01, 2)   # vega per 1% move in IV
    except Exception:
        return 0.0

def time_to_expiry(expiry_str):
    """Returns T in years"""
    expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    today  = date.today()
    days   = (expiry - today).days
    return max(days, 0) / 365.0

# ─────────────────────────────────────────────
#  DHAN API — FETCH OPTIONS CHAIN
# ─────────────────────────────────────────────
def fetch_options_chain():
    url = "https://api.dhan.co/v2/optionchain"

    headers = {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_CLIENT_ID,
        "Content-Type": "application/json",
    }

    payload = {
        "UnderlyingScrip": 13,
        "UnderlyingSeg": "IDX_I",
        "Expiry": CONFIG["EXPIRY"],
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=15)

    print("Status Code:", resp.status_code)
    print("Response:", resp.text)

    resp.raise_for_status()

    return resp.json()
def fetch_spot_price():
    url = "https://api.dhan.co/v2/marketfeed/ltp"
    headers = {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id":    DHAN_CLIENT_ID,
        "Content-Type": "application/json",
    }
    payload = {"IDX_I": [13]}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        data = resp.json()
        return float(data["data"]["IDX_I"]["13"]["last_price"])
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
    """Parse Dhan option chain response → list of strike rows"""
    T = time_to_expiry(CONFIG["EXPIRY"])
    r = CONFIG["RISK_FREE"]

    # find ATM
    atm = round(spot / 50) * 50

    rows = []
    oc_data = raw.get("data", {})

    for strike_str, data in oc_data.items():
        strike = float(strike_str)

        ce = data.get("CE", {})
        pe = data.get("PE", {})

        ce_oi  = ce.get("openInterest", 0) or 0
        pe_oi  = pe.get("openInterest", 0) or 0
        ce_iv  = ce.get("impliedVolatility", 0) or 0
        pe_iv  = pe.get("impliedVolatility", 0) or 0
        ce_vol = ce.get("volume", 0) or 0
        pe_vol = pe.get("volume", 0) or 0

        ce_iv_dec = (ce_iv / 100) if ce_iv > 1 else ce_iv
        pe_iv_dec = (pe_iv / 100) if pe_iv > 1 else pe_iv

        ce_vega = calc_vega(spot, strike, T, r, ce_iv_dec)
        pe_vega = calc_vega(spot, strike, T, r, pe_iv_dec)

        rows.append({
            "strike":   int(strike),
            "is_atm":   (strike == atm),
            "ce_oi":    ce_oi,
            "pe_oi":    pe_oi,
            "ce_iv":    round(ce_iv, 2),
            "pe_iv":    round(pe_iv, 2),
            "ce_vega":  ce_vega,
            "pe_vega":  pe_vega,
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
def write_live_oi(sheet, rows, spot, atm, prev_oi):
    ws = get_or_create_tab(sheet, "Live OI")
    now = datetime.now().strftime("%d-%b-%Y %H:%M")

    total_ce_oi = sum(r["ce_oi"] for r in rows)
    total_pe_oi = sum(r["pe_oi"] for r in rows)
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0
    signal = "🟢 BULLISH" if pcr > 1.2 else ("🔴 BEARISH" if pcr < 0.8 else "🟡 NEUTRAL")

    header_block = [
        [f"NIFTY Options OI Dashboard — {now}", "", "", "", "", "", "", ""],
        [f"Spot: {spot:.0f}", f"ATM: {atm}", f"Expiry: {CONFIG['EXPIRY']}", "", f"OI PCR: {pcr}", f"Signal: {signal}", "", ""],
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
    ws.update("A1", all_rows)
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
    ws.update("A1", header + data_rows)
    print("✅ Vega Table updated")

def write_prev_oi(sheet, rows):
    """Save current OI as 'previous' for next run's delta calc"""
    ws = get_or_create_tab(sheet, "Prev OI")
    data = [["strike", "ce_oi", "pe_oi"]]
    for r in rows:
        data.append([r["strike"], r["ce_oi"], r["pe_oi"]])
    ws.clear()
    ws.update("A1", data)

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
    if not existing or existing[0][0] != "Time":
        ws.update("A1", [["Time", "Spot", "ATM", "CE OI", "CE ΔOI", "PE OI", "PE ΔOI",
                           "CE Vega", "PE Vega", "CE IV%", "PE IV%", "PCR", "Signal"]])

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

    # 1. Fetch data
    spot = fetch_spot_price()
    raw  = fetch_options_chain()
    print(f"   Spot: {spot}")

    # 2. Process
    rows, atm = process_chain(raw, spot)

    # 3. Connect to Sheets
    sheet = get_sheet_client()

    # 4. Read previous OI for delta
    prev_oi = read_prev_oi(sheet)

    # 5. Write all tabs
    pcr, signal = write_live_oi(sheet, rows, spot, atm, prev_oi)
    write_vega_table(sheet, rows, spot, atm)
    write_history(sheet, spot, atm, rows, pcr, signal)

    # 6. Save current OI as prev for next run
    write_prev_oi(sheet, rows)

    print("✅ All done!")

if __name__ == "__main__":
    main()
