# --- PART 1 START: IMPORTS & PUBLIC ROUTES ---
import os
import logging
import hashlib
import math
import re
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs
from xml.sax.saxutils import escape as xml_escape

from flask import (
    Flask, render_template, redirect, url_for, request,
    jsonify, send_from_directory, Response, make_response, abort
)
from sqlalchemy import func
from dotenv import load_dotenv
from redis import Redis
from werkzeug.exceptions import NotFound

# UPDATED: Added SkippedFile import
from .models import db, TVShow, Genre, SkippedFile

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your_secret_key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///tv_shows.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 30
db.init_app(app)

SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://ibox-tv.com').rstrip('/')
SITE_HOST = urlparse(SITE_BASE_URL).netloc.lower()
LEGACY_SITE_HOSTS = {
    host.strip().lower()
    for host in os.environ.get(
        'LEGACY_SITE_HOSTS',
        'anime.ibox-tv.com,movies.ibox-tv.com,www.ibox-tv.com',
    ).split(',')
    if host.strip()
}
SITEMAP_PAGE_SIZE = 25000

CATEGORY_CONFIG = {
    'tv': {'db': 'tv', 'label': 'TV shows', 'detail_endpoint': 'tv_detail', 'home_endpoint': 'index'},
    'anime': {'db': 'anime', 'label': 'Anime', 'detail_endpoint': 'anime_detail', 'home_endpoint': 'anime_index'},
    'movies': {'db': 'movie', 'label': 'Movies', 'detail_endpoint': 'movie_detail', 'home_endpoint': 'list_movies'},
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- HELPERS ---

def get_site_mode():
    """Return the public category represented by the current canonical path."""
    endpoint = request.endpoint or ''
    path = request.path.lower()
    if endpoint in {'anime_index', 'browse_anime', 'anime_detail'} or path.startswith('/anime'):
        return 'anime'
    if endpoint in {'list_movies', 'movie_detail'} or path.startswith('/movies'):
        return 'movies'
    return 'tv'


def _primary_url_for(endpoint: str, **values) -> str:
    """Build an absolute URL on the one public canonical host."""
    relative = url_for(endpoint, _external=False, **values)
    return f"{SITE_BASE_URL}{relative}"


def _compact_params(values):
    return {key: value for key, value in values.items() if value not in (None, '')}


def _public_query(category: str):
    """Records that are usable by a visitor, regardless of index eligibility."""
    return TVShow.query.filter(
        TVShow.category == category,
        TVShow.show_name.isnot(None),
        TVShow.show_name != '',
        TVShow.slug.isnot(None),
        TVShow.slug != '',
        TVShow.download_link.isnot(None),
        TVShow.download_link != '',
    )


def _indexable_query(category: str):
    """Only submit complete, available records to search engines."""
    return _public_query(category).filter(
        TVShow.poster_path.isnot(None),
        TVShow.poster_path != '',
        TVShow.overview.isnot(None),
        TVShow.overview != '',
    )


def _is_indexable(show: TVShow) -> bool:
    return bool(
        show.download_link
        and show.poster_path
        and show.overview
        and show.show_name
        and show.slug
    )


def _category_key_for_show(show: TVShow) -> str:
    return 'movies' if show.category == 'movie' else show.category


def _public_slug(show: TVShow) -> str:
    title_slug = re.sub(r'[^a-z0-9]+', '-', (show.show_name or '').lower()).strip('-')
    if show.tmdb_id and title_slug:
        return f"{show.tmdb_id}-{title_slug}"
    return show.slug


def content_url(show: TVShow, external: bool = False) -> str:
    category = _category_key_for_show(show)
    endpoint = CATEGORY_CONFIG.get(category, CATEGORY_CONFIG['tv'])['detail_endpoint']
    if external:
        return _primary_url_for(endpoint, slug=_public_slug(show))
    return url_for(endpoint, slug=_public_slug(show))


def category_home_url(category: str, external: bool = False, **params) -> str:
    config = CATEGORY_CONFIG.get(category, CATEGORY_CONFIG['tv'])
    endpoint = config['home_endpoint']
    if external:
        return _primary_url_for(endpoint, **_compact_params(params))
    return url_for(endpoint, **_compact_params(params))


def category_browse_url(category: str, external: bool = False, **params) -> str:
    endpoint = 'browse_anime' if category == 'anime' else 'browse_tv'
    if category == 'movies':
        endpoint = 'list_movies'
    if external:
        return _primary_url_for(endpoint, **_compact_params(params))
    return url_for(endpoint, **_compact_params(params))


@app.before_request
def enforce_primary_host():
    """Collapse historic category subdomains onto the canonical domain."""
    if app.testing:
        return None
    host = request.host.split(':', 1)[0].lower()
    if host in LEGACY_SITE_HOSTS:
        path = request.path
        if path == '/':
            path = {
                'anime.ibox-tv.com': '/anime',
                'movies.ibox-tv.com': '/movies',
            }.get(host, '/')
        query = f"?{request.query_string.decode('utf-8')}" if request.query_string else ''
        return redirect(f"{SITE_BASE_URL}{path}{query}", code=301)
    return None


@app.after_request
def add_public_response_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    return response

@app.context_processor
def inject_globals():
    """Injects 'now' and 'site_mode' into every template."""
    return {
        'now': datetime.utcnow,
        'site_mode': get_site_mode(),
        'content_url': content_url,
        'category_home_url': category_home_url,
        'category_browse_url': category_browse_url,
        'site_base_url': SITE_BASE_URL,
    }

def get_trending_shows(limit: int = 6, category: str = 'tv'):
    """Fetches top clicked shows FOR THE CURRENT CATEGORY only."""
    with app.app_context():
        target_cat = 'movie' if category == 'movies' else category
        return _public_query(target_cat).order_by(
            TVShow.clicks.desc(), TVShow.updated_at.desc()
        ).limit(limit).all()

def count_search_results(category: str, query_str: str) -> int:
    """
    NEW: consistently counts results for a category to populate the search tabs.
    Uses ILIKE for speed/consistency across tabs.
    """
    if not query_str:
        return 0
    try:
        # Note: We map 'movies' (site mode) to 'movie' (DB category) if needed,
        # but the caller should pass the correct DB category ('tv', 'anime', 'movie').
        return _public_query(category).filter(
            TVShow.show_name.ilike(f'%{query_str}%')
        ).count()
    except Exception:
        return 0

def _page_urls(base_endpoint: str, page_obj, extra_params=None):
    extra_params = _compact_params(extra_params or {})

    def _u(p):
        params = dict(extra_params)
        if p > 1:
            params['page'] = p
        return _primary_url_for(base_endpoint, **params)

    prev_url = _u(page_obj.prev_num) if page_obj.has_prev else None
    next_url = _u(page_obj.next_num) if page_obj.has_next else None
    canonical_url = _u(page_obj.page)
    meta_robots = "index,follow" if page_obj.page == 1 and not extra_params else "noindex,follow"
    return canonical_url, prev_url, next_url, meta_robots

@app.template_filter('hostonly')
def hostonly(url):
    try:
        return urlparse(url).netloc or '—'
    except Exception:
        return '—'

# ----------------------------- Public pages -----------------------------

def _render_index(mode: str, endpoint: str):
    db_category = CATEGORY_CONFIG[mode]['db']
    search_query = (request.args.get('search') or '').strip()
    page = max(request.args.get('page', 1, type=int), 1)
    per_page = 20
    base_query = _public_query(db_category)
    trending_shows = get_trending_shows(limit=6, category=mode)
    message = None
    result_counts = {'tv': 0, 'anime': 0, 'movies': 0}

    if search_query:
        try:
            shows = base_query.filter(
                TVShow.show_name.ilike(f'%{search_query}%')
            ).order_by(TVShow.updated_at.desc()).paginate(
                page=page, per_page=per_page, error_out=False
            )
            if not shows.items:
                message = f"No {CATEGORY_CONFIG[mode]['label'].lower()} matched your search."
        except Exception as e:
            logger.error(f"Database error during search: {e}")
            db.session.rollback()
            shows = base_query.filter(TVShow.id == -1).paginate(
                page=page, per_page=per_page, error_out=False
            )
            message = "Search is temporarily unavailable. Please try again."

        page_title = f"Search results for {search_query}"
        result_counts['tv'] = count_search_results('tv', search_query)
        result_counts['anime'] = count_search_results('anime', search_query)
        result_counts['movies'] = count_search_results('movie', search_query)
    else:
        shows = base_query.order_by(TVShow.updated_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        page_title = "Latest anime" if mode == 'anime' else "Latest TV shows"

    canonical_url, prev_url, next_url, meta_robots = _page_urls(
        endpoint, shows, extra_params={'search': search_query}
    )

    return render_template('index.html',
        shows=shows, search_query=search_query, trending_shows=trending_shows,
        message=message, title=page_title, site_mode=mode,
        result_counts=result_counts,
        canonical_url=canonical_url, prev_url=prev_url, next_url=next_url, meta_robots=meta_robots
    )


@app.route('/')
def index():
    return _render_index('tv', 'index')


@app.route('/anime')
def anime_index():
    return _render_index('anime', 'anime_index')

@app.route('/shows')
def legacy_list_shows():
    return redirect(url_for('browse_tv'), code=301)


def _render_browse(category: str, endpoint: str):
    try:
        page = max(request.args.get('page', 1, type=int), 1)
        per_page = 30
        genre_filter = request.args.get('genre')
        rating_filter = request.args.get('rating', type=int)
        year_filter = request.args.get('year', type=int)
        sort_by = request.args.get('sort_by', 'name_asc')

        query = _public_query(category)

        if genre_filter:
            query = query.join(TVShow.genres).filter(Genre.name == genre_filter)
        if year_filter:
            query = query.filter(TVShow.year == year_filter)
        if rating_filter is not None:
            lower = float(rating_filter)
            if rating_filter == 10:
                query = query.filter(TVShow.rating >= lower)
            else:
                query = query.filter(TVShow.rating >= lower, TVShow.rating < lower + 1.0)

        if sort_by == 'name_asc':
            query = query.order_by(TVShow.show_name.asc())
        elif sort_by == 'name_desc':
            query = query.order_by(TVShow.show_name.desc())
        elif sort_by == 'date_asc':
            query = query.order_by(TVShow.created_at.asc())
        elif sort_by == 'date_desc':
            query = query.order_by(TVShow.created_at.desc())
        elif sort_by == 'rating_asc':
            query = query.order_by(TVShow.rating.asc().nullslast())
        elif sort_by == 'rating_desc':
            query = query.order_by(TVShow.rating.desc().nullslast())

        shows_paginated = query.paginate(page=page, per_page=per_page, error_out=False)

        all_genres = Genre.query.join(Genre.tv_shows).filter(
            TVShow.category == category
        ).distinct().order_by(Genre.name).all()
        current_year = datetime.utcnow().year
        min_year_result = db.session.query(func.min(TVShow.year)).filter(
            TVShow.category == category, TVShow.year.isnot(None)
        ).scalar()
        min_year = min_year_result if min_year_result is not None else current_year - 20
        years = list(range(current_year, min_year - 1, -1))
        possible_ratings = list(range(10, -1, -1))

        canonical_url, prev_url, next_url, meta_robots = _page_urls(endpoint, shows_paginated, extra_params={
            'genre': genre_filter or '',
            'rating': rating_filter if rating_filter is not None else '',
            'year': year_filter if year_filter is not None else '',
            'sort_by': sort_by if sort_by != 'name_asc' else '',
        })
        return render_template('shows.html',
            shows=shows_paginated, genres=all_genres, ratings=possible_ratings, years=years,
            selected_genre=genre_filter, selected_rating=rating_filter, selected_year=year_filter,
            current_sort_by=sort_by,
            title="Browse anime" if category == 'anime' else "Browse TV shows",
            site_mode=category, browse_endpoint=endpoint,
            canonical_url=canonical_url, prev_url=prev_url, next_url=next_url, meta_robots=meta_robots
        )
    except Exception as e:
        logger.error(f"Error in list_shows route: {e}")
        db.session.rollback()
        return render_template('500.html', title="Server Error",
                               meta_description="An error occurred viewing shows list."), 500


@app.route('/browse/tv')
def browse_tv():
    return _render_browse('tv', 'browse_tv')


@app.route('/browse/anime')
def browse_anime():
    return _render_browse('anime', 'browse_anime')

@app.route('/movies')
def list_movies():
    try:
        page = max(request.args.get('page', 1, type=int), 1)
        per_page = 30
        search_q = (request.args.get('q') or '').strip()
        sort_by = request.args.get('sort_by', 'date_desc')
        year_filter = request.args.get('year', type=int)
        rating_filter = request.args.get('rating', type=int)

        query = _public_query('movie')

        if search_q:
            query = query.filter(TVShow.show_name.ilike(f'%{search_q}%'))

        if year_filter:
            query = query.filter(TVShow.year == year_filter)
        if rating_filter is not None:
             query = query.filter(TVShow.rating >= float(rating_filter))

        if not search_q:
            if sort_by == 'name_asc':
                query = query.order_by(TVShow.show_name.asc())
            elif sort_by == 'rating_desc':
                query = query.order_by(TVShow.rating.desc().nullslast())
            elif sort_by == 'date_asc':
                query = query.order_by(TVShow.updated_at.asc())
            else:
                query = query.order_by(TVShow.created_at.desc())
        else:
            query = query.order_by(TVShow.updated_at.desc())

        movies = query.paginate(page=page, per_page=per_page, error_out=False)

        current_year = datetime.utcnow().year
        years = list(range(current_year, 1970, -1))
        
        canonical_url, prev_url, next_url, meta_robots = _page_urls('list_movies', movies, extra_params={
            'q': search_q,
            'sort_by': sort_by if sort_by != 'date_desc' else '',
            'year': year_filter,
            'rating': rating_filter,
        })

        return render_template('movies.html',
            movies=movies, years=years,
            search_q=search_q, current_sort=sort_by, selected_year=year_filter, selected_rating=rating_filter,
            title="Browse Movies",
            canonical_url=canonical_url, prev_url=prev_url, next_url=next_url, meta_robots=meta_robots
        )
    except Exception as e:
        logger.error(f"Error in list_movies: {e}")
        return render_template('500.html'), 500

def _render_show_details(slug: str, expected_category: str):
    try:
        show = None
        id_match = re.match(r'^(\d+)-', slug)
        if id_match:
            show = TVShow.query.filter_by(
                tmdb_id=int(id_match.group(1)), category=expected_category
            ).first()
        if show is None:
            show = TVShow.query.filter_by(slug=slug).first_or_404()

        if show.category != expected_category or slug != _public_slug(show):
            return redirect(content_url(show), code=301)

        title_parts = [show.show_name]
        if show.category == 'movie':
            if show.year:
                title_parts.append(f"({show.year})")
            title_parts.append("Movie details")
        else:
            if show.episode_title:
                title_parts.append(show.episode_title)
            title_parts.append("Episodes and download")

        page_title = " - ".join(title_parts)

        if show.overview:
            meta_desc = show.overview[:157].rstrip()
            if len(show.overview) > 157:
                meta_desc += "…"
        else:
            meta_desc = f"View availability, details, and the latest update for {show.show_name} on iBOX TV."
        meta_desc = meta_desc[:160]

        related_shows = _public_query(show.category).filter(
            TVShow.id != show.id
        ).order_by(TVShow.updated_at.desc()).limit(6).all()

        return render_template('show_details.html',
            show=show, title=page_title, meta_description=meta_desc,
            canonical_url=content_url(show, external=True),
            meta_robots="index,follow" if _is_indexable(show) else "noindex,follow",
            related_shows=related_shows,
        )
    except Exception as e:
        db.session.rollback()
        if isinstance(e, NotFound):
            raise
        logger.error(f"Error in show details slug={slug}: {e}")
        return render_template('500.html', title="Server Error",
                               meta_description="An error occurred viewing show details.",
                               meta_robots="noindex,nofollow"), 500


@app.route('/tv/<slug>')
def tv_detail(slug):
    return _render_show_details(slug, 'tv')


@app.route('/anime/<slug>')
def anime_detail(slug):
    return _render_show_details(slug, 'anime')


@app.route('/movies/<slug>')
def movie_detail(slug):
    return _render_show_details(slug, 'movie')


@app.route('/show/<slug>')
def show_details(slug):
    show = TVShow.query.filter_by(slug=slug).first_or_404()
    return redirect(content_url(show), code=301)

# --- PART 1 END ---
# ==========================================
# START OF PART 2
# ==========================================

# --- NEW: AdBlock Analytics API 🥷 ---
@app.route('/api/stats/adblock', methods=['POST'])
def track_adblock_stats():
    """Tracks adblock detection and resolution events."""
    try:
        data = request.json
        event_type = data.get('event') # 'detected' or 'resolved'
        
        # Use the existing Redis connection helper
        r = _redis()
        
        if event_type == 'detected':
            r.incr("stats:adblock_detected")
        elif event_type == 'resolved':
            r.incr("stats:adblock_resolved")
            
        return jsonify({"status": "logged"}), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# --- Download Redirect ---
@app.route('/download/<int:show_id>')
def redirect_to_download(show_id):
    try:
        show = TVShow.query.get_or_404(show_id)
        # If we have a direct link
        if show.download_link:
            show.clicks = (show.clicks or 0) + 1
            db.session.commit()
            link = show.download_link
            
            # 🚀 Automatic fix for Telegram Bot Deep Links (Slugify + Smart Truncate)
            if 't.me' in link and 'start=search_' in link:
                try:
                    u = urlparse(link)
                    q = parse_qs(u.query)
                    
                    if 'start' in q:
                        raw_start = q['start'][0]
                        
                        # 1. Get the title part
                        if 'search_' in raw_start:
                            prefix, title = raw_start.split('search_', 1)
                        else:
                            title = raw_start

                        # 2. Remove apostrophes (Better for DB matching)
                        title = title.replace("'", "")

                        # 3. Clean: Replace non-alphanumeric chars with hyphens
                        import re
                        safe_title = re.sub(r'[^a-zA-Z0-9]', '-', title)
                        safe_title = re.sub(r'-+', '-', safe_title).strip('-')
                        
                        # 4. SAFETY: Smart Truncate to 64 chars MAX
                        max_len = 64 - len("search_")
                        
                        if len(safe_title) > max_len:
                            truncated = safe_title[:max_len]
                            last_hyphen = truncated.rfind('-')
                            if last_hyphen != -1:
                                safe_title = truncated[:last_hyphen]
                            else:
                                safe_title = truncated

                        safe_start = f"search_{safe_title}"
                        
                        # Rebuild URL
                        new_query = urlencode({'start': safe_start})
                        link = u._replace(query=new_query).geturl()
                except Exception as ex:
                    logger.error(f"Failed to fix Telegram link: {ex}")
            
            return redirect(link)
        
        # Fallback: If no link, go back to details
        return redirect(content_url(show))
    except Exception as e:
        logger.error(f"Error redirecting to download {show_id}: {e}")
        return redirect(url_for('index'))

# ----------------------------- SEO assets -----------------------------
@app.route('/ads.txt')
def ads_txt_redirect():
    return redirect("https://srv.adstxtmanager.com/75094/ibox-tv.com", code=301)

@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.ico', mimetype='image/x-icon')


@app.route('/privacy-policy')
def privacy_policy():
    return render_template(
        'privacy_policy.html',
        title='Privacy policy',
        canonical_url=_primary_url_for('privacy_policy'),
        meta_robots='index,follow',
    )


@app.route('/about')
def about():
    return render_template(
        'about.html',
        title='About iBOX TV',
        canonical_url=_primary_url_for('about'),
        meta_robots='index,follow',
    )


def _xml_response(xml: str, status: int = 200) -> Response:
    response = Response(xml, status=status, mimetype='application/xml')
    response.headers['Cache-Control'] = 'public, max-age=3600, s-maxage=3600'
    return response


@app.route('/sitemap.xml')
def sitemap_xml():
    try:
        sitemap_urls = [_primary_url_for('core_sitemap')]
        for category in CATEGORY_CONFIG:
            db_category = CATEGORY_CONFIG[category]['db']
            total = _indexable_query(db_category).count()
            for page_number in range(1, math.ceil(total / SITEMAP_PAGE_SIZE) + 1):
                sitemap_urls.append(_primary_url_for(
                    'category_sitemap', category=category, page=page_number
                ))

        entries = "\n".join(
            f"  <sitemap><loc>{xml_escape(url)}</loc></sitemap>" for url in sitemap_urls
        )
        return _xml_response(
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            "<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n"
            f"{entries}\n</sitemapindex>"
        )
    except Exception as e:
        logger.error(f"sitemap error: {e}")
        return _xml_response(
            "<?xml version='1.0' encoding='UTF-8'?><sitemapindex/>",
            status=503,
        )


@app.route('/sitemaps/core.xml')
def core_sitemap():
    urls = [
        (_primary_url_for('index'), 'daily'),
        (_primary_url_for('anime_index'), 'daily'),
        (_primary_url_for('list_movies'), 'daily'),
        (_primary_url_for('browse_tv'), 'weekly'),
        (_primary_url_for('browse_anime'), 'weekly'),
        (_primary_url_for('about'), 'monthly'),
        (_primary_url_for('privacy_policy'), 'yearly'),
    ]
    entries = "\n".join(
        f"  <url><loc>{xml_escape(url)}</loc><changefreq>{frequency}</changefreq></url>"
        for url, frequency in urls
    )
    return _xml_response(
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n"
        f"{entries}\n</urlset>"
    )


@app.route('/sitemaps/<category>-<int:page>.xml')
def category_sitemap(category, page):
    config = CATEGORY_CONFIG.get(category)
    if not config or page < 1:
        abort(404)

    query = _indexable_query(config['db'])
    total = query.count()
    total_pages = math.ceil(total / SITEMAP_PAGE_SIZE)
    if page > total_pages or total == 0:
        abort(404)

    rows = query.with_entities(
        TVShow.tmdb_id, TVShow.show_name, TVShow.slug, TVShow.updated_at, TVShow.created_at
    ).order_by(TVShow.updated_at.desc()).offset(
        (page - 1) * SITEMAP_PAGE_SIZE
    ).limit(SITEMAP_PAGE_SIZE).all()

    endpoint = config['detail_endpoint']
    entries = []
    for tmdb_id, show_name, stored_slug, updated_at, created_at in rows:
        title_slug = re.sub(r'[^a-z0-9]+', '-', (show_name or '').lower()).strip('-')
        public_slug = f"{tmdb_id}-{title_slug}" if tmdb_id and title_slug else stored_slug
        loc = _primary_url_for(endpoint, slug=public_slug)
        lastmod = (updated_at or created_at or datetime.utcnow()).date().isoformat()
        entries.append(
            f"  <url><loc>{xml_escape(loc)}</loc><lastmod>{lastmod}</lastmod></url>"
        )

    entries_xml = "\n".join(entries)
    return _xml_response(
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n"
        f"{entries_xml}\n</urlset>"
    )

# ----------------------------- Nuke panel (auth + dupes) -----------------------------
def _redis():
    return Redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)

def _admin_token():
    return os.environ.get('ADMIN_TOKEN', '')

def _nuke_cookie_ttl_days():
    try:
        return int(os.environ.get('NUKE_COOKIE_TTL_DAYS', '30'))
    except Exception:
        return 30

def _nuke_enabled():
    r = _redis()
    val = r.get('nuke:enabled')
    if val is None:
        r.set('nuke:enabled', '1')
        return True
    return val == '1'

def _nuke_disable():
    _redis().set('nuke:enabled', '0')

def _nuke_enable():
    _redis().set('nuke:enabled', '1')

def _fail_key(ip):
    return f"nuke:fail:{ip}"

def _cookie_value():
    secret = app.config['SECRET_KEY']
    token = _admin_token()
    return hashlib.sha256(f"{token}:{secret}".encode()).hexdigest()

def _is_authed(req):
    return req.cookies.get('nuke_auth') == _cookie_value()

@app.route('/nuke', methods=['GET'])
def nuke_home():
    if not _nuke_enabled():
        return render_template('maintenance.html', title="Maintenance"), 503

    if not _is_authed(request):
        msg = request.args.get('msg', '')
        return render_template('nuke_login.html', title="Access Nuke", message=msg)
    
    # --- FETCH ADBLOCK STATS ---
    r = _redis()
    adblock_stats = {
        'detected': r.get("stats:adblock_detected") or 0,
        'resolved': r.get("stats:adblock_resolved") or 0
    }

    q = (request.args.get('q') or '').strip()
    view_dupes = request.args.get('dupes')
    if not q and view_dupes is None:
        view_dupes = '1'
    
    # Only fetch last 20 skipped files to prevent page lag
    recent_skipped = []
    try:
        recent_skipped = SkippedFile.query.order_by(SkippedFile.created_at.desc()).limit(20).all()
    except Exception as e:
        logger.error(f"Error fetching skipped files: {e}")

    if view_dupes:
        # IGNORE MOVIES IN DUPLICATE SCAN
        rows = db.session.query(
            TVShow.download_link, func.count(TVShow.id).label('cnt')
        ).filter(
            TVShow.download_link.isnot(None),
            TVShow.category.in_(['tv', 'anime']) 
        ).group_by(
            TVShow.download_link
        ).having(
            func.count(TVShow.id) > 1
        ).order_by(
            func.count(TVShow.id).desc()
        ).all()

        dupe_groups = []
        for link, _cnt in rows:
            shows = TVShow.query.filter(
                TVShow.download_link == link,
                TVShow.category.in_(['tv', 'anime'])
            ).order_by(TVShow.created_at.desc()).all()
            
            dupe_groups.append({
                'link': link,
                'domain': urlparse(link).netloc if link else '',
                'shows': shows
            })
        return render_template('nuke.html', title="Nuke", view_dupes=True, dupe_groups=dupe_groups, q=q, skipped_files=recent_skipped, adblock_stats=adblock_stats)

    page = request.args.get('page', 1, type=int)
    per_page = 30
    query = TVShow.query
    if q:
        try:
            query = query.filter(func.similarity(TVShow.show_name, q) > 0.1).order_by(func.similarity(TVShow.show_name, q).desc())
        except Exception:
            query = query.filter(TVShow.show_name.ilike(f"%{q}%")).order_by(TVShow.created_at.desc())
    else:
        query = query.order_by(TVShow.created_at.desc())

    shows = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('nuke.html', title="Nuke", shows=shows, q=q, view_dupes=False, skipped_files=recent_skipped, adblock_stats=adblock_stats)

@app.route('/nuke/login', methods=['POST'])
def nuke_login():
    if not _nuke_enabled():
        return render_template('maintenance.html', title="Maintenance"), 503

    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '0.0.0.0').split(',')[0].strip()
    token = (request.form.get('token') or '').strip()
    if not token:
        return redirect(url_for('nuke_home', msg="Token required"))

    if token != _admin_token():
        r = _redis()
        fk = _fail_key(ip)
        fails = int(r.incr(fk))
        r.expire(fk, 3600)
        if fails >= 2:
            _nuke_disable()
            return redirect(url_for('nuke_home', msg="Locked after 2 failed attempts"))
        return redirect(url_for('nuke_home', msg=f"Invalid token. Attempt {fails}/2"))

    resp = make_response(redirect(url_for('nuke_home')))
    resp.set_cookie('nuke_auth', _cookie_value(), max_age=_nuke_cookie_ttl_days()*24*3600, httponly=True, samesite='Lax', secure=True)
    _redis().delete(_fail_key(ip))
    return resp

@app.route('/nuke/logout', methods=['POST'])
def nuke_logout():
    resp = make_response(redirect(url_for('nuke_home', msg="Logged out")))
    resp.set_cookie('nuke_auth', '', max_age=0)
    return resp

@app.route('/nuke/unlock', methods=['POST'])
def nuke_unlock():
    token = (request.form.get('token') or '').strip()
    if token != _admin_token():
        return redirect(url_for('nuke_home', msg="Wrong key"))
    _nuke_enable()
    return redirect(url_for('nuke_home', msg="Nuke enabled"))

@app.route('/nuke/delete/<int:show_id>', methods=['POST'])
def nuke_delete(show_id):
    if not _is_authed(request):
        return redirect(url_for('nuke_home', msg="Login required"))
    try:
        show = TVShow.query.get_or_404(show_id)
        db.session.delete(show)
        db.session.commit()
        return redirect(f"{url_for('nuke_home')}?{urlencode({'msg': f'Deleted {show.show_name}'})}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"/nuke delete error {show_id}: {e}")
        return redirect(url_for('nuke_home', msg="Delete failed, check logs"))

@app.route('/nuke/bulk-delete', methods=['POST'])
def nuke_bulk_delete():
    if not _is_authed(request):
        return redirect(url_for('nuke_home', msg="Login required"))
    link = (request.form.get('link') or '').strip()
    mode = (request.form.get('mode') or '').strip()
    ids = request.form.getlist('ids')
    try:
        if not link:
            return redirect(url_for('nuke_home', msg="No link provided"))
        if mode == 'selected':
            if not ids:
                return redirect(url_for('nuke_home', dupes=1, msg="No items selected"))
            TVShow.query.filter(TVShow.id.in_(ids), TVShow.download_link == link).delete(synchronize_session=False)
        elif mode == 'all_but_latest':
            items = TVShow.query.filter_by(download_link=link).order_by(TVShow.created_at.desc(), TVShow.id.desc()).all()
            for s in items[1:]:
                db.session.delete(s)
        elif mode == 'all':
            TVShow.query.filter_by(download_link=link).delete(synchronize_session=False)
        else:
            return redirect(url_for('nuke_home', dupes=1, msg="Unknown mode"))
        db.session.commit()
        return redirect(url_for('nuke_home', dupes=1, msg="Bulk delete done"))
    except Exception as e:
        db.session.rollback()
        logger.error(f"/nuke bulk-delete error: {e}")
        return redirect(url_for('nuke_home', dupes=1, msg="Bulk delete failed"))

# --- BACKFILL CONTROLS ---

@app.route('/nuke/backfill/start', methods=['POST'])
def nuke_backfill_start():
    if not _is_authed(request):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        from .tasks import backfill_movies_task
        _redis().delete('backfill:pause')
        backfill_movies_task.delay()
        return jsonify({'success': True, 'message': 'Backfill task started'})
    except Exception as e:
        logger.error(f"Backfill start error: {e}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/nuke/backfill/pause', methods=['POST'])
def nuke_backfill_pause():
    if not _is_authed(request):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        _redis().set('backfill:pause', '1')
        return jsonify({'success': True, 'message': 'Pause signal sent'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/nuke/backfill/reset', methods=['POST'])
def nuke_backfill_reset():
    """Clears Redis stats and checkpoints to force a fresh start."""
    if not _is_authed(request):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        r = _redis()
        # 1. Clear status and live logs
        r.delete('backfill:status', 'backfill:current_file', 'backfill:logs') 
        # 2. CRITICAL: Clear stuck locks (The Unjammer)
        r.delete('backfill:active', 'update_tv_shows_lock')
        
        # 3. Clear checkpoint (Need correct DB name key)
        db_name = os.environ.get('MONGO_DB_NAME', 'Huswy')
        r.delete(f"backfill:checkpoint:{db_name}")
        
        return jsonify({'success': True, 'message': 'Backfill memory cleared. Engine is ready to restart.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/nuke/movies/purge', methods=['POST'])
def nuke_movies_purge():
    """Deletes ALL movies and ALL skipped files from the database."""
    if not _is_authed(request):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        # 1. Delete all movies
        deleted_shows = TVShow.query.filter_by(category='movie').delete()
        # 2. Delete all skipped logs
        deleted_skips = SkippedFile.query.delete()
        
        db.session.commit()
        return jsonify({'success': True, 'message': f'Purged {deleted_shows} movies and {deleted_skips} skipped logs.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/nuke/backfill/status')
def nuke_backfill_status():
    if not _is_authed(request):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        r = _redis()
        status = r.hgetall('backfill:status')
        # Add the live file processing log
        status['current_file'] = r.get('backfill:current_file') or 'Idle'
        # Add the log list for the matrix view
        status['logs'] = r.lrange('backfill:logs', 0, 49) # Matrix Logs
        return jsonify(status)
    except Exception:
        return jsonify({})

# ----------------------------- Health & errors -----------------------------
@app.route('/healthz')
def healthz():
    return jsonify(status="ok", time=datetime.utcnow().isoformat()), 200

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html', title="Page Not Found",
                           meta_description="The page you were looking for could not be found.",
                           meta_robots="noindex,follow"), 404

@app.errorhandler(500)
def internal_server_error(e):
    try:
        db.session.rollback()
    except Exception as rollback_error:
        logger.error(f"Error during rollback in 500 handler: {rollback_error}")
    return render_template('500.html', title="Internal Server Error",
                           meta_description="We encountered an internal error. Please try again later.",
                           meta_robots="noindex,nofollow"), 500

# --- PART 2 END ---
