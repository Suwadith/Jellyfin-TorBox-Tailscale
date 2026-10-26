#!/usr/bin/env python3
"""
Sync watch history from a local Plex Media Server to Jellyfin.
Strictly syncs watch status and timestamps without modifying any media files or other settings.
"""

import os
import sys
import sqlite3
import requests
import json
import re
from datetime import datetime, timezone
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

PLEX_DB_PATH = os.path.expanduser(
    "~/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
)
JELLYFIN_BASE = "http://127.0.0.1:8096"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
API_KEY_FILE = os.path.join(PROJECT_DIR, "jellyfin_api_key.txt")

def clean_str(s):
    if not s:
        return ""
    return re.sub(r'[^a-z0-9]', '', s.lower())

def get_jellyfin_token():
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.read().strip()
    env_token = os.environ.get("JELLYFIN_API_KEY")
    if env_token:
        return env_token
    raise RuntimeError(f"Could not find Jellyfin API key in {API_KEY_FILE} or environment.")

def get_plex_tags(cur, item_id):
    cur.execute("""
        SELECT t.tag FROM tags t
        JOIN taggings tg ON tg.tag_id = t.id
        WHERE tg.metadata_item_id = ? AND t.tag_type = 314;
    """, (item_id,))
    tags = [r[0] for r in cur.fetchall()]
    parsed = {}
    for tag in tags:
        if tag.startswith("tmdb://"):
            parsed["tmdb"] = tag[7:]
        elif tag.startswith("imdb://"):
            parsed["imdb"] = tag[7:].lower()
        elif tag.startswith("tvdb://"):
            parsed["tvdb"] = tag[7:]
    return parsed

def sync_watch_history(target_user_name="root", dry_run=False):
    token = get_jellyfin_token()
    headers = {
        "Authorization": f'MediaBrowser Token="{token}"',
        "Content-Type": "application/json"
    }

    session = requests.Session()
    session.headers.update(headers)

    # 1. Verify Jellyfin connectivity and resolve target user
    logging.info(f"Connecting to Jellyfin at {JELLYFIN_BASE}...")
    r_users = session.get(f"{JELLYFIN_BASE}/Users")
    if r_users.status_code != 200:
        raise RuntimeError(f"Failed to fetch Jellyfin users: {r_users.status_code} {r_users.text}")
    
    users = r_users.json()
    target_user = None
    for u in users:
        if u["Name"].lower() == target_user_name.lower():
            target_user = u
            break
            
    if not target_user:
        available = [u["Name"] for u in users]
        raise RuntimeError(f"User '{target_user_name}' not found in Jellyfin. Available users: {available}")
        
    user_id = target_user["Id"]
    logging.info(f"Resolved Jellyfin user: '{target_user['Name']}' (ID: {user_id})")

    # 2. Fetch Jellyfin Library metadata for this user
    logging.info("Fetching Jellyfin Series library...")
    r_series = session.get(f"{JELLYFIN_BASE}/Items?Recursive=true&IncludeItemTypes=Series&Fields=ProviderIds,Path")
    series_items = r_series.json().get("Items", [])
    logging.info(f"Found {len(series_items)} series in Jellyfin.")

    jf_series_by_tmdb = {}
    jf_series_by_tvdb = {}
    jf_series_by_imdb = {}
    jf_series_by_clean_title = {}
    tybw_series_id = None

    for s in series_items:
        sid = s["Id"]
        pids = s.get("ProviderIds", {})
        if "Tmdb" in pids:
            jf_series_by_tmdb[str(pids["Tmdb"])] = sid
        if "Tvdb" in pids:
            jf_series_by_tvdb[str(pids["Tvdb"])] = sid
        if "Imdb" in pids:
            jf_series_by_imdb[str(pids["Imdb"]).lower()] = sid
        ct = clean_str(s.get("Name"))
        if ct:
            jf_series_by_clean_title[ct] = sid

        # Identify Bleach TYBW isolated series
        if "Thousand-Year" in s.get("Name", "") or pids.get("Tmdb") == "335660":
            tybw_series_id = sid

    logging.info(f"Fetching Jellyfin Movies library for user '{target_user['Name']}'...")
    r_movies = session.get(
        f"{JELLYFIN_BASE}/Users/{user_id}/Items?Recursive=true&IncludeItemTypes=Movie&Fields=ProviderIds,Path,UserData"
    )
    movie_items = r_movies.json().get("Items", [])
    logging.info(f"Found {len(movie_items)} movies in Jellyfin.")

    jf_movies_by_tmdb = {}
    jf_movies_by_imdb = {}
    jf_movies_by_path = {}
    jf_movies_by_title_year = {}

    for m in movie_items:
        pids = m.get("ProviderIds", {})
        if "Tmdb" in pids:
            jf_movies_by_tmdb[str(pids["Tmdb"])] = m
        if "Imdb" in pids:
            jf_movies_by_imdb[str(pids["Imdb"]).lower()] = m
        path = m.get("Path", "")
        if path:
            jf_movies_by_path[os.path.basename(path).lower()] = m
        t_key = (clean_str(m.get("Name")), m.get("ProductionYear"))
        if t_key[0]:
            jf_movies_by_title_year[t_key] = m

    logging.info(f"Fetching Jellyfin Episodes library for user '{target_user['Name']}'...")
    r_episodes = session.get(
        f"{JELLYFIN_BASE}/Users/{user_id}/Items?Recursive=true&IncludeItemTypes=Episode&Fields=ProviderIds,Path,SeriesId,ParentIndexNumber,IndexNumber,UserData"
    )
    episode_items = r_episodes.json().get("Items", [])
    logging.info(f"Found {len(episode_items)} episodes in Jellyfin.")

    jf_episodes_by_series_s_e = {}
    jf_episodes_by_path = {}

    for ep in episode_items:
        sid = ep.get("SeriesId")
        s_num = ep.get("ParentIndexNumber")
        e_num = ep.get("IndexNumber")
        if sid and s_num is not None and e_num is not None:
            jf_episodes_by_series_s_e[(sid, int(s_num), int(e_num))] = ep
        path = ep.get("Path", "")
        if path:
            jf_episodes_by_path[os.path.basename(path).lower()] = ep

    # 3. Read watch history from Plex DB
    if not os.path.exists(PLEX_DB_PATH):
        raise FileNotFoundError(f"Plex database not found at {PLEX_DB_PATH}")

    logging.info(f"Opening Plex database (read-only) at {PLEX_DB_PATH}...")
    con = sqlite3.connect(f"file:{PLEX_DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()

    # Determine Plex account id
    cur.execute("SELECT id, name FROM accounts WHERE name != '';")
    accounts = cur.fetchall()
    logging.info(f"Plex accounts found: {accounts}")

    plex_account_id = None
    for acc_id, acc_name in accounts:
        if target_user_name.lower() in acc_name.lower():
            plex_account_id = acc_id
            break

    if not plex_account_id:
        if accounts:
            plex_account_id = accounts[0][0]
            logging.warning(f"Could not map '{target_user_name}' to a specific Plex account. Defaulting to first Plex account '{accounts[0][1]}' (id={plex_account_id}).")
        else:
            plex_account_id = 1
            logging.warning(f"No named Plex accounts found. Defaulting to account id 1.")
    else:
        logging.info(f"Mapped Jellyfin user '{target_user_name}' to Plex account '{acc_name}' (id={plex_account_id}).")

    # Fetch watched movies from Plex
    cur.execute("""
        SELECT 
            m.id,
            m.title,
            m.year,
            s.view_count,
            s.last_viewed_at,
            (SELECT mp.file FROM media_items mi JOIN media_parts mp ON mp.media_item_id = mi.id WHERE mi.metadata_item_id = m.id LIMIT 1) as file_path
        FROM metadata_item_settings s
        JOIN metadata_items m ON m.guid = s.guid
        WHERE s.account_id = ? AND s.view_count > 0 AND m.metadata_type = 1;
    """, (plex_account_id,))
    plex_watched_movies = cur.fetchall()

    # Fetch watched episodes from Plex
    cur.execute("""
        SELECT 
            e.id,
            e.title,
            s."index" as season_num,
            e."index" as ep_num,
            show.id as show_id,
            show.title as show_title,
            show.year as show_year,
            settings.view_count,
            settings.last_viewed_at,
            (SELECT mp.file FROM media_items mi JOIN media_parts mp ON mp.media_item_id = mi.id WHERE mi.metadata_item_id = e.id LIMIT 1) as file_path
        FROM metadata_item_settings settings
        JOIN metadata_items e ON e.guid = settings.guid
        JOIN metadata_items s ON s.id = e.parent_id
        JOIN metadata_items show ON show.id = s.parent_id
        WHERE settings.account_id = ? AND settings.view_count > 0 AND e.metadata_type = 4;
    """, (plex_account_id,))
    plex_watched_episodes = cur.fetchall()

    logging.info(f"Found {len(plex_watched_movies)} watched movies and {len(plex_watched_episodes)} watched episodes in Plex.")

    # 4. Process Movies
    movies_matched = 0
    movies_already_watched = 0
    movies_marked_new = 0
    movies_unmatched = []

    for m in plex_watched_movies:
        mid, title, year, v_count, last_viewed_at, fpath = m
        tags = get_plex_tags(cur, mid)
        jf_m = None

        if "tmdb" in tags and tags["tmdb"] in jf_movies_by_tmdb:
            jf_m = jf_movies_by_tmdb[tags["tmdb"]]
        elif "imdb" in tags and tags["imdb"] in jf_movies_by_imdb:
            jf_m = jf_movies_by_imdb[tags["imdb"]]
        elif fpath and os.path.basename(fpath).lower() in jf_movies_by_path:
            jf_m = jf_movies_by_path[os.path.basename(fpath).lower()]
        elif (clean_str(title), year) in jf_movies_by_title_year:
            jf_m = jf_movies_by_title_year[(clean_str(title), year)]

        if not jf_m:
            movies_unmatched.append((title, year, tags, fpath))
            continue

        movies_matched += 1
        user_data = jf_m.get("UserData", {})
        if user_data.get("Played"):
            movies_already_watched += 1
            continue

        # Format timestamp
        iso_date = ""
        if last_viewed_at:
            dt = datetime.fromtimestamp(last_viewed_at, tz=timezone.utc)
            iso_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if not dry_run:
            url = f"{JELLYFIN_BASE}/UserPlayedItems/{jf_m['Id']}?userId={user_id}"
            if iso_date:
                url += f"&datePlayed={iso_date}"
            resp = session.post(url)
            if resp.status_code in (200, 204):
                movies_marked_new += 1
            else:
                logging.error(f"Failed to mark movie '{title}' as played: {resp.status_code} {resp.text}")
        else:
            movies_marked_new += 1

    # 5. Process Episodes
    episodes_matched = 0
    episodes_already_watched = 0
    episodes_marked_new = 0
    episodes_unmatched = []

    for ep in plex_watched_episodes:
        eid, etitle, s_num, e_num, show_id, show_title, show_year, v_count, last_viewed_at, fpath = ep
        show_tags = get_plex_tags(cur, show_id)

        target_s_num = int(s_num)
        target_e_num = int(e_num)
        jf_sid = None

        # Bleach TYBW isolated series routing rule
        fpath_lower = (fpath or "").lower()
        if (
            ("Bleach" in show_title and int(s_num) == 17)
            or "thousand" in show_title.lower()
            or "thousand" in fpath_lower
            or "tybw" in fpath_lower
        ):
            jf_sid = tybw_series_id
            target_s_num = 1
            target_e_num = int(e_num)
        else:
            if "tmdb" in show_tags and show_tags["tmdb"] in jf_series_by_tmdb:
                jf_sid = jf_series_by_tmdb[show_tags["tmdb"]]
            elif "tvdb" in show_tags and show_tags["tvdb"] in jf_series_by_tvdb:
                jf_sid = jf_series_by_tvdb[show_tags["tvdb"]]
            elif "imdb" in show_tags and show_tags["imdb"] in jf_series_by_imdb:
                jf_sid = jf_series_by_imdb[show_tags["imdb"]]
            elif clean_str(show_title) in jf_series_by_clean_title:
                jf_sid = jf_series_by_clean_title[clean_str(show_title)]

        jf_ep = None
        if jf_sid and (jf_sid, target_s_num, target_e_num) in jf_episodes_by_series_s_e:
            jf_ep = jf_episodes_by_series_s_e[(jf_sid, target_s_num, target_e_num)]
        elif fpath and os.path.basename(fpath).lower() in jf_episodes_by_path:
            jf_ep = jf_episodes_by_path[os.path.basename(fpath).lower()]

        if not jf_ep:
            episodes_unmatched.append((show_title, s_num, e_num, etitle, show_tags))
            continue

        episodes_matched += 1
        user_data = jf_ep.get("UserData", {})
        if user_data.get("Played"):
            episodes_already_watched += 1
            continue

        # Format timestamp
        iso_date = ""
        if last_viewed_at:
            dt = datetime.fromtimestamp(last_viewed_at, tz=timezone.utc)
            iso_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if not dry_run:
            url = f"{JELLYFIN_BASE}/UserPlayedItems/{jf_ep['Id']}?userId={user_id}"
            if iso_date:
                url += f"&datePlayed={iso_date}"
            resp = session.post(url)
            if resp.status_code in (200, 204):
                episodes_marked_new += 1
            else:
                logging.error(f"Failed to mark episode '{show_title} S{target_s_num}E{target_e_num}' as played: {resp.status_code} {resp.text}")
        else:
            episodes_marked_new += 1

    con.close()

    # 6. Print Comprehensive Summary
    action = "Would mark" if dry_run else "Successfully marked"
    print("\n" + "=" * 60)
    print(f" PLEX -> JELLYFIN WATCH HISTORY SYNC SUMMARY ({'DRY RUN' if dry_run else 'EXECUTED'})")
    print(f" Target Jellyfin User: {target_user['Name']} ({user_id})")
    print("=" * 60)
    print(f"MOVIES:")
    print(f"  Plex Watched Movies:          {len(plex_watched_movies)}")
    print(f"  Matched in Jellyfin:          {movies_matched} / {len(plex_watched_movies)}")
    print(f"  Already Watched in Jellyfin:  {movies_already_watched}")
    print(f"  {action} New:       {movies_marked_new}")
    if movies_unmatched:
        print(f"  Not in Jellyfin ({len(movies_unmatched)}):")
        for um in movies_unmatched:
            print(f"    - {um[0]} ({um[1]})")

    print(f"\nEPISODES:")
    print(f"  Plex Watched Episodes:        {len(plex_watched_episodes)}")
    print(f"  Matched in Jellyfin:          {episodes_matched} / {len(plex_watched_episodes)}")
    print(f"  Already Watched in Jellyfin:  {episodes_already_watched}")
    print(f"  {action} New:       {episodes_marked_new}")
    if episodes_unmatched:
        print(f"  Not in Jellyfin ({len(episodes_unmatched)}):")
        for ue in episodes_unmatched:
            print(f"    - {ue[0]} S{ue[1]}E{ue[2]} - {ue[3]}")

    total_watched = (movies_marked_new + movies_already_watched) + (episodes_marked_new + episodes_already_watched)
    total_new = movies_marked_new + episodes_marked_new
    print("=" * 60)
    print(f"TOTAL: {total_watched} items confirmed watched in Jellyfin ({total_new} newly synced).")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync watch history from Plex to Jellyfin.")
    parser.add_argument("--user", default="root", help="Jellyfin username to sync into (default: root)")
    parser.add_argument("--dry-run", action="store_true", help="Perform matching without updating Jellyfin")
    args = parser.parse_args()

    sync_watch_history(target_user_name=args.user, dry_run=args.dry_run)
