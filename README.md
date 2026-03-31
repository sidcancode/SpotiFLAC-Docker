# 🎧 SpotiFLAC WebUI (Docker)

A lightweight **Flask-based web interface** for SpotiFLAC that lets you download Spotify tracks and playlists in high quality (FLAC) using multiple fallback sources — fully containerized with Docker.

---

## 🚀 Features

* 🌐 **Web UI** – Download tracks/playlists directly from your browser
* 🎵 **Spotify link support** – Paste track, album, or playlist URLs
* 🔄 **Multi-source fallback** – Uses:

  * Tidal
  * Qobuz
  * Amazon Music
* 🧠 **ISRC-based matching** for accurate track retrieval
* 🏷️ **Automatic metadata tagging** (FLAC tags, cover art)
* 📁 **Custom filename & folder structure**
* 💾 **Persistent config system** (saved to JSON)
* ⚙️ **Configurable via environment variables**
* 📦 **Fully Dockerized**
* 🧵 **Background job system** (non-blocking downloads)
* 🔌 **API endpoints** for frontend interaction

---

## 🧠 How it works

1. You paste a Spotify URL into the web UI
2. The app extracts track metadata
3. It resolves the **ISRC** using multiple providers
4. Searches external services (Tidal/Qobuz/Amazon)
5. Downloads the best available FLAC
6. Applies metadata (tags, cover art, filenames)

---

## 📦 Installation

### 🐳 Docker (recommended)

```bash
docker run -d \
  -p 7171:7171 \
  -v ./downloads:/downloads \
  -v ./config:/config \  
  -e SPOTIFY_CLIENT_ID=your_id \
  -e SPOTIFY_CLIENT_SECRET=your_secret \
  -e DOWNLOAD_DIR=/downloads \
  -e CONFIG_FILE=/config/settings.json \  
  --name spotiflac-webui \
  ghcr.io/sidcancode/spotiflac-docker:latest
```

Open:

```
http://localhost:7171
```

---

### 🧩 Docker Compose

```yaml
version: "3.8"

services:
  spotiflac-ui:
    image: ghcr.io/sidcancode/spotiflac-docker:latest
    container_name: spotiflac-webui
    ports:
      - "7171:7171"
    volumes:
      - ./downloads:/downloads
      - ./config:/config
    environment:
      - SPOTIFY_CLIENT_ID=your_id
      - SPOTIFY_CLIENT_SECRET=your_secret
      - CONFIG_FILE=/config/settings.json
      - DOWNLOAD_DIR=/downloads
    restart: unless-stopped
```

```bash
docker-compose up -d
```

---

## ⚙️ Environment Variables

| Variable                | Description           | Default                            |
| ----------------------- | --------------------- | ---------------------------------- |
| `DOWNLOAD_DIR`          | Output directory      | `/downloads`                       |
| `SPOTIFY_CLIENT_ID`     | Spotify API client ID | required                           |
| `SPOTIFY_CLIENT_SECRET` | Spotify API secret    | required                           |
| `CONFIG_FILE`           | Config file path      | `/downloads/spotiflac_config.json` |

---

## 🛠️ Configuration

Settings are stored in a JSON file and include:

* Filename format
* Folder structure
* Artist handling
* Enabled download services

These can be modified via the UI or API.

---

## 📡 API Endpoints

| Endpoint             | Description        |
| -------------------- | ------------------ |
| `/api/config`        | Get current config |
| `/api/config` (POST) | Update config      |
| `/api/download`      | Start download job |
| `/api/status/<id>`   | Check job status   |

---

## 📂 Project Structure

```
.
├── app.py                  # Flask app + API
├── downloader.py           # Core download logic
├── templates/index.html    # Web UI
├── Dockerfile
├── docker-compose.yml
└── entrypoint.sh  
```

---

## 🧩 Technical Details

* Uses **requests sessions** with custom headers
* Multiple ISRC resolution providers for reliability
* FLAC tagging via `mutagen`
* Background jobs handled with threading
* Uses subprocess calls for downloader integration

---

## ⚠️ Disclaimer

This project:

* Does **not** download audio from Spotify directly
* Uses Spotify only for metadata
* Downloads audio from external services

Use responsibly and respect copyright laws.

---

## 🧠 Roadmap

* [ ] Download progress bar
* [ ] Queue management
* [ ] Better error handling
* [ ] Authentication system
* [ ] UI improvements

---

## 🤝 Contributing

Pull requests are welcome.
Feel free to open issues for bugs or feature requests.

---

## ⭐ Credits

* SpotiFLAC (original project)
* Flask
* Docker
* Mutagen

---

## 📜 License

MIT License
