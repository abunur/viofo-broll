---
name: viofo-broll
description: Pull VIOFO A329S 3-channel dashcam footage (front, rear, interior) for specific days, extract its embedded GPS, find stretches that overlap cycling ride tracks (GPX/FIT), and build a Final Cut Pro FCPXML with one multicam clip per segment plus a "B-roll matches" timeline and map-overlay GeoJSON. Use when the user mentions VIOFO, dashcam footage, dash cam B-roll, matching car GPS to ride routes, or importing dashcam clips into Final Cut.
---

# VIOFO dashcam → B-roll (video + GPS + time sync)

Everything runs through one stdlib-only CLI: `scripts/viofo_broll.py` (path relative to this skill's folder). Run `python3 <skill>/scripts/viofo_broll.py -h` for flags.

## Camera facts (A329S 3CH)
- Front 4K/30 (has mic audio), Rear 2K/30, Interior 2K/30 210° fisheye with IR (night = black and white).
- Files: `YYYY_MMDD_HHMMSS_NNN[P]{F|R|I}.MP4`. A329S firmware numbers every file separately (`…_060894F`, `…_060895I`, `…_060896R`) and a channel can start 1–3 s after the others, so the script groups one moment's files by start time (within 3 s), adjacent sequence numbers and parking flag; the group's stem is the Front file's name minus the channel letter (older firmware that shares one stem across channels groups the same way). `P` = parking-mode clip (skipped by default). Locked clips live in an `RO` folder; `ingest` searches recursively, so they're included.
- Filename time = camera clock in the local time zone set on the camera. GPS timestamps are UTC. The `gps` step measures the offset (rounded to 15 min) and per-clip drift.
- GPS is embedded in the MP4 (usually the F file). **Never trim, merge or transcode before extracting GPS** — ffmpeg/LosslessCut/MKVToolNix strip it.

## Prerequisites (macOS)
`brew install exiftool ffmpeg`. Optional for Garmin/Wahoo rides: `pip3 install fitdecode`.

## Getting footage off the camera
Preferred: connect the camera to the Mac with a USB-C **data** cable and choose mass-storage mode on the camera. It mounts as a volume under `/Volumes/` (~50 MB/s). A microSD in a card reader also works and is faster for bulk dumps. Recording stops while connected — remind the user to restart it.

## Workflow
Work in a project folder the user chooses (default `./dashcam`). Confirm dates with the user if they weren't given.

1. **Probe once** (new camera/firmware):
   `viofo_broll.py probe /Volumes/<CAM>/.../<one>F.MP4`
   Exit 2 = no GPS readable → tell the user; fallbacks are `brew upgrade exiftool`, Telemetry Extractor, or Dashcam Viewer GPX export. Stop before building on missing GPS.
2. **Ingest** the days needed (dry-run first to show segment count and GB):
   `viofo_broll.py ingest --src /Volumes/<CAM> --dest ./dashcam --date 2026-09-12 [--date …] [--start 09:30 --end 11:00] --dry-run`
   then rerun without `--dry-run`. Copies are skipped if already present (same size). Files go to `./dashcam/YYYY-MM-DD/`. Never modify or delete anything on the camera.
3. **GPS** per day folder:
   `viofo_broll.py gps ./dashcam/2026-09-12`
   → `_gps/<stem>.csv` (clip_t, utc, lat, lon, speed_kmh, track), `_gps/<day>.gpx`, `gps_index.json` (clip UTC starts, tz offset, drift). Report the detected offset and any clip without GPS. The offset comes from driving clips (median); parking-mode clips often keep a stale last fix, so on parking-only days the smallest offset is used. If the camera clock is known, pass `--tz-offset <minutes ahead of UTC>` to override. A large `clock_drift_s` on a clip right after parking is a stale fix, not clock drift.
4. **Rides** — need GPX. Convert FIT with `viofo_broll.py fit2gpx ride.fit`, or ask the user to export GPX from Garmin Connect / Strava.
5. **Match** car footage to ride routes:
   `viofo_broll.py match ./dashcam/2026-09-12 --rides ride1.gpx ride2.gpx [--radius 20] [--min-seconds 5]`
   → `matches.json/.csv/.geojson`. Each match: stem, in/out seconds in the clip, ride km range, direction (`same` = car travelling the ride's direction, `opposite`; crossings are dropped). Show the user the table. Widen `--radius` (30–40 m) for divided highways or poor GPS; dashcam rides on one side of the road.
6. **FCPXML**:
   `viofo_broll.py fcpxml ./dashcam/2026-09-12 --matches ./dashcam/2026-09-12/matches.json --out ./dashcam/dashcam-2026-09-12.fcpxml [--handles 2] [--keep-gaps]`
   Produces one event with: a multicam clip per moment (angles Front / Rear / Interior, each offset by its filename start-time difference, so sync is within ±1 s — fine-tune in FCP with Synchronize by audio if needed), a `Dashcam <day> (all)` project, and a `B-roll matches` project (each match + handles, Front angle active, notes with ride km). Media is referenced in place by absolute `file://` path — keep the folder where it is, or relink in FCP.
   If the `fcpxml` MCP tools are available (e.g. `fcpxml diagnose` / `inspect`), run diagnose on the output before handing it off.
7. **Map overlay**: hand `matches.geojson` (ride tracks + highlighted car stretches) and the per-clip CSVs to whatever renders the map in the video project. Clip time `t` in a CSV + clip `utc_start` lines up with ride GPX time for animated markers.

## Rules
- Report results in short bullets: segments copied, GB, GPS points, tz offset, matches found.
- Front angle has the audio; rear/interior usually have none. Interior footage shows people in the car — mention it if the user plans to publish it.
- Don't burn GPS/speed stamps: suggest turning off the camera's on-screen stamps for future B-roll.
- If several days are involved, run gps/match per day folder, then fcpxml per day (or point `fcpxml` at the parent `./dashcam` folder to put every day in one event).
