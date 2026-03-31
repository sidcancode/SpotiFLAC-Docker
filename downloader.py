"""
SpotiFLAC downloader — rebuilt from the original Go source (afkarxyz/SpotiFLAC).
Uses the exact same APIs, endpoints, and fallback chains as the desktop app.
"""

import os
import re
import sys
import time
import json
import base64
import random
import logging
import requests
import subprocess
from urllib.parse import quote
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dl")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

def new_session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s

SESSION = new_session()

def sanitize(s, fallback="Unknown"):
    if not s: return fallback
    return re.sub(r'[\\/*?:"<>|]', "", str(s)).strip() or fallback

# ── ISRC resolution — 4 providers (same as Go source) ────────────────────────

def isrc_via_phpstack(spotify_url):
    """phpstack cloudways API"""
    api = f"https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php?q={quote(spotify_url)}"
    r = SESSION.get(api, headers={"Referer": "https://phpstack-822472-6184058.cloudwaysapps.com/?"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    isrc = data.get("isrc", "").strip().upper()
    if not isrc: raise Exception("no ISRC in response")
    return isrc

def isrc_via_findmyisrc(spotify_url):
    """findmyisrc AWS Lambda"""
    r = SESSION.post(
        "https://lxtzsnh4l3.execute-api.ap-southeast-2.amazonaws.com/prod/find-my-isrc",
        json={"uris": [spotify_url]},
        headers={"Origin": "https://www.findmyisrc.com", "Referer": "https://www.findmyisrc.com/"},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    for item in (data if isinstance(data, list) else []):
        isrc = (item.get("data") or {}).get("isrc", "").strip().upper()
        if isrc: return isrc
    raise Exception("no ISRC in findmyisrc response")

def isrc_via_mixvibe(spotify_url):
    """mixvibe tools"""
    r = SESSION.post(
        "https://tools.mixviberecords.com/api/find-isrc",
        json={"url": spotify_url},
        headers={"Origin": "https://tools.mixviberecords.com",
                 "Referer": "https://tools.mixviberecords.com/isrc-finder"},
        timeout=15
    )
    r.raise_for_status()
    body = r.text
    # regex scan for ISRC pattern
    m = re.search(r'\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b', body.upper())
    if m: return m.group(1)
    raise Exception("no ISRC in mixvibe response")

def isrc_via_isrcfinder(spotify_url):
    """isrcfinder.com with CSRF token"""
    import http.cookiejar
    jar = requests.cookies.RequestsCookieJar()
    s = new_session()
    s.cookies = jar
    r = s.get("https://www.isrcfinder.com/", headers={"Referer": "https://www.isrcfinder.com/"}, timeout=20)
    # extract CSRF token
    m = re.search(r'name=["\']csrfmiddlewaretoken["\'][^>]*value=["\']([^"\']+)["\']', r.text)
    token = m.group(1) if m else ""
    if not token:
        for cookie in s.cookies:
            if cookie.name == "csrftoken":
                token = cookie.value
                break
    if not token: raise Exception("CSRF token not found")
    r2 = s.post("https://www.isrcfinder.com/",
        data={"csrfmiddlewaretoken": token, "URI": spotify_url},
        headers={"Referer": "https://www.isrcfinder.com/", "Origin": "https://www.isrcfinder.com",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20)
    m = re.search(r'\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b', r2.text.upper())
    if m: return m.group(1)
    raise Exception("no ISRC in isrcfinder response")

def get_isrc(spotify_track_id):
    """Try all 4 ISRC providers in order (same as Go source)"""
    spotify_url = f"https://open.spotify.com/track/{spotify_track_id}"
    providers = [
        ("isrcfinder", isrc_via_isrcfinder),
        ("phpstack", isrc_via_phpstack),
        ("findmyisrc", isrc_via_findmyisrc),
        ("mixvibe", isrc_via_mixvibe),
    ]
    for name, fn in providers:
        try:
            isrc = fn(spotify_url)
            log.info(f"[isrc] ✅ {name}: {isrc}")
            return isrc
        except Exception as e:
            log.warning(f"[isrc] {name}: {e}")
    raise Exception("All ISRC providers failed")

# ── Song.link / platform URL resolution ──────────────────────────────────────

def get_platform_urls(spotify_track_id):
    """Get Tidal/Amazon/Deezer URLs via song.link API (same as Go source)"""
    spotify_url = f"https://open.spotify.com/track/{spotify_track_id}"
    log.info(f"[songlink] Resolving {spotify_track_id}")
    try:
        r = SESSION.get(
            f"https://api.song.link/v1-alpha.1/links?url={quote(spotify_url)}",
            timeout=20
        )
        if r.status_code == 429:
            raise Exception("song.link rate limited")
        r.raise_for_status()
        data = r.json()
        links = data.get("linksByPlatform", {})
        result = {}
        if "tidal" in links:
            result["tidal"] = links["tidal"]["url"]
            log.info(f"[songlink] ✅ Tidal: {result['tidal']}")
        if "amazonMusic" in links:
            result["amazon"] = links["amazonMusic"]["url"]
            log.info(f"[songlink] ✅ Amazon: {result['amazon']}")
        if "deezer" in links:
            result["deezer"] = links["deezer"]["url"]
            log.info(f"[songlink] ✅ Deezer: {result['deezer']}")
        return result
    except Exception as e:
        log.warning(f"[songlink] {e}")
        return {}

def get_deezer_url_from_isrc(isrc):
    """Deezer ISRC API (used as fallback in Go source)"""
    try:
        r = SESSION.get(f"https://api.deezer.com/track/isrc:{isrc}", timeout=10)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise Exception(data["error"].get("message", "deezer error"))
        if data.get("link"):
            return data["link"]
        if data.get("id"):
            return f"https://www.deezer.com/track/{data['id']}"
    except Exception as e:
        log.warning(f"[deezer-isrc] {e}")
    return None

# ── Tidal ─────────────────────────────────────────────────────────────────────

TIDAL_APIS = [
    "https://hifi-one.spotisaver.net",
    "https://hifi-two.spotisaver.net",
    "https://eu-central.monochrome.tf",
    "https://us-west.monochrome.tf",
    "https://api.monochrome.tf",
    "https://monochrome-api.samidy.com",
    "https://tidal.kinoplus.online",
]

def get_tidal_track_id(tidal_url):
    m = re.search(r'/track/(\d+)', tidal_url)
    if m: return int(m.group(1))
    raise Exception(f"Cannot extract track ID from {tidal_url}")

def get_tidal_download_url(track_id, quality="HI_RES"):
    """Rotate through all Tidal APIs (same as Go getDownloadURLRotated)"""
    apis = TIDAL_APIS.copy()
    random.shuffle(apis)
    qualities = [quality, "LOSSLESS"] if quality == "HI_RES" else [quality]
    for qual in qualities:
        log.info(f"[tidal] Trying quality={qual} across {len(apis)} APIs")
        for api in apis:
            url = f"{api}/track/?id={track_id}&quality={qual}"
            try:
                r = SESSION.get(url, timeout=15)
                if r.status_code != 200:
                    log.warning(f"[tidal] {api.split('/')[2]}: HTTP {r.status_code}")
                    continue
                body = r.json()
                # v2 manifest
                if isinstance(body, dict) and body.get("data", {}).get("manifest"):
                    log.info(f"[tidal] ✅ manifest from {api.split('/')[2]}")
                    return "MANIFEST:" + body["data"]["manifest"]
                # v1 direct URL
                if isinstance(body, list):
                    for item in body:
                        if item.get("OriginalTrackUrl"):
                            log.info(f"[tidal] ✅ direct URL from {api.split('/')[2]}")
                            return item["OriginalTrackUrl"]
                log.warning(f"[tidal] {api.split('/')[2]}: no URL in response")
            except Exception as e:
                log.warning(f"[tidal] {api.split('/')[2]}: {e}")
    raise Exception("All Tidal APIs failed")

def download_tidal_manifest(manifest_b64, out_path):
    """Parse and download DASH/BTS manifest (ported from Go parseManifest)"""
    manifest_bytes = base64.b64decode(manifest_b64)
    manifest_str = manifest_bytes.decode(errors="ignore").strip()

    if manifest_str.startswith("{"):
        # BTS JSON format
        data = json.loads(manifest_str)
        urls = data.get("urls", [])
        mime = data.get("mimeType", "")
        if not urls: raise Exception("no URLs in BTS manifest")
        log.info(f"[tidal] BTS manifest ({mime})")
        if "flac" in mime.lower() or not mime:
            stream_download(urls[0], out_path)
        else:
            tmp = out_path + ".m4a.tmp"
            stream_download(urls[0], tmp)
            convert_to_flac(tmp, out_path)
        return

    # DASH XML format
    log.info("[tidal] DASH manifest")
    import xml.etree.ElementTree as ET
    tmp = out_path + ".m4a.tmp"
    try:
        root = ET.fromstring(manifest_str)
        ns = {"mpd": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        seg = None
        best_bw = 0
        for rep in root.findall(".//{*}Representation"):
            bw = int(rep.get("bandwidth", 0))
            st = rep.find("{*}SegmentTemplate") or rep.find("SegmentTemplate")
            if st is not None and bw > best_bw:
                best_bw = bw
                seg = st
        if seg is None:
            for st in root.findall(".//{*}SegmentTemplate"):
                seg = st; break

        if seg is None:
            raise Exception("no SegmentTemplate in DASH manifest")

        init_url = seg.get("initialization", "").replace("&amp;", "&")
        media_tmpl = seg.get("media", "").replace("&amp;", "&")
        timeline = seg.find("{*}SegmentTimeline") or seg.find("SegmentTimeline")
        count = 0
        if timeline is not None:
            for s in list(timeline):
                count += int(s.get("r", 0)) + 1

        if count == 0:
            raise Exception("no segments in DASH manifest")

        log.info(f"[tidal] DASH: {count} segments")
        with open(tmp, "wb") as f:
            r = SESSION.get(init_url, timeout=30)
            r.raise_for_status()
            f.write(r.content)
            for i in range(1, count + 1):
                seg_url = media_tmpl.replace("$Number$", str(i))
                r = SESSION.get(seg_url, timeout=30)
                r.raise_for_status()
                f.write(r.content)
        convert_to_flac(tmp, out_path)

    except Exception as e:
        # regex fallback
        init_m = re.search(r'initialization="([^"]+)"', manifest_str)
        media_m = re.search(r'media="([^"]+)"', manifest_str)
        if not init_m:
            raise Exception(f"DASH parse failed: {e}")
        init_url = init_m.group(1).replace("&amp;", "&")
        media_tmpl = media_m.group(1).replace("&amp;", "&") if media_m else ""
        segs = re.findall(r'<S\s[^>]*>', manifest_str)
        count = sum(int(re.search(r'r="(\d+)"', s).group(1)) + 1 if re.search(r'r="(\d+)"', s) else 1 for s in segs)
        log.info(f"[tidal] DASH regex fallback: {count} segments")
        with open(tmp, "wb") as f:
            r = SESSION.get(init_url, timeout=30); r.raise_for_status(); f.write(r.content)
            for i in range(1, count + 1):
                seg_url = media_tmpl.replace("$Number$", str(i))
                r = SESSION.get(seg_url, timeout=30); r.raise_for_status(); f.write(r.content)
        convert_to_flac(tmp, out_path)

# ── Qobuz ─────────────────────────────────────────────────────────────────────

# Exact providers from Go source (qbz.afkarxyz.qzz.io is the correct one, NOT .fun)
QOBUZ_PROVIDERS = [
    ("dab.yeet.su",   "https://dab.yeet.su/api/stream?trackId={id}&quality={q}"),
    ("dabmusic.xyz",  "https://dabmusic.xyz/api/stream?trackId={id}&quality={q}"),
    ("qbz.afkarxyz",  "https://qbz.afkarxyz.qzz.io/api/track/{id}?quality={q}"),
]
QOBUZ_QUALITIES = ["6", "7", "27", "5"]

def search_qobuz_by_isrc(isrc):
    """Search Qobuz by ISRC using public API (same as Go searchByISRC)"""
    r = SESSION.get(
        f"https://www.qobuz.com/api.json/0.2/track/search?query={isrc}&limit=1&app_id=798273057",
        timeout=15
    )
    if r.status_code != 200:
        raise Exception(f"Qobuz search HTTP {r.status_code}")
    items = r.json().get("tracks", {}).get("items", [])
    if not items:
        raise Exception(f"No Qobuz track for ISRC {isrc}")
    return items[0]

def get_qobuz_download_url(track_id):
    """Try all Qobuz providers across all qualities (same as Go GetDownloadURL)"""
    providers = QOBUZ_PROVIDERS.copy()
    random.shuffle(providers)
    for quality in QOBUZ_QUALITIES:
        for name, tmpl in providers:
            url = tmpl.format(id=track_id, q=quality)
            try:
                r = SESSION.get(url, timeout=20)
                if r.status_code != 200:
                    log.warning(f"[qobuz] {name} q={quality}: HTTP {r.status_code}")
                    continue
                data = r.json()
                dl_url = data.get("url") or (data.get("data") or {}).get("url")
                if dl_url:
                    log.info(f"[qobuz] ✅ {name} q={quality}")
                    return dl_url
                log.warning(f"[qobuz] {name} q={quality}: no URL in response: {str(data)[:80]}")
            except Exception as e:
                log.warning(f"[qobuz] {name} q={quality}: {e}")
    raise Exception("All Qobuz providers failed")

# ── Amazon ────────────────────────────────────────────────────────────────────

def normalize_amazon_url(url):
    """Extract track ASIN and build canonical URL (same as Go normalizeAmazonMusicURL)"""
    if "trackAsin=" in url:
        m = re.search(r'trackAsin=([^&]+)', url)
        if m: return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
    m = re.search(r'/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})', url)
    if m: return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
    m = re.search(r'/tracks/(B[0-9A-Z]{9})', url)
    if m: return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
    return ""

def get_amazon_track_id(amazon_url):
    m = re.search(r'/tracks/(B[0-9A-Z]{9})', amazon_url)
    return m.group(1) if m else None

AMAZON_APIS = [
    "https://hifi-one.spotisaver.net",
    "https://hifi-two.spotisaver.net",
    "https://eu-central.monochrome.tf",
    "https://us-west.monochrome.tf",
    "https://api.monochrome.tf",
    "https://monochrome-api.samidy.com",
    "https://tidal.kinoplus.online",
]

def get_amazon_download_url(track_id):
    """Same API rotation as Tidal but for Amazon"""
    apis = AMAZON_APIS.copy()
    random.shuffle(apis)
    for api in apis:
        url = f"{api}/track/?id={track_id}&quality=HIGH"
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code != 200: continue
            body = r.json()
            if isinstance(body, dict) and body.get("data", {}).get("manifest"):
                return "MANIFEST:" + body["data"]["manifest"]
            if isinstance(body, list):
                for item in body:
                    if item.get("OriginalTrackUrl"):
                        return item["OriginalTrackUrl"]
        except Exception as e:
            log.warning(f"[amazon] {api.split('/')[2]}: {e}")
    raise Exception("All Amazon APIs failed")

# ── File utilities ────────────────────────────────────────────────────────────

def stream_download(url, filepath, progress_cb=None):
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    tmp = filepath + ".part"
    try:
        with SESSION.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb: progress_cb(done, total)
        os.replace(tmp, filepath)
        log.info(f"[dl] ✅ {done/1048576:.2f} MB → {os.path.basename(filepath)}")
    except Exception as e:
        if os.path.exists(tmp): os.remove(tmp)
        raise Exception(f"Download failed: {e}")

def convert_to_flac(src, dst):
    log.info(f"[ffmpeg] Converting {os.path.basename(src)} → FLAC")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "flac", dst],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {result.stderr[-300:]}")
    try: os.remove(src)
    except: pass

def embed_metadata(filepath, meta, cover_url=None):
    try:
        audio = FLAC(filepath)
        audio.delete()
        for k, v in meta.items():
            if v and str(v) not in ("", "0"):
                audio[k] = str(v)
        if cover_url:
            try:
                data = SESSION.get(cover_url, timeout=15).content
                pic = Picture()
                pic.data = data
                pic.type = PictureType.COVER_FRONT
                pic.mime = "image/jpeg"
                audio.add_picture(pic)
            except Exception as e:
                log.warning(f"[meta] cover: {e}")
        audio.save()
        log.info("[meta] ✅ Metadata embedded")
    except Exception as e:
        log.error(f"[meta] Failed: {e}")

# ── Main entry point ──────────────────────────────────────────────────────────

def download_track(isrc, output_dir, spotify_meta, services=None, progress_cb=None, filename_format=None):
    """
    Download a track. spotify_meta must contain:
    title, artist, album, album_artist, release_date, cover_url,
    track_number, disc_number, total_tracks, total_discs,
    isrc, spotify_url, spotify_id
    """
    if services is None:
        services = ["tidal", "qobuz", "amazon"]

    title = sanitize(spotify_meta.get("title", "Unknown"))
    artist = sanitize(spotify_meta.get("artist", "Unknown"))
    track_num = int(spotify_meta.get("track_number") or 1)
    # Build filename from format string
    if not filename_format:
        filename_format = "{track_number} - {title}"
    filename = (filename_format
        .replace("{title}", title)
        .replace("{artist}", artist)
        .replace("{album}", sanitize(spotify_meta.get("album", "")))
        .replace("{album_artist}", sanitize(spotify_meta.get("album_artist", "")))
        .replace("{track_number}", f"{track_num:02d}")
        .replace("{disc_number}", str(spotify_meta.get("disc_number", 1)))
        .replace("{year}", (spotify_meta.get("release_date") or "")[:4])
        .replace("{isrc}", isrc or "")
    ) + ".flac"
    filename = sanitize(filename, fallback="track.flac")
    filepath = os.path.join(output_dir, filename)

    log.info(f"\n{'='*60}")
    log.info(f"[{track_num:02d}] {title} — {artist}")
    log.info(f"ISRC: {isrc or '(none)'}")
    log.info(f"OUT:  {filepath}")

    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        log.info(f"[skip] Already exists ({os.path.getsize(filepath)//1024}KB)")
        return filepath

    os.makedirs(output_dir, exist_ok=True)

    meta_tags = {
        "TITLE": spotify_meta.get("title", ""),
        "ARTIST": spotify_meta.get("artist", ""),
        "ALBUM": spotify_meta.get("album", ""),
        "ALBUMARTIST": spotify_meta.get("album_artist", ""),
        "DATE": (spotify_meta.get("release_date") or "")[:4],
        "TRACKNUMBER": str(track_num),
        "TRACKTOTAL": str(spotify_meta.get("total_tracks", 0)),
        "DISCNUMBER": str(spotify_meta.get("disc_number", 1)),
        "DISCTOTAL": str(spotify_meta.get("total_discs", 1)),
        "ISRC": isrc or "",
        "DESCRIPTION": "https://github.com/afkarxyz/SpotiFLAC",
    }
    cover_url = spotify_meta.get("cover_url", "")
    spotify_id = spotify_meta.get("spotify_id", "")

    # Resolve ISRC if not provided
    if not isrc and spotify_id:
        try:
            isrc = get_isrc(spotify_id)
            meta_tags["ISRC"] = isrc
        except Exception as e:
            log.warning(f"[isrc] Failed: {e}")

    # Resolve platform URLs via song.link
    platform_urls = {}
    if spotify_id:
        platform_urls = get_platform_urls(spotify_id)

    # Add Deezer fallback via ISRC
    if "deezer" not in platform_urls and isrc:
        dz_url = get_deezer_url_from_isrc(isrc)
        if dz_url:
            platform_urls["deezer"] = dz_url
            log.info(f"[deezer] ISRC fallback: {dz_url}")

    for service in services:
        log.info(f"\n--- Trying {service.upper()} ---")
        try:
            if service == "tidal":
                tidal_url = platform_urls.get("tidal", "")
                if not tidal_url:
                    raise Exception("No Tidal URL from song.link")
                track_id = get_tidal_track_id(tidal_url)
                log.info(f"[tidal] Track ID: {track_id}")
                dl_url = get_tidal_download_url(track_id, "HI_RES")
                if dl_url.startswith("MANIFEST:"):
                    download_tidal_manifest(dl_url[9:], filepath)
                else:
                    stream_download(dl_url, filepath, progress_cb)
                embed_metadata(filepath, meta_tags, cover_url)
                log.info(f"✅ SUCCESS via Tidal: {filename}")
                return filepath

            elif service == "qobuz":
                if not isrc:
                    raise Exception("No ISRC — cannot search Qobuz")
                track = search_qobuz_by_isrc(isrc)
                track_id = track.get("id")
                if not track_id:
                    raise Exception("Qobuz track not found")
                quality_info = "Standard"
                if track.get("hires"):
                    quality_info = f"Hi-Res ({track.get('maximum_bit_depth',24)}-bit / {track.get('maximum_sampling_rate',96)}kHz)"
                log.info(f"[qobuz] Track ID: {track_id} Quality: {quality_info}")
                dl_url = get_qobuz_download_url(track_id)
                stream_download(dl_url, filepath, progress_cb)
                embed_metadata(filepath, meta_tags, cover_url)
                log.info(f"✅ SUCCESS via Qobuz: {filename}")
                return filepath

            elif service == "amazon":
                amazon_url = normalize_amazon_url(platform_urls.get("amazon", ""))
                if not amazon_url:
                    raise Exception("No Amazon URL from song.link")
                track_id = get_amazon_track_id(amazon_url)
                if not track_id:
                    raise Exception("Cannot extract Amazon track ASIN")
                log.info(f"[amazon] Track ASIN: {track_id}")
                dl_url = get_amazon_download_url(track_id)
                if dl_url.startswith("MANIFEST:"):
                    download_tidal_manifest(dl_url[9:], filepath)
                else:
                    tmp = filepath + ".amz.tmp"
                    stream_download(dl_url, tmp, progress_cb)
                    with open(tmp, "rb") as f:
                        header = f.read(4)
                    if header == b"fLaC":
                        os.replace(tmp, filepath)
                    else:
                        convert_to_flac(tmp, filepath)
                embed_metadata(filepath, meta_tags, cover_url)
                log.info(f"✅ SUCCESS via Amazon: {filename}")
                return filepath

        except Exception as e:
            log.error(f"✗ {service.upper()} FAILED: {e}")
            continue

    raise Exception(f"ALL SERVICES FAILED for: {title} — {artist} (ISRC: {isrc})")
