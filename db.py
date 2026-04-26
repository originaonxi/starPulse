"""SQLite database layer for StarPulse library."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

DB_PATH = Path(__file__).parent / "starpulse.db"


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS repos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name   TEXT    NOT NULL UNIQUE,
            platform    TEXT    NOT NULL DEFAULT 'github',
            name        TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            url         TEXT    NOT NULL,
            language    TEXT    DEFAULT '',
            topics      TEXT    DEFAULT '[]',
            categories  TEXT    DEFAULT '[]',
            owner_avatar TEXT   DEFAULT '',
            stars       INTEGER DEFAULT 0,
            forks       INTEGER DEFAULT 0,
            open_issues INTEGER DEFAULT 0,
            delta_1d    INTEGER DEFAULT 0,
            delta_7d    INTEGER DEFAULT 0,
            delta_30d   INTEGER DEFAULT 0,
            score       REAL    DEFAULT 0,
            is_breakout INTEGER DEFAULT 0,
            insight     TEXT    DEFAULT '',
            pushed_at   TEXT    DEFAULT '',
            created_at  TEXT    DEFAULT '',
            last_updated TEXT   DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            snap_date TEXT NOT NULL,
            stars     INTEGER NOT NULL,
            UNIQUE(full_name, snap_date)
        );

        CREATE TABLE IF NOT EXISTS papers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            arxiv_id     TEXT UNIQUE,
            source       TEXT    DEFAULT 'arxiv',
            title        TEXT    NOT NULL,
            authors      TEXT    DEFAULT '',
            abstract     TEXT    DEFAULT '',
            categories   TEXT    DEFAULT '[]',
            why_notable  TEXT    DEFAULT '',
            url          TEXT    NOT NULL,
            published_at TEXT    DEFAULT '',
            last_updated TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS contributors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner       TEXT NOT NULL UNIQUE,
            total_stars INTEGER DEFAULT 0,
            repo_count  INTEGER DEFAULT 0,
            top_repo    TEXT    DEFAULT '',
            last_updated TEXT   DEFAULT ''
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS repos_fts USING fts5(
            full_name,
            description,
            topics,
            categories,
            content='repos',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS repos_ai AFTER INSERT ON repos BEGIN
            INSERT INTO repos_fts(rowid, full_name, description, topics, categories)
            VALUES (new.id, new.full_name, new.description, new.topics, new.categories);
        END;

        CREATE TRIGGER IF NOT EXISTS repos_ad AFTER DELETE ON repos BEGIN
            INSERT INTO repos_fts(repos_fts, rowid, full_name, description, topics, categories)
            VALUES ('delete', old.id, old.full_name, old.description, old.topics, old.categories);
        END;

        CREATE TRIGGER IF NOT EXISTS repos_au AFTER UPDATE ON repos BEGIN
            INSERT INTO repos_fts(repos_fts, rowid, full_name, description, topics, categories)
            VALUES ('delete', old.id, old.full_name, old.description, old.topics, old.categories);
            INSERT INTO repos_fts(rowid, full_name, description, topics, categories)
            VALUES (new.id, new.full_name, new.description, new.topics, new.categories);
        END;

        CREATE INDEX IF NOT EXISTS idx_repos_score    ON repos(score DESC);
        CREATE INDEX IF NOT EXISTS idx_repos_stars    ON repos(stars DESC);
        CREATE INDEX IF NOT EXISTS idx_repos_delta7d  ON repos(delta_7d DESC);
        CREATE INDEX IF NOT EXISTS idx_repos_breakout ON repos(is_breakout DESC, score DESC);
        CREATE INDEX IF NOT EXISTS idx_snaps_date     ON snapshots(snap_date, full_name);
        CREATE INDEX IF NOT EXISTS idx_papers_cat     ON papers(categories);
        """)


def upsert_repo(r: dict):
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute("""
        INSERT INTO repos
            (full_name, platform, name, description, url, language, topics,
             categories, owner_avatar, stars, forks, open_issues,
             delta_1d, delta_7d, delta_30d, score, is_breakout,
             insight, pushed_at, created_at, last_updated)
        VALUES
            (:full_name, :platform, :name, :description, :url, :language, :topics,
             :categories, :owner_avatar, :stars, :forks, :open_issues,
             :delta_1d, :delta_7d, :delta_30d, :score, :is_breakout,
             :insight, :pushed_at, :created_at, :now)
        ON CONFLICT(full_name) DO UPDATE SET
            stars       = excluded.stars,
            forks       = excluded.forks,
            open_issues = excluded.open_issues,
            delta_1d    = excluded.delta_1d,
            delta_7d    = excluded.delta_7d,
            delta_30d   = excluded.delta_30d,
            score       = excluded.score,
            is_breakout = excluded.is_breakout,
            insight     = CASE WHEN excluded.insight != '' THEN excluded.insight ELSE repos.insight END,
            pushed_at   = excluded.pushed_at,
            language    = excluded.language,
            topics      = excluded.topics,
            categories  = excluded.categories,
            last_updated = excluded.last_updated
        """, {**r, "now": now})


def record_snapshot(full_name: str, stars: int, snap_date: Optional[str] = None):
    d = snap_date or date.today().isoformat()
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO snapshots (full_name, snap_date, stars) VALUES (?, ?, ?)",
            (full_name, d, stars),
        )


def get_stars_on(full_name: str, days_ago: int) -> Optional[int]:
    target = (datetime.utcnow().date().__class__.fromordinal(
        datetime.utcnow().toordinal() - days_ago
    )).isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT stars FROM snapshots WHERE full_name=? AND snap_date<=? ORDER BY snap_date DESC LIMIT 1",
            (full_name, target),
        ).fetchone()
    return row["stars"] if row else None


def upsert_paper(p: dict):
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute("""
        INSERT INTO papers
            (arxiv_id, source, title, authors, abstract, categories, why_notable, url, published_at, last_updated)
        VALUES
            (:arxiv_id, :source, :title, :authors, :abstract, :categories, :why_notable, :url, :published_at, :now)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            why_notable  = CASE WHEN excluded.why_notable != '' THEN excluded.why_notable ELSE papers.why_notable END,
            last_updated = excluded.last_updated
        """, {**p, "now": now})


def rebuild_contributors():
    with _conn() as con:
        con.execute("DELETE FROM contributors")
        rows = con.execute("""
            SELECT
                substr(full_name, 1, instr(full_name, '/')-1) AS owner,
                SUM(stars) AS total_stars,
                COUNT(*) AS repo_count,
                full_name AS top_repo
            FROM repos
            GROUP BY owner
            ORDER BY total_stars DESC
            LIMIT 500
        """).fetchall()
        now = datetime.utcnow().isoformat()
        for row in rows:
            con.execute("""
                INSERT INTO contributors (owner, total_stars, repo_count, top_repo, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (row["owner"], row["total_stars"], row["repo_count"], row["top_repo"], now))


# ── Query helpers ────────────────────────────────────────────────────────────

def query_repos(
    category: str = "all",
    sort: str = "score",
    limit: int = 25,
    offset: int = 0,
    platform: str = "all",
    breakout_only: bool = False,
) -> List[dict]:
    clauses = []
    params: list = []

    if category != "all":
        clauses.append("categories LIKE ?")
        params.append(f'%"{category}"%')

    if platform != "all":
        clauses.append("platform = ?")
        params.append(platform)

    if breakout_only:
        clauses.append("is_breakout = 1")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sort_col = {
        "score":  "score DESC",
        "stars":  "stars DESC",
        "delta7d": "delta_7d DESC",
        "delta30d": "delta_30d DESC",
        "forks":  "forks DESC",
    }.get(sort, "score DESC")

    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM repos {where} ORDER BY {sort_col} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows]


def search_repos_fts(q: str, limit: int = 25) -> List[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT r.* FROM repos_fts f
               JOIN repos r ON r.id = f.rowid
               WHERE repos_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def query_papers(category: str = "all", limit: int = 10) -> List[dict]:
    with _conn() as con:
        if category == "all":
            rows = con.execute(
                "SELECT * FROM papers ORDER BY published_at DESC, last_updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM papers WHERE categories LIKE ? ORDER BY published_at DESC LIMIT ?",
                (f'%{category}%', limit),
            ).fetchall()
    return [dict(r) for r in rows]


def query_contributors(limit: int = 50) -> List[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM contributors ORDER BY total_stars DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    with _conn() as con:
        total_repos = con.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        total_papers = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        breakouts = con.execute("SELECT COUNT(*) FROM repos WHERE is_breakout=1").fetchone()[0]
        last_updated = con.execute(
            "SELECT MAX(last_updated) FROM repos"
        ).fetchone()[0] or ""
    return {
        "total_repos": total_repos,
        "total_papers": total_papers,
        "breakouts": breakouts,
        "last_updated": last_updated,
    }
