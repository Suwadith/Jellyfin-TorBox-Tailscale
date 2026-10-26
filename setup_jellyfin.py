#!/usr/bin/env python3
import time
import json
import urllib.request
import urllib.parse
import os
import sys
import logging
import secrets
import string

# Optionally load dotenv if installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] SETUP: %(message)s")

BASE_URL = os.getenv("JELLYFIN_URL", "http://127.0.0.1:8096").rstrip("/")
ADMIN_USER = os.getenv("JELLYFIN_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("JELLYFIN_ADMIN_PASSWORD", "")
if not ADMIN_PASS:
    alphabet = string.ascii_letters + string.digits
    ADMIN_PASS = "".join(secrets.choice(alphabet) for _ in range(16))

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(WORKSPACE_DIR, "jellyfin_api_key.txt")
CREDENTIALS_FILE = os.path.join(WORKSPACE_DIR, "credentials.txt")

AUTH_BASE = 'MediaBrowser Client="Jellyfin Web", Device="Chrome", DeviceId="setup-script-01", Version="10.9.0"'

def make_request(method, endpoint, data=None, token=None, headers=None):
    url = f"{BASE_URL}{endpoint}"
    auth = AUTH_BASE
    if token:
        auth += f', Token="{token}"'
        
    req_headers = {
        "Accept": "application/json",
        "Authorization": auth,
        "X-Emby-Authorization": auth,
    }
    if token:
        req_headers["X-Emby-Token"] = token
        req_headers["X-MediaBrowser-Token"] = token
    if headers:
        req_headers.update(headers)
        
    encoded_data = None
    if data is not None:
        if isinstance(data, (dict, list)):
            encoded_data = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        elif isinstance(data, str):
            encoded_data = data.encode("utf-8")
        elif isinstance(data, bytes):
            encoded_data = data
            
    req = urllib.request.Request(url, data=encoded_data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
            if content:
                try:
                    return resp.status, json.loads(content.decode("utf-8"))
                except Exception:
                    return resp.status, content.decode("utf-8")
            return resp.status, None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else ""
        logging.error(f"HTTPError on {method} {endpoint}: {e.code} - {err_body}")
        return e.code, err_body
    except Exception as e:
        logging.error(f"Request failed on {method} {endpoint}: {e}")
        return 0, str(e)

def wait_for_server():
    logging.info("Waiting for Jellyfin server to be ready on 127.0.0.1:8096...")
    for _ in range(30):
        try:
            status, data = make_request("GET", "/System/Info/Public")
            if status == 200 and isinstance(data, dict):
                logging.info(f"Server online: {data.get('ServerName')} (v{data.get('Version')})")
                return data
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("Timed out waiting for Jellyfin server.")

def complete_startup_wizard():
    status, data = make_request("GET", "/System/Info/Public")
    if data and data.get("StartupWizardCompleted"):
        logging.info("Startup wizard is already marked completed.")
        return
        
    logging.info("Executing initial setup wizard...")
    
    # 1. Update/Create User
    status, res = make_request("POST", "/Startup/User", {"Name": ADMIN_USER, "Password": ADMIN_PASS})
    logging.info(f"Configured {ADMIN_USER} user: status {status}")
    
    # 2. Set Configuration
    status, res = make_request("POST", "/Startup/Configuration", {
        "UICulture": "en-US",
        "MetadataCountryCode": "US",
        "PreferredMetadataLanguage": "en"
    })
    logging.info(f"Set startup config: status {status}")
    
    # 3. Complete Startup
    status, res = make_request("POST", "/Startup/Complete")
    logging.info(f"Completed startup wizard: status {status}")
    time.sleep(1)

def authenticate():
    logging.info(f"Authenticating as {ADMIN_USER}...")
    status, res = make_request("POST", "/Users/AuthenticateByName", {
        "Username": ADMIN_USER,
        "Pw": ADMIN_PASS
    })
    if status != 200 or not isinstance(res, dict):
        raise RuntimeError(f"Authentication failed: status {status}, response: {res}")
        
    token = res.get("AccessToken")
    user = res.get("User", {})
    user_id = user.get("Id")
    logging.info(f"Authenticated successfully as {user.get('Name')}. User ID: {user_id}")
    
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    logging.info(f"Saved API token to {TOKEN_FILE}")
    
    return token, user_id

def enforce_zero_transcoding(token, user_id):
    logging.info("Enforcing Zero-Transcoding lockdown policy on admin user...")
    status, user_obj = make_request("GET", f"/Users/{user_id}", token=token)
    if status == 200 and isinstance(user_obj, dict):
        policy = user_obj.get("Policy", {})
        policy["EnableAudioPlaybackTranscoding"] = True
        policy["EnableVideoPlaybackTranscoding"] = False
        policy["EnablePlaybackRemuxing"] = True
        policy["ForceRemoteSourceTranscoding"] = False
        policy["EnableSyncTranscoding"] = False
        policy["EnableMediaConversion"] = False
        policy["EnableMediaPlayback"] = True
        
        status, res = make_request("POST", f"/Users/{user_id}/Policy", data=policy, token=token)
        logging.info(f"Updated user policy: status {status}")

def setup_libraries(token):
    logging.info("Setting up media libraries in Jellyfin...")
    
    libraries = [
        {"name": "Films", "type": "movies", "path": "/media/Movies"},
        {"name": "TV Programmes", "type": "tvshows", "path": "/media/TV"},
        {"name": "Films (Anime)", "type": "movies", "path": "/media/Anime Movies"},
        {"name": "TV Programmes (Anime)", "type": "tvshows", "path": "/media/Anime TV"},
    ]
    
    # Check existing virtual folders
    status, existing_vfs = make_request("GET", "/Library/VirtualFolders", token=token)
    existing_names = set()
    if status == 200 and isinstance(existing_vfs, list):
        existing_names = {vf.get("Name") for vf in existing_vfs}
        
    for lib in libraries:
        if lib["name"] in existing_names:
            logging.info(f"Library '{lib['name']}' already exists.")
            continue
            
        logging.info(f"Creating library: '{lib['name']}' ({lib['type']}) -> {lib['path']}...")
        query_params = urllib.parse.urlencode({
            "name": lib["name"],
            "collectionType": lib["type"],
            "paths": [lib["path"]],
            "refreshLibrary": "false"
        }, doseq=True)
        
        status, res = make_request("POST", f"/Library/VirtualFolders?{query_params}", token=token)
        logging.info(f"Added virtual folder '{lib['name']}': status {status}")

    # Optimize Library Options: Disable Chapter, Trickplay, and heavy extractors
    time.sleep(1)
    status, vfs = make_request("GET", "/Library/VirtualFolders", token=token)
    if status == 200 and isinstance(vfs, list):
        for vf in vfs:
            vf_name = vf.get("Name")
            vf_item_id = vf.get("ItemId")
            options = vf.get("LibraryOptions", {})
            
            # Disable heavy background tasks
            options["EnableChapterImageExtraction"] = False
            options["ExtractChapterImagesDuringLibraryScan"] = False
            options["EnableTrickplayImageExtraction"] = False
            options["ExtractTrickplayImagesDuringLibraryScan"] = False
            options["SaveLocalMetadata"] = False
            options["EnableRealtimeMonitor"] = False
            options["EnableAutomaticSeriesGrouping"] = True
            
            payload = {
                "Id": vf_item_id,
                "LibraryOptions": options
            }
            status, res = make_request("POST", "/Library/VirtualFolders/LibraryOptions", data=payload, token=token)
            logging.info(f"Optimized fast-scan options for '{vf_name}': status {status}")

def trigger_scan(token):
    logging.info("Triggering initial library refresh...")
    status, res = make_request("POST", "/Library/Refresh", token=token)
    logging.info(f"Library refresh triggered: status {status}")

def save_credentials():
    with open(CREDENTIALS_FILE, "w") as f:
        f.write(f"JELLYFIN_USERNAME={ADMIN_USER}\n")
        f.write(f"JELLYFIN_PASSWORD={ADMIN_PASS}\n")
        f.write("LOCAL_URL=http://127.0.0.1:8096\n")
    logging.info(f"Credentials written to {CREDENTIALS_FILE}")

def main():
    wait_for_server()
    complete_startup_wizard()
    token, user_id = authenticate()
    enforce_zero_transcoding(token, user_id)
    setup_libraries(token)
    trigger_scan(token)
    save_credentials()
    logging.info("=== JELLYFIN AUTO-PROVISIONING COMPLETE ===")

if __name__ == "__main__":
    main()
