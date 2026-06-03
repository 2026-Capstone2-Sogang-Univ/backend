from __future__ import annotations

from statistics import mean

from .simulation import RAW_SURGE_BUCKETS


def raw_surge_bucket(raw_surge: float) -> str:
    for bucket, lower, upper, _ in RAW_SURGE_BUCKETS:
        if raw_surge >= lower and (upper is None or raw_surge < upper):
            return bucket
    return RAW_SURGE_BUCKETS[-1][0]


def aggregate_surge_band_from_buckets(buckets: dict[str, dict]) -> dict:
    """Band KPIs from fast-bench summary buckets (no per-decision event list)."""
    out: dict = {}
    for bucket, _, _, target in RAW_SURGE_BUCKETS:
        row = buckets.get(bucket) or {}
        n = int(row.get("request_count") or 0)
        if n <= 0:
            out[f"band_{bucket}_dispatch_n"] = 0
            out[f"band_{bucket}_accept_rate"] = None
            out[f"band_{bucket}_target_p"] = target
            out[f"band_{bucket}_avg_p_actual"] = None
            out[f"band_{bucket}_p_error"] = None
            continue
        matched = int(row.get("matched_count") or 0)
        p_count = int(row.get("p_actual_count") or 0)
        p_sum = float(row.get("p_actual_sum") or 0.0)
        avg_p = p_sum / p_count if p_count else 0.0
        out[f"band_{bucket}_dispatch_n"] = n
        out[f"band_{bucket}_accept_rate"] = matched / n
        out[f"band_{bucket}_target_p"] = target
        out[f"band_{bucket}_avg_p_actual"] = avg_p
        out[f"band_{bucket}_p_error"] = avg_p - target
    return out


def aggregate_surge_band_metrics(decisions: list[dict]) -> dict:
    """Per raw_surge band: dispatch acceptance vs target P* (55/70/80/85%)."""
    bucket_targets = {b: t for b, _, _, t in RAW_SURGE_BUCKETS}
    by_bucket: dict[str, list[dict]] = {b: [] for b, _, _, _ in RAW_SURGE_BUCKETS}
    for decision in decisions:
        raw = decision.get("raw_surge")
        if raw is None:
            continue
        by_bucket[raw_surge_bucket(float(raw))].append(decision)

    out: dict = {}
    for bucket, rows in by_bucket.items():
        if not rows:
            out[f"band_{bucket}_dispatch_n"] = 0
            out[f"band_{bucket}_accept_rate"] = None
            out[f"band_{bucket}_target_p"] = bucket_targets[bucket]
            out[f"band_{bucket}_avg_p_actual"] = None
            out[f"band_{bucket}_p_error"] = None
            continue
        accepted = [r for r in rows if r.get("accepted")]
        p_vals = [float(r.get("p_actual", 0.0)) for r in rows]
        target = bucket_targets[bucket]
        out[f"band_{bucket}_dispatch_n"] = len(rows)
        out[f"band_{bucket}_accept_rate"] = len(accepted) / len(rows)
        out[f"band_{bucket}_target_p"] = target
        out[f"band_{bucket}_avg_p_actual"] = mean(p_vals)
        out[f"band_{bucket}_p_error"] = mean(p_vals) - target
    return out
