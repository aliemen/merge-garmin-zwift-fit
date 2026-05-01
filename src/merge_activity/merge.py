from datetime import timedelta

import numpy as np
from garmin_fit_sdk import Profile

# Fields the Profile knows for these messages — used to drop sub-field expansions
# (e.g. `garmin_product`, which is decoded for convenience but is NOT a base field
# the Encoder accepts) and proprietary numeric-keyed fields.
_RECORD_BASE_FIELDS = {
    f["name"] for f in Profile["messages"][Profile["mesg_num"]["RECORD"]]["fields"].values()
}
_LAP_BASE_FIELDS = {
    f["name"] for f in Profile["messages"][Profile["mesg_num"]["LAP"]]["fields"].values()
}
_SESSION_BASE_FIELDS = {
    f["name"] for f in Profile["messages"][Profile["mesg_num"]["SESSION"]]["fields"].values()
}

# Zwift-sourced record fields layered on top of Garmin's record stream.
_ZWIFT_RECORD_FIELDS = (
    "position_lat",
    "position_long",
    "altitude",
    "enhanced_altitude",
    "distance",
    "speed",
    "enhanced_speed",
)


def _is_nan(x):
    return isinstance(x, float) and x != x


def _filter_known(mesg, allowed_names):
    """Prepare a decoded message for the Encoder:
    - Drop NaN values (Encoder rejects them; Garmin emits NaN for fields like
      `avg_flow` / `total_grit` on rides where they don't apply).
    - Drop `developer_fields` (Zwift's per-record dev-data — separate API).
    - Keep all string-keyed fields in `allowed_names`. String keys not in the
      set are sub-field expansions like `garmin_product` — drop those, the
      Encoder doesn't accept them as inputs.
    - **Rename integer-keyed proprietary fields to `field_<id>` strings** so
      they line up with the augmented Profile entries the Encoder uses to
      write Garmin's session / record / lap extension fields. Field 178 of
      the session, for example, is `sweat_loss` in mL — without this rename,
      it never makes it into the merged file.
    """
    out = {}
    for k, v in mesg.items():
        if k == "developer_fields":
            continue
        if _is_nan(v):
            continue
        if isinstance(v, list) and any(_is_nan(x) for x in v):
            cleaned = [x for x in v if not _is_nan(x)]
            if not cleaned:
                continue
            v = cleaned
        if isinstance(k, int):
            out[f"field_{k}"] = v
        elif isinstance(k, str) and k in allowed_names:
            out[k] = v
    return out


def merge_records(garmin_records, zwift_records, zwift_offset_s):
    """Master timeline = Garmin records. For each, interpolate Zwift's
    position / altitude / distance / speed and copy onto the Garmin record.

    `zwift_offset_s` is added to Zwift timestamps before interpolation, so the
    two streams compare in a common time frame.
    """
    if not zwift_records:
        return [_filter_known(r, _RECORD_BASE_FIELDS) for r in garmin_records]

    z_t = np.array(
        [r["timestamp"].timestamp() + zwift_offset_s for r in zwift_records],
        dtype=float,
    )
    # Build per-field arrays once
    z_arrays = {}
    for f in _ZWIFT_RECORD_FIELDS:
        vals = []
        for r in zwift_records:
            v = r.get(f)
            vals.append(float(v) if v is not None else np.nan)
        z_arrays[f] = np.asarray(vals, dtype=float)

    z_min, z_max = z_t[0], z_t[-1]
    merged = []
    for g in garmin_records:
        out = _filter_known(g, _RECORD_BASE_FIELDS)
        t = g["timestamp"].timestamp()
        if z_min <= t <= z_max:
            for f, arr in z_arrays.items():
                # np.interp ignores NaNs poorly; mask them
                mask = ~np.isnan(arr)
                if not mask.any():
                    continue
                v = float(np.interp(t, z_t[mask], arr[mask]))
                if f in ("position_lat", "position_long"):
                    out[f] = int(round(v))
                else:
                    out[f] = v
        merged.append(out)
    return merged


def _normalized_power(powers):
    """30 s rolling mean ^ 4 mean ^ 1/4. Standard NP definition."""
    p = np.asarray(powers, dtype=float)
    if len(p) == 0:
        return 0
    if len(p) < 30:
        return float(p.mean())
    kernel = np.ones(30) / 30
    rolling = np.convolve(p, kernel, mode="valid")
    return int(round(float((rolling ** 4).mean()) ** 0.25))


def _avg(records, field):
    vals = [r[field] for r in records if r.get(field) is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _maxv(records, field):
    vals = [r[field] for r in records if r.get(field) is not None]
    return max(vals) if vals else None


def build_laps(zwift_laps, merged_records, zwift_offset_s):
    """Use Zwift's lap structure (positions, distance, ascent), but recompute
    HR / power / cadence / pedal-dynamics aggregates from the merged record
    slice belonging to each lap window."""
    new_laps = []
    for i, zlap in enumerate(zwift_laps):
        # Translate Zwift lap times into the merged-record time frame (which
        # is Garmin's clock — same as the merged records' timestamps).
        gstart = zlap["start_time"] + timedelta(seconds=zwift_offset_s)
        elapsed = zlap.get("total_elapsed_time") or 0
        gend = gstart + timedelta(seconds=elapsed)
        slc = [r for r in merged_records if gstart <= r["timestamp"] < gend]

        candidate = {
            "message_index": i,
            "timestamp": gend,
            "start_time": gstart,
            "event": "lap",
            "event_type": "stop",
            "sport": "cycling",
            "sub_sport": "virtual_activity",
            "lap_trigger": zlap.get("lap_trigger") or "manual",
            "intensity": zlap.get("intensity") or "active",
            "total_elapsed_time": zlap.get("total_elapsed_time"),
            "total_timer_time": zlap.get("total_timer_time"),
            "total_distance": zlap.get("total_distance"),
            "total_ascent": zlap.get("total_ascent"),
            "total_descent": zlap.get("total_descent"),
            "start_position_lat": zlap.get("start_position_lat"),
            "start_position_long": zlap.get("start_position_long"),
            "end_position_lat": zlap.get("end_position_lat"),
            "end_position_long": zlap.get("end_position_long"),
        }

        if slc:
            ahr = _avg(slc, "heart_rate")
            if ahr is not None:
                candidate["avg_heart_rate"] = int(round(ahr))
                candidate["max_heart_rate"] = _maxv(slc, "heart_rate")
            powers = [r["power"] for r in slc if r.get("power") is not None]
            if powers:
                candidate["avg_power"] = int(round(sum(powers) / len(powers)))
                candidate["max_power"] = max(powers)
                candidate["normalized_power"] = _normalized_power(powers)
            acad = _avg(slc, "cadence")
            if acad is not None:
                candidate["avg_cadence"] = int(round(acad))
                candidate["max_cadence"] = _maxv(slc, "cadence")
            for k_in, k_out in (
                ("left_torque_effectiveness", "avg_left_torque_effectiveness"),
                ("right_torque_effectiveness", "avg_right_torque_effectiveness"),
                ("left_pedal_smoothness", "avg_left_pedal_smoothness"),
                ("right_pedal_smoothness", "avg_right_pedal_smoothness"),
                ("left_right_balance", "avg_left_right_balance"),
            ):
                v = _avg(slc, k_in)
                if v is not None and k_out in _LAP_BASE_FIELDS:
                    candidate[k_out] = v

        new_laps.append(_filter_known(candidate, _LAP_BASE_FIELDS))
    return new_laps


def _bounding_box(merged_records):
    """Return (start_lat, start_long, nec_lat, nec_long, swc_lat, swc_long) derived
    from the first record with GPS and the lat/long extremes across all records.
    Zwift's session message often leaves these at 0, so we fill them ourselves."""
    lats, lons = [], []
    start_lat = start_long = None
    for r in merged_records:
        lat, lon = r.get("position_lat"), r.get("position_long")
        if lat is None or lon is None:
            continue
        if start_lat is None:
            start_lat, start_long = lat, lon
        lats.append(lat)
        lons.append(lon)
    if not lats:
        return None, None, None, None, None, None
    return start_lat, start_long, max(lats), max(lons), min(lats), min(lons)


def build_session(garmin_session, zwift_session, merged_records, new_laps, zwift_offset_s):
    """Start from Garmin's session message and override only the Zwift-sourced
    geography (distance, ascent, positions, speeds) and `sub_sport`.

    Why no recomputation: the merged record stream IS Garmin's record stream
    (we only layered Zwift's GPS/altitude/distance on top), so Garmin's session
    aggregates — avg/max HR, power, cadence, normalized_power, total_calories,
    metabolic_calories, total_work, training_effect, threshold_power,
    seated/standing power-position arrays, pedal-phase data, respiration rate,
    etc. — are all still correct. Carrying them through verbatim is what makes
    Garmin Connect render the full activity detail (Kalorien-Aufschlüsselung,
    sitzend/stehend, Schweißverlust, etc.) instead of just a basic ride.
    """
    if not merged_records:
        return {}

    # Start with everything Garmin knows about this session.
    session = _filter_known(garmin_session, _SESSION_BASE_FIELDS)

    s_lat, s_lon, ne_lat, ne_lon, sw_lat, sw_lon = _bounding_box(merged_records)

    # Override Zwift-sourced fields.
    session["sub_sport"] = "virtual_activity"
    session["num_laps"] = len(new_laps)
    session["first_lap_index"] = 0
    if zwift_session.get("total_distance"):
        session["total_distance"] = zwift_session["total_distance"]
    if zwift_session.get("total_ascent") is not None:
        session["total_ascent"] = zwift_session["total_ascent"]
    if zwift_session.get("total_descent") is not None:
        session["total_descent"] = zwift_session["total_descent"]
    for k in ("enhanced_avg_speed", "enhanced_max_speed", "avg_speed", "max_speed"):
        v = zwift_session.get(k)
        if v:
            session[k] = v
    # Position fields: prefer Zwift session; else fall back to record-derived.
    session["start_position_lat"] = zwift_session.get("start_position_lat") or s_lat
    session["start_position_long"] = zwift_session.get("start_position_long") or s_lon
    session["nec_lat"] = zwift_session.get("nec_lat") or ne_lat
    session["nec_long"] = zwift_session.get("nec_long") or ne_lon
    session["swc_lat"] = zwift_session.get("swc_lat") or sw_lat
    session["swc_long"] = zwift_session.get("swc_long") or sw_lon

    return session
