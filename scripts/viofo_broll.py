#!/usr/bin/env python3
"""viofo_broll.py — VIOFO A329S (3CH) dashcam -> B-roll pipeline.

Subcommands
  probe   Check one clip for embedded GPS (run this first on new firmware).
  ingest  Copy F/R/I clips for given days (and optional time window) from the
          camera / card / folder into a working directory.
  gps     Extract GPS from each clip with exiftool -> per-stem CSV, per-day GPX,
          and gps_index.json (clip start times in UTC, tz offset).
  match   Find stretches where the car drove the same roads as ride GPX tracks
          -> matches.json, matches.csv, matches.geojson.
  fit2gpx Convert Garmin/Wahoo .FIT rides to GPX (needs: pip3 install fitdecode).
  fcpxml  Build an FCPXML 1.10 library: one multicam clip per stem
          (Front/Rear/Interior angles) + a "B-roll matches" project.

Stdlib only. Requires exiftool and ffprobe on PATH.
Filenames: YYYY_MMDD_HHMMSS_NNN[P]{F|R|I}.MP4 (P = parking).
"""
import argparse, csv, json, math, os, re, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

NAME_RE = re.compile(r"^(\d{4})_(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_(\d+)(P?)([FRI])\.MP4$", re.I)
CHANNELS = {"F": "Front", "R": "Rear", "I": "Interior"}


# ---------------------------------------------------------------- helpers
def parse_name(name):
    m = NAME_RE.match(name)
    if not m:
        return None
    y, mo, d, h, mi, s, seq, park, ch = m.groups()
    return {
        "stem": name[: m.start(8)],
        "local_start": datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)),
        "seq": seq, "parking": bool(park), "channel": ch.upper(), "name": name,
    }


GROUP_WINDOW_S = 3   # cameras of one moment can start up to ~1-2 s apart
GROUP_SEQ_GAP = 6    # their sequence numbers are adjacent (e.g. F=n, I=n+1, R=n+2)


def scan(root):
    """Return {stem: {channel: Path}} for VIOFO files under root (recursive).

    Older firmware gives all three channels one shared stem. Newer firmware (A329S 3CH)
    numbers every file separately (…_060894F, …_060895I, …_060896R) and a channel can
    start a second later than the others, so files are grouped as one moment when they
    share parking mode, start within GROUP_WINDOW_S and have nearby sequence numbers.
    The group's stem is the Front file's prefix (else the earliest file's).
    """
    infos = []
    for p in Path(root).rglob("*"):
        if p.is_file() and not p.name.startswith("._"):
            info = parse_name(p.name)
            if info:
                infos.append((info, p))
    infos.sort(key=lambda x: (x[0]["local_start"], int(x[0]["seq"])))
    groups = []
    for info, p in infos:
        g = groups[-1] if groups else None
        if (g and info["channel"] not in g["files"] and info["parking"] == g["parking"]
                and (info["local_start"] - g["t"]).total_seconds() <= GROUP_WINDOW_S
                and abs(int(info["seq"]) - g["seq"]) <= GROUP_SEQ_GAP):
            g["files"][info["channel"]] = p
            g["infos"][info["channel"]] = info
        else:
            groups.append({"t": info["local_start"], "seq": int(info["seq"]), "parking": info["parking"],
                           "files": {info["channel"]: p}, "infos": {info["channel"]: info}})
    out = {}
    for g in groups:
        lead = g["infos"].get("F") or min(g["infos"].values(), key=lambda i: (i["local_start"], int(i["seq"])))
        out[lead["stem"]] = g["files"]
    return out


def channel_offsets(files):
    """Seconds each channel started after the group's earliest file (filename clock, 1 s resolution)."""
    ts = {ch: parse_name(p.name)["local_start"] for ch, p in files.items()}
    t0 = min(ts.values())
    return {ch: (t - t0).total_seconds() for ch, t in ts.items()}


def stem_time(stem):
    return parse_name(stem + "F.MP4")["local_start"]


def need(tool):
    if not shutil.which(tool):
        sys.exit(f"ERROR: '{tool}' not found. Install with: brew install {'exiftool' if tool == 'exiftool' else 'ffmpeg'}")


def hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def ang_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def parse_gps_dt(s):
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    for fmt in ("%Y:%m:%d %H:%M:%S.%f%z", "%Y:%m:%d %H:%M:%S%z", "%Y:%m:%d %H:%M:%S.%f", "%Y:%m:%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- exiftool GPS
def exif_gps(path):
    """Return list of samples: {t (clip seconds or None), utc, lat, lon, speed_kmh, track}."""
    cmd = ["exiftool", "-api", "largefilesupport=1", "-ee", "-n", "-j", "-G3",
           "-GPSDateTime", "-GPSLatitude", "-GPSLongitude", "-GPSSpeed", "-GPSSpeedRef",
           "-GPSTrack", "-SampleTime", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1) or not r.stdout.strip():
        return []
    data = json.loads(r.stdout)[0]
    docs = {}
    for k, v in data.items():
        if ":" not in k:
            continue
        grp, tag = k.split(":", 1)
        if not grp.startswith("Doc"):
            continue
        docs.setdefault(grp, {})[tag] = v
    samples = []
    for grp in sorted(docs, key=lambda g: int(re.sub(r"\D", "", g) or 0)):
        d = docs[grp]
        lat, lon = d.get("GPSLatitude"), d.get("GPSLongitude")
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:
            continue
        spd = d.get("GPSSpeed")
        ref = str(d.get("GPSSpeedRef", "K")).upper()
        if spd is not None:
            spd = float(spd) * (1.609344 if ref.startswith("M") else 1.852 if ref.startswith("N") else 1.0)
        st = d.get("SampleTime")
        try:
            st = float(st) if st is not None else None
        except (TypeError, ValueError):
            st = None
        samples.append({"t": st, "utc": parse_gps_dt(d.get("GPSDateTime")), "lat": lat, "lon": lon,
                        "speed_kmh": spd, "track": d.get("GPSTrack")})
    return samples


# ---------------------------------------------------------------- ffprobe
def probe_video(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    j = json.loads(r.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    rate = v.get("r_frame_rate") or v.get("avg_frame_rate") or "30/1"
    num, den = (int(x) for x in rate.split("/"))
    return {"width": v.get("width"), "height": v.get("height"), "fps": Fraction(num, den),
            "duration": float(j.get("format", {}).get("duration", 0) or 0),
            "audio": a is not None,
            "audio_rate": int(a.get("sample_rate", 48000)) if a else None,
            "audio_channels": int(a.get("channels", 1)) if a else None}


# ---------------------------------------------------------------- probe
def cmd_probe(a):
    need("exiftool")
    p = Path(a.file)
    s = exif_gps(p)
    print(f"{p.name}: {len(s)} GPS samples")
    if s:
        print(f"  first: {s[0]['utc']} {s[0]['lat']:.6f},{s[0]['lon']:.6f} t={s[0]['t']}")
        print(f"  last:  {s[-1]['utc']} {s[-1]['lat']:.6f},{s[-1]['lon']:.6f} t={s[-1]['t']}")
        print("OK: exiftool reads this camera's GPS.")
    else:
        print("NO GPS found. Check: GPS enabled + fix acquired; try the F file; update exiftool (brew upgrade exiftool);"
              " fallback: Telemetry Extractor or Dashcam Viewer export to GPX.")
        sys.exit(2)


# ---------------------------------------------------------------- ingest
def cmd_ingest(a):
    src, dest = Path(a.src), Path(a.dest)
    days = {d.replace("-", "") for d in a.date}
    t0 = datetime.strptime(a.start, "%H:%M").time() if a.start else None
    t1 = datetime.strptime(a.end, "%H:%M").time() if a.end else None
    chans = set(a.channels.upper())
    stems = scan(src)
    copied = skipped = 0
    total = 0
    picked = []
    for stem, files in stems.items():
        lt = stem_time(stem)
        if lt.strftime("%Y%m%d") not in days:
            continue
        if not a.include_parking and any(parse_name(p.name)["parking"] for p in files.values()):
            continue
        if t0 and lt.time() < t0:
            continue
        if t1 and lt.time() > t1:
            continue
        picked.append(stem)
        for ch, p in files.items():
            if ch in chans:
                total += p.stat().st_size
    print(f"{len(picked)} segments, {total/1e9:.1f} GB to consider")
    if a.dry_run:
        for s in picked:
            print("  ", s, "".join(sorted(stems[s])))
        return
    for stem in picked:
        day_dir = dest / stem_time(stem).strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        for ch, p in sorted(stems[stem].items()):
            if ch not in chans:
                continue
            out = day_dir / p.name
            if out.exists() and out.stat().st_size == p.stat().st_size:
                skipped += 1
                continue
            shutil.copy2(p, out)
            copied += 1
            print("copied", out.name)
    print(f"done: {copied} copied, {skipped} already present -> {dest}")


# ---------------------------------------------------------------- gps
def write_gpx(path, tracks):
    gpx = ET.Element("gpx", version="1.1", creator="viofo_broll", xmlns="http://www.topografix.com/GPX/1/1")
    for name, pts in tracks:
        trk = ET.SubElement(gpx, "trk")
        ET.SubElement(trk, "name").text = name
        seg = ET.SubElement(trk, "trkseg")
        for p in pts:
            tp = ET.SubElement(seg, "trkpt", lat=f"{p['lat']:.7f}", lon=f"{p['lon']:.7f}")
            if p.get("utc"):
                ET.SubElement(tp, "time").text = p["utc"] if isinstance(p["utc"], str) else iso(p["utc"])
    ET.indent(gpx)
    ET.ElementTree(gpx).write(path, encoding="utf-8", xml_declaration=True)


def cmd_gps(a):
    need("exiftool"); need("ffprobe")
    root = Path(a.dir)
    stems = scan(root)
    gps_dir = root / "_gps"
    gps_dir.mkdir(exist_ok=True)
    index = {"tz_offset_minutes": None, "clips": {}}
    offsets = []
    utc_starts = {}
    per_day = {}
    for stem, files in sorted(stems.items()):
        src = None
        samples = []
        for ch in ("F", "R", "I"):
            if ch in files:
                samples = exif_gps(files[ch])
                if samples:
                    src = ch
                    break
        info = probe_video(files.get("F") or next(iter(files.values())))
        lt = stem_time(stem)
        entry = {"channels": {c: str(p.resolve()) for c, p in files.items()}, "local_start": lt.isoformat(),
                 "duration": info["duration"], "gps_source": src, "samples": len(samples)}
        if samples:
            n = len(samples)
            dur = info["duration"] or n
            for i, s in enumerate(samples):
                if s["t"] is None:
                    s["t"] = i * dur / n
            first = next((s for s in samples if s["utc"]), None)
            if first:
                utc0 = first["utc"] - timedelta(seconds=first["t"])
                entry["utc_start"] = iso(utc0)
                utc_starts[stem] = utc0
                parked = any(parse_name(p.name)["parking"] for p in files.values())
                offsets.append(((lt - utc0.replace(tzinfo=None)).total_seconds() / 60, parked))
            with open(gps_dir / f"{stem}.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["clip_t", "utc", "lat", "lon", "speed_kmh", "track"])
                for s in samples:
                    w.writerow([f"{s['t']:.3f}", iso(s["utc"]) if s["utc"] else "", f"{s['lat']:.7f}",
                                f"{s['lon']:.7f}", "" if s["speed_kmh"] is None else f"{s['speed_kmh']:.1f}",
                                "" if s["track"] is None else s["track"]])
            per_day.setdefault(lt.strftime("%Y-%m-%d"), []).append((stem, samples))
        index["clips"][stem] = entry
        print(f"{stem}: {len(samples)} pts (from {src or '-'})")
    if offsets:
        # Parking-mode clips often carry the last fix from before the car was parked, so their
        # GPS time can be hours stale, which only ever inflates (camera time - GPS time).
        # Use the median of driving clips; with parking clips only, the smallest is the freshest.
        driving = sorted(o for o, parked in offsets if not parked)
        est = driving[len(driving) // 2] if driving else min(o for o, _ in offsets)
        tz = a.tz_offset if a.tz_offset is not None else int(round(est / 15.0) * 15)
        index["tz_offset_minutes"] = tz
        # clips without GPS: derive utc_start from filename + tz
        for stem, e in index["clips"].items():
            fn_utc = (stem_time(stem) - timedelta(minutes=tz)).replace(tzinfo=timezone.utc)
            if "utc_start" not in e:
                e["utc_start"] = iso(fn_utc)
                e["utc_start_source"] = "filename+tz"
            else:
                e["utc_start_source"] = "gps"
                e["clock_drift_s"] = round((utc_starts[stem] - fn_utc).total_seconds(), 2)
        print(f"camera clock offset vs UTC: {tz:+d} min")
    elif a.tz_offset is not None:
        index["tz_offset_minutes"] = a.tz_offset
    for day, items in per_day.items():
        write_gpx(gps_dir / f"{day}.gpx", [(stem, [dict(s, utc=s["utc"]) for s in smp]) for stem, smp in items])
    (root / "gps_index.json").write_text(json.dumps(index, indent=2, default=str))
    print(f"wrote {gps_dir}/ and gps_index.json")


# ---------------------------------------------------------------- match
def read_gpx(path):
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    tree = ET.parse(path)
    pts = []
    for tp in tree.iter():
        if tp.tag.endswith("trkpt") or tp.tag.endswith("rtept"):
            t = None
            for c in tp:
                if c.tag.endswith("time") and c.text:
                    t = c.text
            pts.append((float(tp.get("lat")), float(tp.get("lon")), t))
    return pts


class Grid:
    def __init__(self, pts, cell_m):
        self.pts, self.cell = pts, cell_m / 111320.0
        self.g = {}
        for i, (la, lo, _) in enumerate(pts):
            self.g.setdefault((int(la / self.cell), int(lo / self.cell)), []).append(i)

    def nearest(self, la, lo):
        ci, cj = int(la / self.cell), int(lo / self.cell)
        best, bi = 1e18, -1
        for di in (-1, 0, 1):
            for dj in (-2, -1, 0, 1, 2):
                for i in self.g.get((ci + di, cj + dj), ()):
                    d = hav(la, lo, self.pts[i][0], self.pts[i][1])
                    if d < best:
                        best, bi = d, i
        return bi, best


def ride_bearing(pts, i):
    j0, j1 = max(0, i - 3), min(len(pts) - 1, i + 3)
    if j0 == j1:
        return None
    return bearing(pts[j0][0], pts[j0][1], pts[j1][0], pts[j1][1])


def cmd_match(a):
    root = Path(a.dir)
    index = json.loads((root / "gps_index.json").read_text())
    rides = []
    for rp in a.rides:
        pts = read_gpx(rp)
        cum = [0.0]
        for k in range(1, len(pts)):
            cum.append(cum[-1] + hav(pts[k - 1][0], pts[k - 1][1], pts[k][0], pts[k][1]))
        rides.append({"name": Path(rp).stem, "pts": pts, "cum": cum, "grid": Grid(pts, max(a.radius, 25))})
        print(f"ride {Path(rp).name}: {len(pts)} pts, {cum[-1]/1000:.1f} km")
    matches = []
    for stem, e in sorted(index["clips"].items()):
        csvp = root / "_gps" / f"{stem}.csv"
        if not csvp.exists():
            continue
        rows = list(csv.DictReader(open(csvp)))
        samp = [(float(r["clip_t"]), float(r["lat"]), float(r["lon"])) for r in rows]
        for ride in rides:
            hits = []
            for k, (t, la, lo) in enumerate(samp):
                i, d = ride["grid"].nearest(la, lo)
                if i < 0 or d > a.radius:
                    hits.append(None); continue
                cb = None
                if 0 < k < len(samp) - 1:
                    p, n = samp[k - 1], samp[k + 1]
                    if hav(p[1], p[2], n[1], n[2]) > 3:
                        cb = bearing(p[1], p[2], n[1], n[2])
                rb = ride_bearing(ride["pts"], i)
                direction = None
                if cb is not None and rb is not None:
                    dd = ang_diff(cb, rb)
                    direction = "same" if dd < 45 else "opposite" if dd > 135 else "crossing"
                hits.append((t, i, d, direction))
            # group consecutive hits (allow gaps of up to 2 samples)
            run, gap = [], 0
            def flush(run):
                if not run:
                    return
                t_in, t_out = run[0][0], run[-1][0]
                if t_out - t_in < a.min_seconds:
                    return
                dirs = [h[3] for h in run if h[3]]
                if dirs and dirs.count("crossing") > len(dirs) / 2:
                    return
                main = max(set(dirs), key=dirs.count) if dirs else "unknown"
                i0, i1 = run[0][1], run[-1][1]
                seg = samp[[s[0] for s in samp].index(t_in): [s[0] for s in samp].index(t_out) + 1]
                matches.append({
                    "stem": stem, "gps_source": e.get("gps_source"), "ride": ride["name"],
                    "in_s": round(t_in, 2), "out_s": round(t_out, 2),
                    "duration_s": round(t_out - t_in, 2), "direction": main,
                    "mean_dist_m": round(sum(h[2] for h in run) / len(run), 1),
                    "ride_km_from": round(ride["cum"][min(i0, i1)] / 1000, 2),
                    "ride_km_to": round(ride["cum"][max(i0, i1)] / 1000, 2),
                    "ride_time_at_in": ride["pts"][i0][2],
                    "clip_utc_start": e.get("utc_start"), "channels": e["channels"],
                    "coords": [[round(lo, 7), round(la, 7)] for _, la, lo in seg],
                })
            for h in hits:
                if h is None:
                    gap += 1
                    if gap > 2:
                        flush(run); run, gap = [], 0
                else:
                    run.append(h); gap = 0
            flush(run)
    matches.sort(key=lambda m: (m["ride"], m["ride_km_from"]))
    (root / "matches.json").write_text(json.dumps(matches, indent=2))
    with open(root / "matches.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ride", "ride_km_from", "ride_km_to", "stem", "in_s", "out_s", "duration_s", "direction", "mean_dist_m"])
        for m in matches:
            w.writerow([m["ride"], m["ride_km_from"], m["ride_km_to"], m["stem"], m["in_s"], m["out_s"],
                        m["duration_s"], m["direction"], m["mean_dist_m"]])
    feats = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": m["coords"]},
              "properties": {k: m[k] for k in ("stem", "ride", "in_s", "out_s", "direction", "ride_km_from", "ride_km_to")}}
             for m in matches]
    for ride in rides:
        feats.append({"type": "Feature", "properties": {"ride": ride["name"], "kind": "ride"},
                      "geometry": {"type": "LineString", "coordinates": [[lo, la] for la, lo, _ in ride["pts"]]}})
    (root / "matches.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"{len(matches)} matching stretches -> matches.json / .csv / .geojson")
    for m in matches:
        print(f"  {m['ride']} km {m['ride_km_from']}-{m['ride_km_to']}: {m['stem']} {m['in_s']}s-{m['out_s']}s ({m['direction']})")


# ---------------------------------------------------------------- fcpxml
class TimeBase:
    def __init__(self, fps):
        self.fd = Fraction(1, 1) / fps  # frame duration in seconds

    def frames(self, seconds):
        return int(round(Fraction(seconds).limit_denominator(1000000) / self.fd))

    def t(self, seconds=None, frames=None):
        n = self.frames(seconds) if frames is None else frames
        v = self.fd * n
        return "0s" if v == 0 else (f"{v.numerator}s" if v.denominator == 1 else f"{v.numerator}/{v.denominator}s")

    def fd_str(self):
        return f"{self.fd.numerator}/{self.fd.denominator}s"


def cmd_fcpxml(a):
    need("ffprobe")
    root = Path(a.dir)
    stems = scan(root)
    if not stems:
        sys.exit("no VIOFO clips found")
    matches = json.loads(Path(a.matches).read_text()) if a.matches else []
    fx = ET.Element("fcpxml", version="1.10")
    res = ET.SubElement(fx, "resources")
    formats, rid = {}, [0]

    def nid(prefix="r"):
        rid[0] += 1
        return f"{prefix}{rid[0]}"

    def fmt_for(info):
        key = (info["width"], info["height"], info["fps"])
        if key not in formats:
            fid = nid()
            ET.SubElement(res, "format", id=fid, name=f"{info['width']}x{info['height']}@{float(info['fps']):.2f}",
                          frameDuration=TimeBase(info["fps"]).fd_str(), width=str(info["width"]), height=str(info["height"]))
            formats[key] = fid
        return formats[key]

    mc = {}
    seq_info = None
    for stem, files in sorted(stems.items()):
        infos = {ch: probe_video(p) for ch, p in files.items()}
        lead = infos.get("F") or next(iter(infos.values()))
        if seq_info is None:
            seq_info = lead
        tb = TimeBase(lead["fps"])
        assets = {}
        for ch, p in sorted(files.items()):
            inf = infos[ch]
            atb = TimeBase(inf["fps"])
            aid = nid()
            at = {"id": aid, "name": p.stem, "start": "0s", "duration": atb.t(inf["duration"]),
                  "hasVideo": "1", "format": fmt_for(inf), "videoSources": "1"}
            if inf["audio"]:
                at.update(hasAudio="1", audioSources="1", audioChannels=str(inf["audio_channels"]),
                          audioRate=str(inf["audio_rate"]))
            ae = ET.SubElement(res, "asset", **at)
            ET.SubElement(ae, "media-rep", kind="original-media",
                          src="file://" + quote(str(p.resolve())))
            assets[ch] = (aid, inf)
        mid = nid()
        media = ET.SubElement(res, "media", id=mid, name=f"{stem} MC")
        mce = ET.SubElement(media, "multicam", format=fmt_for(lead), tcStart="0s", tcFormat="NDF")
        offs = channel_offsets(files)
        dur = min(offs[ch] + i["duration"] for ch, i in infos.items())
        for ch in ("F", "R", "I"):
            if ch in assets:
                aid, inf = assets[ch]
                ang = ET.SubElement(mce, "mc-angle", name=CHANNELS[ch], angleID=f"{stem}-{ch}")
                ET.SubElement(ang, "asset-clip", ref=aid, offset=tb.t(offs[ch]),
                              name=f"{stem}{ch}", duration=TimeBase(inf["fps"]).t(inf["duration"]), start="0s")
        audio_angle = f"{stem}-F" if "F" in assets and assets["F"][1]["audio"] else None
        mc[stem] = {"id": mid, "dur": dur, "tb": tb, "angles": [f"{stem}-{c}" for c in ("F", "R", "I") if c in assets],
                    "audio": audio_angle, "offs": offs,
                    "lt": stem_time(stem) - timedelta(seconds=offs[next(c for c in ("F", "R", "I") if c in offs)])}

    def mc_clip(parent, stem, offset_f, start_s, dur_s, name, video_angle=None):
        m = mc[stem]
        tb = m["tb"]
        el = ET.SubElement(parent, "mc-clip", ref=m["id"], offset=tb.t(frames=offset_f), name=name,
                           start=tb.t(start_s), duration=tb.t(dur_s))
        va = video_angle or m["angles"][0]
        if m["audio"] and m["audio"] != va:
            ET.SubElement(el, "mc-source", angleID=va, srcEnable="video")
            ET.SubElement(el, "mc-source", angleID=m["audio"], srcEnable="audio")
        else:
            ET.SubElement(el, "mc-source", angleID=va, srcEnable="all")
        return el, tb.frames(dur_s)

    lib = ET.SubElement(fx, "library")
    ev = ET.SubElement(lib, "event", name=a.event)
    tb0 = TimeBase(seq_info["fps"])
    seq_fmt = fmt_for(seq_info)
    # Browser clips: each multicam appears in the event, keyworded by day
    for stem in sorted(mc):
        el, _ = mc_clip(ev, stem, 0, 0, mc[stem]["dur"], f"{stem} MC")
        el.set("start", "0s")
        ET.SubElement(el, "keyword", start="0s", duration=mc[stem]["tb"].t(mc[stem]["dur"]),
                      value=f"dashcam, {mc[stem]['lt'].date()}")
    # Full-day timelines: one project per day, clips in time order with real gaps
    by_day = {}
    for stem in sorted(mc):
        by_day.setdefault(mc[stem]["lt"].date(), []).append(stem)
    for day, sts in sorted(by_day.items()):
        proj = ET.SubElement(ev, "project", name=f"Dashcam {day} (all)")
        seq = ET.SubElement(proj, "sequence", format=seq_fmt, tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")
        spine = ET.SubElement(seq, "spine")
        t0 = mc[sts[0]]["lt"]
        cursor = 0
        for stem in sts:
            off = tb0.frames((mc[stem]["lt"] - t0).total_seconds()) if a.keep_gaps else cursor
            if off > cursor:
                ET.SubElement(spine, "gap", name="Gap", offset=tb0.t(frames=cursor), duration=tb0.t(frames=off - cursor), start="0s")
                cursor = off
            el, nf = mc_clip(spine, stem, cursor, 0, mc[stem]["dur"], stem)
            cursor += nf
        seq.set("duration", tb0.t(frames=cursor))
    # Matches project
    if matches:
        proj = ET.SubElement(ev, "project", name="B-roll matches")
        seq = ET.SubElement(proj, "sequence", format=seq_fmt, tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")
        spine = ET.SubElement(seq, "spine")
        cursor = 0
        pad = a.handles
        for m in matches:
            if m["stem"] not in mc:
                continue
            # match times are clip times of the GPS source file; shift onto the multicam timeline
            sh = mc[m["stem"]]["offs"].get(m.get("gps_source") or "F", 0.0)
            s = max(0.0, m["in_s"] + sh - pad)
            e = min(mc[m["stem"]]["dur"], m["out_s"] + sh + pad)
            name = f"{m['ride']} km{m['ride_km_from']}-{m['ride_km_to']} ({m['direction']})"
            el, nf = mc_clip(spine, m["stem"], cursor, s, e - s, name)
            note = ET.Element("note")
            el.insert(0, note)
            note.text = (f"ride {m['ride']} km {m['ride_km_from']}-{m['ride_km_to']}, "
                                              f"direction {m['direction']}, mean offset {m['mean_dist_m']} m")
            cursor += nf
        seq.set("duration", tb0.t(frames=cursor))
    ET.indent(fx)
    out = Path(a.out)
    with open(out, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        ET.ElementTree(fx).write(f, encoding="utf-8", xml_declaration=False)
    print(f"wrote {out} ({len(mc)} multicam clips, {len(matches)} matches)")


# ---------------------------------------------------------------- fit2gpx
def cmd_fit2gpx(a):
    try:
        import fitdecode
    except ImportError:
        sys.exit("fit2gpx needs fitdecode: pip3 install fitdecode  (or export GPX from Garmin Connect/Strava)")
    for fp in a.files:
        pts = []
        with fitdecode.FitReader(fp) as fr:
            for fr_ in fr:
                if isinstance(fr_, fitdecode.FitDataMessage) and fr_.name == "record":
                    v = {f.name: f.value for f in fr_.fields}
                    la, lo, ts = v.get("position_lat"), v.get("position_long"), v.get("timestamp")
                    if la is None or lo is None:
                        continue
                    k = 180.0 / 2 ** 31  # semicircles -> degrees
                    pts.append({"lat": la * k, "lon": lo * k,
                                "utc": iso(ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) if ts else None})
        out = Path(fp).with_suffix(".gpx")
        write_gpx(out, [(Path(fp).stem, pts)])
        print(f"{out}: {len(pts)} pts")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe"); p.add_argument("file"); p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("ingest")
    p.add_argument("--src", required=True, help="camera volume, card, or folder (searched recursively)")
    p.add_argument("--dest", required=True)
    p.add_argument("--date", action="append", required=True, help="YYYY-MM-DD (repeatable)")
    p.add_argument("--start", help="HH:MM local (camera clock)"); p.add_argument("--end", help="HH:MM local")
    p.add_argument("--channels", default="FRI"); p.add_argument("--include-parking", action="store_true")
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("gps"); p.add_argument("dir")
    p.add_argument("--tz-offset", type=int, help="camera minutes ahead of UTC (e.g. -240 for EDT); overrides the GPS estimate")
    p.set_defaults(fn=cmd_gps)
    p = sub.add_parser("match"); p.add_argument("dir"); p.add_argument("--rides", nargs="+", required=True)
    p.add_argument("--radius", type=float, default=20.0, help="max meters from ride track")
    p.add_argument("--min-seconds", type=float, default=5.0); p.set_defaults(fn=cmd_match)
    p = sub.add_parser("fcpxml"); p.add_argument("dir"); p.add_argument("--matches")
    p.add_argument("--out", required=True); p.add_argument("--event", default="Dashcam B-roll")
    p.add_argument("--keep-gaps", action="store_true", help="day timelines keep real-time gaps between clips")
    p.add_argument("--handles", type=float, default=2.0, help="seconds of pre/post roll on matches")
    p.set_defaults(fn=cmd_fcpxml)
    p = sub.add_parser("fit2gpx"); p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_fit2gpx)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
