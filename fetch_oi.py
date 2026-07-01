"""
Options OI + Vega Dashboard - GitHub Actions Script
Data source: NSE India (free, no subscription needed)
"""

import os
import json
import gspread
from nse import NSE
from gspread_formatting import (
    CellFormat, Color, TextFormat, format_cell_range, set_frozen
)
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from pathlib import Path

IST = timedelta(hours=5, minutes=30)

def now_ist():
    return datetime.utcnow() + IST

GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")

CONFIG = {"STRIKES": 10}

# ─────────────────────────────────────────────
#  NSE FETCH
# ─────────────────────────────────────────────
def fetch_nse_option_chain():
    DIR = Path("/tmp/nse_cache")
    DIR.mkdir(exist_ok=True)
    with NSE(download_folder=DIR, server=True) as nse:
        raw = nse.optionChain("nifty")
    return raw

def process_nse_chain(raw):
    records  = raw.get("records", {})
    expiries = records.get("expiryDates", [])
    all_data = records.get("data", [])

    if not expiries:
        raise RuntimeError("No expiry dates in NSE response")

    print(f"   📋 Expiries available: {expiries[:4]}")
    print(f"   📋 Total records: {len(all_data)}")

    # Pick nearest Tuesday expiry from the top-level expiryDates list
    nearest_expiry = None
    nearest_dt     = None
    for exp in expiries:
        try:
            exp_date = datetime.strptime(exp, "%d-%b-%Y")
            if exp_date.weekday() == 1:   # Tuesday
                nearest_expiry = exp
                nearest_dt     = exp_date
                break
        except Exception:
            pass
    if not nearest_expiry:
        nearest_expiry = expiries[0]
        nearest_dt     = datetime.strptime(nearest_expiry, "%d-%b-%Y")
    print(f"   📅 Using expiry: {nearest_expiry}")

    spot = float(records.get("underlyingValue", 0))
    atm  = round(spot / 50) * 50
    print(f"   Spot: {spot}  ATM: {atm}")

    # The actual structure from NseIndiaApi:
    # each item in data[] has: strikePrice, expiryDates (list), CE (dict), PE (dict)
    # We filter by checking if our target expiry is IN the item's expiryDates list,
    # OR if the item has no expiryDates key, we try matching CE/PE data directly.
    rows = []
    for rec in all_data:
        strike = int(rec.get("strikePrice", 0))
        if strike == 0:
            continue

        # Check expiry — the record has an "expiryDates" list (note: plural)
        rec_expiries = rec.get("expiryDates", [])
        if rec_expiries:
            # Filter: only include this strike if our target expiry is in its list
            # Try matching both the string directly and by parsed date
            match = False
            for re in rec_expiries:
                try:
                    if re == nearest_expiry:
                        match = True
                        break
                    # Try parsing in case format differs
                    if datetime.strptime(re, "%d-%b-%Y").date() == nearest_dt.date():
                        match = True
                        break
                except Exception:
                    pass
            if not match:
                continue

        ce = rec.get("CE", {}) or {}
        pe = rec.get("PE", {}) or {}

        # Skip rows where both CE and PE are empty dicts
        if not ce and not pe:
            continue

        rows.append({
            "strike":  strike,
            "is_atm":  (strike == atm),
            "ce_oi":   ce.get("openInterest", 0) or 0,
            "ce_doi":  ce.get("changeinOpenInterest", 0) or 0,
            "ce_iv":   round(ce.get("impliedVolatility", 0) or 0, 2),
            "ce_vega": 0,
            "pe_oi":   pe.get("openInterest", 0) or 0,
            "pe_doi":  pe.get("changeinOpenInterest", 0) or 0,
            "pe_iv":   round(pe.get("impliedVolatility", 0) or 0, 2),
            "pe_vega": 0,
        })

    print(f"   📊 Parsed {len(rows)} strike rows")

    # If still empty, just take all rows regardless of expiry
    # (means our expiry filtering logic still has edge case)
    if not rows:
        print("   ⚠️  Still empty after filtering — taking all records without expiry filter")
        for rec in all_data:
            strike = int(rec.get("strikePrice", 0))
            if strike == 0:
                continue
            ce = rec.get("CE", {}) or {}
            pe = rec.get("PE", {}) or {}
            if not ce and not pe:
                continue
            rows.append({
                "strike":  strike,
                "is_atm":  (strike == atm),
                "ce_oi":   ce.get("openInterest", 0) or 0,
                "ce_doi":  ce.get("changeinOpenInterest", 0) or 0,
                "ce_iv":   round(ce.get("impliedVolatility", 0) or 0, 2),
                "ce_vega": 0,
                "pe_oi":   pe.get("openInterest", 0) or 0,
                "pe_doi":  pe.get("changeinOpenInterest", 0) or 0,
                "pe_iv":   round(pe.get("impliedVolatility", 0) or 0, 2),
                "pe_vega": 0,
            })
        print(f"   📊 Fallback parsed {len(rows)} rows")

    rows.sort(key=lambda x: x["strike"])
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

    total_ce = sum(r["ce_oi"] for r in rows)
    total_pe = sum(r["pe_oi"] for r in rows)
    pcr      = round(total_pe / total_ce, 3) if total_ce else 0
    signal   = "🟢 BULLISH" if pcr > 1.2 else ("🔴 BEARISH" if pcr < 0.8 else "🟡 NEUTRAL")

    header = [
        ["NIFTY Options OI Dashboard (NSE Live)", "", "", "", "", "", "", "", "", ""],
        [f"🕒 Last Updated: {now}", "", f"⏭ Next Refresh: ~{nxt}", "", "", "", "", "", "", ""],
        [f"Spot: {spot:.0f}", f"ATM: {atm}", f"Expiry: {expiry}", "",
         f"OI PCR: {pcr}", f"Signal: {signal}", "", "", "", ""],
        [""],
        ["STRIKE", "CE OI", "CE ΔOI", "CE IV%", "CE VEGA",
         "PE OI",  "PE ΔOI", "PE IV%", "PE VEGA", "ATM"],
    ]
    data_rows = [[
        r["strike"], r["ce_oi"], r["ce_doi"], r["ce_iv"], r["ce_vega"],
        r["pe_oi"],  r["pe_doi"], r["pe_iv"], r["pe_vega"],
        "◀ ATM" if r["is_atm"] else "",
    ] for r in rows]

    ws.clear()
    ws.update(values=header + data_rows, range_name="A1")
    print(f"✅ Live OI updated — {now}")
    format_live_oi(ws, rows)
    return pcr, signal

def format_live_oi(ws, rows):
    try:
        dark  = Color(0.05, 0.07, 0.09); green = Color(0.0, 1.0, 0.53)
        tgrn  = Color(0.10, 0.18, 0.10); lgrn  = Color(0.49, 0.91, 0.53)
        dgray = Color(0.09, 0.10, 0.13); lgray = Color(0.79, 0.82, 0.85)
        hdr   = Color(0.13, 0.15, 0.18); blue  = Color(0.35, 0.65, 1.0)
        atmbg = Color(0.18, 0.16, 0.0);  atmfg = Color(1.0, 0.84, 0.0)
        format_cell_range(ws, "A1:J1", CellFormat(backgroundColor=dark,  textFormat=TextFormat(bold=True, foregroundColor=green, fontSize=13)))
        format_cell_range(ws, "A2:J2", CellFormat(backgroundColor=tgrn,  textFormat=TextFormat(bold=True, foregroundColor=lgrn,  fontSize=12)))
        format_cell_range(ws, "A3:J3", CellFormat(backgroundColor=dgray, textFormat=TextFormat(foregroundColor=lgray, fontSize=11)))
        format_cell_range(ws, "A5:J5", CellFormat(backgroundColor=hdr,   textFormat=TextFormat(bold=True, foregroundColor=blue,  fontSize=11)))
        for i, r in enumerate(rows):
            rn  = 6 + i
            fmt = CellFormat(backgroundColor=atmbg, textFormat=TextFormat(bold=True, foregroundColor=atmfg)) if r["is_atm"] else \
                  CellFormat(backgroundColor=dark,  textFormat=TextFormat(foregroundColor=lgray))
            format_cell_range(ws, f"A{rn}:J{rn}", fmt)
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
        r["strike"], r["ce_iv"], r["ce_vega"], r["pe_iv"], r["pe_vega"],
        round(r["ce_vega"] + r["pe_vega"], 2), "◀ ATM" if r["is_atm"] else "",
    ] for r in rows]
    ws.clear()
    ws.update(values=header + data_rows, range_name="A1")
    print("✅ Vega Table updated")
    try:
        dark  = Color(0.05, 0.07, 0.09); purp  = Color(0.65, 0.49, 0.98)
        hdr   = Color(0.13, 0.15, 0.18); blue  = Color(0.35, 0.65, 1.0)
        lgray = Color(0.79, 0.82, 0.85); atmbg = Color(0.18, 0.16, 0.0); atmfg = Color(1.0, 0.84, 0.0)
        format_cell_range(ws, "A1:G1", CellFormat(backgroundColor=dark, textFormat=TextFormat(bold=True, foregroundColor=purp, fontSize=13)))
        format_cell_range(ws, "A2:G2", CellFormat(backgroundColor=hdr,  textFormat=TextFormat(bold=True, foregroundColor=blue)))
        for i, r in enumerate(rows):
            rn  = 3 + i
            fmt = CellFormat(backgroundColor=atmbg, textFormat=TextFormat(bold=True, foregroundColor=atmfg)) if r["is_atm"] else \
                  CellFormat(backgroundColor=dark,  textFormat=TextFormat(foregroundColor=lgray))
            format_cell_range(ws, f"A{rn}:G{rn}", fmt)
        set_frozen(ws, rows=2)
    except Exception as e:
        print(f"   ⚠️  Vega formatting skipped: {e}")

def read_prev_oi(sheet):
    try:
        ws = sheet.worksheet("Prev OI")
        return {int(r["strike"]): {"ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"]} for r in ws.get_all_records()}
    except Exception:
        return {}

def write_prev_oi(sheet, rows):
    ws = get_or_create_tab(sheet, "Prev OI")
    ws.clear()
    ws.update(values=[["strike","ce_oi","pe_oi"]] + [[r["strike"],r["ce_oi"],r["pe_oi"]] for r in rows], range_name="A1")

def write_history(sheet, spot, atm, rows, pcr, signal):
    ws      = get_or_create_tab(sheet, "History", rows=2000, cols=13)
    now     = now_ist().strftime("%d-%b %H:%M")
    atm_row = next((r for r in rows if r["is_atm"]), None)
    if not atm_row:
        return
    existing = ws.get_all_values()
    if not (bool(existing) and bool(existing[0]) and existing[0][0] == "Time"):
        ws.update(values=[["Time","Spot","ATM","CE OI","CE ΔOI","PE OI","PE ΔOI",
                            "CE Vega","PE Vega","CE IV%","PE IV%","PCR","Signal"]], range_name="A1")
    ws.append_row([now, spot, atm,
                   atm_row["ce_oi"], atm_row["ce_doi"], atm_row["pe_oi"], atm_row["pe_doi"],
                   atm_row["ce_vega"], atm_row["pe_vega"], atm_row["ce_iv"], atm_row["pe_iv"],
                   pcr, signal])
    print("✅ History updated")

def write_atm_oi_log(sheet, rows):
    ws      = get_or_create_tab(sheet, "ATM OI Log", rows=2000, cols=4)
    now_str = now_ist().strftime("%H:%M")
    atm_row = next((r for r in rows if r["is_atm"]), None)
    if not atm_row:
        return
    existing = ws.get_all_values()
    if not (bool(existing) and bool(existing[0]) and existing[0][0] == "time"):
        ws.update(values=[["time", "ATM CE OI change", "ATM PE OI change"]], range_name="A1")
        try:
            format_cell_range(ws, "A1:C1", CellFormat(
                backgroundColor=Color(0.13, 0.15, 0.18),
                textFormat=TextFormat(bold=True, foregroundColor=Color(0.35, 0.65, 1.0))))
            set_frozen(ws, rows=1)
        except Exception:
            pass
    ws.append_row([now_str, atm_row["ce_doi"], atm_row["pe_doi"]])
    print(f"✅ ATM OI Log — {now_str}")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    print(f"🚀 Starting OI fetch — {now_ist().strftime('%H:%M:%S')} IST")

    raw                     = fetch_nse_option_chain()
    rows, atm, spot, expiry = process_nse_chain(raw)

    sheet   = get_sheet_client()
    prev_oi = read_prev_oi(sheet)

    pcr, signal = write_live_oi(sheet, rows, spot, atm, expiry)
    write_vega_table(sheet, rows, spot, atm)
    write_history(sheet, spot, atm, rows, pcr, signal)
    write_atm_oi_log(sheet, rows)
    write_prev_oi(sheet, rows)

    print("✅ All done!")

if __name__ == "__main__":
    main()
