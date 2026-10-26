# Jellyfin + TorBox + Tailscale Media Server

[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Jellyfin](https://img.shields.io/badge/Jellyfin-10.9+-00A4DC?logo=jellyfin&logoColor=white)](https://jellyfin.org/)
[![Tailscale](https://img.shields.io/badge/Tailscale-Mesh%20%26%20Funnel-24292e?logo=tailscale&logoColor=white)](https://tailscale.com/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An all-in-one, isolated Docker-based **Jellyfin** media server tightly integrated with **TorBox** cloud debrid streaming (`.strm` generation, cache warming, TMDb metadata resolution) and **Tailscale** for private WireGuard mesh access and zero-config public HTTPS streaming (Tailscale Funnel).

Engineered specifically for **100% Direct Play streaming** with **0% CPU transcoding strain** on the host machine.

---

## Architecture Overview

```
                                  ┌────────────────────────────────────────────────────────┐
                                  │               Docker Network Namespace                 │
                                  │                                                        │
 ┌─────────────────────────┐      │   ┌────────────────────────────────────────────────┐   │
 │   Tailscale WireGuard   │◄────┼───┤               tailscale-jellyfin               │   │
 │   Mesh (100.x.y.z:8096) │      │   │  - Dedicated container network namespace       │   │
 └─────────────────────────┘      │   │  - Host network completely untouched           │   │
                                  │   │  - Public HTTPS Funnel via Let's Encrypt       │   │
 ┌─────────────────────────┐      │   └───────────────────────▲────────────────────────┘   │
 │   Tailscale Funnel      │◄────┼───────────────────────────┤ network_mode:              │
 │   Public HTTPS (443)    │      │                           │ service:tailscale          │
 └─────────────────────────┘      │   ┌───────────────────────▼────────────────────────┐   │
                                  │   │                    jellyfin                    │   │
 ┌─────────────────────────┐      │   │  - Media library loaded from ./media (.strm)   │   │
 │   Local Maintenance     │◄────┼───┤  - Video Transcoding: DISABLED (0% CPU load)   │   │
 │   (127.0.0.1:8096)      │      │   │  - Audio Remuxing: ENABLED (Apple DTS -> AAC)  │   │
 └─────────────────────────┘      │   └───────────────────────▲────────────────────────┘   │
                                  │                           │ network_mode:              │
                                  │                           │ service:tailscale          │
                                  │   ┌───────────────────────▼────────────────────────┐   │
                                  │   │                torbox-assistant                │   │
                                  │   │  - Autonomous TMDb ID matcher & STRM generator │   │
                                  │   │  - Automated TorBox Cache Warmer daemon        │   │
                                  │   │  - Docker network namespace watchdog           │   │
                                  │   │  - Tailscale Funnel keepalive heartbeat        │   │
                                  │   └────────────────────────────────────────────────┘   │
                                  └────────────────────────────────────────────────────────┘
```

---

## Key Features

### 1. Standalone TorBox Assistant (`torbox-assistant`)
- **Autonomous Cloud Sync**: Periodically polls the TorBox API (torrents and web downloads), matches titles against The Movie Database (TMDb), downloads high-resolution posters and NFOs, and outputs standardized `.strm` files directly into `./media/`.
- **Zero Plex Dependency**: 100% self-contained. Generates standalone `.strm` files and maintains its own persistent SQLite database (`torbox_assistant/data/media_cache.db`).
- **Two-Way Persistence**: If you manually move a title (e.g. from `Uncategorized` to `Movies`, or `Anime Movies` to `Movies`) or customize the `[tmdbid-XXXXX]` folder tag on disk, the generator automatically detects your modification, persists it in the database, and never reverts your changes.
- **Multi-Version Quality Disambiguation**: When multiple releases exist for the same title (e.g. 4K IMAX Remux vs. 1080p SDR), files are written using Jellyfin's multi-version schema (`Title (Year) [tmdbid-XXXXX] - [Quality].strm`). Jellyfin merges them cleanly under a single movie card with a version dropdown.
- **Regional & Anime Isolation**:
  - Heuristics automatically separate anime movies and TV shows into dedicated libraries (`Anime Movies` and `Anime TV`).
  - Strict filtering handles non-anime titles correctly.
  - Enhanced heuristics ensure accurate TMDb matching for Indian regional cinema (Tamil, Telugu, Malayalam, Hindi, Kannada).
  - Isolates *Bleach: Thousand-Year Blood War* releases into their own TV series structure to prevent contamination of the legacy 2004 Bleach library.
- **Extras & Sample Filtering**: Non-episode content (samples, creditless OPs/EDs, trailers, featurettes) is automatically filtered out from TV seasons so it never overwrites Episode 1.

### 2. The Transcoding Lockdown Invariant
- **0% Host CPU Load (`EnableVideoPlaybackTranscoding=False`)**: Heavy 4K Remux video streams are never re-encoded by Jellyfin. Your host machine stays whisper quiet and cool.
- **Apple DTS Compatibility (`EnableAudioPlaybackTranscoding=True` & `EnablePlaybackRemuxing=True`)**:
  - *The Problem*: Apple devices (iOS, macOS, Apple TV) lack native decoders for DTS audio tracks (DTS-HD MA, DTS 5.1). If audio transcoding is completely blocked, Jellyfin fails to play the file entirely with `"Playback error, failed to load file"`.
  - *The Solution*: Audio remuxing repackages DTS into AAC (`-codec:v copy -codec:a libfdk_aac`) with negligible ($< 1\%$) CPU overhead while keeping video 100% Direct Stream.

### 3. Containerized Tailscale Mesh & Funnel
- **Network Isolation**: Tailscale runs inside Docker via `service:tailscale` with `--device /dev/net/tun`. The host system's network routing is never modified.
- **Private Tailscale Mesh**: Secure peer-to-peer WireGuard connections on `100.x.y.z:8096` or `http://jellyfin-server:8096` with zero bandwidth throttling and lowest latency.
- **Public HTTPS Funnel**: Exposes Jellyfin publicly on port 443 with automated Let's Encrypt TLS certificates. Friends and family can stream without installing Tailscale or creating an account.

### 4. Automated Auto-Healing & Watchdogs
- **Docker UI Restart Watchdog**: When containers share a network namespace (`network_mode: service:tailscale`), restarting Tailscale in Docker Desktop leaves dependent containers orphaned in a dead namespace. `supervisor.py` monitors the Docker API socket and automatically reconnects networks and restarts Jellyfin and TorBox Assistant when needed.
- **DERP Keepalive Heartbeat**: Pings the public Tailscale Funnel endpoint every 60 seconds to prevent edge relay NAT state dropouts. If the public ingress session goes stale while local Jellyfin is healthy, the daemon automatically re-establishes the tunnel.

### 5. TorBox Cache Warmer
- Runs continuously on a configurable multi-day cycle (default: 10 days).
- Automatically calculates rate-limited request pacing across all active torrents and downloads to keep TorBox debrid caches alive without hitting API rate limits.
- Resumes progress across container restarts.

### 6. Automated 1-Click Provisioning (`setup_jellyfin.py`)
- Automatically walks through the initial Jellyfin setup wizard.
- Creates the admin account with your desired or auto-generated secure credentials.
- Configures the 4 default media libraries:
  - `Films` (`/media/Movies`)
  - `TV Programmes` (`/media/TV`)
  - `Films (Anime)` (`/media/Anime Movies`)
  - `TV Programmes (Anime)` (`/media/Anime TV`)
- Disables expensive background extraction tasks (trickplay, chapter images) for instant library scans.
- Enforces the zero-transcode policy and exports the API key.

---

## Directory Structure

```text
.
├── .env.example                       # Environment variables template
├── .gitignore                         # Protects secrets, configs, and media from git
├── docker-compose.yml                 # Tailscale + Jellyfin + TorBox Assistant stack
├── setup_jellyfin.py                  # Automated 1-click Jellyfin setup & library configurator
├── TAILSCALE_FUNNEL.md                # Guide for public HTTPS access via Tailscale Funnel
├── media/                             # Auto-populated with .strm files and NFOs (runtime)
│   ├── Movies/
│   ├── TV/
│   ├── Anime Movies/
│   └── Anime TV/
├── scripts/
│   └── sync_plex_watch_history.py     # Utility to migrate watch history from Plex to Jellyfin
├── tailscale_config/
│   └── serve.json                     # Universal Funnel configuration (${TS_CERT_DOMAIN})
└── torbox_assistant/
    ├── Dockerfile                     # Python assistant container
    ├── requirements.txt               # Dependencies (requests, tmdbv3api, python-dotenv)
    ├── supervisor.py                  # Process supervisor, watchdog, and Funnel keepalive
    ├── jellyfin_strm_generator.py     # STRM generator & TMDb metadata resolver
    ├── warmer.py                      # Autonomous TorBox cache warmer
    ├── proxy.py                       # Hybrid Swarm streaming proxy
    ├── data/                          # Persistent SQLite database & warmer progress
    └── logs/                          # Generator, supervisor, and warmer logs
```

---

## Prerequisites

Before starting, ensure you have:
1. **Docker & Docker Compose** installed and running on your host system.
2. A **[TorBox](https://torbox.app)** account (Pro or Standard) with an API token.
3. A free **[TMDb](https://www.themoviedb.org/settings/api)** API Key (v3).
4. A **[Tailscale](https://tailscale.com/)** account.
5. **Python 3.8+** on your host (to run `setup_jellyfin.py`).

---

## Quick Start Guide

### Step 1: Clone the Repository

```bash
git clone https://github.com/your-username/jellyfin-torbox-tailscale.git
cd jellyfin-torbox-tailscale
```

### Step 2: Configure Environment Variables

Copy the example environment file:

```bash
cp .env.example .env
```

Open `.env` in your text editor and fill in your keys:

```ini
# TorBox API Bearer Token (from https://torbox.app/settings)
TORBOX_API_TOKEN=your_torbox_api_token_here

# TMDb API v3 Key (from https://www.themoviedb.org/settings/api)
TMDB_API_KEY=your_tmdb_api_key_here

# Optional: Tailscale pre-auth key (leave blank for interactive browser login)
TS_AUTHKEY=
```

### Step 3: Create Placeholder Key Files

To prevent Docker from creating directories for mounted files:

```bash
touch jellyfin_api_key.txt credentials.txt
```

### Step 4: Start the Stack

```bash
docker compose up -d --build
```

### Step 5: Authenticate Tailscale

If you left `TS_AUTHKEY` blank in `.env`, fetch the authentication URL from the Tailscale container logs:

```bash
docker compose logs -f tailscale
```

Click the login link displayed in the logs to authorize `jellyfin-server` in your Tailscale admin console.

### Step 6: Run the 1-Click Automated Jellyfin Setup

Once the containers are running and Tailscale is authenticated:

```bash
python3 setup_jellyfin.py
```

This automated script will:
- Complete the initial Jellyfin setup wizard.
- Create your admin user (saved to `credentials.txt`).
- Enforce the Zero-Transcoding lockdown policy.
- Create and configure the 4 media libraries (`Films`, `TV Programmes`, `Films (Anime)`, `TV Programmes (Anime)`).
- Generate and save your Jellyfin API token to `jellyfin_api_key.txt`.
- Trigger the initial media library scan.

---

## Accessing Your Jellyfin Server

### 1. Local Network Access
- Open `http://127.0.0.1:8096` in your browser.
- Login with the admin credentials printed in `credentials.txt`.

### 2. Tailscale Mesh (Recommended for Personal Devices)
Connect to your private Tailscale network from your Mac, iPhone, iPad, Apple TV, or Android device:
- URL: `http://jellyfin-server:8096` *(or `http://100.x.y.z:8096`)*
- Provides direct peer-to-peer WireGuard speeds, lowest latency, and zero bandwidth caps.

### 3. Public HTTPS (Tailscale Funnel)
To share your server with friends and family without requiring them to install Tailscale:
- See the complete step-by-step setup in [TAILSCALE_FUNNEL.md](TAILSCALE_FUNNEL.md).
- URL: `https://jellyfin-server.<your-tailnet>.ts.net`

---

## Client Playback Recommendations

Because media files are `.strm` links pointing to the TorBox cloud CDN:

| Client | How to Connect | Playback Performance |
| :--- | :--- | :--- |
| **Swiftfin (Apple TV / iOS)** | Connect to your Jellyfin URL | **100% Direct Play** via native VLCKit engine. Full 4K HDR/DV support. |
| **Jellyfin Mobile (iOS / Android)** | Connect to your Jellyfin URL | **100% Direct Play** via remote `.strm` redirect. |
| **Web Browser (Chrome / Safari / Edge)** | Open web dashboard | Direct play with HTML5 player. |
| **Jellyfin Media Player (Desktop)** | Connect to your Jellyfin URL | **100% Direct Play** with MPV engine. |
| **Infuse & VidHub** | Connect directly to TorBox WebDAV (`https://webdav.torbox.app`) | Bypasses server-side proxy buffering for multi-gigabyte static downloads. |

---

## Migrating Watch History from Plex

If you are migrating from Plex Media Server, you can automatically transfer your watched state and play counts to Jellyfin using the included migration script:

```bash
python3 scripts/sync_plex_watch_history.py \
  --plex-db "~/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db" \
  --jellyfin-user "admin"
```

Use `--dry-run` to inspect matching items before applying changes.

---

## Environment Variable Reference

| Variable | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `TORBOX_API_TOKEN` | Yes | — | TorBox API Bearer Token |
| `TMDB_API_KEY` | Yes | — | The Movie Database (TMDb) API Key v3 |
| `TS_AUTHKEY` | No | — | Tailscale pre-authentication key |
| `SYNC_INTERVAL_SECONDS` | No | `300` | TorBox Assistant sync loop interval in seconds (5 min) |
| `WARMER_CYCLE_DAYS` | No | `10` | TorBox cache warmer cycle duration in days |
| `FUNNEL_URL` | No | — | Public Funnel URL for automated keepalive heartbeat |
| `CF_UPDATE_URL` | No | — | Cloudflare Worker URL to update dynamic home IP |
| `CF_STREAM_URL` | No | — | Cloudflare Worker URL to proxy TorBox streams (optional) |
| `JELLYFIN_ADMIN_USER` | No | `admin` | Admin username for `setup_jellyfin.py` |
| `JELLYFIN_ADMIN_PASSWORD` | No | *(Random)* | Admin password for `setup_jellyfin.py` |

---

## Troubleshooting & Maintenance

### Checking Container Logs

```bash
# View Tailscale logs & Funnel status
docker compose logs -f tailscale

# View TorBox Assistant logs (STRM generator & cache warmer)
docker compose logs -f torbox-assistant

# View Jellyfin server logs
docker compose logs -f jellyfin
```

### Inspecting TorBox Assistant Logs

Log files are persisted in `./torbox_assistant/logs/`:
- `generator.log`: Output of the STRM generator, TMDb matching, and file operations.
- `warmer.log`: TorBox cache warmer status and download request pacing.
- `supervisor.log`: Watchdog events and Funnel keepalive heartbeats.

### Manual STRM Generation Trigger

To force an immediate TorBox sync cycle without waiting for the timer:

```bash
docker exec torbox-assistant python3 /app/jellyfin_strm_generator.py
```

---

## License

This project is licensed under the [MIT License](LICENSE).
