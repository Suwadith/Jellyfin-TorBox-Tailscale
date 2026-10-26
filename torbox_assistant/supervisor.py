import os
import sys
import time
import signal
import subprocess
import logging
import threading
import urllib.request
import http.client
import socket
import json

LOG_DIR = os.getenv("LOG_DIR", "/app/logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] SUPERVISOR: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "supervisor.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)

running = True
proxy_proc = None
warmer_proc = None

def signal_handler(signum, frame):
    global running, proxy_proc, warmer_proc
    logging.info(f"Received signal {signum}. Shutting down services...")
    running = False
    if proxy_proc and proxy_proc.poll() is None:
        proxy_proc.terminate()
    if warmer_proc and warmer_proc.poll() is None:
        warmer_proc.terminate()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def check_and_start_proxy():
    global proxy_proc
    if proxy_proc is None or proxy_proc.poll() is not None:
        logging.info("Starting Swarm Proxy (proxy.py)...")
        proxy_proc = subprocess.Popen([sys.executable, "/app/proxy.py"])

def check_and_start_warmer():
    global warmer_proc
    if warmer_proc is None or warmer_proc.poll() is not None:
        logging.info("Starting TorBox Cache Warmer (warmer.py)...")
        warmer_proc = subprocess.Popen([sys.executable, "/app/warmer.py"])

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)

def docker_api(method, path, body=None, timeout=10):
    socket_path = "/var/run/docker.sock"
    if not os.path.exists(socket_path):
        return 0, None
    try:
        conn = UnixHTTPConnection(socket_path, timeout=timeout)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        res = conn.getresponse()
        raw = res.read().decode("utf-8", errors="ignore")
        conn.close()
        try:
            return res.status, json.loads(raw)
        except Exception:
            return res.status, raw
    except Exception as e:
        return 0, str(e)

def stack_watchdog_worker():
    """
    Monitors the Docker network namespace between tailscale-jellyfin and jellyfin.
    If tailscale-jellyfin is restarted or rebooted via the Docker Desktop UI,
    dependent containers lose their network namespace. This watchdog automatically:
    1. Reconnects tailscale-jellyfin to its bridge network if Docker started it detached.
    2. Detects if tailscale-jellyfin has a newer StartedAt timestamp than jellyfin.
    3. Triggers an automatic restart of jellyfin and torbox-assistant so they seamlessly
       re-join the new network namespace without any manual intervention.
    """
    socket_path = "/var/run/docker.sock"
    if not os.path.exists(socket_path):
        logging.warning("Docker socket /var/run/docker.sock not found. Stack auto-heal watchdog disabled.")
        return

    logging.info("Starting Docker Stack Watchdog (auto-heals network namespace on Docker UI restarts)...")
    last_restart_trigger = 0
    cooldown = 45  # seconds cooldown between restart triggers to prevent looping
    probe_failures = 0
    network_name = os.getenv("DOCKER_NETWORK_NAME", "jellyfin_tailscale_net")

    while running:
        time.sleep(5)
        now = time.time()
        if now - last_restart_trigger < cooldown:
            continue

        try:
            # 1. Inspect tailscale-jellyfin
            ts_code, ts_info = docker_api("GET", "/containers/tailscale-jellyfin/json")
            if ts_code != 200 or not isinstance(ts_info, dict):
                continue

            ts_state = ts_info.get("State", {})
            if not ts_state.get("Running"):
                # Tailscale is currently stopped, do nothing until it comes back up
                continue

            # Check network attachment: Ensure tailscale has bridge network
            ts_nets = ts_info.get("NetworkSettings", {}).get("Networks", {})
            if not ts_nets:
                logging.warning(f"WATCHDOG: tailscale-jellyfin has no active network bridge! Auto-reconnecting to {network_name}...")
                docker_api("POST", f"/networks/{network_name}/connect", {"Container": "tailscale-jellyfin"})

            ts_started_at = ts_state.get("StartedAt", "")

            # 2. Inspect jellyfin
            jf_code, jf_info = docker_api("GET", "/containers/jellyfin/json")
            if jf_code != 200 or not isinstance(jf_info, dict):
                continue

            jf_state = jf_info.get("State", {})
            jf_running = jf_state.get("Running", False)
            jf_started_at = jf_state.get("StartedAt", "")

            # 3. Check for out-of-sync network namespace
            needs_resync = False
            reason = ""

            if jf_running and ts_started_at and jf_started_at:
                if ts_started_at > jf_started_at:
                    needs_resync = True
                    reason = f"tailscale-jellyfin restarted at {ts_started_at} which is newer than jellyfin at {jf_started_at}"

            # 4. Fallback health probe: If jellyfin has been up > 45s, probe local port 8096
            if jf_running and not needs_resync:
                try:
                    probe_req = urllib.request.Request("http://127.0.0.1:8096/health")
                    with urllib.request.urlopen(probe_req, timeout=3) as probe_res:
                        if probe_res.status == 200:
                            probe_failures = 0
                except Exception:
                    probe_failures += 1
                    if probe_failures >= 4:  # ~20 seconds of consecutive local failure while running
                        needs_resync = True
                        reason = f"Jellyfin port 8096 unresponsive for {probe_failures} consecutive watchdog checks (broken netns)"
                        probe_failures = 0

            # 5. Execute auto-heal resync if needed
            if needs_resync:
                logging.warning(f"WATCHDOG: Resync triggered: {reason}")
                logging.info("WATCHDOG: Restarting jellyfin container to attach to new Tailscale network namespace...")
                docker_api("POST", "/containers/jellyfin/restart")
                last_restart_trigger = time.time()

                # Also restart torbox-assistant so it leaves the old dead netns
                logging.info("WATCHDOG: Exiting torbox-assistant so Docker restarts it into the new network namespace...")
                time.sleep(2)
                os._exit(0)

        except Exception as e:
            logging.error(f"WATCHDOG: Error in watchdog loop: {e}")

def funnel_keepalive_worker():
    """Continuously pings the public Funnel URL to prevent Tailscale DERP edge relays and NAT states from idling out, and auto-heals stale Funnel sessions."""
    funnel_url = os.getenv("FUNNEL_URL", "").strip()
    if not funnel_url:
        logging.info("FUNNEL: FUNNEL_URL not configured. Funnel keepalive heartbeat disabled.")
        return

    keepalive_interval = int(os.getenv("FUNNEL_KEEPALIVE_SECONDS", "60"))
    logging.info(f"Starting Tailscale Funnel Keepalive Heartbeat targeting {funnel_url} (every {keepalive_interval}s)...")
    
    failures = 0
    last_funnel_heal = 0
    funnel_heal_cooldown = 180  # 3 minutes between auto-heal attempts

    while running:
        try:
            req = urllib.request.Request(
                funnel_url,
                headers={"User-Agent": "TorBoxAssistant-Keepalive/1.0"}
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=8) as resp:
                elapsed = time.time() - t0
                if failures > 0:
                    logging.info(f"Funnel keepalive recovered! HTTP {resp.status} in {elapsed:.2f}s")
                    failures = 0
        except Exception as e:
            failures += 1
            if failures in (1, 2, 3) or failures % 5 == 0:
                logging.warning(f"Funnel keepalive probe failed ({failures}x): {e}")

            # Auto-healing: If Funnel fails for 3 consecutive probes (~3 min), and cooldown elapsed
            now = time.time()
            if failures >= 3 and (now - last_funnel_heal >= funnel_heal_cooldown):
                # Verify local Jellyfin is actually alive before restarting Tailscale
                local_ok = False
                try:
                    probe_req = urllib.request.Request("http://127.0.0.1:8096/health")
                    with urllib.request.urlopen(probe_req, timeout=3) as probe_res:
                        if probe_res.status == 200:
                            local_ok = True
                except Exception:
                    local_ok = False

                if local_ok:
                    logging.warning(
                        f"FUNNEL AUTO-HEAL: Local Jellyfin is healthy, but public Funnel URL has failed {failures}x. "
                        "Restarting tailscale-jellyfin to re-establish public Funnel edge session..."
                    )
                    last_funnel_heal = now
                    docker_api("POST", "/containers/tailscale-jellyfin/restart")
        
        for _ in range(keepalive_interval):
            if not running:
                break
            time.sleep(1)

def run_generator_cycle():
    logging.info("Running Jellyfin STRM generator cycle...")
    try:
        proc = subprocess.run([sys.executable, "/app/jellyfin_strm_generator.py"], check=False)
        logging.info(f"Generator cycle finished with exit code {proc.returncode}.")
    except Exception as e:
        logging.error(f"Error during generator cycle: {e}")

def main():
    logging.info("=== TorBox Assistant Supervisor Starting ===")
    check_and_start_proxy()
    check_and_start_warmer()

    # Start automated Funnel Keepalive Heartbeat thread if configured
    if os.getenv("FUNNEL_URL", "").strip():
        keepalive_thread = threading.Thread(target=funnel_keepalive_worker, daemon=True)
        keepalive_thread.start()
    else:
        logging.info("FUNNEL: FUNNEL_URL not configured. Funnel keepalive heartbeat disabled.")

    # Start automated Stack Auto-Heal Watchdog thread (heals network namespace on Docker UI restarts)
    watchdog_thread = threading.Thread(target=stack_watchdog_worker, daemon=True)
    watchdog_thread.start()

    # Initial sync run
    run_generator_cycle()

    last_sync = time.time()
    sync_interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))  # Default 5 minutes

    while running:
        time.sleep(5)
        # Ensure daemon processes stay alive
        check_and_start_proxy()
        check_and_start_warmer()

        # Check periodic generator run
        now = time.time()
        if now - last_sync >= sync_interval:
            run_generator_cycle()
            last_sync = now

if __name__ == "__main__":
    main()
