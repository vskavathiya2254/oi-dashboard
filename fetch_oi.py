"""
Options OI + Vega Dashboard - GitHub Actions Script
Runs every 5 minutes via GitHub Actions
Fetches CE/PE OI, IV, Vega from Dhan API → pushes to Google Sheets

MANUAL TOKEN VERSION — paste a fresh DHAN_ACCESS_TOKEN secret in
GitHub once a day before market open (Dhan tokens expire every 24h).
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

IST = timedelta(hours=5, minutes=30)

def now_ist():
    return datetime.utcnow() + IST

# ─────────────────────────────────────────────
#  CREDENTIALS — set these as GitHub Secrets
# ─────────────────────────────────────────────
DHAN_CLIENT_ID    = os.environ.get("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")

CONFIG = {
    "SYMBOL":  "NIFTY",
    "STRIKES": 10,
}

# ─────────────────────────────────────────────
#  DHAN API
# ─────────────────────────────────────────────
def fetch_nearest_expiry():
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

    tuesday_expiries = [
        e for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").weekday() == 1
    ]
    if tuesday_expiries:
        nearest = tuesday_expiries[0]
        print(f"   📅 Using nearest Tuesday expiry: {nearest}")
    else:
        nearest = expiries[0]
        print(f"   ⚠️  No Tuesday expiry found, falling back to: {nearest}")
    return nearest

def fetch_options_chain(expiry):
    url = "https://api.dhan.co/v2/optionchain"
    headers = {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id":    DHAN_CLIENT_ID,
        "Content-Type": "application/json",
    }
    payload = {
        "UnderlyingScrip": 13,
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          expiry,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"   Dhan API error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()

def fetch_spot_price(chain_json):
    try:
        return float(chain_json["data"]["last_price"])
    except Exception:
        return 0.0

# ─────────────────────────────────────────────
#  GOOGLE SHEETS
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
    atm = round(spot / 50) * 50
    rows = []
    oc_data = raw.get("data", {}).get("oc", {})

    for strike_str, data in oc_data.items():
        strike = float(strike_str)
        ce = data.get("ce", {}) or {}
        pe = data.get("pe", {}) or {}

        rows.append({
            "strike":  int(strike),
            "is_atm":  (strike == atm),
            "ce_oi":   ce.get("oi", 0) or 0,
            "pe_oi":   pe.get("oi", 0) or 0,
            "ce_iv":   round(ce.get("implied_volatility", 0) or 0, 2),
            "pe_iv":   round(pe.get("implied_volatility", 0) or 0, 2),
            "ce_vega": round(ce.get("greeks", {}).get("vega", 0) or 0, 2),
            "pe_vega": round(pe.get("greeks", {}).get("vega", 0) or 0, 2),
        })

    rows.sort(key=lambda x: x["strike"])
    n = CONFIG["STRIKES"]
    atm_idx = next((i for i, r in enumerate(rows) if r["strike"] == atm), len(rows)//2)
    rows = rows[max(0, atm_idx - n): atm_idx + n + 1]
    return rows, atm

# ─────────────────────────────────────────────
#  WRITE TABS
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
        ["NIFTY Options OI Dashboard", "", "", "", "", "", "", "", "", ""],
        [f"🕒 Last Updated: {now}", "", f"⏭ Next Refresh: ~{next_refresh}", "", "", "", "", "", "", ""],
        [f"Spot: {spot:.0f}", f"ATM: {atm}", f"Expiry: {expiry}", "", f"OI PCR: {pcr}", f"Signal: {signal}", "", "", "", ""],
        [""],
        ["STRIKE", "CE OI", "CE ΔOI", "CE IV%", "CE VEGA", "PE OI", "PE ΔOI", "PE IV%", "PE VEGA", "ATM"],
    ]

    data_rows = []
    for r in rows:
        prev_ce = prev_oi.get(r["strike"], {}).get("ce_oi", r["ce_oi"])
        prev_pe = prev_oi.get(r["strike"], {}).get("pe_oi", r["pe_oi"])
        data_rows.append([
            r["strike"], r["ce_oi"], r["ce_oi"] - prev_ce,
            r["ce_iv"], r["ce_vega"],
            r["pe_oi"], r["pe_oi"] - prev_pe,
            r["pe_iv"], r["pe_vega"],
            "◀ ATM" if r["is_atm"] else "",
        ])

    ws.clear()
    ws.update(values=header_block + data_rows, range_name="A1")
    print(f"✅ Live OI updated — {now}")
    format_live_oi(ws, rows)
    return pcr, signal

def format_live_oi(ws, rows):
    try:
        dark   = Color(0.05, 0.07, 0.09)
        green  = Color(0.0,  1.0,  0.53)
        tgreen = Color(0.10, 0.18, 0.10)
        lgreen = Color(0.49, 0.91, 0.53)
        dgray  = Color(0.09, 0.10, 0.13)
        lgray  = Color(0.79, 0.82, 0.85)
        hdr    = Color(0.13, 0.15, 0.18)
        blue   = Color(0.35, 0.65, 1.0)
        atmbg  = Color(0.18, 0.16, 0.0)
        atmfg  = Color(1.0,  0.84, 0.0)

        format_cell_range(ws, "A1:J1", CellFormat(backgroundColor=dark,   textFormat=TextFormat(bold=True, foregroundColor=green,  fontSize=13)))
        format_cell_range(ws, "A2:J2", CellFormat(backgroundColor=tgreen, textFormat=TextFormat(bold=True, foregroundColor=lgreen, fontSize=12)))
        format_cell_range(ws, "A3:J3", CellFormat(backgroundColor=dgray,  textFormat=TextFormat(foregroundColor=lgray, fontSize=11)))
        format_cell_range(ws, "A5:J5", CellFormat(backgroundColor=hdr,    textFormat=TextFormat(bold=True, foregroundColor=blue,   fontSize=11)))

        for i, r in enumerate(rows):
            rn = 6 + i
            if r["is_atm"]:
                format_cell_range(ws, f"A{rn}:J{rn}", CellFormat(backgroundColor=atmbg, textFormat=TextFormat(bold=True, foregroundColor=atmfg)))
            else:
                format_cell_range(ws, f"A{rn}:J{rn}", CellFormat(backgroundColor=dark,  textFormat=TextFormat(foregroundColor=lgray)))

        set_frozen(ws, rows=5)
        print("   🎨 Formatting applied")
    except Exception as e:
        print(f"   ⚠️  Formatting skipped: {e}")

def write_vega_table(sheet, rows, spot, atm):
    ws = get_or_create_tab(sheet, "Vega Table")
    now = now_ist().strftime("%d-%b %H:%M")
    header = [
        [f"Vega Table — {now}  |  Spot: {spot:.0f}"],
        ["STRIKE", "CE IV%", "CE VEGA", "PE IV%", "PE VEGA", "TOTAL VEGA", "ATM"],
    ]
    data_rows = []
    for r in rows:
        data_rows.append([
            r["strike"], r["ce_iv"], r["ce_vega"],
            r["pe_iv"],  r["pe_vega"],
            round(r["ce_vega"] + r["pe_vega"], 2),
            "◀ ATM" if r["is_atm"] else "",
        ])
    ws.clear()
    ws.update(values=header + data_rows, range_name="A1")
    print("✅ Vega Table updated")
    try:
        dark  = Color(0.05, 0.07, 0.09)
        purp  = Color(0.65, 0.49, 0.98)
        hdr   = Color(0.13, 0.15, 0.18)
        blue  = Color(0.35, 0.65, 1.0)
        lgray = Color(0.79, 0.82, 0.85)
        atmbg = Color(0.18, 0.16, 0.0)
        atmfg = Color(1.0,  0.84, 0.0)
        format_cell_range(ws, "A1:G1", CellFormat(backgroundColor=dark, textFormat=TextFormat(bold=True, foregroundColor=purp, fontSize=13)))
        format_cell_range(ws, "A2:G2", CellFormat(backgroundColor=hdr,  textFormat=TextFormat(bold=True, foregroundColor=blue)))
        for i, r in enumerate(rows):
            rn = 3 + i
            if r["is_atm"]:
                format_cell_range(ws, f"A{rn}:G{rn}", CellFormat(backgroundColor=atmbg, textFormat=TextFormat(bold=True, foregroundColor=atmfg)))
            else:
                format_cell_range(ws, f"A{rn}:G{rn}", CellFormat(backgroundColor=dark,  textFormat=TextFormat(foregroundColor=lgray)))
        set_frozen(ws, rows=2)
    except Exception as e:
        print(f"   ⚠️  Vega formatting skipped: {e}")

def read_prev_oi(sheet):
    try:
        ws = sheet.worksheet("Prev OI")
        records = ws.get_all_records()
        return {int(r["strike"]): {"ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"]} for r in records}
    except Exception:
        return {}

def write_prev_oi(sheet, rows):
    ws = get_or_create_tab(sheet, "Prev OI")
    data = [["strike", "ce_oi", "pe_oi"]] + [[r["strike"], r["ce_oi"], r["pe_oi"]] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")

def write_history(sheet, spot, atm, rows, pcr, signal):
    ws = get_or_create_tab(sheet, "History", rows=2000, cols=13)
    now = now_ist().strftime("%d-%b %H:%M")
    atm_row = next((r for r in rows if r["is_atm"]), None)
    if not atm_row:
        return
    existing = ws.get_all_values()
    has_header = bool(existing) and bool(existing[0]) and existing[0][0] == "Time"
    if not has_header:
        ws.update(values=[["Time","Spot","ATM","CE OI","CE ΔOI","PE OI","PE ΔOI",
                            "CE Vega","PE Vega","CE IV%","PE IV%","PCR","Signal"]], range_name="A1")
    ws.append_row([now, spot, atm,
                   atm_row["ce_oi"], 0, atm_row["pe_oi"], 0,
                   atm_row["ce_vega"], atm_row["pe_vega"],
                   atm_row["ce_iv"], atm_row["pe_iv"], pcr, signal])
    print("✅ History updated")

def write_atm_oi_log(sheet, rows, atm, prev_oi):
    ws = get_or_create_tab(sheet, "ATM OI Log", rows=2000, cols=4)
    now_str = now_ist().strftime("%H:%M")
    atm_row = next((r for r in rows if r["is_atm"]), None)
    if not atm_row:
        return
    prev_ce = prev_oi.get(atm_row["strike"], {}).get("ce_oi", atm_row["ce_oi"])
    prev_pe = prev_oi.get(atm_row["strike"], {}).get("pe_oi", atm_row["pe_oi"])
    ce_delta = atm_row["ce_oi"] - prev_ce
    pe_delta = atm_row["pe_oi"] - prev_pe
    existing = ws.get_all_values()
    has_header = bool(existing) and bool(existing[0]) and existing[0][0] == "time"
    if not has_header:
        ws.update(values=[["time", "ATM CE OI change", "ATM PE OI change"]], range_name="A1")
        try:
            format_cell_range(ws, "A1:C1", CellFormat(
                backgroundColor=Color(0.13, 0.15, 0.18),
                textFormat=TextFormat(bold=True, foregroundColor=Color(0.35, 0.65, 1.0))))
            set_frozen(ws, rows=1)
        except Exception:
            pass
    ws.append_row([now_str, ce_delta, pe_delta])
    print(f"✅ ATM OI Log — {now_str}: CE Δ{ce_delta:+}, PE Δ{pe_delta:+}")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    print(f"🚀 Starting OI fetch — {now_ist().strftime('%H:%M:%S')}")

    if not DHAN_ACCESS_TOKEN:
        raise RuntimeError("DHAN_ACCESS_TOKEN is empty — update it in GitHub Secrets")

    expiry  = fetch_nearest_expiry()
    raw     = fetch_options_chain(expiry)
    spot    = fetch_spot_price(raw)
    print(f"   Spot: {spot}")

    rows, atm = process_chain(raw, spot)
    sheet     = get_sheet_client()
    prev_oi   = read_prev_oi(sheet)

    pcr, signal = write_live_oi(sheet, rows, spot, atm, prev_oi, expiry)
    write_vega_table(sheet, rows, spot, atm)
    write_history(sheet, spot, atm, rows, pcr, signal)
    write_atm_oi_log(sheet, rows, atm, prev_oi)
    write_prev_oi(sheet, rows)

    print("✅ All done!")

if __name__ == "__main__":
    main()
