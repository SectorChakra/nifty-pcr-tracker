"""Shared, dependency-free PCR computation logic used by both fetch_pcr.py
(live, needs Playwright) and backfill_history.py (historical, stdlib only)."""

SYMBOLS = {
    # pinnedExpiries: extra expiries shown as their own panel until they pass
    # (added 2026-11-03 for the 17 Nov 23750/24000 CE spread). 50pt steps x 10
    # keeps the weekly band's +-500 width but shows every listed strike.
    "nifty": {"tradingsymbol": "NIFTY", "weeklyStep": 100, "monthlyStep": 500, "hasWeekly": True,
              "pinnedExpiries": ["2026-11-17"], "pinnedStep": 50, "pinnedBand": 10},
    # NSE discontinued BANKNIFTY weekly expiries in 2023 -- monthly only.
    "bankNifty": {"tradingsymbol": "BANKNIFTY", "monthlyStep": 500, "hasWeekly": False},
    # SENSEX is BSE, not NSE, and trades weeklies on Thursday. Steps are set to
    # put the band on the SAME SHARE OF SPOT as NIFTY's rather than copying its
    # numbers: SENSEX is ~3.2x NIFTY, so 100/500 there is 300/1000 here, which
    # measured on 2026-10-03 gives +-2.1% weekly and +-7.0% monthly against
    # NIFTY's +-2.2% and +-6.7%. Strike spacing on the chain is 100, so both
    # steps land on strikes that exist.
    "sensex": {"tradingsymbol": "SENSEX", "weeklyStep": 300, "monthlyStep": 1000, "hasWeekly": True},
}
BAND = 5  # strikes each side of ATM, at the symbol's step spacing

# Sensibull renders a window around ATM rather than the whole chain -- measured
# 2026-10-03, +-6.7% for NIFTY and +-4.2% for SENSEX. A monthly band wider than
# that window therefore lands on strikes the page never shows, and select_band
# drops them SILENTLY: NIFTY's monthly PCR has been running on 6-7 of its 11
# strikes all along, which is visible in pcr_data.json. The count is now
# published per timeframe so a thin band is readable instead of implied.

VIOLATION_LOW = 0.7
VIOLATION_HIGH = 2.0
CROSS_LOW = 0.41
CROSS_HIGH = 0.80


def select_band(rows, spot, step, band):
    if not rows:
        return []
    atm = round(spot / step) * step
    wanted = [atm + i * step for i in range(-band, band + 1)]
    return [s for s in wanted if s in rows]


def compute_pcr(rows, strikes):
    call_total = sum(rows[s]["callOI"] for s in strikes)
    put_total = sum(rows[s]["putOI"] for s in strikes)
    if call_total <= 0:
        return None
    return put_total / call_total


def equilibrium_strike(rows, strikes):
    return min(strikes, key=lambda s: abs(rows[s]["callOI"] - rows[s]["putOI"]))
