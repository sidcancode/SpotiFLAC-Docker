import os, re, sys, json, threading, uuid, time, logging, subprocess, importlib.metadata
import requests
from flask import Flask, render_template, request, jsonify

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("app")

app = Flask(__name__)

DOWNLOAD_DIR  = os.environ.get("DOWNLOAD_DIR", "/downloads")
CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
CONFIG_FILE   = os.environ.get("CONFIG_FILE", "/downloads/spotiflac_config.json")

jobs   = {}
_token = {"v": None, "exp": 0}

# ── Config file (settings persistence) ───────────────────────────────────────

CONFIG_DEFAULTS = {
    "filename_format":  "{track_number} - {title}",
    "folder_structure": "artist_album",
    "first_artist_only": False,
    "services": ["tidal", "qobuz", "amazon"],
}

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        return {**CONFIG_DEFAULTS, **data}
    except Exception:
        return dict(CONFIG_DEFAULTS)

def save_config(data):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        merged = {**load_config(), **data}
        with open(CONFIG_FILE, "w") as f:
            json.dump(merged, f, indent=2)
        return merged
    except Exception as e:
        log.error(f"[config] save failed: {e}")
        return load_config()

@app.get("/api/config")
def api_config_get():
    return jsonify(load_config())

@app.post("/api/config")
def api_config_set():
    data = request.get_json() or {}
    return jsonify(save_config(data))

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
        "duration_ms":  t.get("duration_ms", 0),
    }

def expand_url(url):
    url = url.split("?")[0]
    tracks = []
    if "/track/" in url:
        t = sp(f"tracks/{sp_url_id(url)}")
        tracks.append(track_to_meta(t))
    elif "/album/" in url:
        aid = sp_url_id(url)
        alb = sp(f"albums/{aid}")
        offset = 0
        while True:
            page = sp(f"albums/{aid}/tracks", limit=50, offset=offset)
            for t in page.get("items", []):
                tracks.append(track_to_meta(sp(f"tracks/{t['id']}"), alb))
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
                tracks.append(track_to_meta(sp(f"tracks/{t['id']}")))
            if not page.get("next"): break
            offset += 50
    elif "/artist/" in url:
        aid = sp_url_id(url)
        top = sp(f"artists/{aid}/top-tracks", market="US")
        for t in top.get("tracks", []):
            tracks.append(track_to_meta(sp(f"tracks/{t['id']}")))
    log.info(f"[expand] {len(tracks)} tracks")
    return tracks

def album_tracks(aid):
    alb = sp(f"albums/{aid}")
    result, offset = [], 0
    while True:
        page = sp(f"albums/{aid}/tracks", limit=50, offset=offset)
        for t in page.get("items", []):
            result.append({
                "id":           t["id"],
                "track_number": t.get("track_number", 1),
                "name":         t.get("name", ""),
                "artist":       ", ".join(a["name"] for a in t.get("artists", [])),
                "duration_ms":  t.get("duration_ms", 0),
                "explicit":     t.get("explicit", False),
                "url":          f"https://open.spotify.com/track/{t['id']}",
            })
        if not page.get("next"): break
        offset += 50
    return result, alb

# ── Download worker ───────────────────────────────────────────────────────────

def sanitize_dir(s):
    return re.sub(r'[\\/*?:"<>|]', "", str(s or "Unknown")).strip() or "Unknown"

def run_download(job_id, url, services, opts=None):
    from downloader import download_track
    opts = opts or {}
    cfg  = load_config()
    filename_format   = opts.get("filename_format",  cfg["filename_format"])
    folder_structure  = opts.get("folder_structure", cfg["folder_structure"])
    first_artist_only = opts.get("first_artist_only", cfg["first_artist_only"])

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
            try:
                primary_artist = track["artist"].split(",")[0].strip()
                if first_artist_only:
                    track = {**track, "artist": primary_artist}
                if folder_structure == "artist_album":
                    out_dir = os.path.join(DOWNLOAD_DIR, sanitize_dir(primary_artist), sanitize_dir(track["album"]))
                elif folder_structure == "artist":
                    out_dir = os.path.join(DOWNLOAD_DIR, sanitize_dir(primary_artist))
                else:
                    out_dir = DOWNLOAD_DIR
                download_track(track.get("isrc",""), out_dir, track,
                    services=services, filename_format=filename_format)
                jobs[job_id]["done"] += 1
            except Exception as e:
                log.error(f"[job:{job_id}] ✗ {label}: {e}")
                jobs[job_id]["failed"] += 1
                jobs[job_id]["errors"].append(f"[{i+1}] {track['title']}: {e}")
        d, f = jobs[job_id]["done"], jobs[job_id]["failed"]
        jobs[job_id]["status"] = "done" if f == 0 else ("partial" if d > 0 else "error")
        if f > 0 and d == 0:
            jobs[job_id]["error"] = f"All {f} tracks failed"
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

@app.get("/api/suggest")
def api_suggest():
    q = request.args.get("q", "").strip()
    if not q: return jsonify(error="No query"), 400
    try:
        data = requests.get("https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token()}"},
            params={"q": q, "type": "track,album,artist,show", "limit": 5, "market": "US"},
            timeout=8).json()
        results = []
        for ar in data.get("artists", {}).get("items", [])[:2]:
            if not ar: continue
            results.append({"type":"artist","id":sp_url_id(ar["external_urls"]["spotify"]),
                "name":ar["name"],"cover":(ar.get("images") or [{}])[0].get("url"),
                "followers":ar.get("followers",{}).get("total",0),"url":ar["external_urls"]["spotify"]})
        for a in data.get("albums", {}).get("items", [])[:2]:
            if not a: continue
            results.append({"type":"album","id":sp_url_id(a["external_urls"]["spotify"]),
                "name":a["name"],"artist":", ".join(x["name"] for x in a["artists"]),
                "cover":(a.get("images") or [{}])[0].get("url"),
                "track_count":a.get("total_tracks",0),"year":(a.get("release_date") or "")[:4],
                "url":a["external_urls"]["spotify"]})
        for t in data.get("tracks", {}).get("items", [])[:4]:
            if not t: continue
            results.append({"type":"track","id":t["id"],"name":t["name"],
                "artist":", ".join(a["name"] for a in t["artists"]),"album":t["album"]["name"],
                "cover":(t["album"].get("images") or [{}])[0].get("url"),
                "duration_ms":t.get("duration_ms",0),"url":t["external_urls"]["spotify"]})
        for s in data.get("shows", {}).get("items", [])[:2]:
            if not s: continue
            results.append({"type":"show","id":sp_url_id(s["external_urls"]["spotify"]),
                "name":s["name"],"artist":s.get("publisher",""),
                "cover":(s.get("images") or [{}])[0].get("url"),
                "description":s.get("description","")[:80],
                "url":s["external_urls"]["spotify"]})
        return jsonify(results)
    except Exception as e:
        log.error(f"[suggest] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/search")
def api_search():
    q = request.args.get("q","").strip()
    if not q: return jsonify(error="No query"), 400
    try:
        data = requests.get("https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token()}"},
            params={"q": q, "type": "track,album,artist,show", "limit": 20, "market": "US"},
            timeout=10).json()
        results = []
        for ar in data.get("artists",{}).get("items",[]):
            if not ar: continue
            results.append({"type":"artist","id":sp_url_id(ar["external_urls"]["spotify"]),
                "name":ar["name"],"cover":(ar.get("images") or [{}])[0].get("url"),
                "followers":ar.get("followers",{}).get("total",0),"url":ar["external_urls"]["spotify"]})
        for a in data.get("albums",{}).get("items",[]):
            if not a: continue
            results.append({"type":"album","id":sp_url_id(a["external_urls"]["spotify"]),
                "name":a["name"],"artist":", ".join(x["name"] for x in a["artists"]),
                "cover":(a.get("images") or [{}])[0].get("url"),
                "track_count":a.get("total_tracks",0),"year":(a.get("release_date") or "")[:4],
                "url":a["external_urls"]["spotify"]})
        for t in data.get("tracks",{}).get("items",[]):
            if not t: continue
            results.append({"type":"track","id":t["id"],"name":t["name"],
                "artist":", ".join(a["name"] for a in t["artists"]),"album":t["album"]["name"],
                "cover":(t["album"].get("images") or [{}])[0].get("url"),
                "duration_ms":t.get("duration_ms",0),"url":t["external_urls"]["spotify"]})
        for s in data.get("shows",{}).get("items",[]):
            if not s: continue
            results.append({"type":"show","id":sp_url_id(s["external_urls"]["spotify"]),
                "name":s["name"],"artist":s.get("publisher",""),
                "cover":(s.get("images") or [{}])[0].get("url"),
                "description":(s.get("description") or "")[:100],
                "url":s["external_urls"]["spotify"]})
        return jsonify(results)
    except Exception as e:
        log.error(f"[search] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/artist/<aid>")
def api_artist(aid):
    try:
        artist = sp(f"artists/{aid}")
        albums_page = sp(f"artists/{aid}/albums", include_groups="album,single", limit=50)
        top_data = sp(f"artists/{aid}/top-tracks", market="US")
        top_tracks = []
        for t in top_data.get("tracks", [])[:10]:
            top_tracks.append({
                "id":t["id"],"name":t["name"],
                "artist":", ".join(a["name"] for a in t.get("artists",[])),
                "album":t["album"]["name"],
                "cover":(t["album"].get("images") or [{}])[0].get("url",""),
                "duration_ms":t.get("duration_ms",0),
                "url":t["external_urls"]["spotify"],
            })
        return jsonify({
            "id":aid,"name":artist.get("name",""),
            "cover":(artist.get("images") or [{}])[0].get("url",""),
            "followers":artist.get("followers",{}).get("total",0),
            "top_tracks":top_tracks,
            "albums":[{
                "id":sp_url_id(a["external_urls"]["spotify"]),"name":a["name"],
                "images":a.get("images",[]),"total_tracks":a.get("total_tracks",0),
                "release_date":a.get("release_date",""),
            } for a in albums_page.get("items",[])],
        })
    except Exception as e:
        log.error(f"[artist] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/album/<aid>")
def api_album(aid):
    try:
        tracks, alb = album_tracks(aid)
        return jsonify({
            "id":aid,"name":alb.get("name",""),
            "artist":", ".join(a["name"] for a in alb.get("artists",[])),
            "cover":(alb.get("images") or [{}])[0].get("url",""),
            "year":(alb.get("release_date") or "")[:4],
            "total_tracks":alb.get("total_tracks",0),"tracks":tracks,
        })
    except Exception as e:
        log.error(f"[album] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/playlist/<pid>")
def api_playlist(pid):
    try:
        pl = sp(f"playlists/{pid}")
        tracks, offset = [], 0
        while True:
            page = sp(f"playlists/{pid}/tracks", limit=50, offset=offset)
            for item in page.get("items", []):
                t = item.get("track")
                if not t or not t.get("id"): continue
                tracks.append({
                    "id":t["id"],"name":t.get("name",""),
                    "artist":", ".join(a["name"] for a in t.get("artists",[])),
                    "album":t["album"]["name"] if t.get("album") else "",
                    "cover":(t["album"].get("images") or [{}])[0].get("url","") if t.get("album") else "",
                    "duration_ms":t.get("duration_ms",0),
                    "url":t["external_urls"]["spotify"],
                })
            if not page.get("next"): break
            offset += 50
        return jsonify({
            "id":pid,"name":pl.get("name",""),
            "owner":pl.get("owner",{}).get("display_name",""),
            "description":pl.get("description",""),
            "cover":(pl.get("images") or [{}])[0].get("url",""),
            "total_tracks":pl.get("tracks",{}).get("total",0),"tracks":tracks,
        })
    except Exception as e:
        log.error(f"[playlist] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/show/<sid>")
def api_show(sid):
    """Podcast show — returns info + episodes"""
    try:
        show = sp(f"shows/{sid}", market="US")
        episodes_page = sp(f"shows/{sid}/episodes", limit=50, market="US")
        episodes = []
        for ep in episodes_page.get("items", []):
            if not ep: continue
            episodes.append({
                "id":ep["id"],"name":ep.get("name",""),
                "description":(ep.get("description") or "")[:200],
                "duration_ms":ep.get("duration_ms",0),
                "release_date":ep.get("release_date",""),
                "url":ep["external_urls"]["spotify"],
            })
        return jsonify({
            "id":sid,"name":show.get("name",""),
            "publisher":show.get("publisher",""),
            "description":(show.get("description") or "")[:300],
            "cover":(show.get("images") or [{}])[0].get("url",""),
            "total_episodes":show.get("total_episodes",0),
            "episodes":episodes,
        })
    except Exception as e:
        log.error(f"[show] {e}"); return jsonify(error=str(e)), 500

@app.get("/api/lookup")
def api_lookup():
    url = request.args.get("url","").strip()
    if not url: return jsonify(error="No URL"), 400
    try:
        clean = url.split("?")[0]
        if "/track/" in clean:
            tid = sp_url_id(clean); t = sp(f"tracks/{tid}")
            return jsonify({"type":"track","id":tid,"name":t["name"],
                "artist":", ".join(a["name"] for a in t["artists"]),
                "album":t["album"]["name"],"cover":(t["album"].get("images") or [{}])[0].get("url"),
                "duration_ms":t.get("duration_ms",0),"url":f"https://open.spotify.com/track/{tid}"})
        elif "/album/" in clean:
            aid = sp_url_id(clean); a = sp(f"albums/{aid}")
            return jsonify({"type":"album","id":aid,"name":a["name"],
                "artist":", ".join(x["name"] for x in a["artists"]),
                "cover":(a.get("images") or [{}])[0].get("url"),
                "track_count":a.get("total_tracks",0),"year":(a.get("release_date") or "")[:4],
                "url":f"https://open.spotify.com/album/{aid}"})
        elif "/playlist/" in clean:
            pid = sp_url_id(clean); p = sp(f"playlists/{pid}")
            return jsonify({"type":"playlist","id":pid,"name":p["name"],
                "owner":p.get("owner",{}).get("display_name",""),
                "cover":(p.get("images") or [{}])[0].get("url"),
                "track_count":p.get("tracks",{}).get("total",0),
                "url":f"https://open.spotify.com/playlist/{pid}"})
        elif "/artist/" in clean:
            xid = sp_url_id(clean); a = sp(f"artists/{xid}")
            return jsonify({"type":"artist","id":xid,"name":a["name"],
                "cover":(a.get("images") or [{}])[0].get("url"),
                "followers":a.get("followers",{}).get("total",0),
                "url":f"https://open.spotify.com/artist/{xid}"})
        elif "/show/" in clean:
            sid = sp_url_id(clean); s = sp(f"shows/{sid}", market="US")
            return jsonify({"type":"show","id":sid,"name":s["name"],
                "publisher":s.get("publisher",""),
                "cover":(s.get("images") or [{}])[0].get("url"),
                "total_episodes":s.get("total_episodes",0),
                "url":f"https://open.spotify.com/show/{sid}"})
        return jsonify(error="Unsupported URL type"), 400
    except Exception as e:
        log.error(f"[lookup] {e}"); return jsonify(error=str(e)), 500

@app.post("/api/download")
def api_download():
    b = request.get_json() or {}
    url = b.get("url","").strip()
    if not url: return jsonify(error="No URL"), 400
    cfg = load_config()
    services = b.get("services", cfg.get("services", ["tidal","qobuz","amazon"]))
    jid = str(uuid.uuid4())[:8]
    jobs[jid] = dict(id=jid, url=url, status="queued",
        created_at=time.time(), started_at=None, finished_at=None,
        error=None, errors=[], total=0, done=0, failed=0, current_track=None)
    opts = {k: b[k] for k in ("filename_format","folder_structure","first_artist_only") if k in b}
    threading.Thread(target=run_download, args=(jid, url, services, opts), daemon=True).start()
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
