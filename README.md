# merge-activity

Merge a Zwift `.fit` file with a Garmin `.fit` file into a single, complete virtual ride.

## Why

If you ride indoors on Zwift while wearing a Garmin watch and a power meter / HRM, you end up with **two parallel recordings of the same workout**:

- **Zwift** captures your virtual GPS track, in-game distance, virtual elevation, speed, and the lap structure from in-game segments / events.
- **Garmin** captures your real heart rate, power, pedal dynamics (left/right balance, torque effectiveness, pedal smoothness, power phase, PCO), respiration rate, training effect, and a long list of proprietary metrics the watch firmware computes on the fly (Body Battery delta, Performance Condition, Stamina, sweat loss, …).

Neither file alone tells the full story. Garmin Connect shows your physiology but no map and no Zwift laps. Strava shows the Zwift world but flat HR / no power-meter accuracy / no pedal dynamics. `merge-activity` produces a single `.fit` that any platform (Garmin Connect, Strava, TrainingPeaks, intervals.icu, …) reads as one rich virtual activity.

## What it does

The Garmin file is the **master timeline**. The merged file:

- keeps every Garmin record (1 Hz timestamps, HR, power, cadence, temperature, pedal-dynamics, respiration, all proprietary device telemetry),
- layers Zwift's `position_lat`, `position_long`, `altitude`, `enhanced_altitude`, `distance`, `speed`, `enhanced_speed` onto each Garmin record by linear interpolation between the two flanking Zwift samples,
- replaces Garmin's lap structure with Zwift's (e.g. you used in-game lap markers / sprint segments) and **recomputes** every per-lap aggregate Garmin Connect knows how to display, including time standing / sitting, seated/standing power, calories, ascent/descent, pedal-phase length, etc.,
- updates the session message: `sub_sport = virtual_activity`, Zwift's distance / ascent / descent / start position, plus a record-derived bounding box for map rendering,
- carries Garmin's proprietary message types (Performance Condition, Body Battery, Stamina, sweat-loss summary, …) through verbatim by reading the source file's embedded message-definition records and synthesizing matching Profile entries on the fly,
- preserves all 18 Garmin device_info entries (HRM strap, power-meter pedals, electronic shifting, …) plus the full event stream (rider-position changes, gear-shift markers, …) so the connected-sensors panel and cycling-dynamics graphs render correctly.

## Install

```bash
git clone https://github.com/<you>/merge-activity.git
cd merge-activity
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires Python 3.10+. Runtime deps: [`garmin-fit-sdk`](https://pypi.org/project/garmin-fit-sdk/) (Apache 2.0, official Garmin Python SDK) and [`numpy`](https://pypi.org/project/numpy/).

## Usage

```bash
merge-activity \
    --garmin Garmin_22716924242_ACTIVITY.fit \
    --zwift Zwift_2026-04-30-19-50-07.fit \
    --output merged.fit
```

The default behaviour cross-correlates a 1 Hz cadence series from each file (HR fallback) to estimate the offset between the two device clocks, prints the offset and confidence to stdout, then writes the merged file.

### Common workflow: re-uploading without overwriting the original

Garmin Connect and Strava deduplicate uploads by `file_id.serial_number + time_created`. If you've already synced the Garmin activity through your watch, uploading the merged file with the same identity will be rejected as a duplicate or overwrite the original. Pass `--randomize-id` to give the merged file a fresh 32-bit serial number while keeping the real `time_created`:

```bash
merge-activity --garmin G.fit --zwift Z.fit -o merged.fit --randomize-id
```

That uploads as a new activity sitting alongside the original, which you can then delete once you've verified everything looks right.

### All flags

| Flag | Description |
|---|---|
| `--garmin PATH` | Garmin activity .fit file (required) |
| `--zwift PATH` | Zwift activity .fit file (required) |
| `-o, --output PATH` | Output path for the merged .fit (required) |
| `--randomize-id` | Use a random `file_id.serial_number` so the merged file uploads alongside the original instead of being deduplicated |
| `--offset SECONDS` | Manually specify the offset to add to Zwift timestamps; skips auto-correlation |
| `--no-auto-align` | Trust both clocks as-is; equivalent to `--offset 0` |
| `-v, --verbose` | More logging |

## How it works

### Time alignment

Both devices' clocks are usually NTP/satellite-synced to within a second or two, but auto-correlation handles the cases where they aren't. The default behaviour:

1. Resample the cadence stream from each file onto a common 1 Hz grid (mean-fill outside each file's actual range so padding doesn't bias the correlation).
2. Cross-correlate over a ±120 s lag window.
3. If the normalized peak is above a confidence threshold (0.30), use that lag. Otherwise fall back to HR. If both are too weak (no power meter / no HRM), warn and trust the timestamps.

See [src/merge_activity/align.py](src/merge_activity/align.py).

### Per-record merge

Garmin's `record_mesgs` define the master timeline. For each Garmin timestamp the tool linearly interpolates Zwift's `position_lat`, `position_long`, `enhanced_altitude`, `distance`, `enhanced_speed` between the two flanking Zwift samples and writes them into the record. Garmin's HR / power / pedal-dynamics / proprietary fields are preserved verbatim. See [src/merge_activity/merge.py](src/merge_activity/merge.py) `merge_records`.

### Per-lap rebuild

Zwift's lap structure wins (typical case: you use in-game lap markers, segment events, or workout intervals — much more useful than Garmin's auto-laps). For each Zwift lap the tool slices the merged record stream by the lap's time window and recomputes:

- HR / power / NP / cadence (avg / max), torque-effectiveness, pedal-smoothness, L/R balance, PCO, temperature (avg/max/min), respiration (avg/max/min), fractional cadence, total cycles, total work,
- `total_calories` proxied from `session.total_calories × lap_work / session.total_work` (Garmin's actual algorithm uses HR + power + user profile; the proxy adds up to the session total within rounding),
- `total_ascent` / `total_descent` from positive / negative altitude deltas across the lap's records — Zwift fills these only at the session level and leaves per-lap values at zero,
- `time_standing`, `stand_count`, `avg_power_position`, `max_power_position`, `avg_cadence_position`, `max_cadence_position` derived by walking the `rider_position_change` events alongside the records to tag each record seated or standing.

The 4-element `avg_left_power_phase` / `avg_left_power_phase_peak` arrays (and right counterparts) carry [start_angle, end_angle, length, peak_position]. Records only carry the 2-element [start, end] form — Garmin's device-side firmware computes the length and peak position from a finer-grained model that's not exposed in the FIT record stream. These four fields are copied from the Garmin lap with the largest time overlap with the current Zwift lap.

### Garmin's proprietary message types

This is the part that took the longest to get right. Garmin smuggles a lot of data into FIT files in two places:

1. **Whole message types not in the public Profile** — for the test fixture there were 12 such mesg_nums (140, 141, 147, 233, 288, 326, 327, 394, 22, 79, 104, 113) carrying things like the post-activity summary, per-second Body Battery / Performance Condition / Stamina, gear-shift records, and so on. Mesg 233 alone has 1420 entries (one per record).
2. **Integer-keyed extension fields inside otherwise-standard messages** — `session.field_178 = 373` is the sweat loss in mL; `session.fields 205-216` are Performance Condition / Stamina averages; `record.fields 134-144` are per-second Body Battery / Performance Condition / Stamina; `device_info.field_29` is a 6-byte device identifier Garmin Connect uses for accessory recognition.

The official `garmin-fit-sdk` Python encoder rejects any message whose `mesg_num` isn't in its bundled Profile, and silently drops any field whose name isn't in the Profile entry for that message. Both classes of data would be lost without intervention.

The trick: every FIT file embeds the byte layout of every message type it uses (it has to — that's how a generic decoder reads manufacturer-specific data). The `garmin-fit-sdk` Decoder exposes those embedded definitions through a `mesg_definition_listener` callback. `merge-activity` captures them during decode, then for every captured definition either:

- synthesizes a complete Profile entry (for fully-unknown mesg_nums), or
- adds the proprietary fields to the existing Profile entry (for record / session / lap / device_info)

with synthetic `field_<id>` names, FIT base types translated into the SDK's string-named base types, and array vs. scalar inferred from `num_field_elements`. The augmented Profile is installed via a context manager that restores it on exit. Integer-keyed fields in decoded messages are renamed to `field_<id>` strings to match. The encoder then writes everything through. After re-decoding, integer keys come back as integers because downstream tools have no knowledge of our synthetic names — they just read the embedded definitions out of the file.

See [src/merge_activity/encode.py](src/merge_activity/encode.py) `_profile_patched_with`.

## What ends up in the merged file

Concrete checklist of what populates in Garmin Connect after re-upload (verified against the real fixture in [tests/](tests/)):

**Activity overview**

- Map rendered from Zwift coordinates
- Distance, average / max speed, ascent / descent from Zwift
- HR / power / cadence graphs from Garmin
- Calories breakdown ("Aktiv-Kalorien" / "Kalorien in Ruhe" / "Gesamt") from `total_calories` + `metabolic_calories`
- Sweat loss ("Schweißverlust") from session field 178 + user profile
- Performance Condition graph, Stamina graph, Body Battery delta from proprietary mesgs 140 / 233
- Training Effect (aerobic & anaerobic), Training Load, Training Stress Score, intensity factor, threshold power
- Average / max temperature, respiration rate (avg/max/min)
- HR / power zone breakdown chart from session-level `time_in_zone`

**Cycling dynamics**

- Left/right balance, torque effectiveness, pedal smoothness, PCO
- Power-phase start/end/length and peak power-phase angles (4-element arrays in laps and session)
- Rider position graph (seated / standing) from passed-through `rider_position_change` events

**Connected sensors / accessories ("Ausrüstung")**

- Watch
- HRM-Pro Plus (or whatever HRM you wear) with battery status
- Power-meter pedals (e.g. Favero Assioma)
- Electronic shifting (e.g. Shimano Di2) and gear-shift markers from `rear_gear_change` events

**Lap summary table**

- Time / total time / distance, average speed, ascent / descent (per-lap, derived from altitude deltas)
- Avg/max HR, cadence, power, NP
- L/R balance, torque effectiveness, pedal smoothness, PCO
- Power-phase start/end/length, max power-phase angles
- Seated/standing time, seated/standing avg & max power, seated/standing avg & max cadence
- Calories per lap

## Limitations

- **Time alignment**: cadence cross-correlation is robust if the ride has any cadence variability and a few minutes of overlap. For very steady rides (e.g. a 15-minute fixed-cadence test) the correlation can be ambiguous; pass `--offset N` if you know the offset.
- **Multi-session FIT files**: the tool assumes a single session per file (the activity-format FIT structure with one `session_mesgs` entry, which is what Garmin watches and Zwift both produce). It doesn't merge multi-segment activities like a brick triathlon.
- **Per-lap power-phase length / peak position**: copied from the overlapping Garmin lap. Accurate to the extent that Garmin's lap windows roughly align with Zwift's; for very different lap structures the values may be slightly off.
- **Per-lap calories**: derived from the lap's share of session work. Garmin's on-device algorithm is more sophisticated (HR + power + user profile + duration) but isn't fully documented; the proxy adds up to the session total within rounding.
- **Standing detection requires `rider_position_change` events**: only Garmin watches with the Cycling Dynamics suite emit these. If your watch doesn't, the seated/standing breakdown will assume 100% seated.

## Development

```bash
pip install -e '.[dev]'
pytest -v
```

The integration test [tests/test_merge.py](tests/test_merge.py) runs the full CLI against the real fixtures in [tests/](tests/), then asserts that:

- the merged file decodes without integrity errors,
- Garmin's record count, all HR/power/cadence values, and every proprietary message type are preserved verbatim,
- ≥80% of records have GPS layered in,
- the session is `cycling / virtual_activity`,
- `len(laps) == len(zwift.laps)`,
- `session.metabolic_calories` and all 18 Garmin device_info entries survive the round-trip,
- `session.total_distance` is within 1% of Zwift's, `session.avg_heart_rate` within 2 bpm of Garmin's.

Project layout:

```
src/merge_activity/
├── cli.py             # argparse entry point
├── decode.py          # FIT decode + capture of embedded message definitions
├── align.py           # cadence/HR cross-correlation
├── merge.py           # record / lap / session merge logic
└── encode.py          # FIT encode + Profile augmentation for proprietary mesgs
```

## Acknowledgements

- The official [garmin-fit-sdk](https://github.com/garmin/fit-python-sdk) for decoding and (with some help) encoding.
- The FIT file format itself, which makes this kind of round-tripping possible because every file embeds its own message definitions.

## License

MIT — see [LICENSE](LICENSE).
