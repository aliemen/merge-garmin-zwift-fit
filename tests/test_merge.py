import subprocess
import sys
from pathlib import Path

import pytest
from garmin_fit_sdk import Decoder, Stream

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def _pick_one(prefix):
    """Find a single tests/<prefix>*.fit file. Tests are skipped if no
    matching file is present (see tests/README.md for how to drop in your
    own fixtures); they fail loudly if the user has multiple candidates so
    we don't silently pick the wrong one."""
    matches = sorted(TESTS.glob(f"{prefix}*.fit"))
    if not matches:
        pytest.skip(
            f"no tests/{prefix}*.fit fixture found — see tests/README.md "
            "for instructions on adding your own"
        )
    if len(matches) > 1:
        pytest.fail(
            f"multiple tests/{prefix}*.fit fixtures found — keep just one "
            f"(or use {prefix.lower()}.fit): {[m.name for m in matches]}"
        )
    return matches[0]


@pytest.fixture(scope="module")
def GARMIN():
    return _pick_one("Garmin")


@pytest.fixture(scope="module")
def ZWIFT():
    return _pick_one("Zwift")


def _decode(path):
    s = Stream.from_file(str(path))
    return Decoder(s).read()


@pytest.fixture(scope="module")
def merged(tmp_path_factory, GARMIN, ZWIFT):
    out = tmp_path_factory.mktemp("merge") / "merged.fit"
    rc = subprocess.call(
        [
            sys.executable,
            "-m",
            "merge_activity",
            "--garmin",
            str(GARMIN),
            "--zwift",
            str(ZWIFT),
            "-o",
            str(out),
        ],
        cwd=str(ROOT),
    )
    assert rc == 0
    assert out.exists() and out.stat().st_size > 1000
    return out


def test_round_trip_decodes_clean(merged):
    msgs, errors = _decode(merged)
    assert errors == [], errors
    assert "file_id_mesgs" in msgs
    assert "record_mesgs" in msgs
    assert "lap_mesgs" in msgs
    assert "session_mesgs" in msgs


def test_garmin_timeline_preserved(merged, GARMIN):
    msgs, _ = _decode(merged)
    g, _ = _decode(GARMIN)
    assert len(msgs["record_mesgs"]) == len(g["record_mesgs"])


def test_gps_layered_in(merged):
    msgs, _ = _decode(merged)
    n_pos = sum(1 for r in msgs["record_mesgs"] if r.get("position_lat") is not None)
    assert n_pos / len(msgs["record_mesgs"]) >= 0.8, "expected ≥80% records with GPS"


def test_garmin_physiology_preserved(merged, GARMIN):
    msgs, _ = _decode(merged)
    g, _ = _decode(GARMIN)
    g_hr = [r for r in g["record_mesgs"] if r.get("heart_rate") is not None]
    m_hr = [r for r in msgs["record_mesgs"] if r.get("heart_rate") is not None]
    assert len(m_hr) == len(g_hr)
    g_pwr = [r for r in g["record_mesgs"] if r.get("power") is not None]
    m_pwr = [r for r in msgs["record_mesgs"] if r.get("power") is not None]
    assert len(m_pwr) == len(g_pwr)


def test_session_is_virtual_activity(merged):
    msgs, _ = _decode(merged)
    s = msgs["session_mesgs"][0]
    assert s["sport"] == "cycling"
    assert s["sub_sport"] == "virtual_activity"


def test_zwift_lap_count_wins(merged, ZWIFT):
    msgs, _ = _decode(merged)
    z, _ = _decode(ZWIFT)
    assert len(msgs["lap_mesgs"]) == len(z["lap_mesgs"])


def test_session_aggregates_close_to_sources(merged, GARMIN, ZWIFT):
    msgs, _ = _decode(merged)
    g, _ = _decode(GARMIN)
    z, _ = _decode(ZWIFT)
    s = msgs["session_mesgs"][0]
    # Distance should match Zwift's session distance within 1%
    z_dist = z["session_mesgs"][0]["total_distance"]
    assert abs(s["total_distance"] - z_dist) / z_dist < 0.01
    # Avg HR should be very close to Garmin's session avg (within 2 bpm —
    # rounding differences from per-record averaging are expected).
    assert abs(s["avg_heart_rate"] - g["session_mesgs"][0]["avg_heart_rate"]) <= 2


def test_garmin_proprietary_mesgs_preserved(merged, GARMIN):
    """Garmin's proprietary message types (Performance Condition, Body Battery,
    Stamina, sweat-loss summary, etc.) must round-trip into the merged file.
    They appear under stringified-int keys in the decoded dict."""
    msgs, _ = _decode(merged)
    g, _ = _decode(GARMIN)
    proprietary_in_src = {k for k in g if isinstance(k, str) and k.isdigit()}
    proprietary_in_merged = {k for k in msgs if isinstance(k, str) and k.isdigit()}
    # Every proprietary mesg type from the source must exist in the merged.
    assert proprietary_in_src <= proprietary_in_merged, (
        f"missing in merged: {proprietary_in_src - proprietary_in_merged}"
    )
    # And per-message-type counts should match (we passed all instances through).
    for k in proprietary_in_src:
        assert len(msgs[k]) == len(g[k]), (
            f"mesg {k}: {len(g[k])} in source, {len(msgs[k])} in merged"
        )


def test_garmin_metabolic_calories_preserved(merged, GARMIN):
    """`metabolic_calories` is what GC uses for the rest/active calorie split."""
    msgs, _ = _decode(merged)
    g, _ = _decode(GARMIN)
    assert msgs["session_mesgs"][0].get("metabolic_calories") == \
        g["session_mesgs"][0].get("metabolic_calories")


def test_all_garmin_device_infos_preserved(merged, GARMIN):
    msgs, _ = _decode(merged)
    g, _ = _decode(GARMIN)
    # Merged has Garmin's entries + 1 Zwift entry appended.
    assert len(msgs["device_info_mesgs"]) == len(g["device_info_mesgs"]) + 1
