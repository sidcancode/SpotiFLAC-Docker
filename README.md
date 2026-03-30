# SpotiFLAC Docker

A self-hosted web interface for downloading music via [SpotiFLAC](https://github.com/afkarxyz/SpotiFLAC).  
Downloads true lossless FLAC from **Tidal**, **Qobuz** and **Amazon Music** using Spotify URLs for metadata — no paid accounts required.

## Features
- 🔍 Search tracks, albums and artists with cover art
- 💿 Album detail view — browse tracks and download individually
- 🎵 30-second preview player
- 📥 Download queue with live progress
- 🔄 Auto-updates SpotiFLAC on container restart
- 🗂️ Organizes files as `Artist/Album/Track.flac`

## Quick Start

```yaml
services:
  spotiflac:
    image: ghcr.io/YOURUSERNAME/spotiflac-webui:latest
    container_name: spotiflac
    ports:
      - "7171:7171"
    environment:
      - SPOTIFY_CLIENT_ID=your_client_id
      - SPOTIFY_CLIENT_SECRET=your_client_secret
      - DOWNLOAD_DIR=/downloads
    volumes:
      - /your/music/folder:/downloads
    restart: unless-stopped
```

Then open `http://localhost:7171`

## Spotify API Credentials (free)
1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Create an app — any name, redirect URI: `http://localhost:7171`
3. Copy your **Client ID** and **Client Secret** into the compose file

## Build Locally

```bash
git clone https://github.com/YOURUSERNAME/spotiflac-webui
cd spotiflac-webui
docker compose up -d --build
```
