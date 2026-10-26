import os
import requests
import time
import logging
import traceback
import json
from dotenv import load_dotenv

load_dotenv()

TORBOX_API_TOKEN = os.getenv("TORBOX_API_TOKEN")

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
LOG_DIR = os.getenv("LOG_DIR", "/app/logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# 1. System Debug Logger
sys_logger = logging.getLogger('system')
sys_logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

file_handler = logging.FileHandler(os.path.join(LOG_DIR, 'warmer.log'), encoding='utf-8')
file_handler.setFormatter(formatter)
sys_logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
sys_logger.addHandler(console_handler)

# 2. Specific Action Loggers
def setup_simple_logger(name, log_file):
    handler = logging.FileHandler(os.path.join(LOG_DIR, log_file), encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger

success_logger = setup_simple_logger('success', 'warmer_success.log')
failure_logger = setup_simple_logger('failure', 'warmer_failure.log')

HEADERS = {
    "Authorization": f"Bearer {TORBOX_API_TOKEN}" if TORBOX_API_TOKEN else ""
}
RANGE_HEADER = {
    "Range": "bytes=5242880-15728640"
}
BASE_URL = "https://api.torbox.app/v1/api"
PACING_WINDOW_SECONDS = 24 * 60 * 60  # 24 hours
RETRY_WAIT_SECONDS = 5 * 60  # 5 minutes wait on server error
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")
WARMER_CYCLE_DAYS = int(os.getenv("WARMER_CYCLE_DAYS", "10"))
CYCLE_RESET_SECONDS = WARMER_CYCLE_DAYS * 24 * 60 * 60  # Default 10 days


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                data = json.load(f)
                
            cycle_start = data.get("cycle_start_time", 0)
            if time.time() - cycle_start > CYCLE_RESET_SECONDS:
                sys_logger.info(f"Previous progress file is older than {WARMER_CYCLE_DAYS} days. Starting a fresh 24-hour cycle.")
                return {"cycle_start_time": time.time(), "delay_seconds": None, "completed_ids": []}
            else:
                sys_logger.info(f"Resuming from recent progress file. {len(data.get('completed_ids', []))} items already processed.")
                return data
        except Exception as e:
            sys_logger.error(f"Failed to read progress file: {e}. Starting fresh.")
            
    return {"cycle_start_time": time.time(), "delay_seconds": None, "completed_ids": []}

def save_progress(progress_data):
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(progress_data, f, indent=4)
    except Exception as e:
        sys_logger.error(f"Failed to save progress file: {e}")

def get_first_file_id(item):
    """Extracts the first video file ID from an item's files list. Falls back to the first file if no video."""
    video_extensions = (
        '.3g2', '.3gp', '.aaf', '.asf', '.avchd', '.avi', '.drc', '.flv', '.m2ts', '.m2v', 
        '.m4p', '.m4v', '.mkv', '.mng', '.mov', '.mp2', '.mp4', '.mpe', '.mpeg', '.mpg', 
        '.mpv', '.mxf', '.nsv', '.ogg', '.ogv', '.qt', '.rm', '.rmvb', '.roq', '.svi', 
        '.ts', '.vob', '.webm', '.wmv', '.yuv'
    )
    files = item.get("files", [])
    
    if files and isinstance(files, list):
        if isinstance(files[0], dict):
            for f in files:
                name = str(f.get("name", "")).lower()
                short_name = str(f.get("short_name", "")).lower()
                if (name.endswith(video_extensions) or short_name.endswith(video_extensions)) and "id" in f:
                    return f["id"]
            
            if "id" in files[0]:
                return files[0]["id"]
                
        elif isinstance(files[0], int):
            return files[0]
            
    return None

def fetch_all_items():
    """Fetches both torrents and webdls with pagination up to 15k items."""
    tasks = []
    
    if not TORBOX_API_TOKEN:
        sys_logger.critical("TORBOX_API_TOKEN is missing or not configured.")
        return tasks

    # 1. Fetch Torrents with pagination
    sys_logger.info("Fetching active torrents list from TorBox API...")
    offset = 0
    seen_ids = set()
    while offset <= 15000:
        try:
            t_resp = requests.get(f"{BASE_URL}/torrents/mylist", headers=HEADERS, params={"limit": 1000, "offset": offset, "skip": offset}, timeout=15)
            if t_resp.ok:
                batch = t_resp.json().get("data", [])
                if not batch:
                    break
                new_found = False
                for item in batch:
                    item_id = item.get("id")
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        new_found = True
                        file_id = get_first_file_id(item)
                        if item_id is not None and file_id is not None:
                            tasks.append({
                                "type": "torrents",
                                "id": item_id,
                                "file_id": file_id,
                                "name": item.get("name", f"Torrent ID: {item_id}"),
                                "unique_id": f"torrents_{item_id}_{file_id}"
                            })
                if len(batch) < 1000 or not new_found:
                    break
                offset += 1000
            else:
                sys_logger.error(f"Failed to fetch torrents. Status: {t_resp.status_code}")
                break
        except Exception as e:
            sys_logger.exception(f"Unexpected exception while fetching torrents: {e}")
            break

    # 2. Fetch WebDLs with pagination
    sys_logger.info("Fetching active webdl list from TorBox API...")
    offset = 0
    while offset <= 15000:
        try:
            w_resp = requests.get(f"{BASE_URL}/webdl/mylist", headers=HEADERS, params={"limit": 1000, "offset": offset, "skip": offset}, timeout=15)
            if w_resp.ok:
                batch = w_resp.json().get("data", [])
                if not batch:
                    break
                new_found = False
                for item in batch:
                    item_id = item.get("id")
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        new_found = True
                        file_id = get_first_file_id(item)
                        if item_id is not None and file_id is not None:
                            tasks.append({
                                "type": "webdl",
                                "id": item_id,
                                "file_id": file_id,
                                "name": item.get("name", f"WebDL ID: {item_id}"),
                                "unique_id": f"webdl_{item_id}_{file_id}"
                            })
                if len(batch) < 1000 or not new_found:
                    break
                offset += 1000
            else:
                sys_logger.error(f"Failed to fetch webdls. Status: {w_resp.status_code}")
                break
        except Exception as e:
            sys_logger.exception(f"Unexpected exception while fetching webdls: {e}")
            break

    return tasks

def process_task(task):
    """Attempts to warm a single task. Returns True if task is finished (success or file error)."""
    endpoint = f"{BASE_URL}/{task['type']}/requestdl"
    id_param = "torrent_id" if task['type'] == "torrents" else "web_id"
    
    params = {
        "token": TORBOX_API_TOKEN,
        id_param: task["id"],
        "file_id": task["file_id"],
        "redirect": "true"
    }

    while True:
        try:
            sys_logger.debug(f"Attempting to warm [{task['type']}] '{task['name']}' (ID: {task['id']}, File ID: {task['file_id']})")
            dl_resp = requests.get(endpoint, params=params, headers=RANGE_HEADER, stream=True, timeout=20)
            status = dl_resp.status_code
            
            error_body = ""
            if status >= 400:
                try:
                    error_body = dl_resp.text[:200]
                except:
                    error_body = "Could not read response body."
                    
            dl_resp.close()
            
            # Handle 416 Range Not Satisfiable (happens if file is smaller than 5MB)
            if status == 416:
                sys_logger.debug(f"Got 416 (file likely < 5MB). Retrying without Range header for '{task['name']}'...")
                dl_resp = requests.get(endpoint, params=params, stream=True, timeout=20)
                status = dl_resp.status_code
                if status >= 400:
                    try:
                        error_body = dl_resp.text[:200]
                    except:
                        error_body = "Could not read response body."
                dl_resp.close()
            
            if 200 <= status < 400:
                try:
                    for chunk in dl_resp.iter_content(chunk_size=8192):
                        pass
                except requests.exceptions.RequestException as e:
                    sys_logger.debug(f"Stream read aborted (often normal for partial chunks): {e}")

                success_logger.info(f"SUCCESS [{task['type']}] {task['name']} (Status: {status})")
                sys_logger.info(f"Warmed successfully: '{task['name']}' (Status {status})")
                return True
                
            elif 400 <= status < 500:
                msg = f"FILE ERROR [{task['type']}] {task['name']} (Status: {status})"
                failure_logger.error(msg)
                sys_logger.error(f"File Rejected by Server: '{task['name']}'. Status: {status}. Response excerpt: {error_body}. Skipping to next file.")
                return True # Count as finished
                
            elif status >= 500:
                if "DATABASE_ERROR" in error_body:
                    msg = f"DATABASE ERROR [{task['type']}] {task['name']} (Status: {status})"
                    failure_logger.error(msg)
                    sys_logger.error(f"Permanent DATABASE_ERROR detected for '{task['name']}'. Skipping to next file.")
                    return True # Mark as finished
                    
                sys_logger.warning(f"SERVER ERROR detected (Status {status}) while warming '{task['name']}'. Response excerpt: {error_body}.")
                sys_logger.warning(f"Pausing execution for {RETRY_WAIT_SECONDS//60} minutes before retrying...")
                time.sleep(RETRY_WAIT_SECONDS)
                
        except requests.exceptions.RequestException as e:
            sys_logger.error(f"CONNECTION ERROR: {e}")
            sys_logger.warning(f"Pausing execution for {RETRY_WAIT_SECONDS//60} minutes before retrying...")
            time.sleep(RETRY_WAIT_SECONDS)
            
        except Exception as e:
            sys_logger.exception(f"CRITICAL UNHANDLED EXCEPTION while processing '{task['name']}': {e}")
            failure_logger.error(f"CRASH [{task['type']}] {task['name']} - See warmer.log for traceback.")
            return True

def run_warmer_cycle():
    sys_logger.info("=== TorBox Cache Warmer Script Started ===")
    progress = load_progress()
    
    tasks = fetch_all_items()
    total_found = len(tasks)
    
    if total_found == 0:
        sys_logger.warning("No active items discovered from TorBox API.")
        return
        
    completed_set = set(progress.get("completed_ids", []))
    remaining_tasks = [t for t in tasks if t["unique_id"] not in completed_set]
    total_remaining = len(remaining_tasks)
    
    sys_logger.info(f"Discovered {total_found} active items. {len(completed_set)} already processed. {total_remaining} remaining.")
    
    if total_remaining == 0:
        sys_logger.info("All items in current cycle are already warmed. Sleeping until next cycle.")
        return

    if progress.get("delay_seconds") is None:
        delay_seconds = PACING_WINDOW_SECONDS / max(1, total_found)
        progress["delay_seconds"] = delay_seconds
        save_progress(progress)
    else:
        delay_seconds = progress["delay_seconds"]
        
    sys_logger.info(f"Pacing Strategy: {delay_seconds:.2f} seconds between requests.")

    for index, task in enumerate(remaining_tasks):
        sys_logger.info(f"Processing item [{index + 1}/{total_remaining}]...")
        is_finished = process_task(task)
        
        if is_finished:
            progress["completed_ids"].append(task["unique_id"])
            save_progress(progress)
        
        if index < total_remaining - 1:
            time.sleep(delay_seconds)
            
    sys_logger.info("=== Cache warming cycle complete. ===")

def main():
    while True:
        try:
            run_warmer_cycle()
        except Exception as e:
            sys_logger.exception(f"Fatal error in warmer cycle: {e}")
            
        # Check every hour whether a new cycle or resumption is due
        time.sleep(3600)

if __name__ == "__main__":
    main()
