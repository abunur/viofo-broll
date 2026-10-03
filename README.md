# viofo-broll

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-green.svg)
![Final Cut Pro](https://img.shields.io/badge/Final%20Cut%20Pro-FCPXML%201.10-purple.svg)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)

Turn VIOFO A329S 3-channel dashcam footage (front, rear, interior) into cycling-video B-roll. The tool extracts the GPS embedded in each clip, finds the stretches where the car drove the same roads as your ride tracks (GPX/FIT), and builds a Final Cut Pro FCPXML library with one multicam clip per moment plus a "B-roll matches" timeline. It also writes GeoJSON for map overlays.

It ships as a [Claude Code](https://docs.claude.com/en/docs/claude-code) skill (`SKILL.md`), but the script is a plain Python CLI and works on its own.

## Features

- **Ingest** selected days (and an optional time window) from the camera, a microSD card, or a folder, including locked clips in `RO/`. Parking-mode clips are skipped by default.
- **Channel grouping** that handles A329S firmware, which numbers each channel's file separately and can start one channel 1–3 s after the others.
- **GPS extraction** with exiftool to per-clip CSV, per-day GPX and a `gps_index.json` holding each clip's UTC start, the camera's time-zone offset and per-clip clock drift. Stale fixes from parking mode are handled.
- **Route matching** against ride GPX tracks, with direction detection (`same` / `opposite`, crossings dropped) and the ride kilometer range for each match.
- **FCPXML 1.10 output**: one multicam clip per moment (Front / Rear / Interior angles, offset by their start-time difference), a full-day timeline per day, and a `B-roll matches` project with handles and notes.
- **FIT to GPX** conversion for Garmin and Wahoo rides.
- Standard library only. No Python dependencies beyond the optional `fitdecode`.

## Requirements

- Python 3.9+
- [exiftool](https://exiftool.org/) and ffprobe (from [ffmpeg](https://ffmpeg.org/)) on `PATH`
- Optional, for `.fit` rides: `fitdecode`

```bash
brew install exiftool ffmpeg
```

```bash
pip3 install fitdecode
```

## Install as a Claude Code skill

```bash
git clone https://github.com/abunur/viofo-broll.git ~/.claude/skills/viofo-broll
```

Then ask Claude Code something like "pull my VIOFO footage from Saturday and find B-roll that matches my ride." The skill walks through the steps below.

## Usage

```bash
python3 scripts/viofo_broll.py -h
```

1. **Probe** one clip to confirm the camera's GPS is readable (exit code 2 means no GPS):

   ```bash
   python3 scripts/viofo_broll.py probe /Volumes/CAM/DCIM/Movie/2026_0912_093015_000123F.MP4
   ```

2. **Ingest** the days you need. Run with `--dry-run` first to see the segment count and size:

   ```bash
   python3 scripts/viofo_broll.py ingest --src /Volumes/CAM --dest ./dashcam --date 2026-09-12 --start 09:30 --end 11:00 --dry-run
   ```

3. **Extract GPS** for each day folder. Pass `--tz-offset <minutes ahead of UTC>` to override the detected camera clock offset:

   ```bash
   python3 scripts/viofo_broll.py gps ./dashcam/2026-09-12
   ```

4. **Convert FIT rides** if needed:

   ```bash
   python3 scripts/viofo_broll.py fit2gpx ride.fit
   ```

5. **Match** car footage to ride routes. Widen `--radius` to 30–40 m for divided highways or poor GPS:

   ```bash
   python3 scripts/viofo_broll.py match ./dashcam/2026-09-12 --rides ride.gpx --radius 20 --min-seconds 5
   ```

6. **Build the FCPXML** and import it into Final Cut Pro:

   ```bash
   python3 scripts/viofo_broll.py fcpxml ./dashcam/2026-09-12 --matches ./dashcam/2026-09-12/matches.json --out ./dashcam/dashcam-2026-09-12.fcpxml --handles 2
   ```

## Outputs

| File | Contents |
| --- | --- |
| `_gps/<stem>.csv` | Per-clip GPS: `clip_t, utc, lat, lon, speed_kmh, track` |
| `_gps/<day>.gpx` | All of the day's car tracks |
| `gps_index.json` | Clip UTC starts, camera time-zone offset, clock drift |
| `matches.json` / `.csv` | Matching stretches: clip in/out, ride km range, direction |
| `matches.geojson` | Ride tracks plus highlighted car stretches for map overlays |
| `*.fcpxml` | Final Cut Pro library with multicam clips and timelines |

## Notes

- **Extract GPS before editing.** Trimming, merging or transcoding clips (ffmpeg, LosslessCut, MKVToolNix) strips the embedded GPS.
- **Media is referenced in place** by absolute `file://` path. Keep the footage where it is, or relink in Final Cut.
- **Multicam sync** comes from filename start times, so it is within about ±1 s. Fine-tune in Final Cut with Synchronize by audio if needed. Only the front camera records audio.
- **Interior footage shows the people in the car.** Check it before publishing.
- **Turn off the camera's on-screen GPS and speed stamps** for cleaner B-roll.
- Tested against the VIOFO A329S 3CH. Other VIOFO models that use the same `YYYY_MMDD_HHMMSS_NNN[P]{F|R|I}.MP4` naming should work but are untested.

## License

[MIT](LICENSE)
