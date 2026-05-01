import argparse
import secrets
import sys

from . import __version__
from .align import estimate_offset
from .decode import decode_fit
from .encode import write_fit
from .merge import build_laps, build_session, merge_records


def _need(messages, key, label):
    if not messages.get(key):
        sys.exit(f"error: {label} has no `{key}` — is this a valid activity FIT file?")
    return messages[key]


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="merge-activity",
        description="Merge a Zwift .fit file (virtual GPS / laps) into a Garmin .fit file (HR / power / pedal dynamics).",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--garmin", required=True, help="Garmin activity .fit file")
    p.add_argument("--zwift", required=True, help="Zwift activity .fit file")
    p.add_argument("-o", "--output", required=True, help="path for the merged .fit output")
    p.add_argument(
        "--offset",
        type=float,
        default=None,
        metavar="SECONDS",
        help="manual offset in seconds to add to Zwift timestamps; skips auto-correlation",
    )
    p.add_argument(
        "--no-auto-align",
        action="store_true",
        help="disable cross-correlation; trust the timestamps as-is",
    )
    p.add_argument(
        "--randomize-id",
        action="store_true",
        help=(
            "give the merged file a random `file_id.serial_number` so Strava /"
            " Garmin Connect treat it as a brand-new activity (lets you upload"
            " the merged file alongside the original before deleting it)"
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    garmin, g_errs, garmin_defs = decode_fit(args.garmin)
    zwift, z_errs, _ = decode_fit(args.zwift)
    for label, errs in (("garmin", g_errs), ("zwift", z_errs)):
        for e in errs:
            print(f"warning: {label} decode: {e}", file=sys.stderr)

    g_records = _need(garmin, "record_mesgs", "Garmin file")
    z_records = _need(zwift, "record_mesgs", "Zwift file")
    z_laps = _need(zwift, "lap_mesgs", "Zwift file")
    g_session = _need(garmin, "session_mesgs", "Garmin file")[0]
    z_session = _need(zwift, "session_mesgs", "Zwift file")[0]

    if args.offset is not None:
        offset, conf, source = float(args.offset), 1.0, "manual"
    elif args.no_auto_align:
        offset, conf, source = 0.0, 0.0, "trust-timestamps"
    else:
        offset, conf, source = estimate_offset(g_records, z_records)
        if source is None:
            print(
                "warning: could not estimate Zwift offset (cadence + HR both too weak); using 0",
                file=sys.stderr,
            )
            offset, conf, source = 0.0, 0.0, "trust-timestamps"
    print(f"zwift offset: {offset:+.0f}s  confidence={conf:.2f}  via={source}")

    merged = merge_records(g_records, z_records, offset)
    new_laps = build_laps(
        z_laps, merged, offset,
        garmin_laps=garmin.get("lap_mesgs"),
        garmin_events=garmin.get("event_mesgs"),
        garmin_session=g_session,
    )
    new_session = build_session(g_session, z_session, merged, new_laps, offset)

    serial_override = secrets.randbits(32) if args.randomize_id else None
    if serial_override is not None:
        print(f"randomized file_id.serial_number = {serial_override}")

    _, n_proprietary = write_fit(
        args.output,
        garmin_messages=garmin,
        zwift_messages=zwift,
        merged_records=merged,
        new_laps=new_laps,
        new_session=new_session,
        garmin_definitions=garmin_defs,
        file_id_serial_override=serial_override,
    )

    n_pos = sum(1 for r in merged if r.get("position_lat") is not None)
    print(
        f"wrote {args.output}: {len(merged)} records, "
        f"{n_pos} with GPS, {len(new_laps)} laps, "
        f"{n_proprietary} proprietary mesgs preserved"
    )


if __name__ == "__main__":
    main()
