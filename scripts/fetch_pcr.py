import json
import os
import re
import sys
from datetime import datetime, date, timezone

from playwright.sync_api import sync_playwright

from pcr_common import (
    SYMBOLS, BAND, VIOLATION_LOW, VIOLATION_HIGH, CROSS_LOW, CROSS_HIGH,
    select_band, compute_pcr, equilibrium_strike,
)

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "pcr_state.json")
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "pcr_data.json")
# Today's per-strike OI snapshots, one per run, so a panel can show the change
# over the last hour. Reset each day; only the last few hours are kept.
INTRADAY_PATH = os.path.join(os.path.dirname(__file__), "..", "oi_intraday.json")
HOUR_S = 3600
KEEP_S = 3 * HOUR_S

# Wide-viewport combined layout renders one row as:
# call_chg_pct, call_oi(lakh), call_ltp, strike, iv, put_ltp, put_oi(lakh), put_chg_pct
ROW_RE = re.compile(
    r"(-?[\d.]+%)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n(\d+)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n([\d.]+)\n\t\n(-?[\d.]+%)"
)


def day_change(oi, chg_pct):
    """OI added today, from Sensibull's "OI Chg%" (change vs the previous
    day's closing OI -- checked 2026-11-03 against NSE's 02-Nov bhavcopy:
    NIFTY 03-Nov 22500 PE 124.6 -> 172.3 lakh = +38%, shown "38%"). The
    percentage is rounded to ~2 significant figures, so this is within about
    half a percent of the OI, not exact."""
    if chg_pct is None or chg_pct <= -100:
        return None
    return oi - oi / (1 + chg_pct / 100)


def parse_pct(txt):
    try:
        return float(txt.rstrip("%"))
    except ValueError:
        return None


def month_key(d):
    return (d.year, d.month)


def parse_expiry_label(label, today):
    dt = datetime.strptime(label + f" {today.year}", "%d %b %Y").date()
    if dt < today:
        dt = dt.replace(year=today.year + 1)
    return dt


def get_expiries(page, today):
    page.get_by_role("combobox").first.click(timeout=5000)
    page.wait_for_selector('[role="option"]', timeout=5000)
    labels = page.locator('[role="option"]').all_inner_texts()
    dates = []
    for label in labels:
        m = re.match(r"(\d{1,2} [A-Za-z]{3})", label.strip())
        if m:
            dates.append(parse_expiry_label(m.group(1), today))
    page.keyboard.press("Escape")
    return sorted(set(dates))


def pick_weekly_and_monthly(expiries):
    weekly = expiries[0]
    current_month = month_key(weekly)
    same_month = [d for d in expiries if month_key(d) == current_month]
    monthly = max(same_month)
    return weekly, monthly


def fetch_chain_for_expiry(page, tradingsymbol, expiry_date):
    url = f"https://web.sensibull.com/option-chain?tradingsymbol={tradingsymbol}&expiry={expiry_date.isoformat()}"
    page.goto(url, timeout=30000, wait_until="networkidle")
    page.wait_for_timeout(1500)
    text = page.inner_text("body")

    spot_match = re.search(re.escape(tradingsymbol) + r"\s*\n?\s*([\d,]+\.\d+)", text)
    spot = float(spot_match.group(1).replace(",", "")) if spot_match else None

    rows = {}
    for m in ROW_RE.finditer(text):
        (call_chg_pct, call_oi, call_ltp, strike, _iv,
         put_ltp, put_oi, put_chg_pct) = m.groups()
        call_oi, put_oi = float(call_oi), float(put_oi)
        rows[int(strike)] = {
            "callOI": call_oi, "putOI": put_oi,
            "callLTP": float(call_ltp), "putLTP": float(put_ltp),
            "callDayChg": day_change(call_oi, parse_pct(call_chg_pct)),
            "putDayChg": day_change(put_oi, parse_pct(put_chg_pct)),
        }
    return spot, rows


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def update_crossing_state(state, key, pcr, today_str):
    day_state = state.get(key, {})
    if day_state.get("date") != today_str:
        day_state = {"date": today_str, "min": pcr, "max": pcr, "crossed": False}
    else:
        day_state["min"] = min(day_state["min"], pcr)
        day_state["max"] = max(day_state["max"], pcr)
    if day_state["min"] <= CROSS_LOW and day_state["max"] >= CROSS_HIGH:
        day_state["crossed"] = True
    state[key] = day_state
    return day_state["crossed"]


def load_intraday(today_str):
    if os.path.exists(INTRADAY_PATH):
        with open(INTRADAY_PATH) as f:
            data = json.load(f)
        if data.get("date") == today_str:
            return data
    return {"date": today_str, "snaps": []}


def hour_baseline(intraday, now_s):
    """The newest snapshot at least an hour old. The pinger is not perfectly
    regular, so the panel shows the baseline's own time rather than claiming
    exactly 60 minutes."""
    old = [sn for sn in intraday["snaps"] if sn["t"] <= now_s - HOUR_S]
    return old[-1] if old else None


def r1(x):
    return None if x is None else round(x, 1)


def build_timeframe_result(rows, spot, step, band, state, key, today_str, baseline=None, snap=None):
    strikes = select_band(rows, spot, step, band)
    if not strikes:
        return None
    pcr = compute_pcr(rows, strikes)
    if pcr is None:
        return None
    violation = pcr < VIOLATION_LOW or pcr > VIOLATION_HIGH
    eq_strike = equilibrium_strike(rows, strikes) if violation else None
    crossed = update_crossing_state(state, key, pcr, today_str)
    base = (baseline or {}).get("oi", {}).get(key, {})
    if snap is not None:
        snap[key] = {str(s): [rows[s]["callOI"], rows[s]["putOI"]] for s in strikes}

    def hour_chg(s, side):
        b = base.get(str(s))
        return None if b is None else rows[s][side + "OI"] - b[0 if side == "call" else 1]

    return {
        "pcr": round(pcr, 3),
        "bandStrikes": len(strikes),
        "bandWanted": 2 * band + 1,
        "violation": violation,
        "equilibriumStrike": eq_strike,
        "crossed0_41_0_80": crossed,
        "strikes": [
            {
                "strike": s, "callOI": rows[s]["callOI"], "putOI": rows[s]["putOI"],
                "callLTP": rows[s]["callLTP"], "putLTP": rows[s]["putLTP"],
                "callDayChg": r1(rows[s]["callDayChg"]), "putDayChg": r1(rows[s]["putDayChg"]),
                "callHourChg": r1(hour_chg(s, "call")), "putHourChg": r1(hour_chg(s, "put")),
            }
            for s in strikes
        ],
        "hourBaselineAt": baseline["at"] if baseline and base else None,
    }


def fetch_symbol(page, symbol_key, cfg, today, today_str, state, baseline=None, snap=None):
    tradingsymbol = cfg["tradingsymbol"]
    page.goto(f"https://web.sensibull.com/option-chain?tradingsymbol={tradingsymbol}",
              timeout=30000, wait_until="networkidle")
    page.wait_for_timeout(1500)

    expiries = get_expiries(page, today)
    if not expiries:
        print(f"{tradingsymbol}: could not read expiry list", file=sys.stderr)
        return None
    weekly_expiry, monthly_expiry = pick_weekly_and_monthly(expiries)
    has_weekly = cfg.get("hasWeekly", True)

    if has_weekly:
        weekly_spot, weekly_rows = fetch_chain_for_expiry(page, tradingsymbol, weekly_expiry)
        if monthly_expiry == weekly_expiry:
            monthly_spot, monthly_rows = weekly_spot, weekly_rows
        else:
            monthly_spot, monthly_rows = fetch_chain_for_expiry(page, tradingsymbol, monthly_expiry)
    else:
        # No separate weekly for this symbol (e.g. BANKNIFTY, monthly-only since 2023) --
        # fetch the monthly chain only.
        monthly_spot, monthly_rows = fetch_chain_for_expiry(page, tradingsymbol, monthly_expiry)

    monthly = build_timeframe_result(
        monthly_rows, monthly_spot, cfg["monthlyStep"], BAND, state, f"{symbol_key}_monthly", today_str,
        baseline, snap)
    if monthly is None:
        print(f"{tradingsymbol}: missing monthly data", file=sys.stderr)
        return None

    result = {
        "spot": monthly_spot,
        "monthly": {"expiry": monthly_expiry.isoformat(), **monthly},
    }

    if has_weekly:
        weekly = build_timeframe_result(
            weekly_rows, weekly_spot, cfg["weeklyStep"], BAND, state, f"{symbol_key}_weekly", today_str,
            baseline, snap)
        if weekly is None:
            print(f"{tradingsymbol}: missing weekly data", file=sys.stderr)
            return None
        result["spot"] = weekly_spot
        result["weekly"] = {"expiry": weekly_expiry.isoformat(), **weekly}

    return result


def main():
    today = date.today()
    today_str = today.isoformat()
    state = load_state()
    now = datetime.now(timezone.utc)
    now_s = int(now.timestamp())
    intraday = load_intraday(today_str)
    baseline = hour_baseline(intraday, now_s)
    snap = {}

    out = {"generatedAt": now.isoformat()}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        for symbol_key, cfg in SYMBOLS.items():
            result = fetch_symbol(page, symbol_key, cfg, today, today_str, state, baseline, snap)
            if result is not None:
                out[symbol_key] = result
        browser.close()

    if "nifty" not in out or "bankNifty" not in out:
        print("Missing one or both symbols, aborting write to avoid a partial snapshot",
              file=sys.stderr)
        sys.exit(1)

    with open(DATA_PATH, "w") as f:
        json.dump(out, f, indent=2)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
    intraday["snaps"] = [sn for sn in intraday["snaps"] if sn["t"] > now_s - KEEP_S]
    intraday["snaps"].append({"t": now_s, "at": now.isoformat(), "oi": snap})
    with open(INTRADAY_PATH, "w") as f:
        json.dump(intraday, f, separators=(",", ":"))

    print(f"Wrote pcr_data.json: "
          f"nifty weekly={out['nifty']['weekly']['pcr']} monthly={out['nifty']['monthly']['pcr']}, "
          f"bankNifty monthly={out['bankNifty']['monthly']['pcr']}")


if __name__ == "__main__":
    main()
