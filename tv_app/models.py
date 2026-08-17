from datetime import datetime
import re
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, text, event

db = SQLAlchemy()

# --- NEW: System State (The Brain for Checkpoints) 🧠 ---
class SystemState(db.Model):
    __tablename__ = 'system_state'
    
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<SystemState {self.key}={self.value}>"

# --- M2M association: TVShow <-> Genre ---
show_genres = db.Table(
    "show_genres",
    db.Column("tvshow_id", db.Integer, db.ForeignKey("tv_shows.id"), primary_key=True),
    db.Column("genre_id", db.Integer, db.ForeignKey("genres.id"), primary_key=True),
)
Index("ix_show_genres_genre_tvshow", show_genres.c.genre_id, show_genres.c.tvshow_id)

class Genre(db.Model):
    __tablename__ = "genres"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

    def __repr__(self) -> str:
        return f"<Genre {self.name!r}>"

class TVShow(db.Model):
    __tablename__ = "tv_shows"

    id = db.Column(db.Integer, primary_key=True)

    # Removed unique=True so we can have duplicates across categories
    tmdb_id = db.Column(db.Integer, unique=False, nullable=True, index=True)

    # Removed unique=True (Message 100 in TV != Message 100 in Anime)
    message_id = db.Column(db.BigInteger, unique=False, nullable=False, index=True)

    show_name = db.Column(db.String(255), nullable=False, index=True)
    episode_title = db.Column(db.String(255), default=None)
    download_link = db.Column(db.Text, default=None)

    overview = db.Column(db.Text)
    vote_average = db.Column(db.Float)
    poster_path = db.Column(db.Text, default=None)

    # Rich TMDB metadata cached for useful detail pages. Keeping this data in
    # the catalogue avoids an external API call on every visitor request.
    tagline = db.Column(db.Text, default=None)
    runtime_minutes = db.Column(db.Integer, default=None)
    number_of_seasons = db.Column(db.Integer, default=None)
    release_status = db.Column(db.String(50), default=None)
    original_language = db.Column(db.String(12), default=None)
    cast_data = db.Column(db.JSON, default=None)
    official_trailer_key = db.Column(db.String(64), default=None)
    official_trailer_name = db.Column(db.String(255), default=None)
    official_trailer_published_at = db.Column(db.String(40), default=None)
    metadata_status = db.Column(db.String(20), default=None, index=True)
    metadata_updated_at = db.Column(db.DateTime, default=None, index=True)

    # Required by homepage/trending
    clicks = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    availability_updated_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    content_hash = db.Column(db.String(64), nullable=False, index=True)

    year = db.Column(db.Integer)
    rating = db.Column(db.Float)

    # Category column (defaults to 'tv')
    category = db.Column(db.String(20), nullable=False, default='tv', index=True)

    # SEO-friendly slug, unique
    slug = db.Column(db.String(255), nullable=False, unique=True, index=True)

    # Many-to-many to Genre
    genres = db.relationship(
        "Genre",
        secondary=show_genres,
        backref=db.backref("tv_shows", lazy="dynamic"),
    )

    __table_args__ = (
        Index("ix_show_name_episode_title", "show_name", "episode_title"),
        Index("ix_tv_shows_category_availability", "category", availability_updated_at.desc()),
        Index("ix_tv_shows_category_clicks_availability", "category", clicks.desc(), availability_updated_at.desc()),
        
        # --- NEW: Composite Unique Key ---
        # This mirrors the SQL: CREATE UNIQUE INDEX ix_tmdb_category ON tv_shows (tmdb_id, category);
        db.UniqueConstraint('tmdb_id', 'category', name='ix_tmdb_category'),

        # trigram index for Postgres; harmless on SQLite (ignored)
        Index(
            "ix_show_name_trgm",
            "show_name",
            postgresql_using="gin",
            postgresql_ops={"show_name": "gin_trgm_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<TVShow {self.show_name!r} - {self.episode_title!r}>"

# --- NEW: Skipped File Model (Negative Cache) ---
class SkippedFile(db.Model):
    __tablename__ = "skipped_files"
    
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(512), unique=True, nullable=False, index=True)
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Skipped {self.filename!r} - {self.reason}>"

# --- Slug helpers ---
_slug_cleaner = re.compile(r"[^a-z0-9]+")
def _slugify(title: str) -> str:
    s = title.strip().lower()
    s = _slug_cleaner.sub("-", s).strip("-")
    return s or "item"

@event.listens_for(TVShow, "before_insert")
def _ensure_slug(mapper, connection, target: TVShow):
    """Generate a unique slug if missing. Keeps DB from bricking if the task forgets."""
    if target.slug and target.slug.strip():
        base = _slugify(target.slug)
    else:
        parts = [p for p in [target.show_name or "", target.episode_title or ""] if p]
        base = _slugify(" ".join(parts)) or "item"

    slug = base
    # ensure uniqueness at DB level using the same connection
    i = 1
    while True:
        exists = connection.execute(
            text("SELECT 1 FROM tv_shows WHERE slug=:s LIMIT 1"), {"s": slug}
        ).fetchone()
        if not exists:
            break
        i += 1
        slug = f"{base}-{i}"
    target.slug = slug
