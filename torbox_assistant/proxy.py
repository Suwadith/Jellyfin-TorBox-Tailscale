import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
import json
import logging
import requests
import threading
import time
import urllib3
import concurrent.futures

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG_DIR = os.getenv("LOG_DIR", "/app/logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] PROXY: %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "proxy.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)

_URL_CACHE = {} 
_SESSION = requests.Session()

CHUNK_SIZE = 128 * 1024 # 128KB
BLOCK_SIZE = 10 * 1024 * 1024 # 10MB per worker task
MAX_BUFFER_BYTES = 1000 * 1024 * 1024 # 1GB memory limit

class HybridSwarmDownloader:
    def __init__(self, real_url, req_headers, start_byte, total_size):
        self.real_url = real_url
        self.req_headers = req_headers
        self.start_byte = start_byte
        self.total_size = total_size
        
        self.buffer = {} # Absolute byte offset -> bytes chunk
        self.buffer_lock = threading.Lock()
        self.buffer_cv = threading.Condition(self.buffer_lock)
        
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.futures = {} # block_idx -> future
        self.fetch_block_idx = 0
        self.consume_offset = start_byte
        self.stop_event = threading.Event()
        self.buffer_size_bytes = 0
        
        self.manager_thread = threading.Thread(target=self._manager)
        self.manager_thread.daemon = True
        self.manager_thread.start()
        
    def _fetch_block(self, block_idx):
        if self.stop_event.is_set(): return False
        
        block_start = self.start_byte + (block_idx * BLOCK_SIZE)
        block_end = min(block_start + BLOCK_SIZE - 1, self.total_size - 1)
        if block_start > block_end: return True # EOF
        
        headers = self.req_headers.copy()
        headers['Range'] = f'bytes={block_start}-{block_end}'
        
        logging.info(f"Worker streaming block {block_idx} ({block_start}-{block_end})...")
        try:
            resp = _SESSION.get(self.real_url, headers=headers, stream=True, timeout=15)
            resp.raise_for_status()
            
            current_offset = block_start
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if self.stop_event.is_set():
                    break
                if not chunk:
                    break
                    
                chunk_len = len(chunk)
                with self.buffer_cv:
                    self.buffer[current_offset] = chunk
                    self.buffer_size_bytes += chunk_len
                    current_offset += chunk_len
                    self.buffer_cv.notify_all()
                    
                # Pause worker if we hit the 1GB memory limit
                while self.buffer_size_bytes >= MAX_BUFFER_BYTES and not self.stop_event.is_set():
                    time.sleep(0.1)

            logging.info(f"Worker finished block {block_idx}")
            resp.close()
            return True
        except Exception as e:
            logging.error(f"Worker failed block {block_idx}: {e}")
            return False

    def _manager(self):
        while not self.stop_event.is_set():
            with self.buffer_cv:
                active_blocks = len(self.futures) + (self.buffer_size_bytes // BLOCK_SIZE)
                
                # If we have space, assign more blocks
                if active_blocks < (MAX_BUFFER_BYTES // BLOCK_SIZE):
                    block_start = self.start_byte + (self.fetch_block_idx * BLOCK_SIZE)
                    if block_start <= self.total_size - 1:
                        if self.fetch_block_idx not in self.futures:
                            consume_block_idx = max(0, (self.consume_offset - self.start_byte) // BLOCK_SIZE)
                            if self.fetch_block_idx >= consume_block_idx:
                                future = self.executor.submit(self._fetch_block, self.fetch_block_idx)
                                self.futures[self.fetch_block_idx] = future
                        self.fetch_block_idx += 1
                        
                # Cleanup and handle retries
                failed_indices = []
                done_indices = []
                for idx, future in list(self.futures.items()):
                    if future.done():
                        done_indices.append(idx)
                        try:
                            success = future.result()
                            if not success and not self.stop_event.is_set():
                                failed_indices.append(idx)
                        except Exception:
                            if not self.stop_event.is_set():
                                failed_indices.append(idx)
                                
                for idx in done_indices:
                    del self.futures[idx]
                    
                for idx in failed_indices:
                    logging.info(f"Manager retrying block {idx}...")
                    future = self.executor.submit(self._fetch_block, idx)
                    self.futures[idx] = future
                    
            time.sleep(0.05)
            
    def get_next_chunk(self):
        with self.buffer_cv:
            while self.consume_offset not in self.buffer and not self.stop_event.is_set():
                if self.consume_offset > self.total_size - 1:
                    return None # EOF
                self.buffer_cv.wait(timeout=1.0)
                
            if self.stop_event.is_set():
                return None
                
            data = self.buffer.pop(self.consume_offset)
            data_len = len(data)
            self.consume_offset += data_len
            self.buffer_size_bytes -= data_len
            return data
            
    def stop(self):
        self.stop_event.set()
        with self.buffer_cv:
            self.buffer_cv.notify_all()
        self.executor.shutdown(wait=False, cancel_futures=True)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    
    def log_message(self, format, *args):
        pass

    def get_real_url(self, target_url):
        now = time.time()
        keys_to_del = [k for k, v in _URL_CACHE.items() if now - v[1] > 3 * 3600]
        for k in keys_to_del: del _URL_CACHE[k]
            
        if target_url in _URL_CACHE:
            logging.info("Skipping TorBox API (Cache Hit)")
            return _URL_CACHE[target_url][0]

        if 'api.torbox.app' in target_url and 'redirect=true' in target_url:
            json_url = target_url.replace('redirect=true', 'redirect=false')
            try:
                response = _SESSION.get(json_url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=10)
                data = response.json()
                if data.get('success') and data.get('data'):
                    real_url = data['data']
                    _URL_CACHE[target_url] = (real_url, now)
                    return real_url
            except Exception as e:
                logging.error(f"Failed to resolve TorBox CDN link: {e}")
        return target_url
        
    def do_HEAD(self):
        target_url = urllib.parse.unquote(self.path.split('?url=')[-1])
        real_url = self.get_real_url(target_url)
        
        try:
            req_headers = {k: v for k, v in self.headers.items() if k.lower() not in ['host', 'connection']}
            response = _SESSION.head(real_url, headers=req_headers, allow_redirects=True, verify=False, timeout=15)
            self.send_response(response.status_code)
            for key, val in response.headers.items():
                if key.lower() != 'transfer-encoding':
                    self.send_header(key, val)
            self.end_headers()
        except Exception as e:
            self.send_error(500, str(e))

    def do_GET(self):
        if "?url=" not in self.path:
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"TorBox Swarm Proxy Online\n")
            return

        target_url = urllib.parse.unquote(self.path.split('?url=')[-1])
        real_url = self.get_real_url(target_url)
        logging.info(f"Streaming from CDN -> {real_url[:80]}...")

        try:
            req_headers = {}
            range_header = 'bytes=0-'
            for key, val in self.headers.items():
                if key.lower() not in ['host', 'connection']:
                    req_headers[key] = val
                if key.lower() == 'range':
                    range_header = val
                    
            head_resp = _SESSION.head(real_url, headers=req_headers, allow_redirects=True, verify=False, timeout=15)
            
            start_byte = 0
            if '=' in range_header:
                range_spec = range_header.split('=')[1]
                if '-' in range_spec:
                    start_str = range_spec.split('-')[0]
                    if start_str: start_byte = int(start_str)
                    
            total_size = -1
            cr = head_resp.headers.get('Content-Range')
            if cr and '/' in cr:
                total_size = int(cr.split('/')[-1])
            elif 'Content-Length' in head_resp.headers and start_byte == 0:
                total_size = int(head_resp.headers['Content-Length'])
                
            if total_size == -1:
                logging.warning("Could not determine total_size via HEAD, falling back to sequential streaming.")
                response = _SESSION.get(real_url, headers=req_headers, stream=True, allow_redirects=True, verify=False, timeout=15)
                self.send_response(response.status_code)
                for key, val in response.headers.items():
                    if key.lower() != 'transfer-encoding':
                        self.send_header(key, val)
                self.end_headers()
                
                for chunk in response.iter_content(chunk_size=131072):
                    try:
                        self.wfile.write(chunk)
                    except:
                        break
                response.close()
                return

            self.send_response(head_resp.status_code)
            for key, val in head_resp.headers.items():
                if key.lower() != 'transfer-encoding':
                    self.send_header(key, val)
            self.end_headers()
            
            downloader = HybridSwarmDownloader(real_url, req_headers, start_byte, total_size)
            try:
                while True:
                    chunk = downloader.get_next_chunk()
                    if chunk is None: break
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionResetError, BrokenPipeError):
                        logging.warning("Player closed pipe. Stopping hybrid swarm.")
                        break
            finally:
                downloader.stop()

        except Exception as e:
            logging.error(f"Proxy Error: {e}")

if __name__ == "__main__":
    from http.server import ThreadingHTTPServer
    port = int(os.getenv("PROXY_PORT", "8080"))
    server = ThreadingHTTPServer(('0.0.0.0', port), ProxyHandler)
    logging.info(f"Hybrid Swarm Proxy online. Listening on port {port}...")
    server.serve_forever()
