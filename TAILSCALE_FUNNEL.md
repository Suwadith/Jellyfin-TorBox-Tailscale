# Tailscale Funnel Guide (Public HTTPS Remote Access)

Tailscale Funnel exposes your containerized Jellyfin media server securely to the public internet with automatic Let's Encrypt TLS/SSL encryption (`https://`).

When Funnel is enabled:
- **No VPN Required for Clients**: Friends and family can open your Jellyfin server in Safari, Chrome, Apple TV, or any Jellyfin app without installing Tailscale or having an account.
- **Zero Port Forwarding**: Your home router requires zero open ports; inbound traffic is securely tunneled through Tailscale's global ingress relays directly to your container.
- **Zero Transcoding Burden**: Because media items are `.strm` links pointing to the cloud debrid CDN (TorBox), Tailscale Funnel only handles lightweight metadata, images, and API JSON payloads ($< 1\text{ MB}$). Heavy 4K/HDR video streams are delivered directly from the CDN to the client's video player.

---

## 1. Tailscale Tailnet Prerequisites

Before enabling Funnel on a container node:
1. Log in to the [Tailscale Admin Console](https://login.tailscale.com/admin).
2. Go to **Access Controls** and verify that Funnel permissions are granted to your users / nodes in your ACL policy:
   ```json
   "nodeAttrs": [
     {
       "target": ["autogroup:member"],
       "attr": ["funnel"]
     }
   ]
   ```
3. Enable **MagicDNS** and **HTTPS Certificates** under **DNS** in the Tailscale Admin Console.

---

## 2. Enabling Funnel

### Option A: Automatic via `serve.json` (Default)
This repository includes `tailscale_config/serve.json` mounted directly into the `tailscale-jellyfin` container with `${TS_CERT_DOMAIN}` substitution. Once your Tailscale container is authenticated, Funnel is automatically provisioned on port 443.

### Option B: Command Line Activation
You can also manually enable or toggle Funnel inside the container at any time:

```bash
docker exec tailscale-jellyfin tailscale funnel --bg 8096
```

To verify the live public URL and TLS status:

```bash
docker exec tailscale-jellyfin tailscale funnel status
```

Your server will be publicly reachable at:
```text
https://jellyfin-server.<your-tailnet-domain>.ts.net
```

---

## 3. Disabling Funnel (Reverting to Private Tailnet Only)

To disable the public link and restrict Jellyfin strictly to authorized private Tailscale VPN devices:

```bash
docker exec tailscale-jellyfin tailscale funnel reset
```

---

## 4. Client Compatibility & Playback Behavior

| Client | Connection URL | Playback Behavior |
| :--- | :--- | :--- |
| **Swiftfin (Apple TV / iOS)** | `https://jellyfin-server.<your-tailnet>.ts.net` | 100% Direct Play via native VLCKit engine |
| **Jellyfin Mobile (iOS / Android)** | `https://jellyfin-server.<your-tailnet>.ts.net` | 100% Direct Play via remote `.strm` link |
| **Web Browser (Safari / Chrome / Firefox)** | `https://jellyfin-server.<your-tailnet>.ts.net` | Direct streaming with HTML5 player |
| **Official Jellyfin Desktop** | `https://jellyfin-server.<your-tailnet>.ts.net` | Direct streaming via MPV engine |
| **Infuse & VidHub** | Connect to TorBox WebDAV (`https://webdav.torbox.app`) | Bypasses Jellyfin server buffering (recommended for multi-gigabyte static streams) |

---

## 5. Automated DERP Keepalive & Auto-Healing

Public Tailscale Funnel edge relays (DERP nodes) may disconnect idle tunnels after periods of inactivity, causing initial remote connection hangs.

This repository includes an autonomous **Funnel Keepalive Heartbeat** inside `torbox-assistant`:
- If `FUNNEL_URL` is set in `.env` (e.g. `https://jellyfin-server.<your-tailnet>.ts.net/System/Info/Public`), the daemon pings the public endpoint every 60 seconds.
- Traverses the public edge relay and WireGuard tunnel to maintain NAT states.
- If 3 consecutive public probes fail while local Jellyfin is healthy, the daemon automatically re-provisions the DERP relay tunnel via Docker API.
