#!/usr/bin/env python3
"""Forecast GTA VI's launch-day Twitch viewers, hour by hour.

Reads one case file (schema makefun/twitch-ccu-case/v1) and writes five
quantiles (levels 0.05/0.25/0.5/0.75/0.95) of the Twitch category viewer
count for each of hours 2..24 of the release day, given everything
recorded up to the end of hour 1, that hour's print included.

Method (v11): every past launch curve is re-indexed into unlock time
(hours since that launch's own unlock), then blended in log space by
three centre members -- a kernel-weighted local-linear ridge of the log
viewer-ratio on log hour-1 print, plus two kernel-weighted mean curves
at different bandwidths -- then corrected with the target's own
pre-release pattern (a one-sided diurnal dip, an inflation shift at the
trough hours gated by the launch's own midday dip, an early-dip echo at
midday, and a small-launch evening lift). Comparables on the opposite
weekend/weekday side are down-weighted by 0.75. The spread is a fixed
per-hour lognormal width.

v11 change vs the v5 centre: SIZE-ADAPTIVE bandwidths (rule A3). The
kernel bandwidths depend on the TARGET's log hour-1 print (big means
logp1 >= 10.0): BW_MEAN1 is 1.0 for big targets, 1.5 otherwise;
BW_LOCLIN is 1.8 for big targets, 1.2 otherwise. Narrower mean
bandwidths help big launches (practice + the big-launch LOO slice)
but hurt the LOO median on mid/small targets, and the narrower
local-linear bandwidth does the opposite; applying each only where it
helps passes both gates. Everything else is the v5 centre, unchanged.

Deterministic, standard library only, offline.
"""

import json
import math
import os
from datetime import date, datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-hour lognormal spread (sigma) for forecast hours 2..24.
SIGMA = [0.26, 0.25, 0.30, 0.21, 0.18, 0.16, 0.18, 0.20, 0.22, 0.29,
         0.32, 0.32, 0.25, 0.24, 0.25, 0.26, 0.23, 0.17, 0.15, 0.15,
         0.15, 0.15, 0.15]

# Standard-normal quantiles at the five forecast levels.
Z_VALUES = (-1.645, -0.674, 0.0, 0.674, 1.645)

# Centre weights.
W_LOCLIN = 0.5
W_MEAN1 = 0.3
W_MEAN2 = 0.2

# Kernel bandwidths. BW_MEAN2 is fixed; BW_MEAN1 / BW_LOCLIN are
# size-adaptive (rule A3): the effective value is chosen per target
# from its log hour-1 print and threaded through as a parameter.
BW_MEAN2 = 2.5
BW_MEAN1_BIG = 1.0
BW_MEAN1_SMALL = 1.5
BW_LOCLIN_BIG = 1.8
BW_LOCLIN_SMALL = 1.2
BIG_THRESHOLD = 10.0

# Local-linear ridge.
LAM_LOCLIN = 0.6
SLOPE_DAMP = 0.3

# Dip-correction coefficients.
DIP_COEF = 0.7
INFL_COEF = 0.02
ECHO_COEF = 0.5
EVENING_COEF = 0.2
EARLY_SIZE_COEF = 0.2
SIZE_REF = 12.0

# Opposite weekend/weekday side down-weight.
WEEKDAY_FACTOR = 0.75

# Simultaneous-global launches: name -> unlock UTC hour. ALIGNMENT
# ONLY -- no curve values are taken from this map.
SIMULTANEOUS_UNLOCK = {
    "Cyberpunk 2077": 0,
    "Black Myth: Wukong": 2,
    "Hollow Knight: Silksong": 14,
    "Battlefield 6": 14,
    "007 First Light": 14,
}


def parse_ts(text):
    """Parse an ISO-8601 UTC timestamp like 2026-09-15T00:00:00Z."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


def is_us_dst(moment):
    """True if US daylight saving is in effect on the date of `moment`.

    DST runs from the second Sunday of March to the first Sunday of
    November (dates, as in the v5 reference: a launch on the second
    Sunday of March itself counts as DST, one on the first Sunday of
    November does not). No pool or practice launch falls on either
    boundary day, so the date rule and the hour-precise rule agree on
    all data this program ever sees.
    """
    day = moment.date()
    mar1 = date(day.year, 3, 1)
    start = mar1.replace(day=1 + ((6 - mar1.weekday()) % 7) + 7)
    nov1 = date(day.year, 11, 1)
    end = nov1.replace(day=1 + ((6 - nov1.weekday()) % 7))
    return start <= day < end


def rolling_unlock(day_start):
    """UTC hour of a rolling midnight-local unlock: 04:00 in DST, else
    05:00 (US East Coast midnight)."""
    return 4 if is_us_dst(parse_ts(day_start)) else 5


def comp_unlock(comp):
    """Unlock UTC hour of a comparable, or None if it must be excluded.

    Excluded: early-access-contaminated, weak-tier, or target-history
    unlock texts; simultaneous / single-global launches (unless their
    name is in the simultaneous map); mixed console-rolling plus
    PC-simultaneous launches. Otherwise the rolling unlock for the
    comparable's own release day.
    """
    text = comp.get("unlock") or ""
    if "early-access-contaminated" in text:
        return None
    if "Tier: weak" in text:
        return None
    if "Tier: target-history" in text:
        return None
    name = comp.get("name", "")
    if name in SIMULTANEOUS_UNLOCK:
        return SIMULTANEOUS_UNLOCK[name]
    low = text.lower()
    if "simultaneous" in low or "single global" in low:
        return None
    if "mixed" in low:
        return None
    return rolling_unlock(comp["day_start"])


# ---------------------------------------------------------------------------
# Comparable pool
# ---------------------------------------------------------------------------

def build_pool(case):
    """Usable comparables, each re-indexed into unlock time.

    A comparable is usable when comp_unlock gives an hour and all
    launch-day hours 0..23 are present with viewers >= 1. The curve
    has 29 entries; entry j is the log ratio of the viewers at
    launch-day hour clamp(unlock + j - 5, 0, 23) to the viewers at
    launch-day hour 0, so index j is the curve (hours since unlock) + 5
    and the first entries cover the hours just before the unlock.
    """
    pool = []
    for comp in case.get("comparables") or []:
        unlock = comp_unlock(comp)
        if unlock is None:
            continue
        day_start = parse_ts(comp["day_start"])
        by_hour = {}
        pre = []
        for point in comp.get("hourly") or []:
            off = int((parse_ts(point["from"]) - day_start)
                      .total_seconds() // 3600)
            if 0 <= off <= 23:
                by_hour[off] = point
            elif off < 0:
                pre.append(point)
        if len(by_hour) < 24:
            continue
        if any((by_hour[h].get("viewers") or 0) < 1 for h in range(24)):
            continue
        v0 = by_hour[0]["viewers"]
        logp1 = math.log(v0)
        curve = []
        for j in range(29):
            hh = min(max(unlock + j - 5, 0), 23)
            curve.append(math.log(by_hour[hh]["viewers"] / v0))
        ch0 = by_hour[0].get("channels")
        logch1 = math.log(ch0) if ch0 else 0.0
        pool.append({
            "name": comp.get("name", ""),
            "logp1": logp1,
            "logch1": logch1,
            "curve": curve,
            "unlock": unlock,
            "pre": pre,
            "weekend": day_start.weekday() >= 5,
            "day_start": comp["day_start"],
        })
    return pool


def build_diurnal(case):
    """The target's own pre-release diurnal profile, or None.

    Mean log viewers by UTC hour over the 14 days before the release
    day (offsets -336..-1 from history_hourly). Every UTC hour needs
    at least 3 points, otherwise there is no profile. The profile is
    then passed through a 3-point moving average twice (edges average
    2 points).
    """
    day_start = parse_ts(case["day_start"])
    by_utc = {h: [] for h in range(24)}
    for point in case.get("history_hourly") or []:
        start = parse_ts(point["from"])
        off = int((start - day_start).total_seconds() // 3600)
        if -336 <= off < 0:
            viewers = point.get("viewers")
            if viewers is None:
                continue
            by_utc[start.hour].append(math.log(max(1, viewers)))
    if any(len(by_utc[h]) < 3 for h in range(24)):
        return None
    prof = [sum(by_utc[h]) / len(by_utc[h]) for h in range(24)]
    for _ in range(2):
        smoothed = []
        for h in range(24):
            if h == 0:
                smoothed.append((prof[0] + prof[1]) / 2.0)
            elif h == 23:
                smoothed.append((prof[22] + prof[23]) / 2.0)
            else:
                smoothed.append((prof[h - 1] + prof[h] + prof[h + 1])
                                / 3.0)
        prof = smoothed
    return prof


# ---------------------------------------------------------------------------
# Centre ensemble
# ---------------------------------------------------------------------------

def kernel_weight(logp1, lp, bw):
    """Gaussian kernel weight of a comparable at log hour-1 print lp."""
    d = (logp1 - lp) / bw
    return math.exp(-0.5 * d * d)


def _weighted_mean(pool, pos, weights):
    sw = sum(weights)
    if sw <= 0:
        if not pool:
            return 0.0
        return sum(c["curve"][pos] for c in pool) / len(pool)
    return sum(w * c["curve"][pos]
               for w, c in zip(weights, pool)) / sw


def _loclin(pool, pos, logp1, weights):
    """Kernel-weighted local-linear ridge of the log-ratio on the log
    hour-1 print, fitted at `pos`, evaluated at the target's logp1.

    Ridge lambda applies to the slope only; the fit is centred at the
    kernel-weighted means; past the data range the slope is damped to
    30% (SLOPE_DAMP) instead of extrapolating at full slope.
    """
    if not pool:
        return 0.0
    sw = sum(weights)
    if sw <= 0:
        return _weighted_mean(pool, pos, weights)
    xbar = sum(w * c["logp1"] for w, c in zip(weights, pool)) / sw
    ybar = sum(w * c["curve"][pos] for w, c in zip(weights, pool)) / sw
    sxx = sum(w * (c["logp1"] - xbar) ** 2
              for w, c in zip(weights, pool))
    sxy = sum(w * (c["logp1"] - xbar) * (c["curve"][pos] - ybar)
              for w, c in zip(weights, pool))
    slope = sxy / (sxx + LAM_LOCLIN)
    xs = [c["logp1"] for c in pool]
    x_eval = min(max(logp1, min(xs)), max(xs))
    return (ybar + slope * (x_eval - xbar)
            + SLOPE_DAMP * slope * (logp1 - x_eval))


def ensemble_centre(pool, logp1, target_unlock, target_weekend,
                    bw_mean1, bw_loclin):
    """Blended log-ratio centre for forecast hours 2..24.

    Per hour h, pos = clamp(h + 4 - unlock, 0, 28) samples every
    comparable's curve at the same offset from its own unlock.
    Centre = 0.5 * loclin + 0.3 * kernel-mean(bw_mean1)
             + 0.2 * kernel-mean(BW_MEAN2).
    The bandwidths are passed in (rule A3: chosen per target, see
    forecast_case) rather than read from module globals.
    """
    centre = []
    for h in range(2, 25):
        pos = min(max(h + 4 - target_unlock, 0), 28)
        w_loc = []
        w_m1 = []
        w_m2 = []
        for comp in pool:
            wf = (WEEKDAY_FACTOR if comp["weekend"] != target_weekend
                  else 1.0)
            lp = comp["logp1"]
            w_loc.append(kernel_weight(logp1, lp, bw_loclin) * wf)
            w_m1.append(kernel_weight(logp1, lp, bw_mean1) * wf)
            w_m2.append(kernel_weight(logp1, lp, BW_MEAN2) * wf)
        loc = _loclin(pool, pos, logp1, w_loc)
        m1 = _weighted_mean(pool, pos, w_m1)
        m2 = _weighted_mean(pool, pos, w_m2)
        centre.append(W_LOCLIN * loc + W_MEAN1 * m1 + W_MEAN2 * m2)
    return centre


# ---------------------------------------------------------------------------
# Dip corrections (from the target's own diurnal profile)
# ---------------------------------------------------------------------------

def apply_dips(centre, diurnal, logp1):
    """Adjust the centre with the target's own pre-release pattern.

    dip[i] = diurnal[i+1] - diurnal[0] for forecast index i (0..22,
    hours 2..24). Early hours take the one-sided dip minus a size
    gate; midday hours take the dip, an inflation shift gated by the
    midday dip, and an echo of the early dip; evening hours take a
    small-launch lift. With no diurnal profile the dips are zero and
    infl is 8.0 (its term is zero anyway, since the midday gate is 0).
    """
    if diurnal is None:
        dips = [0.0] * 23
        infl = 8.0
    else:
        dips = [diurnal[i + 1] - diurnal[0] for i in range(23)]
        infl = min(logp1 - diurnal[0], 10.0)
    dip_early = sum(min(0.0, dips[i]) for i in (0, 1, 2)) / 3.0
    dip_mid = sum(min(0.0, dips[i]) for i in (9, 10, 11, 12, 13)) / 5.0
    gate_mid = min(max(-dip_mid / 0.10, 0.0), 1.0)
    size_gate = min(max(logp1 - SIZE_REF, 0.0), 1.0)
    evening_lift = EVENING_COEF * max(0.0, 11.0 - logp1)
    out = list(centre)
    for i in (0, 1, 2):
        out[i] += DIP_COEF * min(0.0, dips[i]) - EARLY_SIZE_COEF * size_gate
    for i in (9, 10, 11, 12, 13):
        out[i] += (DIP_COEF * min(0.0, dips[i])
                   - INFL_COEF * infl * gate_mid
                   + ECHO_COEF * dip_early)
    for i in range(15, 23):
        out[i] += evening_lift
    return out


# ---------------------------------------------------------------------------
# Quantiles and entry point
# ---------------------------------------------------------------------------

def _round_half_up_2dp(value):
    """value rounded to 2 decimals, half up (whole numbers come back
    as ints, as in the v5 reference)."""
    scaled = value * 100.0
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    rounded = whole / 100.0
    return int(rounded) if rounded == int(rounded) else rounded


def make_rows(centre, logp1):
    """Five quantiles per forecast hour from the log-ratio centre."""
    rows = []
    for i in range(23):
        mu = logp1 + centre[i]
        sigma = SIGMA[i]
        qs = []
        for z in Z_VALUES:
            q = _round_half_up_2dp(math.exp(mu + z * sigma))
            qs.append(min(max(q, 0.01), 1e9))
        for k in range(1, 5):
            if qs[k] < qs[k - 1]:
                qs[k] = qs[k - 1]
        rows.append({"hour": i + 2, "q": qs})
    return rows


def forecast_case(case, case_id):
    """Forecast one case. Returns {"hours": [...]} with 23 rows."""
    known = case.get("known") or []
    p1 = known[0].get("viewers", 1) if known else 1
    logp1 = math.log(max(1, p1))

    # Rule A3 (size-adaptive bandwidths): the effective bandwidths
    # depend on the TARGET's log hour-1 print, threaded as parameters.
    big = logp1 >= BIG_THRESHOLD
    bw_mean1 = BW_MEAN1_BIG if big else BW_MEAN1_SMALL
    bw_loclin = BW_LOCLIN_BIG if big else BW_LOCLIN_SMALL

    pool = build_pool(case)
    target_unlock = rolling_unlock(case["day_start"])
    target_weekend = parse_ts(case["day_start"]).weekday() >= 5
    diurnal = build_diurnal(case)
    if pool:
        centre = ensemble_centre(pool, logp1, target_unlock,
                                 target_weekend, bw_mean1, bw_loclin)
    else:
        centre = [0.0] * 23
    centre = apply_dips(centre, diurnal, logp1)
    return {"hours": make_rows(centre, logp1)}


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    for name in sorted(os.listdir(in_dir)):
        if not name.endswith(".in"):
            continue
        with open(os.path.join(in_dir, name), "r", encoding="utf-8") as f:
            case = json.load(f)
        out = forecast_case(case, case.get("id", name[:-3]))
        with open(os.path.join(out_dir, name[:-3] + ".out"), "w",
                  encoding="utf-8") as f:
            json.dump(out, f)


if __name__ == "__main__":
    main()
