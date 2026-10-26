import os
import re
import sys
import sqlite3
import requests
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from tmdbv3api import TMDb, Search, TV, Movie

load_dotenv()

TORBOX_API_TOKEN = os.getenv("TORBOX_API_TOKEN", "")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
CF_UPDATE_URL = os.getenv("CF_UPDATE_URL", "")
CF_STREAM_URL = os.getenv("CF_STREAM_URL", "")
if os.path.exists("/app"):
    DEFAULT_MEDIA = "/media"
    DEFAULT_DATA = "/app/data"
    DEFAULT_LOGS = "/app/logs"
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    PARENT_DIR = os.path.dirname(BASE_DIR)
    DEFAULT_MEDIA = os.path.join(PARENT_DIR, "media")
    DEFAULT_DATA = os.path.join(BASE_DIR, "data")
    DEFAULT_LOGS = os.path.join(BASE_DIR, "logs")

MEDIA_BASE = os.getenv("MEDIA_DIR", DEFAULT_MEDIA)
DATA_DIR = os.getenv("DATA_DIR", DEFAULT_DATA)
LOG_DIR = os.getenv("LOG_DIR", DEFAULT_LOGS)
JELLYFIN_API_KEY_PATH = os.getenv("JELLYFIN_API_KEY_PATH", "/app/jellyfin_api_key.txt" if os.path.exists("/app") else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jellyfin_api_key.txt"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] JELLYFIN_GEN: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "generator.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)

DEST_MOVIES = os.path.join(MEDIA_BASE, "Movies")
DEST_TV = os.path.join(MEDIA_BASE, "TV")
DEST_ANIME_MOVIES = os.path.join(MEDIA_BASE, "Anime Movies")
DEST_ANIME_TV = os.path.join(MEDIA_BASE, "Anime TV")
DEST_UNCATEGORIZED = os.path.join(MEDIA_BASE, "Uncategorized")

DB_PATH = os.path.join(DATA_DIR, "media_cache.db")
MAX_DELETIONS_PER_CYCLE = 500

# Setup TMDb Client
tmdb = TMDb()
if TMDB_API_KEY:
    tmdb.api_key = TMDB_API_KEY
tmdb.language = 'en'

# Local Parsing Regex Engines
TV_RE = re.compile(
    r"(?<![a-zA-Z])(?:[sS]\d{1,2}(?:[eE]\d{1,3})?(?![kK\d])|\b(?:ep|episode)[\s\._\-]*\d{1,3}\b|\b[sS]eason[\s\._\-]*\d{1,2}\b|\bfinal[\s\._\-]+season\b|\b[sS]ezon[\s\._\-]*\d{1,2}\b|\b\d{1,2}x\d{1,3}\b|Сезон[\s\._\-]*\d{1,2}|Сери[ия][\s\._\-]*\d{1,2}|\[Tenrai-Sensei\])",
    re.IGNORECASE
)
ANIME_GROUP_RE = re.compile(r"^\[(.*?)\]")
TRACKER_TAGS_RE = re.compile(r"^\[(?:bitsearch|btsearch|tgx|rartv|yts|rarbg|ettv|eztv|kickass|1337x|torrenting|req)[^\]]*\]\s*", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
EXTRAS_RE = re.compile(
    r"(?:^|[\/\._\-\s\[\(])(?:sample|trailer|extras?|featurettes?|ncop\d*|nced\d*|opening\d*|ending\d*|\bop\d*\b|\bed\d*\b|menu|creditless|gag[\s\._\-]?reel|behind[\s\._\-]?the[\s\._\-]?scenes|deleted[\s\._\-]?scenes|bonus|interview)(?:[\/\._\-\s\]\)]|\.|$)",
    re.IGNORECASE
)
SAMPLE_RE = EXTRAS_RE
PART_RE = re.compile(r"[-._\s](?:cd|part|pt|disc|disk)[\s._-]?(\d+)", re.IGNORECASE)
RD_MEDIA_DIR = os.getenv("RD_MEDIA_DIR", "")

INDIAN_LANGUAGES = {'ta', 'te', 'ml', 'hi', 'kn', 'bn', 'mr', 'pa'}
INDIAN_INDICATORS = re.compile(
    r"\b(tamilmv|tamilblasters|1tamilmv|1tamilblasters|ospreay|d3us|"
    r"tamil|telugu|malayalam|hindi|kannada|bengali|marathi|punjabi|"
    r"tam|tel|hin|mal|kan|kollywood|tollywood|mollywood|bollywood)\b",
    re.IGNORECASE
)

# =====================================================================
# CLOUDFLARE DYNAMIC IP UPDATE
# =====================================================================
def ping_cloudflare():
    if not CF_UPDATE_URL:
        return
    try:
        req_headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(CF_UPDATE_URL, headers=req_headers, timeout=10)
        logging.info(f"Cloudflare Dynamic IP Ping: Status {resp.status_code} - {resp.text.strip()}")
    except Exception as e:
        logging.error(f"Failed to ping Cloudflare Worker: {e}")

# =====================================================================
# LOCAL DATABASE CACHING LAYER
# =====================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS media_cache (
            raw_title TEXT PRIMARY KEY,
            media_type TEXT,
            is_anime INTEGER,
            tmdb_id INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS torrent_master (
            item_type TEXT,
            item_id TEXT,
            section TEXT,
            title TEXT,
            year INTEGER,
            tmdb_id INTEGER,
            is_anime INTEGER,
            media_type TEXT,
            PRIMARY KEY (item_type, item_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_master (
            item_type TEXT,
            item_id TEXT,
            file_id TEXT,
            section TEXT,
            title TEXT,
            year INTEGER,
            tmdb_id INTEGER,
            is_anime INTEGER,
            media_type TEXT,
            PRIMARY KEY (item_type, item_id, file_id)
        )
    ''')
    cursor.execute("PRAGMA table_info(media_cache)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'tmdb_id' not in columns:
        cursor.execute("ALTER TABLE media_cache ADD COLUMN tmdb_id INTEGER")
    conn.commit()
    conn.close()

def get_master_metadata(item_type, item_id):
    type_key = "torrent_id" if item_type == "torrents" else "web_id"
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT section, title, year, tmdb_id, is_anime, media_type 
            FROM torrent_master 
            WHERE item_type = ? AND item_id = ?
        """, (type_key, str(item_id)))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "section": row[0],
                "title": row[1],
                "year": row[2],
                "tmdb_id": row[3],
                "is_anime": bool(row[4]),
                "media_type": row[5]
            }
    except Exception as e:
        logging.warning(f"Error querying torrent_master for {item_id}: {e}")
    return None

def get_file_metadata(item_type, item_id, file_id):
    type_key = "torrent_id" if item_type == "torrents" else "web_id"
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT section, title, year, tmdb_id, is_anime, media_type 
            FROM file_master 
            WHERE item_type = ? AND item_id = ? AND file_id = ?
        """, (type_key, str(item_id), str(file_id)))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "section": row[0],
                "title": row[1],
                "year": row[2],
                "tmdb_id": row[3],
                "is_anime": bool(row[4]),
                "media_type": row[5]
            }
    except Exception as e:
        logging.warning(f"Error querying file_master for {item_id}/{file_id}: {e}")
    return None

def get_cached_metadata(raw_title):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT media_type, is_anime, tmdb_id FROM media_cache WHERE raw_title = ?", (raw_title,))
    result = cursor.fetchone()
    conn.close()
    return result if result else None

def save_to_cache(raw_title, media_type, is_anime, tmdb_id=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO media_cache (raw_title, media_type, is_anime, tmdb_id) VALUES (?, ?, ?, ?)",
        (raw_title, media_type, int(is_anime), tmdb_id)
    )
    conn.commit()
    conn.close()

def update_master_override(item_type, item_id, section, tmdb_id=None, is_anime=None, media_type=None, title=None, year=None):
    type_key = "torrent_id" if item_type == "torrents" else "web_id"
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE torrent_master 
            SET section = COALESCE(?, section),
                tmdb_id = COALESCE(?, tmdb_id),
                is_anime = COALESCE(?, is_anime),
                media_type = COALESCE(?, media_type),
                title = COALESCE(?, title),
                year = COALESCE(?, year)
            WHERE item_type = ? AND item_id = ?
        """, (section, tmdb_id, int(is_anime) if is_anime is not None else None, media_type, title, year, type_key, str(item_id)))
        
        cursor.execute("""
            UPDATE file_master 
            SET section = COALESCE(?, section),
                tmdb_id = COALESCE(?, tmdb_id),
                is_anime = COALESCE(?, is_anime),
                media_type = COALESCE(?, media_type),
                title = COALESCE(?, title),
                year = COALESCE(?, year)
            WHERE item_type = ? AND item_id = ?
        """, (section, tmdb_id, int(is_anime) if is_anime is not None else None, media_type, title, year, type_key, str(item_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"Error updating torrent_master override for {item_id}: {e}")

def update_file_master_override(item_type, item_id, file_id, section, tmdb_id=None, is_anime=None, media_type=None, title=None, year=None):
    type_key = "torrent_id" if item_type == "torrents" else "web_id"
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE file_master 
            SET section = COALESCE(?, section),
                tmdb_id = COALESCE(?, tmdb_id),
                is_anime = COALESCE(?, is_anime),
                media_type = COALESCE(?, media_type),
                title = COALESCE(?, title),
                year = COALESCE(?, year)
            WHERE item_type = ? AND item_id = ? AND file_id = ?
        """, (section, tmdb_id, int(is_anime) if is_anime is not None else None, media_type, title, year, type_key, str(item_id), str(file_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"Error updating file_master override for {item_id}/{file_id}: {e}")

# =====================================================================
# FILENAME CLEANING & API CALLS
# =====================================================================
def safe_truncate(name: str, max_bytes: int = 255) -> str:
    """Ensure directory/file name does not exceed filesystem limit (255 UTF-8 bytes)."""
    encoded = name.encode('utf-8')
    if len(encoded) <= max_bytes:
        return name
    while len(name.encode('utf-8')) > max_bytes:
        name = name[:-1]
    return name.strip()

def clean_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return safe_truncate(cleaned, 255)

def clean_title(raw_name):
    clean = urllib.parse.unquote(raw_name)
    clean = clean.replace('\xa0', ' ')

    # Strip Russian/Cyrillic characters ONLY if English characters are also present
    # Prevents confusion from hybrid titles like "Жестокое лето Cruel Summer"
    if re.search(r'[А-Яа-яЁё]', clean) and re.search(r'[A-Za-z]', clean):
        clean = re.sub(r'[А-Яа-яЁё]+', '', clean)
        clean = re.sub(r'\s+', ' ', clean)
        clean = re.sub(r'^[\s\-/|]+', '', clean).strip()

    # Strip site prefixes (e.g. www.1TamilMV.lease - , www.1TamilBlasters.pm - )
    clean = re.sub(r'^(?:https?:\/\/)?www\.[^\s\-]+(?:\s*[\-\_:]\s*|\s+)', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\b(?:www\.)?1Tamil(?:MV|Blasters)\b[^\s]*\s*[-:]?\s*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'^(?:REQ\s+|req\s+)', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r"^\[.*?\]\s*", "", clean)
    clean = re.sub(r"\s*\[.*?\]$", "", clean)
    clean = re.split(r'\s+\d+\s+\[?\d+-\d+', clean)[0]
    clean = re.sub(r'\(?\s*Сезон\s*\d+\s*\)?', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\[?\s*\d+-\d+\s*(?:из\s*\d+)?\s*\]?', '', clean, flags=re.IGNORECASE)

    # Find 4-digit release year (19xx or 20xx)
    year = None
    year_match = re.search(r'[\(\[\s\._\-]((?:19|20)\d{2})[\)\]\s\._\-]', clean)
    if year_match:
        year = year_match.group(1)
        idx = year_match.start()
        prefix = clean[:idx].strip(" ._-()[]{}")
        # If there is a clear title before the year, use it as candidate title
        if prefix and not re.match(r'^(?:Season|Сезон|S\d+)', prefix, re.IGNORECASE):
            clean = prefix
    else:
        year_end = re.search(r'[\(\[\s\._\-]((?:19|20)\d{2})\s*$', clean)
        if year_end:
            year = year_end.group(1)
            clean = clean[:year_end.start()].strip(" ._-()[]{}")

    # Clean dots/underscores/dashes
    clean = re.sub(r'[\._\-]', ' ', clean)

    # Strip audio and track brackets (e.g. [Tam + Tel + Hin], (DD+5.1 - 640Kbps))
    clean = re.sub(r'\[.*?\]', ' ', clean)
    clean = re.sub(r'\(.*?\)', ' ', clean)

    # Strip scene junk words
    junk_words = (
        r"\b(2160p|1080p|720p|480p|4k|UHD|HDR|HDR10|HDR10\+|DV|Dolby[\s\.\-]?Vision|REMUX|BLURAY|BDRip|BRRip|"
        r"TRUE[\s\.\-]?(?:WEB\-?DL|WEBRip)?|WEB\-?DL|WEBRip|WEB|HDTV|HD\-?Rip|Pre\-?DVDRip|"
        r"AMZN|NF|ATVP|DSNP|MAX|HMAX|HULU|STAN|CRAV|PCOK|Peacock|"
        r"x264|x265|H\.?264|H\.?265|HEVC|AVC|10\-?Bit|Hi10|DS4K|SDR|"
        r"DDP\d(?:[\.\s]\d)?|DD\+?\d(?:[\.\s]\d)?|DTS\-?HD|DTS(?:\-?MA)?(?:\s*5\.1)?|AAC(?:\s*2\.0)?|FLAC|TrueHD|Atmos|"
        r"Dual[\s\.\-]?Audio|Multi[\s\.\-]?Audio|UNTOUCHED|Restoration|Remastered|Uncut|API|ESub|ESubs|Sub|Subs|"
        r"Tamil|Telugu|Malayalam|Hindi|Kannada|Bengali|Marathi|Punjabi|Tam|Tel|Hin|Mal|Kan|"
        r"Super[\s\.\-]?Duper[\s\.\-]?Cut|Extended|Unrated|Directors[\s\.\-]?Cut|PROPER|REPACK|COMPLETE|Dub|MVO|Original|Multi)\b"
    )
    clean = re.compile(junk_words, re.IGNORECASE).split(clean)[0]
    clean = TV_RE.split(clean)[0]

    if year:
        clean = re.sub(rf"\b{year}\b", "", clean)

    clean = re.sub(r'[\(\)\[\]\{\}]', '', clean)

    # Strip leading acronym if followed by full words (e.g. "LIK Love Insurance Kompany" -> "Love Insurance Kompany")
    clean = re.sub(r'^[A-Z]{2,5}\s+(?=[A-Z][a-z])', '', clean.strip())

    clean = re.sub(r'\s+\d+$', '', clean.strip())
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean, year

def extract_results(res):
    if not res:
        return []
    if hasattr(res, 'results'):
        try:
            return list(res.results)
        except Exception:
            return []
    if isinstance(res, list):
        return res
    return []

def pick_best_match(results, cleaned_title, extracted_year, is_indian=False):
    if not results:
        return None
    cleaned_lower = cleaned_title.lower()
    best_candidate = None
    best_score = -1000
    
    for m in results:
        title = getattr(m, "title", getattr(m, "name", "")) or ""
        title_lower = title.lower()
        orig_title = getattr(m, "original_title", getattr(m, "original_name", "")) or ""
        orig_title_lower = orig_title.lower()
        rel_date = getattr(m, "release_date", getattr(m, "first_air_date", "")) or ""
        m_year = rel_date.split("-")[0] if rel_date and "-" in rel_date else None
        lang = getattr(m, "original_language", "")
        has_poster = bool(getattr(m, "poster_path", None))
        
        score = 0
        if title_lower == cleaned_lower or orig_title_lower == cleaned_lower:
            score += 50
        elif title_lower.startswith(cleaned_lower) or cleaned_lower.startswith(title_lower):
            score += 25
        elif cleaned_lower in title_lower:
            score += 15
            
        if extracted_year and m_year:
            try:
                if str(extracted_year) == str(m_year):
                    score += 30
                elif abs(int(extracted_year) - int(m_year)) == 1:
                    score += 10
                else:
                    score -= 20
            except ValueError:
                pass
                
        if is_indian:
            if lang in INDIAN_LANGUAGES:
                score += 40
            else:
                score -= 10
                
        if has_poster:
            score += 5
            
        if score > best_score:
            best_score = score
            best_candidate = m
            
    return best_candidate or results[0]

def fallback_identify(raw_name, item_files=None):
    guessed_tv = bool(TV_RE.search(raw_name))
    if not guessed_tv and item_files:
        for f in item_files:
            if TV_RE.search(f):
                guessed_tv = True
                break
                
    has_leading_bracket = bool(ANIME_GROUP_RE.search(raw_name)) and not bool(TRACKER_TAGS_RE.search(raw_name))
    cleaned, extracted_year = clean_title(raw_name)
    has_year = bool(extracted_year) or bool(YEAR_RE.search(raw_name))
    
    if guessed_tv:
        if has_leading_bracket:
            return "tv", True, None
        return "tv", False, None
    if has_year:
        return "movie", False, None
    return "uncategorized", False, None

def identify_via_api(raw_name, item_files=None):
    if not TMDB_API_KEY:
        logging.warning("TMDB_API_KEY is not set. Falling back to regex only.")
        return fallback_identify(raw_name, item_files)

    cleaned, extracted_year = clean_title(raw_name)
    if not cleaned:
        return "uncategorized", False, None

    guessed_tv = bool(TV_RE.search(raw_name))
    if not guessed_tv and item_files:
        for f in item_files:
            if TV_RE.search(f):
                guessed_tv = True
                break

    has_leading_bracket = bool(ANIME_GROUP_RE.search(raw_name)) and not bool(TRACKER_TAGS_RE.search(raw_name))
    is_indian = bool(INDIAN_INDICATORS.search(raw_name))
    if not is_indian and item_files:
        for f in item_files:
            if INDIAN_INDICATORS.search(f):
                is_indian = True
                break

    try:
        cleaned_search = re.sub(r'[^\w\s]', '', cleaned).strip()
        search = Search()
        
        results = None
        if guessed_tv:
            results = extract_results(search.tv_shows(term=cleaned_search, release_year=extracted_year))
            if not results and extracted_year:
                results = extract_results(search.tv_shows(term=cleaned_search))
        else:
            results = extract_results(search.movies(term=cleaned_search, year=extracted_year))
            if not results and extracted_year:
                results = extract_results(search.movies(term=cleaned_search))
                    
        if not results:
            results = extract_results(search.multi(term=cleaned_search))

        if results and len(results) > 0:
            top_match = pick_best_match(results, cleaned, extracted_year, is_indian)
            tmdb_id = getattr(top_match, "id", None)
            
            if hasattr(top_match, "media_type"):
                m_type = getattr(top_match, "media_type")
            elif guessed_tv:
                m_type = "tv"
            else:
                m_type = "movie"
            
            genre_ids = getattr(top_match, "genre_ids", []) or []
            origin_country = getattr(top_match, "origin_country", []) or []
            original_language = getattr(top_match, "original_language", "")
            
            is_anime = False
            if 16 in genre_ids and (original_language == "ja" or "JP" in origin_country or has_leading_bracket):
                is_anime = True
                
            # Check Real-Debrid override if path is available
            if os.path.exists(RD_MEDIA_DIR):
                for cat in ["Anime Movies", "Anime TV", "Movies", "TV"]:
                    if os.path.exists(os.path.join(RD_MEDIA_DIR, cat, raw_name)):
                        if cat == "Anime TV": return "tv", True, tmdb_id
                        elif cat == "Anime Movies": return "movie", True, tmdb_id
                        elif cat == "Movies": return "movie", False, tmdb_id
                        elif cat == "TV": return "tv", False, tmdb_id

            return m_type, is_anime, tmdb_id
            
    except Exception as e:
        logging.warning(f"TMDb search failed for '{raw_name}': {e}")

    # Fallback to RD override if API search didn't match
    if os.path.exists(RD_MEDIA_DIR):
        for cat in ["Anime Movies", "Anime TV", "Movies", "TV"]:
            if os.path.exists(os.path.join(RD_MEDIA_DIR, cat, raw_name)):
                if cat == "Anime TV": return "tv", True, None
                elif cat == "Anime Movies": return "movie", True, None
                elif cat == "Movies": return "movie", False, None
                elif cat == "TV": return "tv", False, None

    return fallback_identify(raw_name, item_files)

_TYBW_SEASON_CACHE = None

def get_tybw_season_data():
    """Fetch and cache Bleach: Thousand-Year Blood War (TMDb TV 30984 Season 2) metadata and episodes."""
    global _TYBW_SEASON_CACHE
    if _TYBW_SEASON_CACHE is not None:
        return _TYBW_SEASON_CACHE
    if not TMDB_API_KEY:
        return None
    try:
        resp = requests.get(f"https://api.themoviedb.org/3/tv/30984/season/2?api_key={TMDB_API_KEY}", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            ep_map = {ep.get("episode_number"): ep for ep in data.get("episodes", []) if ep.get("episode_number")}
            _TYBW_SEASON_CACHE = {
                "title": "Bleach Thousand-Year Blood War",
                "year": "2022",
                "poster_path": data.get("poster_path"),
                "overview": data.get("overview", ""),
                "episodes": ep_map
            }
            return _TYBW_SEASON_CACHE
    except Exception as e:
        logging.warning(f"Error fetching TMDb season details for Bleach TYBW: {e}")
    return None

def get_tmdb_details(media_type: str, tmdb_id: int):
    """Fetch title, year, poster_path, overview from TMDb."""
    if not TMDB_API_KEY or not tmdb_id:
        return None
    if media_type == "tv" and int(tmdb_id) == 335660:
        return get_tybw_season_data()
    try:
        if media_type == "tv":
            tv_api = TV()
            details = tv_api.details(tmdb_id)
            title = getattr(details, "name", "")
            first_air = getattr(details, "first_air_date", "")
            year = first_air.split("-")[0] if first_air and "-" in first_air else None
            poster_path = getattr(details, "poster_path", None)
            overview = getattr(details, "overview", "")
            return {"title": title, "year": year, "poster_path": poster_path, "overview": overview}
        else:
            mov_api = Movie()
            details = mov_api.details(tmdb_id)
            title = getattr(details, "title", "")
            rel_date = getattr(details, "release_date", "")
            year = rel_date.split("-")[0] if rel_date and "-" in rel_date else None
            poster_path = getattr(details, "poster_path", None)
            overview = getattr(details, "overview", "")
            return {"title": title, "year": year, "poster_path": poster_path, "overview": overview}
    except Exception as e:
        logging.warning(f"Error fetching TMDb details for {media_type} ID {tmdb_id}: {e}")
        return None

# =====================================================================
# TORBOX API LOGIC
# =====================================================================
def api_request(endpoint, params=None):
    if not TORBOX_API_TOKEN:
        logging.error("TORBOX_API_TOKEN not set")
        return None
    url = f"https://api.torbox.app/v1/api/{endpoint}"
    headers = {
        "Authorization": f"Bearer {TORBOX_API_TOKEN}",
        "Accept": "application/json"
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Failed fetching {endpoint}: {e}")
        return None

def get_torbox_items():
    items = []
    
    # Torrents with pagination
    offset = 0
    seen_ids = set()
    while offset <= 15000:
        res = api_request("torrents/mylist", params={"limit": 1000, "offset": offset, "skip": offset, "bypass_cache": "true"})
        if res and res.get("success") and "data" in res and res["data"]:
            batch = res["data"]
            new_items_found = False
            for t in batch:
                if t.get("id") not in seen_ids:
                    seen_ids.add(t.get("id"))
                    new_items_found = True
                    if t.get("download_state") == "completed" or t.get("download_finished"):
                        items.append({
                            "id": t.get("id"),
                            "name": t.get("name"),
                            "files": t.get("files", []),
                            "type": "torrents"
                        })
            if len(batch) < 1000 or not new_items_found:
                break
            offset += 1000
        else:
            break

    # WebDLs with pagination
    offset = 0
    while offset <= 15000:
        res = api_request("webdl/mylist", params={"limit": 1000, "offset": offset, "skip": offset, "bypass_cache": "true"})
        if res and res.get("success") and "data" in res and res["data"]:
            batch = res["data"]
            new_items_found = False
            for w in batch:
                if w.get("id") not in seen_ids:
                    seen_ids.add(w.get("id"))
                    new_items_found = True
                    if w.get("download_state") == "completed" or w.get("download_finished"):
                        items.append({
                            "id": w.get("id"),
                            "name": w.get("name"),
                            "files": w.get("files", []),
                            "type": "webdl"
                        })
            if len(batch) < 1000 or not new_items_found:
                break
            offset += 1000
        else:
            break
                
    return items

def get_strm_url(item_type, item_id, file_id):
    if item_type == "torrents":
        torbox_url = f"https://api.torbox.app/v1/api/torrents/requestdl?token={TORBOX_API_TOKEN}&torrent_id={item_id}&file_id={file_id}&redirect=true"
    else:
        torbox_url = f"https://api.torbox.app/v1/api/webdl/requestdl?token={TORBOX_API_TOKEN}&web_id={item_id}&file_id={file_id}&redirect=true"
        
    if CF_STREAM_URL:
        return f"{CF_STREAM_URL}/stream?url={urllib.parse.quote(torbox_url)}"
    return torbox_url

STRICT_EXTRAS_RE = re.compile(
    r"(?:^|[\/\._\-\s\[\(])(?:sample|trailer|ncop\d*|nced\d*|creditless|gag[\s\._\-]?reel|behind[\s\._\-]?the[\s\._\-]?scenes|deleted[\s\._\-]?scenes)(?:[\/\._\-\s\]\)]|\.|$)",
    re.IGNORECASE
)
FOLDER_EXTRAS_RE = re.compile(
    r"(?:^|\/)(?:extras?|featurettes?|bonus|trailers?|samples?|ncop|nced|menus?|interviews?)\/",
    re.IGNORECASE
)

def is_video_file(filename: str) -> bool:
    if not filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ['.mkv', '.mp4', '.avi', '.ts', '.webm', '.m2ts', '.wmv', '.mov']:
        return False
    if re.search(r"\bS\d{1,2}E\d{1,3}\b", filename, re.IGNORECASE):
        if not STRICT_EXTRAS_RE.search(filename) and not FOLDER_EXTRAS_RE.search(filename):
            return True
    if SAMPLE_RE.search(filename):
        return False
    return True

def is_valid_item_file(f: dict) -> bool:
    full_fn = f.get("name", "")
    short_fn = f.get("short_name", "")
    if not is_video_file(full_fn) and not is_video_file(short_fn):
        return False
    parts = full_fn.split('/')
    rel_fn = '/'.join(parts[1:]) if len(parts) > 1 else full_fn
    if re.search(r"\bS\d{1,2}E\d{1,3}\b", short_fn or rel_fn, re.IGNORECASE):
        if not STRICT_EXTRAS_RE.search(rel_fn) and not FOLDER_EXTRAS_RE.search(rel_fn):
            return True
    if EXTRAS_RE.search(rel_fn) or EXTRAS_RE.search(short_fn):
        return False
    return True

# =====================================================================
# METADATA & NFO WRITING
# =====================================================================
def write_nfo(nfo_path: str, root_tag: str, title: str, year: str | None, overview: str | None, tmdb_id: int | None):
    if os.path.exists(nfo_path):
        return
    try:
        root = ET.Element(root_tag)
        ET.SubElement(root, "title").text = title
        if year:
            ET.SubElement(root, "year").text = str(year)
        if overview:
            ET.SubElement(root, "plot").text = overview
        if tmdb_id:
            u = ET.SubElement(root, "uniqueid", type="tmdb", default="true")
            u.text = str(tmdb_id)
            
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ", level=0)
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
    except Exception as e:
        logging.warning(f"Failed to write NFO {nfo_path}: {e}")

def download_poster(poster_path: str, tmdb_poster_slug: str):
    if not tmdb_poster_slug or os.path.exists(poster_path):
        return
    try:
        url = f"https://image.tmdb.org/t/p/w500{tmdb_poster_slug}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            with open(poster_path, "wb") as f:
                f.write(resp.content)
            logging.info(f"Downloaded poster: {poster_path}")
    except Exception as e:
        logging.warning(f"Failed to download poster from {tmdb_poster_slug}: {e}")

# =====================================================================
# JELLYFIN LIBRARY REFRESH
# =====================================================================
def notify_jellyfin():
    if not os.path.isfile(JELLYFIN_API_KEY_PATH):
        logging.warning("Jellyfin API key file not found. Skipping library refresh trigger.")
        return
        
    try:
        with open(JELLYFIN_API_KEY_PATH, "r") as f:
            token = f.read().strip()
            
        url = "http://127.0.0.1:8096/Library/Refresh"
        headers = {
            "Authorization": f'MediaBrowser Token="{token}"',
            "X-Emby-Token": token,
        }
        resp = requests.post(url, headers=headers, timeout=10)
        if resp.status_code in (200, 204):
            logging.info("Triggered Jellyfin Library Refresh successfully.")
        else:
            logging.warning(f"Jellyfin Library Refresh returned status: {resp.status_code}")
    except Exception as e:
        logging.error(f"Failed to trigger Jellyfin Library Refresh: {e}")

# =====================================================================
# SYNC ENGINE FOR JELLYFIN
# =====================================================================
def parse_season_episode(filename: str, fallback_season: int = 1):
    base = os.path.splitext(filename)[0]
    
    # Check if Season or Series is explicitly mentioned in filename
    season = fallback_season
    sm = re.search(r'\b(?:Season|Series|Sezon|Сезон)\s*(\d{1,2})\b', base, re.IGNORECASE)
    if sm:
        season = int(sm.group(1))

    # 1. Check S01E02 format
    m = re.search(r"S(\d{1,2})E(\d{1,3})", base, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
        
    # 2. Check 1x02 format
    m = re.search(r"\b(\d{1,2})x(\d{1,3})\b", base, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
        
    # 3. Check OVA numbering
    m = re.search(r"\bOVA\s*(\d{1,2})\b", base, re.IGNORECASE)
    if m:
        return 0, int(m.group(1))
        
    # 4. Check E02 or episode numbers
    m = re.search(r"\bE(\d{1,3})\b", base, re.IGNORECASE)
    if m:
        return season, int(m.group(1))

    # 5. Check Episode / Ep numbering e.g. "Episode 02" or "Ep 02"
    m = re.search(r"\b(?:Episode|Ep)\s*(\d{1,3})\b", base, re.IGNORECASE)
    if m:
        return season, int(m.group(1))
        
    # 6. Check anime dash/underscore numbering e.g. "Bleach - 27", "_-_02_"
    m = re.search(r"[-_]+\s*(\d{1,3})(?:[-_\s\(\[]|$)", base)
    if m:
        return season, int(m.group(1))
        
    # 7. Check standalone episode number before bracket/paren: 'Title 02 [BD'
    no_brackets = re.sub(r'\[.*?\]|\(.*?\)', '', base).strip()
    m = re.search(r'(?:^|\s)(\d{1,3})\s*$', no_brackets)
    if m:
        return season, int(m.group(1))

    return season, 1

def extract_quality_tag(raw_name: str, filename: str = "") -> tuple[str, str | None]:
    text = f"{raw_name} {filename}"
    tags = []
    
    # 1. Resolution
    res_m = re.search(r'\b(2160p|4k|1080p|720p|480p|576p)\b', text, re.IGNORECASE)
    if res_m:
        r = res_m.group(1).lower()
        if r == '4k':
            r = '2160p'
        tags.append(r)

    # 2. Edition
    if re.search(r'\bIMAX\b', text, re.IGNORECASE):
        tags.append('IMAX')
        
    # 3. Source / Edition
    if re.search(r'\bREMUX\b', text, re.IGNORECASE):
        tags.append('Remux')
    elif re.search(r'\b(?:BluRay|BDRip|BRRip)\b', text, re.IGNORECASE):
        tags.append('BluRay')
    elif re.search(r'\b(?:WEB-?DL|WEBRip)\b', text, re.IGNORECASE):
        tags.append('WEB-DL')
    elif re.search(r'\bHDTV\b', text, re.IGNORECASE):
        tags.append('HDTV')
        
    # 3. Dynamic Range
    if re.search(r'\b(?:DV|DoVi|Dolby[\s\.\-]?Vision)\b', text, re.IGNORECASE):
        tags.append('DV')
    elif re.search(r'\b(?:HDR10\+|HDR10|HDR)\b', text, re.IGNORECASE):
        tags.append('HDR')
        
    # 4. Release Group / Distinguishing tag
    clean_f = re.sub(r'\.[a-zA-Z0-9]{3,4}$', '', filename or raw_name)
    grp_m = re.search(r'-([A-Za-z0-9_]+)$', clean_f) or re.search(r'\[([A-Za-z0-9_\s\-]+)\]$', clean_f)
    grp = grp_m.group(1).strip() if grp_m else None
    
    base_label = " ".join(tags) if tags else "Unknown"
    return base_label, grp

def disambiguate_versions(version_items: list[dict]) -> list[str]:
    """
    Given a list of file entries mapped to the same destination base strm name,
    return a list of unique version strings corresponding to each entry.
    """
    initial_labels = [extract_quality_tag(v["raw_name"], v["short_name"]) for v in version_items]
    counts = {}
    for lbl, _ in initial_labels:
        counts[lbl] = counts.get(lbl, 0) + 1
        
    final_labels = []
    seen = {}
    for lbl, grp in initial_labels:
        if counts[lbl] > 1:
            if grp and grp.lower() not in lbl.lower():
                cand = f"{lbl} - {grp}"
            else:
                cand = lbl
        else:
            cand = lbl
            
        if cand in seen:
            seen[cand] += 1
            cand = f"{cand} ({seen[cand]})"
        else:
            seen[cand] = 1
            
        final_labels.append(cand)
        
    return final_labels

def map_high_school_dxd(short_name: str, fallback_season: int = 1):
    """Ported from strm_generator.py to map High School DxD seasons and OVAs into Jellyfin."""
    mapped_name = short_name
    if not re.search(r'S\d{2}E\d{2}', mapped_name, re.IGNORECASE):
        if "NEW" in mapped_name and "OVA" not in mapped_name and "SP" not in mapped_name:
            mapped_name = re.sub(r'-\s*(\d{2})', r'- S02E\1', mapped_name)
        elif "BorN" in mapped_name and "OVA" not in mapped_name and "SP" not in mapped_name:
            mapped_name = re.sub(r'-\s*(\d{2})', r'- S03E\1', mapped_name)
        elif "Hero" in mapped_name:
            mapped_name = re.sub(r'Hero\s*(\d{2})', r'- S04E\1', mapped_name)
        elif "OVA" not in mapped_name and "SP" not in mapped_name:
            mapped_name = re.sub(r'-\s*(\d{2})', r'- S01E\1', mapped_name)

        if "OVA" in mapped_name or "SP" in mapped_name:
            m = re.search(r'(?:SP)0?(\d+)|0?(\d+)\s*OVA', mapped_name, re.IGNORECASE)
            num = int(m.group(1) or m.group(2)) if m else 1
            ep = None
            if "BorN" in mapped_name:
                if "OVA" in mapped_name:
                    ep = 16
                else:
                    ep_map = {1: 12, 2: 13, 3: 14, 4: 15, 5: 17, 6: 18}
                    ep = ep_map.get(num, 99)
            elif "NEW" in mapped_name:
                ep = 11
            else:
                if "OVA" in mapped_name:
                    ep = 8 if num == 13 else 9
                else:
                    ep = num
            mapped_name = f"S00E{ep:02d} - {mapped_name}"
    
    return parse_season_episode(mapped_name, fallback_season)

BLEACH_TYBW_RE = re.compile(
    r"\b(?:thousand[\s\._\-]*year[\s\._\-]*blood[\s\._\-]*war|sennen[\s\._\-]*kessen|tybw|ketsubetsu|soukoku|kashin)\b",
    re.IGNORECASE
)
BLEACH_S17_RE = re.compile(r"\b(?:S17|Season[\s\._\-]*17)\b", re.IGNORECASE)

def is_bleach_tybw(raw_name: str, file_names: list[str] | None = None) -> bool:
    """Detect whether a Bleach release belongs to Bleach: Thousand-Year Blood War (including S17 releases)."""
    combined = " ".join([raw_name] + (file_names or []))
    if "bleach" not in combined.lower():
        return False
    return bool(BLEACH_TYBW_RE.search(combined) or BLEACH_S17_RE.search(combined))

def map_bleach_tybw(short_name: str, fallback_season: int = 1) -> tuple[int, int]:
    """Map Bleach: Thousand-Year Blood War episode numbers (including S17Exx and cour S02-S04 numbering) into Season 01."""
    s_num, ep_num = parse_season_episode(short_name, fallback_season)
    if s_num == 17:
        return 1, ep_num
    if s_num == 2 and 1 <= ep_num <= 13:
        return 1, 13 + ep_num
    if s_num == 3 and 1 <= ep_num <= 14:
        return 1, 26 + ep_num
    if s_num == 4 and 1 <= ep_num <= 14:
        return 1, 40 + ep_num
    if ep_num >= 367:
        return 1, ep_num - 366
    return 1, ep_num

def write_tybw_episode_metadata(season_dir: str, base_strm_name: str, ep_num: int):
    """Write episode NFO and thumbnail for Bleach: Thousand-Year Blood War from TMDb TV 30984 Season 2."""
    tybw_data = get_tybw_season_data()
    if not tybw_data:
        return
    ep_info = tybw_data.get("episodes", {}).get(ep_num)
    if not ep_info:
        return
    nfo_path = os.path.join(season_dir, f"{base_strm_name}.nfo")
    if not os.path.exists(nfo_path):
        try:
            root = ET.Element("episodedetails")
            ET.SubElement(root, "title").text = ep_info.get("name") or f"Episode {ep_num}"
            ET.SubElement(root, "season").text = "1"
            ET.SubElement(root, "episode").text = str(ep_num)
            if ep_info.get("air_date"):
                ET.SubElement(root, "aired").text = ep_info["air_date"]
            if ep_info.get("overview"):
                ET.SubElement(root, "plot").text = ep_info["overview"]
            tree = ET.ElementTree(root)
            ET.indent(tree, space="  ", level=0)
            tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        except Exception as e:
            logging.warning(f"Failed to write TYBW episode NFO {nfo_path}: {e}")
    still_slug = ep_info.get("still_path")
    thumb_path = os.path.join(season_dir, f"{base_strm_name}-thumb.jpg")
    if still_slug and not os.path.exists(thumb_path):
        download_poster(thumb_path, still_slug)

def sync_jellyfin():
    init_db()
    ping_cloudflare()

    if not TORBOX_API_TOKEN:
        logging.warning("TORBOX_API_TOKEN missing. Aborting sync.")
        return

    # Ensure default section directories exist
    default_sections = ["Movies", "TV", "Anime Movies", "Anime TV", "Uncategorized"]
    for s in default_sections:
        os.makedirs(os.path.join(MEDIA_BASE, s), exist_ok=True)

    # Discover all section directories under MEDIA_BASE (including any custom user folders)
    all_sections = [
        d for d in os.listdir(MEDIA_BASE)
        if os.path.isdir(os.path.join(MEDIA_BASE, d)) and not d.startswith(".")
    ]
    for s in default_sections:
        if s not in all_sections:
            all_sections.append(s)

    # Disk directory indexes across all sections
    # tmdb_id -> list of (sec_name, sec_path, dir_name)
    disk_dirs_by_tmdb = {}
    # clean_title_key -> list of (sec_name, sec_path, dir_name)
    disk_dirs_by_title = {}
    # exact_dir_name.lower() -> (sec_name, sec_path, dir_name)
    disk_dirs_by_exact = {}

    for sec_name in all_sections:
        sec_path = os.path.join(MEDIA_BASE, sec_name)
        if not os.path.exists(sec_path):
            continue
        try:
            entries = os.listdir(sec_path)
        except Exception:
            entries = []
        for d in entries:
            full_d = os.path.join(sec_path, d)
            if not os.path.isdir(full_d) or d.startswith("."):
                continue
            
            # 1. TMDb ID
            m_tmdb = re.search(r"\[tmdbid-(\d+)\]|\{tmdb-(\d+)\}", d, re.IGNORECASE)
            if m_tmdb:
                tid = int(m_tmdb.group(1) or m_tmdb.group(2))
                disk_dirs_by_tmdb.setdefault(tid, []).append((sec_name, sec_path, d))

            # 2. Normalized Title Key
            t_clean = re.sub(r"\[tmdbid-\d+\]|\{tmdb-\d+\}", "", d, flags=re.IGNORECASE)
            t_clean = re.sub(r"\(?\b(19\d{2}|20\d{2})\b\)?", "", t_clean)
            t_clean = re.sub(r"[^\w\s]", "", t_clean).strip().lower()
            if t_clean:
                disk_dirs_by_title.setdefault(t_clean, []).append((sec_name, sec_path, d))

            # 3. Exact folder name
            disk_dirs_by_exact[d.lower()] = (sec_name, sec_path, d)

    logging.info("Fetching active items from TorBox...")
    active_items = get_torbox_items()
    
    if not active_items:
        logging.warning("API returned 0 active items. Network drop suspected. Skipping sync cycle to protect library.")
        return

    logging.info(f"Found {len(active_items)} active items. Synchronizing Jellyfin library...")
    generated_strm_paths = set()
    planned_strm_targets = {}
    files_added_or_updated = 0

    # Process each active item
    for item in active_items:
        raw_name = item["name"]
        
        # Info Hash fallback
        if re.match(r'^[a-fA-F0-9]{40}$', raw_name) or raw_name.lower().startswith("magnet"):
            video_files = [f for f in item.get("files", []) if is_video_file(f.get("name", ""))]
            if video_files:
                first_name = video_files[0].get("short_name", video_files[0].get("name", ""))
                better_name = os.path.splitext(first_name)[0]
                if better_name:
                    raw_name = better_name

        # Filter valid video files (ignoring samples, extras, featurettes)
        valid_files = [f for f in item.get("files", []) if is_valid_item_file(f)]
        if not valid_files:
            continue

        # Check for multi-movie pack (e.g. Marvel Cinematic Universe Collection)
        file_tmdb_ids = set()
        for f in valid_files:
            fm = get_file_metadata(item["type"], item["id"], f.get("id"))
            if fm and fm.get("tmdb_id"):
                file_tmdb_ids.add(fm["tmdb_id"])

        if len(file_tmdb_ids) > 1:
            # Multi-movie pack: process each file individually into its corresponding canonical folder
            for f in valid_files:
                fm = get_file_metadata(item["type"], item["id"], f.get("id"))
                if not fm:
                    continue
                sec = fm["section"]
                target_sec = os.path.join(MEDIA_BASE, sec)
                tid = fm["tmdb_id"]
                c_title = fm["title"]
                c_yr = fm["year"]

                # Check if this movie exists on disk (or was manually moved)
                found_pack_disk = None
                if tid and tid in disk_dirs_by_tmdb:
                    found_pack_disk = disk_dirs_by_tmdb[tid][0]
                
                if found_pack_disk:
                    p_sec, p_sec_path, p_dir_name = found_pack_disk
                    if p_sec != sec:
                        logging.info(f"Manual relocation detected for multi-pack movie '{c_title}': {sec} -> {p_sec}")
                        update_file_master_override(item["type"], item["id"], f.get("id"), p_sec)
                    target_sec = p_sec_path
                    f_name = p_dir_name
                else:
                    c_no_yr = re.sub(rf"\s*\({c_yr}\)\s*$", "", c_title) if c_yr else c_title
                    f_name = safe_truncate(f"{c_no_yr} ({c_yr}) [tmdbid-{tid}]" if c_yr else f"{c_no_yr} [tmdbid-{tid}]")
                    disk_dirs_by_tmdb.setdefault(tid, []).append((sec, target_sec, f_name))

                i_dir = os.path.join(target_sec, f_name)
                os.makedirs(i_dir, exist_ok=True)

                # NFO & Poster if missing
                t_meta = get_tmdb_details("movie", tid)
                if t_meta:
                    p_file = os.path.join(i_dir, "poster.jpg")
                    if not os.path.exists(p_file) and t_meta.get("poster_path"):
                        download_poster(p_file, t_meta["poster_path"])
                    n_file = os.path.join(i_dir, "movie.nfo")
                    if not os.path.exists(n_file):
                        write_nfo(n_file, "movie", c_title, c_yr, t_meta.get("overview"), tid)

                c_no_yr = re.sub(rf"\s*\({c_yr}\)\s*$", "", c_title) if c_yr else c_title
                base_strm_name = f"{c_no_yr}{f' ({c_yr})' if c_yr else ''}"
                stream_url = get_strm_url(item["type"], item["id"], f.get("id"))
                planned_strm_targets.setdefault((i_dir, base_strm_name), []).append({
                    "stream_url": stream_url,
                    "raw_name": raw_name,
                    "short_name": f.get("short_name", f.get("name", "")),
                    "file_id": f.get("id")
                })
            continue

        # 1. Master Knowledge Lookup (from Plex-synchronized torrent_master table or file_master)
        master_meta = get_master_metadata(item["type"], item["id"])
        if not master_meta and valid_files:
            master_meta = get_file_metadata(item["type"], item["id"], valid_files[0].get("id"))

        media_type, is_anime, tmdb_id = None, None, None
        sec = None
        target_section = None
        folder_name = None
        canon_title = None
        canon_year = None

        if master_meta:
            media_type = master_meta["media_type"]
            is_anime = master_meta["is_anime"]
            tmdb_id = master_meta["tmdb_id"]
            sec = master_meta["section"]
            canon_title = master_meta["title"]
            canon_year = master_meta["year"]
        else:
            # Cache lookup fallback
            cached = get_cached_metadata(raw_name)
            if cached and cached[0] != "uncategorized" and cached[2] is not None:
                media_type, is_anime, tmdb_id = cached
                
            file_names = [f.get("name", "") for f in item.get("files", [])]
            if media_type is None or tmdb_id is None:
                media_type, is_anime, tmdb_id = identify_via_api(raw_name, file_names)
                save_to_cache(raw_name, media_type, is_anime, tmdb_id)
                logging.info(f"Analyzed '{raw_name}' -> Type: {media_type.upper()}, Anime: {bool(is_anime)}, TMDB: {tmdb_id}")

            # High School DxD custom rules
            if "High School DxD" in raw_name:
                media_type = "tv"
                is_anime = True
                tmdb_id = 45950

            # Default section assignment
            if media_type == "uncategorized":
                sec = "Uncategorized"
            elif is_anime:
                sec = "Anime TV" if media_type == "tv" else "Anime Movies"
            else:
                sec = "TV" if media_type == "tv" else "Movies"

        # Bleach: Thousand-Year Blood War (including S17 releases) isolation rule
        all_item_files = [f.get("name", "") for f in item.get("files", [])]
        if is_bleach_tybw(raw_name, all_item_files):
            media_type = "tv"
            is_anime = True
            tmdb_id = 335660
            sec = "Anime TV"
            canon_title = "Bleach Thousand-Year Blood War"
            canon_year = 2022
            if master_meta and (master_meta.get("tmdb_id") != 335660 or master_meta.get("title") != "Bleach Thousand-Year Blood War"):
                update_master_override(item["type"], item["id"], "Anime TV", 335660, True, "tv", "Bleach Thousand-Year Blood War", 2022)
            save_to_cache(raw_name, "tv", True, 335660)

        # --- MANUAL OVERRIDE CHECK ON DISK ---
        # If user moved the folder into another section or manually edited TMDb ID, respect it
        found_disk = None

        # 1. Match by TMDb ID across compatible sections
        if tmdb_id and tmdb_id in disk_dirs_by_tmdb:
            matching = [
                entry for entry in disk_dirs_by_tmdb[tmdb_id]
                if (media_type == "tv" and entry[0] in ("TV", "Anime TV")) or
                   (media_type == "movie" and entry[0] in ("Movies", "Anime Movies"))
            ]
            if matching:
                found_disk = matching[0]
            elif not media_type:
                found_disk = disk_dirs_by_tmdb[tmdb_id][0]

        # 2. Match by Clean Title Key
        if not found_disk:
            c_raw, _ = clean_title(raw_name)
            key_title = re.sub(r"[^\w\s]", "", c_raw).strip().lower()
            if key_title in disk_dirs_by_title:
                found_disk = disk_dirs_by_title[key_title][0]

        # 3. Match by Exact Folder Name
        if not found_disk:
            exact_key = raw_name.lower()
            if exact_key in disk_dirs_by_exact:
                found_disk = disk_dirs_by_exact[exact_key]

        if found_disk:
            disk_sec, disk_sec_path, disk_dir_name = found_disk
            
            # Check if user manually changed TMDb ID in the folder name
            m_new_tmdb = re.search(r"\[tmdbid-(\d+)\]|\{tmdb-(\d+)\}", disk_dir_name, re.IGNORECASE)
            if m_new_tmdb:
                disk_tmdb_id = int(m_new_tmdb.group(1) or m_new_tmdb.group(2))
                if disk_tmdb_id != tmdb_id:
                    logging.info(f"Manual TMDb ID edit detected on disk for '{raw_name}': {tmdb_id} -> {disk_tmdb_id}")
                    tmdb_id = disk_tmdb_id

            # Infer media_type and is_anime if moved between standard sections
            orig_sec = sec
            if disk_sec == "Anime Movies":
                media_type, is_anime = "movie", True
            elif disk_sec == "Anime TV":
                media_type, is_anime = "tv", True
            elif disk_sec == "Movies":
                media_type, is_anime = "movie", False
            elif disk_sec == "TV":
                media_type, is_anime = "tv", False

            target_section = disk_sec_path
            folder_name = disk_dir_name

            # If section or TMDb ID changed, persist the move permanently in the database
            if orig_sec != disk_sec:
                logging.info(f"Manual relocation detected on disk for '{raw_name}': {orig_sec or 'default'} -> {disk_sec} ('{disk_dir_name}')")
                update_master_override(item["type"], item["id"], disk_sec, tmdb_id, is_anime, media_type)
                save_to_cache(raw_name, media_type, is_anime, tmdb_id)
                sec = disk_sec

        # If not found on disk, assign default target section
        if not target_section:
            target_section = os.path.join(MEDIA_BASE, sec if sec else "Uncategorized")

        # Get canonical details from TMDb if overview/poster needed or if title not yet set
        tmdb_meta = get_tmdb_details(media_type, tmdb_id) if tmdb_id else None
        poster_slug = None
        overview = None
        if tmdb_meta:
            poster_slug = tmdb_meta.get("poster_path")
            overview = tmdb_meta.get("overview")
            if not canon_title and tmdb_meta.get("title"):
                canon_title = clean_filename(tmdb_meta["title"])
                canon_year = tmdb_meta.get("year")

        if not canon_title:
            c_name, c_yr = clean_title(raw_name)
            canon_title = clean_filename(c_name)
            canon_year = c_yr

        # Construct target folder name if not already resolved from disk
        if not folder_name:
            clean_title_no_year = re.sub(rf"\s*\({canon_year}\)\s*$", "", canon_title) if canon_year else canon_title
            suffix = f" ({canon_year})" if canon_year else ""
            if tmdb_id:
                suffix += f" [tmdbid-{tmdb_id}]"
            folder_name = safe_truncate(f"{clean_title_no_year}{suffix}")
            if tmdb_id:
                disk_dirs_by_tmdb.setdefault(tmdb_id, []).append((os.path.basename(target_section), target_section, folder_name))

        item_dir = os.path.join(target_section, folder_name)
        os.makedirs(item_dir, exist_ok=True)

        # Poster & NFO
        poster_file = os.path.join(item_dir, "poster.jpg")
        if not os.path.exists(poster_file) and poster_slug:
            download_poster(poster_file, poster_slug)

        if media_type == "tv":
            nfo_file = os.path.join(item_dir, "tvshow.nfo")
            if not os.path.exists(nfo_file):
                write_nfo(nfo_file, "tvshow", canon_title, canon_year, overview, tmdb_id)
        else:
            nfo_file = os.path.join(item_dir, "movie.nfo")
            if not os.path.exists(nfo_file):
                write_nfo(nfo_file, "movie", canon_title, canon_year, overview, tmdb_id)

        # Multi-Part Movie Preservation or Main Movie Detection
        movie_files_to_write = []
        if media_type != "tv":
            if len(valid_files) == 1:
                movie_files_to_write.append((valid_files[0], None))
            else:
                # Check for multi-part naming (e.g. pt1, cd1, part2)
                part_matches = []
                for f in valid_files:
                    fn = f.get("short_name", f.get("name", ""))
                    m = PART_RE.search(fn)
                    if m:
                        part_matches.append((int(m.group(1)), f))
                
                if len(part_matches) == len(valid_files) and len(part_matches) > 1:
                    part_matches.sort(key=lambda x: x[0])
                    for p_num, f in part_matches:
                        movie_files_to_write.append((f, f"pt{p_num}"))
                else:
                    max_f = max(valid_files, key=lambda x: x.get("size", 0))
                    max_size = max_f.get("size", 0)
                    dominant = all(f == max_f or f.get("size", 0) < 0.25 * max_size for f in valid_files)
                    if dominant:
                        movie_files_to_write.append((max_f, None))
                    else:
                        for idx, f in enumerate(valid_files, 1):
                            movie_files_to_write.append((f, f"pt{idx}"))

        # Fallback season from torrent name
        fallback_season = 1
        s_match = re.search(r"S(\d{1,2})", raw_name, re.IGNORECASE) or re.search(r"Season\s*(\d{1,2})", raw_name, re.IGNORECASE)
        if s_match:
            fallback_season = int(s_match.group(1))

        # Collect planned .strm files
        if media_type == "tv":
            for f in valid_files:
                filename = f.get("name", "")
                short_name = f.get("short_name", filename)
                file_id = f.get("id")
                stream_url = get_strm_url(item["type"], item["id"], file_id)

                # Special DxD and Bleach TYBW season & episode mapping
                if "High School DxD" in raw_name:
                    s_num, ep_num = map_high_school_dxd(short_name, fallback_season)
                elif tmdb_id == 335660 or is_bleach_tybw(raw_name, [filename, short_name]):
                    s_num, ep_num = map_bleach_tybw(short_name, fallback_season)
                else:
                    s_num, ep_num = parse_season_episode(short_name, fallback_season)

                pt_m = PART_RE.search(short_name)
                part_suffix = f" - pt{pt_m.group(1)}" if pt_m else ""

                season_dir = os.path.join(item_dir, f"Season {s_num:02d}")
                os.makedirs(season_dir, exist_ok=True)
                clean_title_no_year = re.sub(rf"\s*\({canon_year}\)\s*$", "", canon_title) if canon_year else canon_title
                base_strm_name = f"{clean_title_no_year} - S{s_num:02d}E{ep_num:02d}{part_suffix}"
                if tmdb_id == 335660:
                    write_tybw_episode_metadata(season_dir, base_strm_name, ep_num)
                planned_strm_targets.setdefault((season_dir, base_strm_name), []).append({
                    "stream_url": stream_url,
                    "raw_name": raw_name,
                    "short_name": short_name,
                    "file_id": file_id
                })
        else:
            for f, part_tag in movie_files_to_write:
                file_id = f.get("id")
                stream_url = get_strm_url(item["type"], item["id"], file_id)
                clean_title_no_year = re.sub(rf"\s*\({canon_year}\)\s*$", "", canon_title) if canon_year else canon_title
                suffix = f" ({canon_year})" if canon_year else ""
                part_suffix = f" - {part_tag}" if part_tag else ""
                base_strm_name = f"{clean_title_no_year}{suffix}{part_suffix}"
                planned_strm_targets.setdefault((item_dir, base_strm_name), []).append({
                    "stream_url": stream_url,
                    "raw_name": raw_name,
                    "short_name": f.get("short_name", f.get("name", "")),
                    "file_id": file_id
                })

    # --- PHASE 2: WRITE STRM FILES WITH MULTI-VERSION DISAMBIGUATION ---
    total_planned = sum(len(v) for v in planned_strm_targets.values())
    logging.info(f"Planned {total_planned} files across {len(planned_strm_targets)} media targets. Applying multi-version disambiguation...")

    for (target_dir, base_strm_name), entries in planned_strm_targets.items():
        if len(entries) == 1:
            strm_name = safe_truncate(f"{base_strm_name}.strm")
            entry_list = [(entries[0], strm_name)]
        else:
            labels = disambiguate_versions(entries)
            entry_list = []
            folder_basename = os.path.basename(target_dir)
            is_season_dir = bool(re.match(r"^Season\s+\d+$", folder_basename, re.IGNORECASE))
            if is_season_dir:
                version_prefix = base_strm_name
            else:
                part_m = re.search(r"(\s+-\s+pt\d+)$", base_strm_name, re.IGNORECASE)
                part_suffix = part_m.group(1) if part_m else ""
                version_prefix = f"{folder_basename}{part_suffix}"
            for entry, lbl in zip(entries, labels):
                strm_name = safe_truncate(f"{version_prefix} - [{lbl}].strm")
                entry_list.append((entry, strm_name))

        for entry, strm_name in entry_list:
            strm_path = os.path.join(target_dir, strm_name)
            generated_strm_paths.add(os.path.abspath(strm_path))
            stream_url = entry["stream_url"]

            needs_write = True
            if os.path.isfile(strm_path):
                try:
                    with open(strm_path, "r", encoding="utf-8") as existing_f:
                        if existing_f.read().strip() == stream_url.strip():
                            needs_write = False
                except Exception:
                    pass

            if needs_write:
                try:
                    with open(strm_path, "w", encoding="utf-8") as out_f:
                        out_f.write(stream_url + "\n")
                    files_added_or_updated += 1
                except Exception as e:
                    logging.error(f"Failed to write STRM {strm_path}: {e}")

    # --- CLEANUP PHASE ---
    logging.info("Scanning for dead or deleted STRM files across all sections...")
    dead_strm_files = []
    for sec_name in all_sections:
        section_path = os.path.join(MEDIA_BASE, sec_name)
        if not os.path.exists(section_path):
            continue
        for root, _, files in os.walk(section_path):
            for f in files:
                if f.endswith(".strm"):
                    full_p = os.path.abspath(os.path.join(root, f))
                    if full_p not in generated_strm_paths:
                        dead_strm_files.append(full_p)

    logging.info(f"Identified {len(dead_strm_files)} STRM files not present in TorBox.")
    files_deleted = 0
    if len(dead_strm_files) > MAX_DELETIONS_PER_CYCLE:
        logging.warning(f"SAFETY SHIELD TRIGGERED: {len(dead_strm_files)} dead files exceeds safety threshold ({MAX_DELETIONS_PER_CYCLE}). Deletion aborted.")
    else:
        for dead_file in dead_strm_files:
            try:
                os.remove(dead_file)
                files_deleted += 1
                logging.info(f"Deleted dead STRM: {dead_file}")
            except Exception as e:
                logging.error(f"Failed to delete {dead_file}: {e}")

        # Clean empty directories across all sections
        for sec_name in all_sections:
            section_path = os.path.join(MEDIA_BASE, sec_name)
            if not os.path.exists(section_path):
                continue
            for root, dirs, files in os.walk(section_path, topdown=False):
                # Remove .DS_Store if it's the only remaining file
                ds_store = os.path.join(root, ".DS_Store")
                if os.path.exists(ds_store) and len(os.listdir(root)) == 1:
                    try: os.remove(ds_store)
                    except: pass

                # Remove directory if empty or only contains nfo/poster without any strm
                strm_in_dir = []
                for _, _, subfiles in os.walk(root):
                    strm_in_dir.extend([sf for sf in subfiles if sf.endswith(".strm")])
                    
                if not strm_in_dir and root != section_path:
                    try:
                        for remnant in os.listdir(root):
                            p = os.path.join(root, remnant)
                            if os.path.isfile(p): os.remove(p)
                        os.rmdir(root)
                        logging.info(f"Removed empty directory: {root}")
                    except Exception:
                        pass

    logging.info(f"Sync complete. Added/Updated: {files_added_or_updated}, Deleted: {files_deleted}")

    if files_added_or_updated > 0 or files_deleted > 0:
        logging.info("Changes detected. Notifying Jellyfin...")
        notify_jellyfin()

if __name__ == "__main__":
    sync_jellyfin()
