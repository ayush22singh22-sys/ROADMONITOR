"""Per-road-segment aggregation of snap-matched events.

For every net segment with events:
  - event counts per type, mean/p95 severity
  - UNIQUE reporting trips + distinct vehicle types
  - confirmed only when >= min_confirm_trips (config, default 3) INDEPENDENT
    trips - independence = distinct trip_id, i.e., distinct recorded runs
  - low-data handling for the 150 m privacy trim: the app never records the
    first/last ~150 m of a trip, so the segments NEAR a trip's recorded
    start/end boundary are systematically under-sampled. An event inside
    boundary_grace_m of its trip's first or last INGESTED window is flagged
    'boundary' and cannot, by itself, confirm a segment.

Writes segment_stats; priority.py then adds the priority score.
"""
import json
from datetime import datetime, timezone


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    i = p / 100.0 * (len(sorted_vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] * (1 - (i - lo)) + sorted_vals[hi] * (i - lo)


def _trip_boundaries(store, grace_m: float) -> dict:
    """trip_id -> (first_lat, first_lon, last_lat, last_lon) from the
    ingested 10 m windows (segments table rows the app managed to record)."""
    from ..geo import haversine_m
    out = {}
    for trip_id, in store.conn.execute(
            "SELECT DISTINCT trip_id FROM segments").fetchall():
        rows = store.conn.execute(
            "SELECT lat, lon FROM segments WHERE trip_id=? AND lat IS NOT NULL"
            " ORDER BY id", (trip_id,)).fetchall()
        if not rows:
            continue
        first, last = rows[0], rows[-1]
        if len(rows) == 1 or haversine_m(first[0], first[1], last[0], last[1]) \
                <= 2 * grace_m:
            out[trip_id] = None                 # tiny/looped drive: boundaries unusable
        else:
            out[trip_id] = (first[0], first[1], last[0], last[1])
    return out


def _near_boundary(ev: dict, bounds: dict, grace_m: float) -> bool:
    from ..geo import haversine_m
    b = bounds.get(ev["trip_id"])
    if b is None or ev["lat"] is None or ev["lon"] is None:
        return False
    d_first = haversine_m(ev["lat"], ev["lon"], b[0], b[1])
    d_last = haversine_m(ev["lat"], ev["lon"], b[2], b[3])
    return d_first <= grace_m or d_last <= grace_m


def compute(store, cfg) -> dict:
    """Full rebuild of segment_stats from anomaly_events + net_segments."""
    min_trips = cfg["anomaly"].get("min_confirm_trips", 3)
    grace = cfg["anomaly"].get("boundary_grace_m", 200.0)
    rows = store.conn.execute(
        "SELECT event_type, severity, trip_id, vehicle_type, segment_id,"
        " lat, lon, ts FROM anomaly_events"
        " WHERE segment_id IS NOT NULL AND severity IS NOT NULL"
    ).fetchall()
    bounds = _trip_boundaries(store, grace)

    by_seg = {}
    for r in rows:
        e = dict(r)
        e["boundary"] = _near_boundary(e, bounds, grace)
        by_seg.setdefault(e["segment_id"], []).append(e)

    updated = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for seg_id, evs in by_seg.items():
        # confirmation needs enough trips reporting OUTSIDE the trim boundary
        solid_trips = {e["trip_id"] for e in evs if not e["boundary"]}
        trips = {e["trip_id"] for e in evs}
        vtypes = {e["vehicle_type"] for e in evs if e["vehicle_type"]}
        sevs = sorted(e["severity"] for e in evs)
        counts = {}
        for e in evs:
            counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
        confirmed = len(solid_trips) >= min_trips
        stat = {
            "segment_id": seg_id,
            "n_events": len(evs),
            "n_trips": len(trips),
            "solid_trip_count": len(solid_trips),
            "n_vehicle_types": len(vtypes),
            "sev_mean": round(sum(sevs) / len(sevs), 4),
            "sev_p95": round(_pct(sevs, 95), 4),
            "confirmed": int(confirmed),
            "low_coverage": int(not confirmed),
            "boundary_events": sum(1 for e in evs if e["boundary"]),
            "event_counts": counts,
            "updated_at": now,
        }
        updated[seg_id] = stat

    with store.lock:
        store.conn.execute("DELETE FROM segment_stats")
        for st in updated.values():
            counts = st.pop("event_counts")
            store.conn.execute(
                "INSERT INTO segment_stats (segment_id, n_events, n_trips,"
                " n_vehicle_types, sev_mean, sev_p95, confirmed, priority,"
                " components, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (st["segment_id"], st["n_events"], st["n_trips"],
                 st["n_vehicle_types"], st["sev_mean"], st["sev_p95"],
                 st["confirmed"], st["priority"] if st.get("priority") else None,
                 json.dumps({**st, "event_counts": counts}), st["updated_at"]))
        store.conn.commit()
    return {"segments_with_events": len(updated),
            "confirmed": sum(1 for st in updated.values() if st["confirmed"])}
