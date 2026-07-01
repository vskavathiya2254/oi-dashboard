"""
Options OI + Vega Dashboard - GitHub Actions Script
Data source: NSE India (free, no subscription needed)
Runs every 5 minutes via GitHub Actions → pushes to Google Sheets
"""

import os
import json
import time
import requests
import gspread
from gspread_formatting import (
    CellFormat, Color, TextFormat, format_cell_range, set_frozen
)
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

IST = timedelta(hours=5, minutes=30)

def now_ist():
    return datetime.utcnow() + IST

# ─────────────────────────────────────────────
#  CREDENTIALS — set these as GitHub Secrets
#  (No Dhan credentials needed anymore!)
# ─────────────────────────────────────────────
GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")

CONFIG = {
    "SYMBOL":  "NIFTY",
    "STRIKES": 10,   # strikes above and below ATM to show
}

# ─────────────────────────────────────────────
#  NSE DATA FETCH (free, no login needed)
# ─────────────────────────────────────────────
NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/option-chain",
}

def fetch_nse_option_chain():
    """Fetches NIFTY option chain from NSE's free public API.
    NSE requires a session cookie — we grab it by visiting the main
    page first, then hit the API. Retries up to 3 times on failure."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)

    for attempt in range(3):
        try:
            # Step 1: visit main page to get cookies
            session.get("https://www.nseindia.com", timeout=10)
            time.sleep(1.5)   # small pause so NSE doesn't block us

            # Step 2: visit option chain page to get more cookies
            session.get("https://www.nseindia.com/option-chain", timeout=10)
            time.sleep(1.0)

            # Step 3: hit the actual API
            url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
            resp = session.get(url, timeout=15)

            if resp.status_code == 200:
                print(f"   ✅ NSE data fetched successfully")
                return resp.json()
            else:
                print(f"   ⚠️  NSE returned {resp.status_code} on attempt {attempt+1}, retrying...")
                time.sleep(5)

        except Exception as e:
            print(f"   ⚠️  Attempt {attempt+1} failed: {e}")
            time.sleep(5)

    raise RuntimeError("Failed to fetch NSE option chain after 3 attempts")

def process_nse_chain(raw):
    """Parse NSE option chain response into our standard row format.
    NSE already provides changeinOpenInterest (OI delta since previous
    close) so we use that directly for the OI change columns."""
    records  = raw.get("records", {})
    expiries = records.get("expiryDates", [])
    all_data = records.get("data", [])

    if not expiries:
        raise RuntimeError("No expiry dates in NSE response")

    # Pick nearest Tuesday expiry (NIFTY weekly expiry day)
    # NSE returns expiry dates like "03-Jul-2025"
    nearest_expiry = None
    for exp in expiries:
        exp_date = datetime.strptime(exp, "%d-%b-%Y")
        if exp_date.weekday() == 1:   # Tuesday
            nearest_expiry = exp
            break
    if not nearest_expiry:
        nearest_expiry = expiries[0]   # fallback to soonest
    print(f"   📅 Using expiry: {nearest_expiry}")

    # Spot price is in the underlying info
    spot = float(raw.get("records", {}).get("underlyingValue", 0))
    atm  = round(spot / 50) * 50
    print(f"   Spot: {spot}  ATM: {atm}")

    rows = []
    for rec in all_data:
        if rec.get("expiryDate") != nearest_expiry:
            continue

        strike = int(rec.get("strikePrice", 0))
        ce     = rec.get("CE", {}) or {}
        pe     = rec.get("PE", {}) or {}

        rows.append({
            "strike":   strike,
            "is_atm":   (strike == atm),
            "ce_oi":    ce.get("openInterest", 0) or 0,
            "ce_doi":   ce.get("changeinOpenInterest", 0) or 0,   # NSE gives OI change directly
            "ce_iv":    round(ce.get("impliedVolatility", 0) or 0, 2),
            "ce_vega":  0,   # NSE doesn't provide greeks; vega col will show 0
            "pe_oi":    pe.get("openInterest", 0) or 0,
            "pe_doi":   pe.get("changeinOpenInterest", 0) or 0,
            "pe_iv":    round(pe.get("impliedVolatility", 0) or 0, 2),
            "pe_vega":  0,
        })

    rows.sort(key=lambda x: x["strike"])

    # filter ± N strikes around ATM
    n       = CONFIG["STRIKES"]
    atm_idx = next((i for i, r in enumerate(rows) if r["strike"] == atm), len(rows)//2)
    rows    = rows[max(0, atm_idx - n): atm_idx + n + 1]

    return rows, atm, spot, nearest_expiry

# ─────────────────────────────────────────────
#  GOOGLE SHEETS
# ─────────────────────────────────────────────
def get_sheet_client():
    scopes     = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc         = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)

def get_or_create_tab(sheet, title, rows=500, cols=20):
    try:
        return sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)

# ─────────────────────────────────────────────
#  WRITE TABS
# ─────────────────────────────────────────────
def write_live_oi(sheet, rows, spot, atm, expiry):
    ws     = get_or_create_tab(sheet, "Live OI")
    now_dt = now_ist()
    now    = now_dt.strftime("%d-%b-%Y %H:%M:%S")
    nxt    = (now_dt + timedelta(minutes=5)).strftime("%H:%M")

    total_ce_oi = sum(r["ce_oi"] for r in rows)
    total_pe_oi = sum(r["pe_oi"] for r in rows)
    pcr    = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0
    signal = "🟢 BULLISH" if pcr > 1.2 else ("🔴 BEARISH" if pcr < 0.8 else "🟡 NEUTRAL")

    header = [
        ["NIFTY Options OI Dashboard (NSE Live)", "", "", "", "", "", "", "", "", ""],
        [f"🕒 Last Updated: {now}", "", f"⏭ Next Refresh: ~{nxt}", "", "", "", "", "", "", ""],
        [f"Spot: {spot:.0f}", f"ATM: {atm}", f"Expiry: {expiry}", "",
         f"OI PCR: {pcr}", f"Signal: {signal}", "", "", "", ""],
        [""],
        ["STRIKE", "CE OI", "CE ΔOI", "CE IV%", "CE VEGA",
         "PE OI",  "PE ΔOI", "PE IV%", "PE VEGA", "ATM"],
    ]

    data_rows = []
    for r in rows:
        data_rows.append([
            r["strike"],
            r["ce_oi"],  r["ce_doi"],  r["ce_iv"],  r["ce_vega"],
            r["pe_oi"],  r["pe_doi"],  r["pe_iv"],  r["pe_vega"],
            "◀ ATM" if r["is_atm"] else "",
        ])

    ws.clear()
    ws.update(values=header + data_rows, range_name="A1")
    print(f"✅ Live OI updated — {now}")
    format_live_oi(ws, rows)
    return pcr, signal

def format_live_oi(ws, rows):
    try:
        dark  = Color(0.05, 0.07, 0.09)
        green = Color(0.0,  1.0,  0.53)
        tgrn  = Color(0.10, 0.18, 0.10)
        lgrn  = Color(0.49, 0.91, 0.53)
        dgray = Color(0.09, 0.10, 0.13)
        lgray = Color(0.79, 0.82, 0.85)
        hdr   = Color(0.13, 0.15, 0.18)
        blue  = Color(0.35, 0.65, 1.0)
        atmbg = Color(0.18, 0.16, 0.0)
        atmfg = Color(1.0,  0.84, 0.0)

        format_cell_range(ws, "A1:J1", CellFormat(backgroundColor=dark,  textFormat=TextFormat(bold=True, foregroundColor=green, fontSize=13)))
        format_cell_range(ws, "A2:J2", CellFormat(backgroundColor=tgrn,  textFormat=TextFormat(bold=True, foregroundColor=lgrn,  fontSize=12)))
        format_cell_range(ws, "A3:J3", CellFormat(backgroundColor=dgray, textFormat=TextFormat(foregroundColor=lgray, fontSize=11)))
        format_cell_range(ws, "A5:J5", CellFormat(backgroundColor=hdr,   textFormat=TextFormat(bold=True, foregroundColor=blue,  fontSize=11)))

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
    ws  = get_or_create_tab(sheet, "Vega Table")
    now = now_ist().strftime("%d-%b %H:%M")
    header = [
        [f"Vega Table — {now}  |  Spot: {spot:.0f}"],
        ["STRIKE", "CE IV%", "CE VEGA", "PE IV%", "PE VEGA", "TOTAL VEGA", "ATM"],
    ]
    data_rows = [[
        r["strike"], r["ce_iv"], r["ce_vega"],
        r["pe_iv"],  r["pe_vega"],
        round(r["ce_vega"] + r["pe_vega"], 2),
        "◀ ATM" if r["is_atm"] else "",
    ] for r in rows]

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
        ws      = sheet.worksheet("Prev OI")
        records = ws.get_all_records()
        return {int(r["strike"]): {"ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"]} for r in records}
    except Exception:
        return {}

def write_prev_oi(sheet, rows):
    ws   = get_or_create_tab(sheet, "Prev OI")
    data = [["strike", "ce_oi", "pe_oi"]] + [[r["strike"], r["ce_oi"], r["pe_oi"]] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")

def write_history(sheet, spot, atm, rows, pcr, signal):
    ws      = get_or_create_tab(sheet, "History", rows=2000, cols=13)
    now     = now_ist().strftime("%d-%b %H:%M")
    atm_row = next((r for r in rows if r["is_atm"]), None)
    if not atm_row:
        return
    existing   = ws.get_all_values()
    has_header = bool(existing) and bool(existing[0]) and existing[0][0] == "Time"
    if not has_header:
        ws.update(values=[["Time","Spot","ATM","CE OI","CE ΔOI","PE OI","PE ΔOI",
                            "CE Vega","PE Vega","CE IV%","PE IV%","PCR","Signal"]], range_name="A1")
    ws.append_row([now, spot, atm,
                   atm_row["ce_oi"], atm_row["ce_doi"],
                   atm_row["pe_oi"], atm_row["pe_doi"],
                   atm_row["ce_vega"], atm_row["pe_vega"],
                   atm_row["ce_iv"],  atm_row["pe_iv"],
                   pcr, signal])
    print("✅ History updated")

def write_atm_oi_log(sheet, rows):
    ws      = get_or_create_tab(sheet, "ATM OI Log", rows=2000, cols=4)
    now_str = now_ist().strftime("%H:%M")
    atm_row = next((r for r in rows if r["is_atm"]), None)
    if not atm_row:
        return
    # NSE provides changeinOpenInterest directly — more accurate than our delta calc
    ce_delta = atm_row["ce_doi"]
    pe_delta = atm_row["pe_doi"]

    existing   = ws.get_all_values()
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
    print(f"🚀 Starting OI fetch — {now_ist().strftime('%H:%M:%S')} IST")

    # 1. Fetch from NSE (free, no token needed)
    raw                      = fetch_nse_option_chain()
    rows, atm, spot, expiry  = process_nse_chain(raw)

    # 2. Connect to Sheets
    sheet   = get_sheet_client()
    prev_oi = read_prev_oi(sheet)

    # 3. Write all tabs
    pcr, signal = write_live_oi(sheet, rows, spot, atm, expiry)
    write_vega_table(sheet, rows, spot, atm)
    write_history(sheet, spot, atm, rows, pcr, signal)
    write_atm_oi_log(sheet, rows)
    write_prev_oi(sheet, rows)

    print("✅ All done!")

if __name__ == "__main__":
    main()
