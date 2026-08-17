# --- START OF PART 1 ---
import os
import re
import sys
import asyncio
import logging
import itertools
import hashlib
import gc
from typing import Dict, Optional, List, Any
from urllib.parse import quote_plus
from datetime import datetime
from pathlib import Path

import aiohttp
from celery import Celery
from dotenv import load_dotenv
from redis import Redis
from thefuzz import fuzz, process
from pymongo import MongoClient, DESCENDING
from sqlalchemy.exc import IntegrityError
from bson.objectid import ObjectId

from .tmdb_enrichment import apply_enrichment, fetch_tmdb_details

# --- CONFIGURATION ---
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(dotenv_path=env_path)
sys.path.append(str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

celery = Celery(__name__)
try:
    import celeryconfig
    celery.config_from_object("celeryconfig")
except ImportError:
    celery.conf.update(
        broker_url=os.environ.get('REDIS_URL'),
        result_backend=os.environ.get('REDIS_URL')
    )

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

# ==============================================================================
#                               TEXT HELPERS
# ==============================================================================
_ACRONYM_DOTS = re.compile(r"\b([A-Z]\.){2,}\b")       
_NON_BASIC = re.compile(r"[^\w\s,&'\-.:]")
_TOK = re.compile(r"[a-z0-9]+")
ARTICLES = {"the", "a", "an"}

def normalize(s: Optional[str]) -> str:
    if not s: return ""
    def _join(m): return m.group(0).replace(".", "")
    s = _ACRONYM_DOTS.sub(_join, s)
    s = "".join(c for c in s if c.isprintable())
    s = _NON_BASIC.sub("", s)
    return re.sub(r"\s+", " ", s).strip().lower()

def tokens(s: str) -> List[str]:
    return _TOK.findall(s.lower())

def strip_leading_article(s: str) -> str:
    toks = tokens(s)
    if toks and toks[0] in ARTICLES:
        return " ".join(toks[1:])
    return " ".join(toks)

def strong_title_score(query: str, candidate: str) -> int:
    qn = normalize(query)
    cn = normalize(candidate)
    if qn == cn: return 100
    sq = strip_leading_article(qn)
    sc = strip_leading_article(cn)
    if sq == sc: return 99
    base = fuzz.token_sort_ratio(qn, cn)
    len_q, len_c = len(qn), len(cn)
    if len_q > 0 and len_c > len_q:
        ratio = len_c / len_q
        if ratio > 2.0: base -= 15
        elif ratio > 1.5: base -= 5
    return base

def parse_season_info(line: str) -> Optional[int]:
    nums = re.findall(r"\d+", line)
    return max(int(n) for n in nums) if nums else None

# ==============================================================================
#                        TV / ANIME LOGIC (TELEGRAM)
# ==============================================================================

async def fetch_new_telegram_posts(channel_env_var: str, redis_key_suffix: str) -> list:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel_id = os.environ.get(channel_env_var)
    if not channel_id: return []

    redis_client = Redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)
    last_offset_key = f"last_telegram_update_id:{redis_key_suffix}"
    last_offset = int(redis_client.get(last_offset_key) or 0)

    from telegram.ext import Application
    try:
        app = Application.builder().token(token).build()
        updates = await app.bot.get_updates(offset=last_offset + 1, allowed_updates=["channel_post", "edited_channel_post"], timeout=60)
        if hasattr(app, 'shutdown'): await app.shutdown()
        
        posts = [u.channel_post or u.edited_channel_post for u in updates if (u.channel_post or u.edited_channel_post) and str((u.channel_post or u.edited_channel_post).sender_chat.id) == channel_id]
        if updates: redis_client.set(last_offset_key, updates[-1].update_id)
        
        if posts: logger.info(f"[{channel_env_var}] Found {len(posts)} new posts.")
        return posts
    except Exception as e:
        logger.exception(f"Error fetching Telegram posts for {channel_env_var}: {e}")
        return []

def parse_telegram_post(post) -> Optional[Dict]:
    try:
        text = post.caption or post.text or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2: return None
        
        norm_title = normalize(re.sub(r"[\[\]\(\)]", " ", lines[0]))
        year_match = re.search(r"(\d{4})$", norm_title)
        search_year = int(year_match.group(1)) if year_match else None
        show_name = re.sub(r"\s*\d{4}$", "", norm_title).strip() if year_match else norm_title
        
        download_link_from_post = None
        
        # 1. Look for "Click Here" links
        if post.caption_entities:
            for ent in post.caption_entities:
                if ent.type == "text_link":
                    et = text[ent.offset: ent.offset + ent.length]
                    if "click here" in et.lower():
                        download_link_from_post = ent.url
                        break
        
        # 2. Look for ANY text link (if step 1 failed)
        if not download_link_from_post and post.caption_entities:
            for ent in reversed(post.caption_entities):
                if ent.type == "text_link":
                    et = text[ent.offset: ent.offset + ent.length]
                    if "#_" not in et:
                        download_link_from_post = ent.url
                        break
        
        # 3. Look for raw URLs in text lines
        if not download_link_from_post:
            for ln in reversed(lines):
                if "#_" in ln: continue
                m = re.search(r"(https?://\S+)", ln)
                if m:
                    download_link_from_post = m.group(1)
                    break

        return {
            "show_name_for_search": show_name,
            "search_year": search_year,
            "search_season": parse_season_info(lines[1]),
            "season_episode_from_post": lines[1],
            "download_link_from_post": download_link_from_post,
            "message_id": int(post.message_id),
        }
    except Exception: return None

async def fetch_tmdb_tv_data(show_name: str, search_year: int, search_season: int) -> Optional[Dict]:
    token = os.environ.get('TMDB_BEARER_TOKEN')
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            url = f"{TMDB_BASE_URL}/search/tv?query={quote_plus(show_name)}&language=en-US"
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200: return None
                data = await resp.json()
        except Exception: return None

        if not data.get("results"): return None
        
        detailed = []
        for r in data["results"]:
            try:
                async with session.get(f"{TMDB_BASE_URL}/tv/{r['id']}", timeout=5) as d:
                    if d.status == 200: detailed.append(await d.json())
            except: continue
            
        best = (None, -1)
        qn = normalize(show_name)

        for r in detailed:
            name = r.get("name") or ""
            oname = r.get("original_name") or ""
            
            s = max(strong_title_score(show_name, name), strong_title_score(show_name, oname))
            
            fa = r.get("first_air_date") or ""
            if search_year and fa[:4].isdigit() and int(fa[:4]) == search_year: 
                s += 10
            
            if search_season:
                sc = int(r.get("number_of_seasons") or 0)
                if sc >= search_season:
                    s += max(0, 6 - abs(sc - search_season))
            
            if s > best[1]: best = (r, s)
            
        found = best[0]
        
        if not found or best[1] < 50:
            names = [x.get("name") for x in detailed if x.get("name")]
            pick = process.extractOne(qn, names, scorer=fuzz.token_set_ratio)
            if pick:
                for r in detailed:
                    if r.get("name") == pick[0]:
                        found = r; break

        if not found: return None
        
        full_details = await fetch_tmdb_details(session, found["id"], "tv", token) or found

        return {
            "tmdb_id": found["id"],
            "show_name_from_tmdb": found["name"],
            "poster_path": f"{TMDB_IMAGE_BASE_URL}{found.get('poster_path')}" if found.get("poster_path") else None,
            "overview": found.get("overview"),
            "vote_average": found.get("vote_average"),
            "year": int(found["first_air_date"][:4]) if found.get("first_air_date") else None,
            "rating": found.get("vote_average"),
            "details": full_details,
        }

@celery.task(bind=True, retry_backoff=True, max_retries=3)
def update_tv_shows(self):
    redis_client = Redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)
    lock = redis_client.lock("update_tv_shows_lock", timeout=540)
    if not lock.acquire(blocking=False): return

    from tv_app.app import app
    with app.app_context():
        try:
            from tv_app.models import db, TVShow
            logger.info("update_tv_shows: Started processing...")
            
            for src in [{'type':'tv','v':'TELEGRAM_CHANNEL_ID','k':'tv_main'}, {'type':'anime','v':'TELEGRAM_ANIME_CHANNEL_ID','k':'anime_main'}]:
                posts = asyncio.run(fetch_new_telegram_posts(src['v'], src['k']))
                for post in posts:
                    processed_key = f"processed_messages:{src['k']}:{post.message_id}"
                    if redis_client.exists(processed_key):
                        continue
                    p = parse_telegram_post(post)
                    if not p: continue
                    
                    if not p["download_link_from_post"]:
                        logger.warning(f"Skipping {p['show_name_for_search']}: Link parsing failed.")
                        continue

                    tmdb = asyncio.run(fetch_tmdb_tv_data(p["show_name_for_search"], p["search_year"], p["search_season"]))
                    if not tmdb: continue

                    c_hash = f"{tmdb['tmdb_id']}-{p['season_episode_from_post']}"
                    
                    existing_entries = TVShow.query.filter_by(tmdb_id=tmdb["tmdb_id"], category=src['type']).all()
                    
                    target_entry = None
                    if existing_entries:
                        target_entry = existing_entries[0]
                        if len(existing_entries) > 1:
                            for extra in existing_entries[1:]:
                                db.session.delete(extra)
                    
                    if target_entry:
                        target_entry.message_id = p["message_id"]
                        target_entry.show_name = tmdb["show_name_from_tmdb"]
                        target_entry.episode_title = p["season_episode_from_post"]
                        target_entry.download_link = p["download_link_from_post"]
                        target_entry.poster_path = tmdb["poster_path"]
                        target_entry.overview = tmdb["overview"]
                        target_entry.vote_average = tmdb["vote_average"]
                        target_entry.year = tmdb["year"]
                        target_entry.rating = tmdb["rating"]
                        target_entry.content_hash = c_hash
                        target_entry.created_at = datetime.utcnow()
                        target_entry.updated_at = datetime.utcnow()
                        target_entry.availability_updated_at = datetime.utcnow()
                        apply_enrichment(target_entry, tmdb["details"])
                        logger.info(f"♻️ Updated: {tmdb['show_name_from_tmdb']}")
                    else:
                        new_show = TVShow(
                            tmdb_id=tmdb["tmdb_id"],
                            message_id=p["message_id"],
                            show_name=tmdb["show_name_from_tmdb"],
                            episode_title=p["season_episode_from_post"],
                            download_link=p["download_link_from_post"],
                            poster_path=tmdb["poster_path"],
                            overview=tmdb["overview"],
                            vote_average=tmdb["vote_average"],
                            year=tmdb["year"],
                            rating=tmdb["rating"],
                            category=src['type'],
                            content_hash=c_hash,
                            slug=re.sub(r'[^a-z0-9]+', '-', tmdb["show_name_from_tmdb"].lower()).strip('-')
                        )
                        db.session.add(new_show)
                        db.session.flush()
                        apply_enrichment(new_show, tmdb["details"])
                        logger.info(f"✅ Added: {tmdb['show_name_from_tmdb']}")
                    
                    redis_client.set(processed_key, 1, ex=86400)
            
            db.session.commit()
            logger.info("update_tv_shows: Batch Committed.")
        except Exception as e:
            logger.error(f"Error in update_tv_shows: {e}")
            db.session.rollback()
        finally:
            if lock.locked(): lock.release()

@celery.task(name="tv_app.tasks.reset_clicks")
def reset_clicks():
    from tv_app.app import app, _popularity_cache_keys, _popularity_leaderboard_key, _redis
    with app.app_context():
        from tv_app.models import db, TVShow
        TVShow.query.update({TVShow.clicks: 0})
        db.session.commit()
        try:
            redis_client = _redis()
            keys = []
            for category in ('tv', 'anime', 'movie'):
                keys.append(_popularity_leaderboard_key(category))
                keys.extend(_popularity_cache_keys(category))
            redis_client.delete(*keys)
        except Exception as exc:
            # The next leaderboard read will naturally expire if Redis is
            # unavailable during the scheduled reset.
            logger.warning("Could not reset live popularity leaderboard: %s", exc)

@celery.task(name="tv_app.tasks.test_task")
def test_task():
    return "Test task complete"
# --- END OF PART 1 ---
# --- START OF PART 2 ---
# ==============================================================================
#                        MOVIE LOGIC (BATCH ENGINE)
# ==============================================================================

# --- TV Pattern Filters ---
TV_PATTERNS = [
    re.compile(r'\bS\d{1,2}E\d{1,2}\b', re.IGNORECASE),    
    re.compile(r'S\d+E\d+', re.IGNORECASE),                
    re.compile(r'\b\d{1,2}x\d{1,2}\b', re.IGNORECASE),     
    re.compile(r'\b(Episode|Ep)\s*\d+\b', re.IGNORECASE), 
    re.compile(r'\bE\d{2,}\b', re.IGNORECASE),             
    re.compile(r'\bSeason\s*\d+', re.IGNORECASE)           
]

def is_likely_tv_show(filename: str) -> bool:
    """Checks if filename matches any TV show patterns."""
    return any(p.search(filename) for p in TV_PATTERNS)

def clean_movie_name(raw_name: str) -> Dict[str, Any]:
    """
    AGGRESSIVE CLEANER v8.0
    """
    if not raw_name: return {"raw_title": "", "year": None}
    
    clean = raw_name
    clean = re.sub(r'[._]', ' ', clean)

    year = None
    year_matches = list(re.finditer(r'\b(19[5-9]\d|20\d{2})\b', clean))
    
    if year_matches:
        match = year_matches[-1]
        year = int(match.group(0))
        clean = clean[:match.start()]

    clean = re.sub(r'\[.*?\]', '', clean)
    clean = re.sub(r'\(.*?\)', ' ', clean) 
    clean = re.sub(r'\{.*?\}', '', clean)
    clean = re.sub(r'(@\w+|https?://\S+|www\.\S+)', '', clean) 

    kill_list = [
        r'\bjoin\b', r'\bchannel\b', r'\bofficial\b', r'\bsearch\b',
        r'\bmkv\b', r'\bmp4\b', r'\bavi\b', r'\bwebm\b',
        r'\bhindi\b', r'\benglish\b', r'\btamil\b', r'\btelugu\b', r'\bkannada\b', r'\bmalayalam\b',
        r'\b1080p\b', r'\b720p\b', r'\b480p\b', r'\b4k\b', r'\b5k\b', r'\bHQ\b', r'\bHD\b', r'\bLQ\b',
        r'\bbluray\b', r'\bweb-dl\b', r'\bhdrip\b', r'\bcamrip\b', r'\bx264\b', r'\bx265\b', r'\bhevc\b',
        r'\besub\b', r'\bdual audio\b', r'\bmulti audio\b', 
        r'\btheatrical\b', r'\bextended\b', r'\buncut\b', r'\bdubbed\b', r'\bremastered\b',
        r'\bfull length movie\b', r'\bhorror movies\b', r'\bgallery\b', r'\bopus\b', r'\bcompany\b',
        r'\bAHA\b', r'\bAMZN\b', r'\bNF\b', r'\bNETFLIX\b', r'\bZEE5\b', r'\bHotstar\b',
        r'\bAkai\b', r'\bCinema\b', r'\bBrRip\b', r'\bDVDRip\b', r'\bHDTV\b'
    ]
    for pattern in kill_list:
        clean = re.sub(pattern, '', clean, flags=re.IGNORECASE)

    clean = re.sub(r'\b\d+(\.\d+)?\s*(MB|GB)\b', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'^\s*[A-Z0-9]{2,3}\s+', '', clean)
    clean = re.sub(r'^\s*(blasters|movies|links)\s+', '', clean, flags=re.IGNORECASE)

    clean = re.sub(r"[^a-zA-Z0-9\s'-]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    
    if len(clean) < 2: return {"raw_title": "", "year": year}
    return {"raw_title": clean, "year": year}

# Shared TMDb Token Cycler
_tokens = [t.strip() for t in os.environ.get("TMDB_BACKFILL_TOKENS", "").split(",") if t.strip()]
if not _tokens: _tokens = [os.environ.get("TMDB_BEARER_TOKEN")]
_token_cycle = itertools.cycle(_tokens)

def get_tmdb_token(): 
    return next(_token_cycle)

async def resolve_single_movie(file_name: str, doc_id: str, session: aiohttp.ClientSession) -> Dict:
    info = clean_movie_name(file_name)
    q, y = info["raw_title"], info["year"]
    
    if not q: return {'status': 'bad_name', 'file': file_name, 'cleaned': 'Empty'}

    token = get_tmdb_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    strategies = [q]
    q_no_digits = re.sub(r'\d+', '', q).strip()
    if len(q_no_digits) > 2 and q_no_digits != q:
        strategies.append(q_no_digits)

    best_match = None

    for attempt_q in strategies:
        if not attempt_q: continue
        url = f"{TMDB_BASE_URL}/search/movie?query={quote_plus(attempt_q)}&language=en-US"
        if y: url += f"&primary_release_year={y}"
        
        try:
            async with session.get(url, headers=headers, timeout=5) as resp:
                if resp.status == 429: return {'status': 'rate_limit'}
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    for res in results:
                        score = fuzz.token_sort_ratio(attempt_q, res['title'])
                        
                        if y and res.get('release_date', '').startswith(str(y)): score += 20
                        if attempt_q.lower() == res['title'].lower(): score += 15

                        if score > 85: 
                            best_match = res; break
        except: pass
        if best_match: break
    
    if not best_match: 
        return {'status': 'no_match', 'file': file_name, 'cleaned': q}

    full_details = await fetch_tmdb_details(
        session,
        best_match['id'],
        'movie',
        token,
    ) or best_match

    return {
        'status': 'found',
        'file': file_name,
        'tmdb': {
            'tmdb_id': best_match['id'],
            'show_name': best_match['title'],
            'overview': best_match.get('overview'),
            'poster_path': f"{TMDB_IMAGE_BASE_URL}{best_match.get('poster_path')}" if best_match.get('poster_path') else None,
            'vote_average': best_match.get('vote_average'),
            'year': int(best_match['release_date'][:4]) if best_match.get('release_date') else y,
            'rating': best_match.get('vote_average'),
            'content_hash': str(doc_id),
            'details': full_details,
        }
    }

# --- NEW: UNIVERSAL DB-BASED CHECKPOINT SYSTEM 🌍 ---

def save_checkpoint_to_db(key_name, valid_object_id_str):
    """
    Saves the checkpoint string (Universal Mode - accepts raw strings).
    """
    from tv_app.app import app
    from tv_app.models import db, SystemState
    
    clean_val = str(valid_object_id_str).strip()
    
    with app.app_context():
        try:
            state = SystemState.query.get(key_name)
            if state:
                state.value = clean_val
            else:
                db.session.add(SystemState(key=key_name, value=clean_val))
            
            db.session.commit()
        except Exception as e:
            logger.error(f"🔥 DB Save Failed: {e}")
            db.session.rollback()

def load_checkpoint_from_db(key_name):
    """
    Loads raw checkpoint string (Universal Mode).
    """
    from tv_app.app import app
    from tv_app.models import db, SystemState
    
    with app.app_context():
        try:
            state = SystemState.query.get(key_name)
            if not state or not state.value:
                return None
            return state.value.strip()
        except Exception as e:
            logger.error(f"🔥 DB Load Failed: {e}")
            return None
# --- END OF PART 2 ---
# --- START OF PART 3 ---
async def batch_processor_engine(uris, db_name, col_name, redis_client):
    from tv_app.app import app
    bot_username = os.environ.get('BOT_USERNAME', 'bot')
    
    with app.app_context():
        try:
            from tv_app.models import db, TVShow, SkippedFile, SystemState
        except ImportError:
            from tv_app.models import db, TVShow; SkippedFile = None

        async with aiohttp.ClientSession() as session:
            # FIX: Iterate URIs with index to create UNIQUE checkpoints per source
            for i, uri in enumerate(uris):
                CHECKPOINT_KEY = f"checkpoint_movies_{db_name}_src_{i}"
                
                try:
                    client = MongoClient(uri)
                    mdb = client[db_name] if db_name in client.list_database_names() else client.get_database()
                    if col_name not in mdb.list_collection_names(): continue
                    coll = mdb[col_name]

                    # --- RESUME LOGIC (Universal) ---
                    last_id_str = load_checkpoint_from_db(CHECKPOINT_KEY)
                    
                    # Basic filter: File size > 300MB
                    base_query = {"file_size": {"$gt": 300 * 1024 * 1024}}
                    query = base_query.copy()

                    if last_id_str:
                        # ⚠️ CRITICAL FIX: Treat ID as raw string, NOT ObjectId
                        query["_id"] = {"$lt": last_id_str}
                        
                        # --- PROGRESS MATH ---
                        try:
                            # Count Remaining (Older than checkpoint)
                            remaining = coll.count_documents(query)
                            
                            # Count Behind/Done (Newer or equal to checkpoint)
                            done_query = base_query.copy()
                            done_query["_id"] = {"$gte": last_id_str}
                            done = coll.count_documents(done_query)
                            
                            log_msg = f"📂 Src {i}: Resuming. Done: {done} | Remaining: {remaining}"
                            redis_client.lpush("backfill:logs", log_msg)
                            logger.info(log_msg)
                        except Exception as e:
                            logger.error(f"Math Error: {e}")
                            redis_client.lpush("backfill:logs", f"📂 Src {i}: Resuming from {last_id_str[:10]}...")

                    else:
                        redis_client.lpush("backfill:logs", f"▶️ Src {i}: Starting Fresh")

                    # ⚠️ SORTING FIX: Explicitly sort by _id DESCENDING
                    cursor = coll.find(query).sort("_id", DESCENDING)
                    BATCH_SIZE = 50
                    
                    while True:
                        if redis_client.get("backfill:pause"): 
                            cursor.close()
                            return "Paused"

                        # ⚠️ MEMORY SAFETY MERGE:
                        gc.collect()

                        batch_docs = []
                        try:
                            for _ in range(BATCH_SIZE): batch_docs.append(cursor.next())
                        except StopIteration: pass
                        if not batch_docs: break

                        redis_client.set("backfill:current_file", f"Src {i}: Batch of {len(batch_docs)}...", ex=60)

                        tasks, valid_docs = [], []
                        for doc in batch_docs:
                            fname = doc.get("file_name")
                            if not fname: continue
                            
                            # TV Filter
                            if is_likely_tv_show(fname):
                                continue

                            fhash = hashlib.md5(fname.encode()).hexdigest()
                            if redis_client.exists(f"backfill:skip:{fhash}"): continue
                            
                            tasks.append(resolve_single_movie(fname, doc['_id'], session))
                            valid_docs.append(doc)

                        # SAVE CHECKPOINT (Empty batch catch)
                        if not tasks:
                            if batch_docs:
                                last_id = str(batch_docs[-1]['_id'])
                                save_checkpoint_to_db(CHECKPOINT_KEY, last_id)
                                redis_client.set(f"backfill:checkpoint:{db_name}_src_{i}", last_id)
                            continue

                        results = await asyncio.gather(*tasks)

                        saves = 0
                        for res in results:
                            if res['status'] == 'rate_limit':
                                await asyncio.sleep(5); redis_client.lpush("backfill:logs", "⏳ Rate Limit"); continue
                            
                            if res['status'] == 'found':
                                tmdb = res['tmdb']
                                syn_id = int(hashlib.sha256(tmdb['content_hash'].encode()).hexdigest(), 16) % (10**18)
                                
                                # Double-lock duplicate check
                                existing = TVShow.query.filter(
                                    ((TVShow.tmdb_id == tmdb['tmdb_id']) | (TVShow.message_id == syn_id)),
                                    TVShow.category == 'movie'
                                ).first()

                                if not existing:
                                    try:
                                        new_movie = TVShow(
                                            tmdb_id=tmdb['tmdb_id'],
                                            message_id=syn_id,
                                            show_name=tmdb['show_name'],
                                            overview=tmdb['overview'],
                                            poster_path=tmdb['poster_path'],
                                            vote_average=tmdb['vote_average'],
                                            year=tmdb['year'],
                                            rating=tmdb['rating'],
                                            category='movie',
                                            download_link=f"https://t.me/{bot_username}?start=search_{quote_plus(tmdb['show_name'][:40])}",
                                            content_hash=tmdb['content_hash']
                                        )
                                        db.session.add(new_movie)
                                        db.session.flush()
                                        apply_enrichment(new_movie, tmdb['details'])
                                        saves += 1
                                        redis_client.lpush("backfill:logs", f"✅ Added: {tmdb['show_name']}")
                                    except IntegrityError:
                                        db.session.rollback()
                                    except Exception:
                                        db.session.rollback()
                                else:
                                    pass # Silent duplicate

                            elif res['status'] == 'no_match' and SkippedFile:
                                if not SkippedFile.query.filter_by(filename=res['file']).first():
                                    try: 
                                        db.session.add(SkippedFile(filename=res['file'], reason=f"Cleaned: {res.get('cleaned')}"))
                                        db.session.commit()
                                    except: db.session.rollback()
                                short_fname = (res['file'][:15] + '..') if len(res['file']) > 15 else res['file']
                                cleaned_q = res.get('cleaned', 'Unknown')
                                redis_client.lpush("backfill:logs", f"⚠️ No: {short_fname} -> {cleaned_q}")

                            redis_client.ltrim("backfill:logs", 0, 49)

                        if saves > 0:
                            try:
                                db.session.commit()
                                redis_client.hincrby("backfill:status", "added", saves)
                            except: db.session.rollback()

                        # --- AUTO PRUNE LOGS ---
                        try:
                            if saves % 5 == 0: 
                                db.session.execute("DELETE FROM skipped_files WHERE id NOT IN (SELECT id FROM skipped_files ORDER BY created_at DESC LIMIT 5000)")
                                db.session.commit()
                        except: pass

                        # --- SAVE CHECKPOINT ---
                        if batch_docs:
                            last_id = str(batch_docs[-1]['_id'])
                            save_checkpoint_to_db(CHECKPOINT_KEY, last_id)
                            redis_client.set(f"backfill:checkpoint:{db_name}_src_{i}", last_id)
                            
                        redis_client.hincrby("backfill:status", "progress", len(batch_docs))
                    
                    cursor.close()
                except Exception as e:
                    logger.error(f"Engine Error: {e}"); continue

@celery.task(bind=True, name="tv_app.tasks.backfill_movies_task")
def backfill_movies_task(self):
    """
    Backfill Task with LOUD DEBUGGING to diagnose Idle issues.
    """
    redis_client = Redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)
    
    # 1. DEBUG: Prove the task started
    logger.info("🚀 BACKFILL TASK STARTED! Checking configurations...")
    redis_client.lpush("backfill:logs", "🚀 Task Triggered by Worker")

    uris = [os.environ.get("MONGO_URI_1"), os.environ.get("MONGO_URI_2")]
    uris = [u for u in uris if u]
    
    # 2. DEBUG: Scream if URIs are missing
    if not uris:
        error_msg = "❌ CRITICAL: No MONGO_URI found in env vars! Task aborting."
        logger.error(error_msg)
        redis_client.lpush("backfill:logs", error_msg)
        return "Failed: No Mongo URI"

    db_name = os.environ.get("MONGO_DB_NAME", "Huswy")
    col_name = os.environ.get("MONGO_COL_NAME", "Husw")
    
    # 3. DEBUG: Verify Database Model Import
    try:
        from tv_app.app import app
        with app.app_context():
            from tv_app.models import SystemState
            logger.info("✅ SystemState model imported successfully.")
    except ImportError:
        error_msg = "❌ CRITICAL: models.py is missing 'SystemState' class!"
        logger.error(error_msg)
        redis_client.lpush("backfill:logs", error_msg)
        return "Failed: Model missing"

    # Start the engine
    redis_client.set("backfill:active", "true", ex=86400)
    redis_client.hset("backfill:status", "state", "Running (DB Checkpoint)")
    
    try:
        asyncio.run(batch_processor_engine(uris, db_name, col_name, redis_client))
    except Exception as e:
        logger.exception(f"🔥 FATAL CRASH in Engine: {e}")
        redis_client.lpush("backfill:logs", f"🔥 FATAL: {str(e)}")
    finally:
        redis_client.delete("backfill:active")
        redis_client.hset("backfill:status", "state", "Idle")
        logger.info("🛑 Backfill Task Finished")

@celery.task(name="tv_app.tasks.sync_movies")
def sync_movies():
    """Restored Sync Functionality"""
    redis_client = Redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)
    uris = [u for u in [os.environ.get("MONGO_URI_1"), os.environ.get("MONGO_URI_2")] if u]
    db_name = os.environ.get("MONGO_DB_NAME", "Huswy")
    col_name = os.environ.get("MONGO_COL_NAME", "Husw")
    
    async def run_sync():
        async with aiohttp.ClientSession() as session:
            for uri in uris:
                try:
                    client = MongoClient(uri)
                    mdb = client[db_name] if db_name in client.list_database_names() else client.get_database()
                    if col_name not in mdb.list_collection_names(): continue
                    coll = mdb[col_name]
                    # Fetch latest 100 via NATURAL ORDER (Creation Time)
                    # This ignores the random File ID and gets the actual newest additions.
                    cursor = coll.find({"file_size": {"$gt": 300 * 1024 * 1024}}).sort("$natural", -1).limit(100)
                    
                    for doc in cursor:
                        fname = doc.get('file_name')
                        if not fname or is_likely_tv_show(fname): continue
                        
                        res = await resolve_single_movie(fname, doc['_id'], session)
                        if res['status'] == 'found':
                            from tv_app.app import app
                            with app.app_context():
                                from tv_app.models import db, TVShow
                                tmdb = res['tmdb']
                                if not TVShow.query.filter_by(tmdb_id=tmdb['tmdb_id'], category='movie').first():
                                    syn_id = int(hashlib.sha256(tmdb['content_hash'].encode()).hexdigest(), 16) % (10**18)
                                    bot = os.environ.get('BOT_USERNAME', 'bot')
                                    try:
                                        new_movie = TVShow(
                                            tmdb_id=tmdb['tmdb_id'],
                                            message_id=syn_id,
                                            show_name=tmdb['show_name'],
                                            overview=tmdb['overview'],
                                            poster_path=tmdb['poster_path'],
                                            vote_average=tmdb['vote_average'],
                                            year=tmdb['year'],
                                            rating=tmdb['rating'],
                                            category='movie',
                                            download_link=f"https://t.me/{bot}?start=search_{quote_plus(tmdb['show_name'][:40])}",
                                            content_hash=tmdb['content_hash']
                                        )
                                        db.session.add(new_movie)
                                        db.session.flush()
                                        apply_enrichment(new_movie, tmdb['details'])
                                        db.session.commit()
                                    except: db.session.rollback()
                except: pass
    
    asyncio.run(run_sync())
    return "Sync Done"

@celery.task(name="tv_app.tasks.hard_reset_backfill")
def hard_reset_backfill():
    """
    MERGED UTILITY: Nukes the checkpoint to restart scanning.
    """
    from tv_app.app import app
    from tv_app.models import db, SystemState
    with app.app_context():
        db.session.query(SystemState).filter(SystemState.key.like('checkpoint_movies_%')).delete(synchronize_session=False)
        db.session.commit()
        return "✅ Ready for Natural Order Scan."

# --- END OF PART 3 (END OF FILE) ---
