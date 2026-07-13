"""SQLite storage for crawled historical news."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "historical_news.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    publish_time TEXT,
    source TEXT,
    channel TEXT,
    content TEXT,
    crawl_time TEXT NOT NULL,
    content_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_time ON articles(publish_time);
CREATE INDEX IF NOT EXISTS idx_articles_channel ON articles(channel);
CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:24]


def content_hash(title: str, content: str) -> str:
    text = f"{title}\n{content}".strip()
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def upsert_article(conn: sqlite3.Connection, article: dict[str, Any]) -> bool:
    """Insert/update article. Returns True if a row was added or changed."""
    url = str(article.get("url") or "").strip()
    title = str(article.get("title") or "").strip()
    if not url or not title:
        return False

    content = str(article.get("content") or "").strip()
    new_hash = content_hash(title, content)
    existing = conn.execute(
        "SELECT publish_time, content_hash FROM articles WHERE url = ?",
        (url,),
    ).fetchone()
    publish_time = str(article.get("publish_time") or "")
    if existing and (existing["publish_time"] or "") == publish_time and existing["content_hash"] == new_hash:
        return False

    row = {
        "id": article_id(url),
        "url": url,
        "title": title,
        "publish_time": publish_time,
        "source": str(article.get("source") or ""),
        "channel": str(article.get("channel") or ""),
        "content": content,
        "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "content_hash": new_hash,
    }
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO articles (
            id, url, title, publish_time, source, channel, content, crawl_time, content_hash
        ) VALUES (
            :id, :url, :title, :publish_time, :source, :channel, :content, :crawl_time, :content_hash
        )
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            publish_time=excluded.publish_time,
            source=excluded.source,
            channel=excluded.channel,
            content=excluded.content,
            crawl_time=excluded.crawl_time,
            content_hash=excluded.content_hash
        """,
        row,
    )
    return conn.total_changes > before


def query_articles_before(
    trade_date: str,
    *,
    cutoff_time: str = "09:30",
    lookback_hours: int = 60,
    channels: set[str] | None = None,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    # A read path must not create an empty database. This matters for strict
    # backtests, where input files are required to remain byte-for-byte stable.
    if not db_path.exists():
        return []
    cutoff = datetime.strptime(f"{trade_date[:10]} {cutoff_time}", "%Y-%m-%d %H:%M")
    start = cutoff - timedelta(hours=lookback_hours)
    sql = (
        "SELECT url, title, publish_time, source, channel, content "
        "FROM articles WHERE publish_time >= ? AND publish_time <= ?"
    )
    params: list[Any] = [start.strftime("%Y-%m-%d %H:%M:%S"), cutoff.strftime("%Y-%m-%d %H:%M:%S")]
    if channels:
        placeholders = ",".join(["?"] * len(channels))
        sql += f" AND channel IN ({placeholders})"
        params.extend(sorted(channels))
    sql += " ORDER BY publish_time ASC"
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "url": row["url"],
            "title": row["title"],
            "published_at": row["publish_time"],
            "source": row["source"] or row["channel"],
            "channel": row["channel"],
            "content": row["content"],
        }
        for row in rows
    ]


def channel_stats(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT channel, COUNT(*) AS n, MIN(publish_time) AS first_time, "
            "MAX(publish_time) AS last_time FROM articles GROUP BY channel ORDER BY n DESC"
        ).fetchall()
    return [
        {
            "channel": row["channel"],
            "count": int(row["n"] or 0),
            "first_time": row["first_time"],
            "last_time": row["last_time"],
        }
        for row in rows
    ]


def stats(db_path: Path = DB_PATH) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(publish_time) AS first_time, MAX(publish_time) AS last_time FROM articles"
        ).fetchone()
    return {
        "count": int(row["n"] or 0),
        "first_time": row["first_time"],
        "last_time": row["last_time"],
        "db_path": str(db_path),
    }
