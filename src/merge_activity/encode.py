from contextlib import contextmanager

from garmin_fit_sdk import Encoder, Profile

N = Profile["mesg_num"]

# FIT base-type id (low 5 bits of the byte) -> Profile string name. This is
# the canonical mapping from the FIT spec; the SDK uses these strings to look
# up encoding logic.
_BASE_TYPE = {
    0: "enum", 1: "sint8", 2: "uint8", 3: "sint16", 4: "uint16",
    5: "sint32", 6: "uint32", 7: "string", 8: "float32", 9: "float64",
    10: "uint8z", 11: "uint16z", 12: "uint32z", 13: "byte",
    14: "sint64", 15: "uint64", 16: "uint64z",
}


def _synth_field(fd):
    """Build a synthetic Profile field definition from a captured FIT field
    definition record."""
    fid = fd["field_id"]
    bt = _BASE_TYPE.get(fd["base_type"] & 0x1F, "uint8")
    is_array = fd["num_field_elements"] > 1
    return {
        "num": fid,
        "name": f"field_{fid}",
        "type": bt,
        "base_type": bt,
        "array": "true" if is_array else "false",
        "scale": [1],
        "offset": [0],
        "units": "",
        "bits": [],
        "components": [],
        "is_accumulated": False,
        "has_components": False,
        "sub_fields": [],
    }


def _synth_profile_entry(num, defn):
    """Build a complete Profile['messages'][num] entry from a captured
    FIT message definition record (used for fully-unknown mesg_nums)."""
    return {
        "num": str(num),
        "name": f"mesg_{num}",
        "messages_key": f"mesg_{num}_mesgs",
        "fields": {fd["field_id"]: _synth_field(fd) for fd in defn["field_definitions"]},
    }


@contextmanager
def _profile_patched_with(definitions):
    """Temporarily extend the bundled Profile with everything we captured from
    the source FIT file's message-definition records:

    1. For mesg_nums NOT in Profile (Garmin's proprietary types — 140, 233,
       etc.) — synthesize a complete entry.
    2. For mesg_nums IN Profile (record / session / lap) — add only the
       proprietary FIELDS from the captured definition that aren't already
       in Profile. This is how Garmin smuggles sweat_loss, Performance
       Condition, Body Battery, etc. into otherwise-standard messages: as
       integer-keyed extension fields on `session`, `record`, `lap`.

    Restores Profile on exit so we don't leak state across runs."""
    new_mesgs = []
    added_fields = []  # list of (mesg_num, field_id)
    try:
        for num, defn in definitions.items():
            if num not in Profile["messages"]:
                Profile["messages"][num] = _synth_profile_entry(num, defn)
                new_mesgs.append(num)
            else:
                existing = Profile["messages"][num]["fields"]
                for fd in defn["field_definitions"]:
                    fid = fd["field_id"]
                    if fid not in existing:
                        existing[fid] = _synth_field(fd)
                        added_fields.append((num, fid))
        yield new_mesgs
    finally:
        for n in new_mesgs:
            Profile["messages"].pop(n, None)
        for num, fid in added_fields:
            Profile["messages"][num]["fields"].pop(fid, None)


def _proprietary_passthrough(enc, garmin_messages, injected_nums):
    """Write Garmin's proprietary messages (Performance Condition, Body
    Battery, Stamina, sweat-loss summary, etc.) using the synthesized Profile
    entries. The decoder stored their fields under the integer-keyed `str(num)`
    bucket; we rename those keys to match the synthesized field_<id> names."""
    written = 0
    for num in injected_nums:
        for src in garmin_messages.get(str(num)) or []:
            payload = {f"field_{k}": v for k, v in src.items()}
            try:
                enc.write_mesg({"mesg_num": num, **payload})
                written += 1
            except Exception:
                # If a single proprietary message fails (rare), skip it
                # rather than fail the whole merge.
                pass
    return written

# Garmin auxiliary messages that we pass through verbatim. Without these,
# Garmin Connect can't compute / display:
#   - resting calories ("Kalorien in Ruhe") — needs user_profile (BMR inputs)
#   - sweat loss ("Schweißverlust")          — needs user_profile + zones_target
#   - HR / power zone breakdown chart        — needs time_in_zone + zones_target
#   - HRV-derived recovery / training status — needs hrv
#   - Connected sensors panel (HRM, pedals)  — needs all device_info messages
_PASSTHROUGH = (
    ("user_profile_mesgs", "USER_PROFILE"),
    ("sport_mesgs", "SPORT"),
    ("zones_target_mesgs", "ZONES_TARGET"),
    ("training_settings_mesgs", "TRAINING_SETTINGS"),
    ("device_settings_mesgs", "DEVICE_SETTINGS"),
    ("timestamp_correlation_mesgs", "TIMESTAMP_CORRELATION"),
    ("device_aux_battery_info_mesgs", "DEVICE_AUX_BATTERY_INFO"),
)


def _file_id(garmin_file_id, serial_override=None):
    return {
        "type": "activity",
        "manufacturer": garmin_file_id.get("manufacturer", "garmin"),
        "product": garmin_file_id.get("product", 0),
        "serial_number": serial_override if serial_override is not None
                         else garmin_file_id.get("serial_number", 0),
        "time_created": garmin_file_id.get("time_created"),
    }


def _file_creator(garmin_file_creator):
    """Carry through Garmin's file_creator if present so Garmin Connect knows
    the activity originated from Garmin firmware (affects which ingest pipeline
    runs and which derived metrics get computed)."""
    if garmin_file_creator:
        return dict(garmin_file_creator[0])
    return {"software_version": 100, "hardware_version": 0}


def _zwift_device_info(zwift_messages):
    """Append a device_info entry for Zwift. device_index 9 is safe — Garmin's
    indices in our fixture top out at 8."""
    z_dis = zwift_messages.get("device_info_mesgs") or []
    if not z_dis:
        return None
    z = z_dis[0]
    return {
        "timestamp": z.get("timestamp"),
        "device_index": 9,
        "manufacturer": z.get("manufacturer", "zwift"),
        "product": z.get("product", 0),
        "serial_number": z.get("serial_number", 0),
        "source_type": "local",
    }


def _events(merged_records):
    if not merged_records:
        return []
    return [
        {
            "timestamp": merged_records[0]["timestamp"],
            "event": "timer",
            "event_type": "start",
            "event_group": 0,
        },
        {
            "timestamp": merged_records[-1]["timestamp"],
            "event": "timer",
            "event_type": "stop_all",
            "event_group": 0,
        },
    ]


def _activity(session_mesg):
    return {
        "timestamp": session_mesg.get("timestamp"),
        "local_timestamp": 0,
        "total_timer_time": session_mesg.get("total_timer_time"),
        "num_sessions": 1,
        "type": "manual",
        "event": "activity",
        "event_type": "stop",
        "event_group": 0,
    }


def _try_write(enc, mesg_num, payload):
    """Write a passthrough message, dropping unknown fields the Encoder may
    choke on. The Encoder silently skips integer-keyed unknown fields, but
    string-keyed sub-field expansions can still raise. Best-effort."""
    try:
        enc.write_mesg({"mesg_num": mesg_num, **payload})
    except Exception:
        # Strip anything that isn't a Profile-known base field for this mesg.
        fields = Profile["messages"].get(mesg_num, {}).get("fields", {})
        names = {f["name"] for f in fields.values()}
        safe = {k: v for k, v in payload.items() if isinstance(k, str) and k in names}
        try:
            enc.write_mesg({"mesg_num": mesg_num, **safe})
        except Exception:
            pass  # last resort: drop this one message


def write_fit(
    out_path,
    *,
    garmin_messages,
    zwift_messages,
    merged_records,
    new_laps,
    new_session,
    garmin_definitions=None,
    file_id_serial_override=None,
):
    """Write the merged FIT file.

    `garmin_definitions` (the captured-from-decode dict of mesg_num ->
    definition record) lets us pass through Garmin's proprietary message
    types (Performance Condition, Body Battery, Stamina, sweat-loss summary,
    etc.) by synthesizing Profile entries the SDK Encoder will then accept.
    """
    with _profile_patched_with(garmin_definitions or {}) as injected:
        enc = Encoder()

        enc.write_mesg({
            "mesg_num": N["FILE_ID"],
            **_file_id(garmin_messages["file_id_mesgs"][0], file_id_serial_override),
        })
        enc.write_mesg({
            "mesg_num": N["FILE_CREATOR"],
            **_file_creator(garmin_messages.get("file_creator_mesgs")),
        })

        # All Garmin-side device_infos (HRM strap, pedals, Di2, etc.) plus a
        # Zwift one.
        for di in garmin_messages.get("device_info_mesgs") or []:
            _try_write(enc, N["DEVICE_INFO"], di)
        z_di = _zwift_device_info(zwift_messages)
        if z_di:
            _try_write(enc, N["DEVICE_INFO"], z_di)

        # Garmin's auxiliary preamble messages.
        for key, name in _PASSTHROUGH:
            for m in garmin_messages.get(key) or []:
                _try_write(enc, N[name], m)

        events = _events(merged_records)
        if events:
            enc.write_mesg({"mesg_num": N["EVENT"], **events[0]})

        for r in merged_records:
            enc.write_mesg({"mesg_num": N["RECORD"], **r})

        # Garmin's proprietary messages — written here, between records and
        # laps, so per-record proprietary streams (e.g. mesg 233, 1420 entries
        # for a 1374-record file) sit adjacent to the records they pair with.
        n_prop = _proprietary_passthrough(enc, garmin_messages, injected)

        for lap in new_laps:
            enc.write_mesg({"mesg_num": N["LAP"], **lap})

        # Session-level time_in_zone (HR + power zone breakdown chart). Drop
        # lap-level entries — their `reference_index` points at Garmin's old
        # laps, which don't exist in the merged file.
        for tz in garmin_messages.get("time_in_zone_mesgs") or []:
            if tz.get("reference_mesg") == "session":
                _try_write(enc, N["TIME_IN_ZONE"], tz)

        # HRV interval data — used by Garmin Connect for stress / recovery.
        for h in garmin_messages.get("hrv_mesgs") or []:
            _try_write(enc, N["HRV"], h)

        if events:
            enc.write_mesg({"mesg_num": N["EVENT"], **events[1]})

        enc.write_mesg({"mesg_num": N["SESSION"], **new_session})
        enc.write_mesg({"mesg_num": N["ACTIVITY"], **_activity(new_session)})

        data = enc.close()

    with open(out_path, "wb") as f:
        f.write(data)
    return data, n_prop
