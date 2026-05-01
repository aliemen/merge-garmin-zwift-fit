# Test fixtures

The integration test in [`test_merge.py`](test_merge.py) runs the full CLI against a real Garmin and a real Zwift `.fit` file from the same workout. It then re-decodes the merged file and checks that all the things `merge-activity` claims to preserve actually round-trip.

These fixtures aren't checked into the repo: `.fit` activity files contain personal data (your `user_profile` with weight / height / gender / resting HR / FTP / configured max HR, device serial numbers, a per-second HR + power timeline, and the absolute timestamp of the workout). The repo's `.gitignore` excludes `tests/*.fit` so you can keep your fixtures local without worrying about accidentally committing them.

## Adding your own fixtures

Pick any same-workout pair from your own data, drop them into this directory, and make sure their filenames start with `Garmin` and `Zwift` respectively:

```
tests/
├── Garmin_<anything>.fit       # the .fit your watch produced
├── Zwift_<anything>.fit        # the .fit Zwift produced for the same ride
├── README.md                   # this file
└── test_merge.py
```

The test discovers them via `tests/Garmin*.fit` and `tests/Zwift*.fit` glob patterns, so you can keep your original Garmin Connect / Zwift filenames (`Garmin_22716924242_ACTIVITY.fit`, `Zwift_2026-04-30-19-50-07.fit`, etc.), no renaming required. If exactly one of each prefix is present, the test uses it. If none, the suite is **skipped** (with a clear message). If multiple of either prefix are present, the test fails loudly to avoid silently picking the wrong file.

### Where to download the files

- **Garmin**: Garmin Connect → activity page → ⚙ menu → *Originaldatei exportieren* / *Export Original*. You get the raw `.fit` your watch produced.
- **Zwift**: open Zwift's local Activities folder. On macOS: `~/Documents/Zwift/Activities/`. On Windows: `%USERPROFILE%\Documents\Zwift\Activities\`. Look for a file named `<YYYY-MM-DD-HH-MM-SS>.fit` matching the date and start time of the ride.

The two files must be from the **same workout** — the test asserts that the merged file's record count matches the Garmin file, that ≥80% of merged records have GPS layered in from Zwift, and that aggregate metrics (`avg_heart_rate`, `total_distance`) round-trip within tolerance. A mismatched pair will fail those assertions immediately.

## Running the tests

From the repo root:

```bash
pip install -e '.[dev]'
pytest -v
```

If the suite skips with `no tests/Garmin*.fit fixture found`, drop a pair of files into this directory and re-run.
