"""Forecast a big game launch's release-day Twitch viewers, hours 2-24.

Method (built and tuned on the four practice launches in the kit):

Every past launch in the case's `comparables` gives a ratio curve
r_h = (viewers in release-day hour h) / (viewers in hour 1).  A forecast for
the target launch is a set of quantiles of log r_h, scaled by the target's
own hour-1 print.  Two views of the comparables are blended 50/50 in log
space:

* pooled   -- every usable comparable counts equally;
* weighted -- comparables count by how similar their pre-release trajectory
  is to the target's.  The trajectory is the log viewer level, normalised by
  each launch's own hour-1 level, over the 24 hours before the release day
  plus hour 1 itself.  That window holds the rolling-midnight unlock
  fingerprint (New Zealand opens ~13 h before the UTC day, the curve ramps,
  then dips into the 00:00 UTC hour), and launches whose unlock worked
  differently (a single global unlock hour, an early-access head start)
  trace a visibly different shape and are down-weighted by a Gaussian kernel
  on the RMS log distance, sigma 0.45.

The blended quantiles are then narrowed around their median by a factor of
0.65: the raw cross-launch spread overstates how uncertain a
trajectory-matched forecast is, and on the practice launches the pinball
loss keeps improving as the band narrows to about this width.

Only the Python standard library is used, the program is deterministic, and
it reads every *.in file in $MAKEFUN_INPUT_DIR (default /input) and writes
<id>.out to $MAKEFUN_OUTPUT_DIR (default /output).
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
Z_BASELINE = (-1.645, -0.674, 0.0, 0.674, 1.645)
TRAJ_SIGMA = 0.45        # Gaussian kernel width for trajectory similarity
TRAJ_FALLBACK = 0.25     # weight when trajectories barely overlap
BLEND_WEIGHTED = 0.5     # share of the trajectory-weighted view in the blend
SPREAD_SCALE = 0.65      # narrowing of the blended band around its median
MIN_DAY_HOURS = 20       # a comparable needs most of its release day present


def parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def relative_hours(day_start, hourly):
    """Map 'hours since day_start' -> viewers for an hourly series."""
    start = parse_ts(day_start)
    rel = {}
    for point in hourly or []:
        viewers = point.get("viewers")
        if not viewers:
            continue
        idx = int((parse_ts(point["from"]) - start).total_seconds() // 3600)
        rel[idx] = float(viewers)
    return rel


def trajectory_distance(target_rel, target_p1, cand_rel, cand_p1):
    """RMS distance between two launches' normalised log trajectories
    over relative hours -24..0; None when they share too few hours."""
    diffs = []
    log_t = math.log(target_p1)
    log_c = math.log(cand_p1)
    for rh in range(-24, 1):
        t_val = target_rel.get(rh)
        c_val = cand_rel.get(rh)
        if t_val and c_val and t_val > 0 and c_val > 0:
            diffs.append((math.log(t_val) - log_t) - (math.log(c_val) - log_c))
    if len(diffs) < 6:
        return None
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


def weighted_quantile(pairs, level):
    """Interpolated weighted quantile of [(value, weight), ...]."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[-1][0]
    target = level * total
    acc = 0.0
    prev_value, prev_acc = pairs[0][0], 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= target:
            if acc == prev_acc:
                return value
            frac = (target - prev_acc) / (acc - prev_acc)
            return prev_value + (value - prev_value) * frac
        prev_value, prev_acc = value, acc
    return pairs[-1][0]


def baseline_row(p1, hour):
    """The house baseline's row: flat median, spread 0.15 * sqrt(h - 1).
    Used only when a case carries no usable comparable at all."""
    return [p1 * math.exp(z * 0.15 * math.sqrt(hour - 1)) for z in Z_BASELINE]


def target_trajectory(case):
    """The target's own relative-hour viewer levels up to the checkpoint.

    Normally these come from the case's history_hourly.  If that series is
    missing or too short to match trajectories against, rebuild whole-hour
    means from the raw history points instead (in the final those are the
    house's minute reads), so the trajectory matching still has its window.
    """
    rel = relative_hours(case["day_start"], case.get("history_hourly"))
    if len(rel) >= 6:
        return rel
    start = parse_ts(case["day_start"])
    buckets = {}
    for point in case.get("history") or []:
        viewers = point.get("viewers")
        at = point.get("at") or point.get("from")
        if not viewers or not at:
            continue
        idx = int((parse_ts(at) - start).total_seconds() // 3600)
        if -24 <= idx <= 0:
            buckets.setdefault(idx, []).append(float(viewers))
    rebuilt = {idx: sum(vals) / len(vals) for idx, vals in buckets.items()}
    return rebuilt if len(rebuilt) > len(rel) else rel


def hour_one_print(case):
    """Hour 1's print: the known row for hour 1, else the first known row."""
    known = case.get("known") or []
    for row in known:
        if row.get("hour") == 1:
            return float(row["viewers"])
    return float(known[0]["viewers"])


def forecast_hours(case):
    """The hours to forecast, taken from the case's own forecast rows."""
    hours = sorted({int(row["hour"]) for row in case.get("forecast") or []})
    return hours if hours else list(range(2, 25))


def forecast_case(case):
    p1 = hour_one_print(case)
    if p1 < 1.0:
        p1 = 1.0

    target_rel = target_trajectory(case)
    target_rel[0] = p1  # relative hour 0 is release-day hour 1 itself

    candidates = []  # (release-day {hour: viewers}, trajectory weight)
    for comp in case.get("comparables") or []:
        rel = relative_hours(comp["day_start"], comp.get("hourly"))
        day = {i + 1: rel[i] for i in range(24) if i in rel}
        if len(day) < MIN_DAY_HOURS or day.get(1, 0) <= 0:
            continue
        dist = trajectory_distance(target_rel, p1, rel, day[1])
        if dist is None:
            weight = TRAJ_FALLBACK
        else:
            weight = math.exp(-(dist * dist) / (2.0 * TRAJ_SIGMA ** 2))
        candidates.append((day, weight))

    rows = []
    for hour in forecast_hours(case):
        if not candidates:
            quantiles = baseline_row(p1, hour)
        else:
            weighted_pairs = []
            pooled_pairs = []
            for day, weight in candidates:
                if hour in day:
                    log_ratio = math.log(day[hour] / day[1])
                    weighted_pairs.append((log_ratio, weight))
                    pooled_pairs.append((log_ratio, 1.0))
            if not weighted_pairs:
                quantiles = baseline_row(p1, hour)
            else:
                logs = []
                for level in LEVELS:
                    wq = weighted_quantile(weighted_pairs, level)
                    pq = weighted_quantile(pooled_pairs, level)
                    logs.append(BLEND_WEIGHTED * wq + (1.0 - BLEND_WEIGHTED) * pq)
                median = logs[2]
                logs = [median + (v - median) * SPREAD_SCALE for v in logs]
                quantiles = [p1 * math.exp(v) for v in logs]
        # enforce the output contract: finite, > 0, <= 1e9, non-decreasing
        cleaned = []
        for q in quantiles:
            q = min(max(float(q), 1e-6), 1e9)
            if cleaned and q < cleaned[-1]:
                q = cleaned[-1]
            cleaned.append(round(q, 2))
        rows.append({"hour": hour, "q": cleaned})
    return {"hours": rows}


def main():
    in_dir = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    out_dir = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(in_dir.glob("*.in")):
        with open(path, encoding="utf-8") as fh:
            case = json.load(fh)
        result = forecast_case(case)
        out_path = out_dir / (path.name[:-3] + ".out")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
