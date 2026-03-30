import os, re, sys, threading, uuid, time, logging, subprocess, importlib.metadata
import requests
from flask import Flask, render_template, request, jsonify

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("app")

app = Flask(__name__)

DOWNLOAD_DIR   = os.environ.get("DOWNLOAD_DIR", "/downloads")
CLIENT_ID      = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET  = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

jobs = {}
_token = {"v": None, "exp": 0}

# ── Spotify auth ──────────────────────────────────────────────────────────────

def token():
    if _token["v"] and time.time() < _token["exp"]:
        return _token["v"]
    r = requests.post("https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET), timeout=10)
    r.raise_for_status()
    d = r.json()
    _token["v"] = d["access_token"]
    _token["exp"] = time.time() + d.get("expires_in", 3600) - 60
    return _token["v"]

def sp(path, **params):
    return requests.get(f"https://api.spotify.com/v1/{path}",
        headers={"Authorization": f"Bearer {token()}"}, params=params, timeout=10).json()

def sp_url_id(url):
    """Strip URL to clean ID"""
    return url.split("?")[0].rstrip("/").split("/")[-1]

# ── API helpers ───────────────────────────────────────────────────────────────

def track_to_meta(t, album=None):
    alb = album or t.get("album", {})
    return {
        "title":        t.get("name", ""),
        "artist":       ", ".join(a["name"] for a in t.get("artists", [])),
        "album":        alb.get("name", ""),
        "album_artist": ", ".join(a["name"] for a in alb.get("artists", [])),
        "release_date": alb.get("release_date", ""),
        "cover_url":    (alb.get("images") or [{}])[0].get("url", ""),
        "track_number": t.get("track_number", 1),
        "disc_number":  t.get("disc_number", 1),
        "total_tracks": alb.get("total_tracks", 1),
        "total_discs":  1,
        "isrc":         t.get("external_ids", {}).get("isrc", ""),
        "spotify_url":  f"https://open.spotify.com/track/{t['id']}",
        "spotify_id":   t["id"],
        "preview_url":  t.get("preview_url"),
        "duration_ms":  t.get("duration_ms", 0),
    }

def expand_url(url):
    """Return list of track meta dicts for any Spotify URL"""
    url = url.split("?")[0]
    tracks = []

    if "/track/" in url:
        tid = sp_url_id(url)
        t = sp(f"tracks/{tid}")
        tracks.append(track_to_meta(t))

    elif "/album/" in url:
        aid = sp_url_id(url)
        alb = sp(f"albums/{aid}")
        offset = 0
        while True:
            page = sp(f"albums/{aid}/tracks", limit=50, offset=offset)
            for t in page.get("items", []):
                full = sp(f"tracks/{t['id']}")
                tracks.append(track_to_meta(full, alb))
            if not page.get("next"): break
            offset += 50

    elif "/playlist/" in url:
        pid = sp_url_id(url)
        offset = 0
        while True:
            page = sp(f"playlists/{pid}/tracks", limit=50, offset=offset)
            for item in page.get("items", []):
                t = item.get("track")
                if not t or not t.get("id"): continue
                full = sp(f"tracks/{t['id']}")
                tracks.append(track_to_meta(full))
            if not page.get("next"): break
            offset += 50

    log.info(f"[expand] {len(tracks)} tracks")
    return tracks

def album_tracks(aid):
    """Return track list for album (with preview + duration for UI)"""
    alb = sp(f"albums/{aid}")
    result = []
    offset = 0
    while True:
        page = sp(f"albums/{aid}/tracks", limit=50, offset=offset)
        for t in page.get("items", []):
            full = sp(f"tracks/{t['id']}")
            result.append({
                "id":           t["id"],
                "track_number": t.get("track_number", 1),
                "name":         t.get("name", ""),
                "artist":       ", ".join(a["name"] for a in t.get("artists", [])),
                "duration_ms":  t.get("duration_ms", 0),
                "preview_url":  full.get("preview_url"),
                "explicit":     t.get("explicit", False),
                "url":          f"https://open.spotify.com/track/{t['id']}",
            })
        if not page.get("next"): break
        offset += 50
    return result, alb

# ── Download worker ───────────────────────────────────────────────────────────

def sanitize_dir(s):
    return re.sub(r'[\\/*?:"<>|]', "", str(s or "Unknown")).strip() or "Unknown"

def run_download(job_id, url, services):
    from downloader import download_track
    jobs[job_id].update(status="downloading", started_at=time.time())
    log.info(f"[job:{job_id}] {url} services={services}")
    try:
        tracks = expand_url(url)
        if not tracks:
            raise Exception("No tracks found")
        total = len(tracks)
        jobs[job_id].update(total=total, done=0, failed=0, errors=[])
        for i, track in enumerate(tracks):
            label = f"{track['artist']} — {track['title']}"
            jobs[job_id]["current_track"] = label
            log.info(f"[job:{job_id}] [{i+1}/{total}] {label}")
            try:
                out_dir = os.path.join(DOWNLOAD_DIR,
                    sanitize_dir(track["artist"].split(",")[0].strip()),
                    sanitize_dir(track["album"]))
                download_track(track.get("isrc",""), out_dir, track, services=services)
                jobs[job_id]["done"] += 1
            except Exception as e:
                log.error(f"[job:{job_id}] ✗ {label}: {e}")
                jobs[job_id]["failed"] += 1
                jobs[job_id]["errors"].append(f"[{i+1}] {track['title']}: {e}")
        d, f = jobs[job_id]["done"], jobs[job_id]["failed"]
        jobs[job_id]["status"] = "done" if f == 0 else ("partial" if d > 0 else "error")
        if f > 0 and d == 0:
            jobs[job_id]["error"] = f"All {f} tracks failed"
        log.info(f"[job:{job_id}] done={d} failed={f}")
    except Exception as e:
        log.error(f"[job:{job_id}] fatal: {e}")
        jobs[job_id].update(status="error", error=str(e))
    jobs[job_id].update(finished_at=time.time(), current_track=None)

# ── Version ───────────────────────────────────────────────────────────────────

def installed_version():
    try: return importlib.metadata.version("SpotiFLAC")
    except: return "unknown"

def latest_version():
    try: return requests.get("https://pypi.org/pypi/SpotiFLAC/json", timeout=8).json()["info"]["version"]
    except: return None

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index(): return render_template("index.html")

@app.get("/api/version")
def api_version():
    iv, lv = installed_version(), latest_version()
    return jsonify(installed=iv, latest=lv, update_available=bool(lv and lv != iv))

@app.post("/api/update")
def api_update():
    threading.Thread(target=lambda: subprocess.run(
        ["pip","install","--upgrade","--quiet","SpotiFLAC"], capture_output=True), daemon=True).start()
    return jsonify(status="updating")

@app.get("/api/search")
def api_search():
    q = request.args.get("q","").strip()
    if not q: return jsonify(error="No query"), 400
    try:
        data = requests.get("https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token()}"},
            params={"q": q, "type": "track,album,artist", "limit": 20},
            timeout=10).json()
        results = []
        for t in data.get("tracks",{}).get("items",[]):
            if not t: continue
            results.append({"type":"track","id":t["id"],
                "name":t["name"],
                "artist":", ".join(a["name"] for a in t["artists"]),
                "album":t["album"]["name"],
                "cover":(t["album"].get("images") or [{}])[0].get("url"),
                "preview_url":t.get("preview_url"),
                "duration_ms":t.get("duration_ms",0),
                "url":t["external_urls"]["spotify"]})
        for a in data.get("albums",{}).get("items",[]):
            if not a: continue
            results.append({"type":"album","id":sp_url_id(a["external_urls"]["spotify"]),
                "name":a["name"],
                "artist":", ".join(x["name"] for x in a["artists"]),
                "cover":(a.get("images") or [{}])[0].get("url"),
                "track_count":a.get("total_tracks",0),
                "year":(a.get("release_date") or "")[:4],
                "url":a["external_urls"]["spotify"]})
        for ar in data.get("artists",{}).get("items",[]):
            if not ar: continue
            results.append({"type":"artist","id":sp_url_id(ar["external_urls"]["spotify"]),
                "name":ar["name"],
                "cover":(ar.get("images") or [{}])[0].get("url"),
                "followers":ar.get("followers",{}).get("total",0),
                "url":ar["external_urls"]["spotify"]})
        return jsonify(results)
    except Exception as e:
        log.error(f"[search] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/album/<aid>")
def api_album(aid):
    """Return full track list for an album (for the album detail view)"""
    try:
        tracks, alb = album_tracks(aid)
        return jsonify({
            "id": aid,
            "name": alb.get("name",""),
            "artist": ", ".join(a["name"] for a in alb.get("artists",[])),
            "cover": (alb.get("images") or [{}])[0].get("url",""),
            "year": (alb.get("release_date") or "")[:4],
            "total_tracks": alb.get("total_tracks",0),
            "tracks": tracks,
        })
    except Exception as e:
        log.error(f"[album] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/lookup")
def api_lookup():
    url = request.args.get("url","").strip()
    if not url: return jsonify(error="No URL"), 400
    try:
        url_clean = url.split("?")[0]
        if "/track/" in url_clean:
            tid = sp_url_id(url_clean)
            t = sp(f"tracks/{tid}")
            return jsonify({"type":"track","id":tid,
                "name":t["name"],
                "artist":", ".join(a["name"] for a in t["artists"]),
                "album":t["album"]["name"],
                "cover":(t["album"].get("images") or [{}])[0].get("url"),
                "preview_url":t.get("preview_url"),
                "duration_ms":t.get("duration_ms",0),
                "url":f"https://open.spotify.com/track/{tid}"})
        elif "/album/" in url_clean:
            aid = sp_url_id(url_clean)
            a = sp(f"albums/{aid}")
            return jsonify({"type":"album","id":aid,
                "name":a["name"],
                "artist":", ".join(x["name"] for x in a["artists"]),
                "cover":(a.get("images") or [{}])[0].get("url"),
                "track_count":a.get("total_tracks",0),
                "year":(a.get("release_date") or "")[:4],
                "url":f"https://open.spotify.com/album/{aid}"})
        elif "/playlist/" in url_clean:
            pid = sp_url_id(url_clean)
            p = sp(f"playlists/{pid}")
            return jsonify({"type":"playlist","id":pid,
                "name":p["name"],
                "artist":p.get("owner",{}).get("display_name"),
                "cover":(p.get("images") or [{}])[0].get("url"),
                "track_count":p.get("tracks",{}).get("total",0),
                "url":f"https://open.spotify.com/playlist/{pid}"})
        elif "/artist/" in url_clean:
            xid = sp_url_id(url_clean)
            a = sp(f"artists/{xid}")
            return jsonify({"type":"artist","id":xid,
                "name":a["name"],
                "cover":(a.get("images") or [{}])[0].get("url"),
                "followers":a.get("followers",{}).get("total",0),
                "url":f"https://open.spotify.com/artist/{xid}"})
        return jsonify(error="Unsupported URL type"), 400
    except Exception as e:
        log.error(f"[lookup] {e}"); return jsonify(error=str(e)), 500

@app.post("/api/download")
def api_download():
    b = request.get_json() or {}
    url = b.get("url","").strip()
    services = b.get("services", ["tidal","qobuz","amazon"])
    if not url: return jsonify(error="No URL"), 400
    jid = str(uuid.uuid4())[:8]
    jobs[jid] = dict(id=jid, url=url, status="queued",
        created_at=time.time(), started_at=None, finished_at=None,
        error=None, errors=[], total=0, done=0, failed=0, current_track=None)
    threading.Thread(target=run_download, args=(jid, url, services), daemon=True).start()
    log.info(f"[job:{jid}] queued {url}")
    return jsonify(job_id=jid)

@app.get("/api/jobs")
def api_jobs(): return jsonify(list(reversed(list(jobs.values()))))

@app.get("/api/jobs/<jid>")
def api_job(jid):
    j = jobs.get(jid)
    return jsonify(j) if j else (jsonify(error="Not found"), 404)

if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    log.info(f"SpotiFLAC Web — dir={DOWNLOAD_DIR} version={installed_version()}")
    app.run(host="0.0.0.0", port=7171, debug=False)
