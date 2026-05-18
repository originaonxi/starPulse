import os
import time
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

from db import (
    init_db, query_repos, search_repos_fts, query_papers,
    query_contributors, get_stats, DB_PATH,
)
from github_client import search_repos as gh_live, scrape_trending
from gitlab_client import search_gitlab_repos
from perplexity_client import enrich_repos, trending_papers as pplx_papers
from arxiv_client import search_papers

app = FastAPI(title="StarPulse")

# Init DB schema on startup (no-op if tables already exist)
init_db()

# Auto-bootstrap on first deploy if DB is empty and token is available
import threading

def _maybe_bootstrap():
    try:
        if get_stats()["total_repos"] == 0 and os.getenv("GITHUB_TOKEN"):
            print("[startup] Empty DB detected — running bootstrap in background...")
            import subprocess, sys
            subprocess.Popen([sys.executable, "collect.py", "--mode", "bootstrap"])
    except Exception as e:
        print(f"[startup] bootstrap check failed: {e}")

threading.Thread(target=_maybe_bootstrap, daemon=True).start()

_cache: dict = {}
CACHE_TTL = int(os.getenv("CACHE_TTL", 600))
PPLX_TTL  = int(os.getenv("PPLX_CACHE_TTL", 3600))

TRENDING_PERIOD_MAP = {"now": "daily", "today": "daily", "week": "weekly", "month": "monthly"}


def _get(key: str, ttl: int):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    return None


def _set(key: str, data):
    _cache[key] = (time.time(), data)


def _db_has_data() -> bool:
    try:
        return get_stats()["total_repos"] > 0
    except Exception:
        return False


def _sort_param(sort: str) -> str:
    return sort if sort in ("score", "stars", "delta7d", "delta30d", "forks") else "score"


@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/trending")
def trending(
    period: str   = Query("week"),
    category: str = Query("all"),
    limit: int    = Query(25, le=50),
    platforms: str = Query("github,gitlab"),
    sort: str     = Query("score"),
):
    key = f"trending:{period}:{category}:{platforms}:{sort}:{limit}"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached), "source": "cache"}

    sort_col = _sort_param(sort)

    # ── DB path (fast, owned library) ────────────────────────────────────────
    if _db_has_data():
        platform_filter = "all"
        if platforms == "github":
            platform_filter = "github"
        elif platforms == "gitlab":
            platform_filter = "gitlab"

        repos = query_repos(
            category=category,
            sort=sort_col,
            limit=limit,
            platform=platform_filter,
        )
        for i, r in enumerate(repos):
            r["rank"] = i + 1
            if "topics" in r and isinstance(r["topics"], str):
                import json
                r["topics"] = json.loads(r["topics"])
            if "categories" in r and isinstance(r["categories"], str):
                import json
                r["categories"] = json.loads(r["categories"])

        _set(key, repos)
        return {"data": repos, "cached": False, "count": len(repos), "source": "db"}

    # ── Live API fallback (before bootstrap runs) ─────────────────────────────
    use_github = "github" in platforms
    use_gitlab = "gitlab" in platforms
    repos_live = []

    if use_github:
        gh_repos = gh_live(period, category, per_page=min(limit, 25))
        repos_live.extend(gh_repos)
        if category == "all" and period in TRENDING_PERIOD_MAP:
            scraped = scrape_trending(since=TRENDING_PERIOD_MAP[period])
            existing = {r.full_name for r in repos_live}
            for r in scraped:
                if r.full_name not in existing:
                    repos_live.append(r)

    if use_gitlab:
        gl_repos = search_gitlab_repos(period, category, per_page=10)
        existing = {r.full_name for r in repos_live}
        for r in gl_repos:
            if r.full_name not in existing:
                repos_live.append(r)

    repos_live.sort(key=lambda r: r.stars, reverse=True)
    repos_live = repos_live[:limit]
    for i, r in enumerate(repos_live):
        r.rank = i + 1

    pplx_key = f"pplx:{period}:{category}"
    insight_map: dict = _get(pplx_key, PPLX_TTL) or {}
    to_enrich = [r for r in repos_live[:10] if r.full_name not in insight_map]
    if to_enrich:
        enriched = enrich_repos(to_enrich, category)
        for e in enriched:
            insight_map[e["full_name"]] = e.get("insight", "")
        _set(pplx_key, insight_map)

    result = []
    for r in repos_live:
        d = r.to_dict()
        d["insight"] = insight_map.get(r.full_name, "")
        result.append(d)

    _set(key, result)
    return {"data": result, "cached": False, "count": len(result), "source": "live"}


@app.get("/api/search")
def search(q: str = Query(..., min_length=2), limit: int = Query(25, le=50)):
    key = f"search:{q}:{limit}"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

    results = search_repos_fts(q, limit)
    import json
    for i, r in enumerate(results):
        r["rank"] = i + 1
        if isinstance(r.get("topics"), str):
            r["topics"] = json.loads(r["topics"])
        if isinstance(r.get("categories"), str):
            r["categories"] = json.loads(r["categories"])

    _set(key, results)
    return {"data": results, "cached": False, "count": len(results)}


@app.get("/api/breakouts")
def breakouts(limit: int = Query(25, le=50)):
    key = f"breakouts:{limit}"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

    results = query_repos(sort="score", limit=limit, breakout_only=True)
    import json
    for i, r in enumerate(results):
        r["rank"] = i + 1
        if isinstance(r.get("topics"), str):
            r["topics"] = json.loads(r["topics"])
        if isinstance(r.get("categories"), str):
            r["categories"] = json.loads(r["categories"])

    _set(key, results)
    return {"data": results, "cached": False, "count": len(results)}


@app.get("/api/contributors")
def contributors(limit: int = Query(50, le=200)):
    key = f"contributors:{limit}"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

    results = query_contributors(limit)
    _set(key, results)
    return {"data": results, "cached": False, "count": len(results)}


@app.get("/api/papers")
def papers(
    category: str = Query("all"),
    period: str   = Query("week"),
):
    key = f"papers:{category}:{period}"
    cached = _get(key, PPLX_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

    if _db_has_data():
        db_papers = query_papers(category, limit=12)
        if db_papers:
            _set(key, db_papers)
            return {"data": db_papers, "cached": False, "count": len(db_papers), "source": "db"}

    # Live fallback
    arxiv = search_papers(category, period, max_results=8)
    pplx = pplx_papers(category, period)

    seen: set = set()
    combined = []
    for p in arxiv + pplx:
        slug = p.get("title", "")[:40].lower()
        if slug and slug not in seen:
            seen.add(slug)
            combined.append(p)

    _set(key, combined)
    return {"data": combined, "cached": False, "count": len(combined), "source": "live"}


# Top 10 categories ranked by composite signal (total_stars × avg_score + velocity)
TOP10_CATS = [
    {"id": "softeng",    "label": "Dev Tools",        "icon": "⚙️"},
    {"id": "research",   "label": "AI Research",       "icon": "🔬"},
    {"id": "finance",    "label": "Finance / Trading",  "icon": "💰"},
    {"id": "codex",      "label": "OpenAI / Codex",    "icon": "🧩"},
    {"id": "cyber",      "label": "Cybersecurity",     "icon": "🔐"},
    {"id": "qa",         "label": "QA / Testing",      "icon": "🧪"},
    {"id": "data",       "label": "Data Engineering",  "icon": "📊"},
    {"id": "claude",     "label": "Claude / AI",       "icon": "🟣"},
    {"id": "animations", "label": "Animation / 3D",    "icon": "✨"},
    {"id": "memory",     "label": "RAG / Memory",      "icon": "🧠"},
]


def _why(r: dict) -> str:
    parts = []
    if r.get("insight"):
        return r["insight"]
    d7 = r.get("delta_7d") or 0
    if r.get("is_breakout") and d7:
        parts.append(f"🚀 Breakout — ▲{d7:,} stars this week")
    elif r.get("is_breakout"):
        parts.append("🚀 Breakout — unusual star velocity")
    if d7 > 500:
        parts.append(f"▲{d7:,}/wk momentum")
    forks = r.get("forks") or 0
    stars = r.get("stars") or 0
    if forks > 20000:
        parts.append(f"🍴 {forks:,} forks — devs build on it")
    elif forks > 5000:
        parts.append(f"🍴 {forks:,} forks")
    if stars > 100000:
        parts.append(f"⭐ {stars:,} — industry standard")
    elif stars > 50000:
        parts.append(f"⭐ {stars:,} — widely adopted")
    if not parts:
        parts.append(f"Score {r.get('score', 0):.1f}")
    return " · ".join(parts[:2])


@app.get("/api/top10")
def top10():
    key = "top10"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "cached": True}

    import sqlite3, json as _json
    result = []

    for cat in TOP10_CATS:
        cid = cat["id"]

        # Pull top 10 by composite score for this category
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT * FROM repos
               WHERE categories LIKE ?
               ORDER BY score DESC, stars DESC
               LIMIT 10""",
            (f'%"{cid}"%',),
        ).fetchall()

        # Category-level signal
        meta = con.execute(
            """SELECT COUNT(*) as cnt, SUM(stars) as ts, ROUND(AVG(score),2) as as_,
                      SUM(delta_7d) as vel, MAX(stars) as top_s
               FROM repos WHERE categories LIKE ?""",
            (f'%"{cid}"%',),
        ).fetchone()
        con.close()

        repos = []
        for rank, row in enumerate(rows, 1):
            r = dict(row)
            # Parse JSON fields
            for f in ("topics", "categories"):
                if isinstance(r.get(f), str):
                    try:
                        r[f] = _json.loads(r[f])
                    except Exception:
                        r[f] = []
            r["rank"] = rank
            r["why"] = _why(r)
            repos.append(r)

        result.append({
            "id":          cid,
            "label":       cat["label"],
            "icon":        cat["icon"],
            "total_repos": meta["cnt"] if meta else 0,
            "total_stars": meta["ts"] if meta else 0,
            "avg_score":   meta["as_"] if meta else 0,
            "velocity_7d": meta["vel"] if meta else 0,
            "repos":       repos,
        })

    _set(key, result)
    return {"data": result, "cached": False}


@app.get("/api/snapshot-dates")
def snapshot_dates():
    import sqlite3
    con = sqlite3.connect(str(DB_PATH))
    rows = con.execute(
        "SELECT DISTINCT snap_date, COUNT(*) as cnt FROM snapshots GROUP BY snap_date ORDER BY snap_date DESC"
    ).fetchall()
    con.close()
    return {"dates": [{"date": r[0], "count": r[1]} for r in rows]}


@app.get("/api/top-by-date")
def top_by_date(
    date: str      = Query(...),
    limit: int     = Query(100, le=500),
    platform: str  = Query("github"),
):
    import sqlite3, json as _json

    key = f"topbydate:{date}:{limit}:{platform}"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "date": date, "count": len(cached), "cached": True}

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    if platform == "gitlab":
        # GitLab has real snapshots — compute velocity from them
        rows = con.execute(
            """
            SELECT r.full_name, r.platform, r.name, r.description, r.url, r.language,
                   r.topics, r.categories, r.owner_avatar, r.forks, r.open_issues,
                   r.insight, r.pushed_at,
                   s1.stars,
                   COALESCE(s1.stars - s7.stars, 0) AS delta_7d
            FROM snapshots s1
            JOIN repos r ON r.full_name = s1.full_name
            LEFT JOIN snapshots s7
                   ON s7.full_name = s1.full_name
                  AND s7.snap_date = (
                        SELECT MAX(snap_date) FROM snapshots
                         WHERE full_name = s1.full_name
                           AND snap_date <= date(?, '-7 days')
                      )
            WHERE s1.snap_date = ?
            """,
            (date, date),
        ).fetchall()
    else:
        # GitHub repos: pull from repos table directly (stored delta_7d/score)
        # Filter to repos updated on or before the requested date
        rows = con.execute(
            """
            SELECT full_name, platform, name, description, url, language,
                   topics, categories, owner_avatar, stars, forks, open_issues,
                   insight, pushed_at, delta_7d, score, is_breakout
            FROM repos
            WHERE platform = 'github'
              AND date(last_updated) <= ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (date, limit),
        ).fetchall()

    con.close()

    results = []
    for row in rows:
        r = dict(row)
        for f in ("topics", "categories"):
            if isinstance(r.get(f), str):
                try:
                    r[f] = _json.loads(r[f])
                except Exception:
                    r[f] = []
        results.append(r)

    if platform == "gitlab":
        import math
        for r in results:
            stars  = max(r.get("stars") or 1, 1)
            forks  = max(r.get("forks") or 1, 1)
            issues = max(r.get("open_issues") or 1, 1)
            delta  = r.get("delta_7d") or 0
            r["score"] = round(
                math.log10(stars) * 1.0
                + math.log10(forks) * 0.5
                + math.log10(issues) * 0.25
                + (delta / stars) * 5.0,
                4,
            )
            r["is_breakout"] = bool(delta > 0 and (delta / max(stars - delta, 1)) > 0.15)
        results.sort(key=lambda x: x["score"], reverse=True)
        results = results[:limit]

    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["why"] = _why(r)

    _set(key, results)
    return {"data": results, "date": date, "count": len(results), "cached": False, "platform": platform}


FRONTIER_ORGS = {
    "openai","anthropics","google-deepmind","facebookresearch","mistralai",
    "deepseek-ai","xai-org","huggingface","EleutherAI","allenai","stabilityai",
    "microsoft","google","apple","cohere-ai","HKUDS","NousResearch",
}


def _rocket_signals(r: dict) -> tuple[float, list[str]]:
    """
    Score a repo like a quant scores a stock.
    Returns (rocket_score 0-100, [signal_tags]).

    Signals:
      BREAKOUT   — >15% star growth this week (breaks resistance level)
      MOMENTUM   — >500 stars/wk sustained (trending channel up)
      VELOCITY   — delta_7d/stars > 5% (high relative velocity)
      BUILDER    — forks/stars > 12% (devs building on it = real value)
      SWEET_SPOT — 1K-30K stars (massive upside headroom)
      EARLY      — created < 12 months ago (still in price discovery)
      FRONTIER   — backed by top lab (institutional quality signal)
      ACTIVE     — pushed within 7 days (not dead)
    """
    import math
    from datetime import datetime, timezone

    stars  = max(r.get("stars") or 0, 1)
    forks  = r.get("forks") or 0
    d7     = r.get("delta_7d") or 0
    score  = 0.0
    sigs: list[str] = []

    # Velocity as % of base (like % move on a stock)
    vel_pct = d7 / stars
    if vel_pct > 0.15:
        score += 30; sigs.append("BREAKOUT")
    elif vel_pct > 0.05:
        score += 20; sigs.append("VELOCITY")
    elif vel_pct > 0.02:
        score += 10; sigs.append("MOMENTUM")

    # Raw weekly velocity
    if d7 > 2000:
        score += 15; sigs.append("HIGH_VOL") if "HIGH_VOL" not in sigs else None
    elif d7 > 500 and "MOMENTUM" not in sigs:
        score += 8; sigs.append("MOMENTUM")

    # Fork ratio — builders > watchers
    fork_ratio = forks / stars
    if fork_ratio > 0.20:
        score += 20; sigs.append("BUILDER")
    elif fork_ratio > 0.12:
        score += 12; sigs.append("BUILDER")
    elif fork_ratio > 0.06:
        score += 6

    # Sweet spot: 1K-30K stars = huge upside
    if 1_000 <= stars <= 30_000:
        score += 15; sigs.append("SWEET_SPOT")
    elif 30_001 <= stars <= 80_000:
        score += 8

    # Frontier lab backing
    owner = (r.get("full_name") or "").split("/")[0]
    if owner in FRONTIER_ORGS:
        score += 10; sigs.append("FRONTIER")

    # Recent activity (dev is active = company is alive)
    pushed = (r.get("pushed_at") or "")[:10]
    if pushed:
        try:
            days_ago = (datetime.now(timezone.utc) - datetime.fromisoformat(pushed + "T00:00:00+00:00")).days
            if days_ago <= 7:
                score += 10; sigs.append("ACTIVE")
            elif days_ago <= 30:
                score += 5
        except Exception:
            pass

    return round(score, 1), sigs


@app.get("/api/rockets")
def rockets(
    min_stars: int = Query(500,  ge=0),
    max_stars: int = Query(80_000, le=500_000),
    limit:     int = Query(50, le=200),
):
    """
    Stock-pattern repo detector.
    Finds AI repos that look like they will 5-10x — high velocity,
    builder signal, sweet-spot size, frontier-lab backing.
    """
    key = f"rockets:{min_stars}:{max_stars}:{limit}"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

    import sqlite3, json as _json

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT full_name, platform, name, description, url, language,
               topics, categories, owner_avatar, stars, forks, open_issues,
               delta_7d, delta_30d, score, is_breakout, insight,
               pushed_at, created_at, last_updated
        FROM repos
        WHERE platform = 'github'
          AND stars BETWEEN ? AND ?
          AND (
              delta_7d > 50
              OR is_breakout = 1
              OR CAST(forks AS REAL) / MAX(stars, 1) > 0.06
          )
        ORDER BY score DESC
        LIMIT 500
        """,
        (min_stars, max_stars),
    ).fetchall()
    con.close()

    results = []
    for row in rows:
        r = dict(row)
        for f in ("topics", "categories"):
            if isinstance(r.get(f), str):
                try:
                    r[f] = _json.loads(r[f])
                except Exception:
                    r[f] = []

        rscore, signals = _rocket_signals(r)
        if rscore < 10:
            continue
        r["rocket_score"] = rscore
        r["signals"]      = signals
        r["why"]          = _why(r)
        results.append(r)

    results.sort(key=lambda x: x["rocket_score"], reverse=True)
    results = results[:limit]
    for i, r in enumerate(results):
        r["rank"] = i + 1

    _set(key, results)
    return {"data": results, "cached": False, "count": len(results)}


@app.get("/api/realtime")
def realtime(limit: int = Query(60, le=200)):
    """Top AI repos ranked by star gain in the last hour (from realtime_snapshots)."""
    key = f"realtime:{limit}"
    cached = _get(key, 60)  # 1-minute cache
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

    import sqlite3, json as _json

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    # Check table exists
    has_rt = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='realtime_snapshots'"
    ).fetchone()
    if not has_rt:
        con.close()
        return {"data": [], "cached": False, "count": 0, "status": "no_data"}

    rows = con.execute(
        """
        SELECT  r1.full_name,
                r1.stars,
                r1.ts                                    AS last_seen,
                COALESCE(r1.stars - r2.stars, 0)         AS delta_1h,
                COALESCE(r1.stars - r6.stars, 0)         AS delta_6h,
                COALESCE(r1.stars - r24.stars, 0)        AS delta_24h,
                repos.description,  repos.language,
                repos.url,          repos.owner_avatar,
                repos.forks,        repos.score,
                repos.is_breakout,  repos.categories,
                repos.delta_7d
        FROM realtime_snapshots r1
        LEFT JOIN realtime_snapshots r2
               ON r2.full_name = r1.full_name
              AND r2.ts = (SELECT MAX(ts) FROM realtime_snapshots
                            WHERE full_name = r1.full_name
                              AND ts < datetime('now','-55 minutes'))
        LEFT JOIN realtime_snapshots r6
               ON r6.full_name = r1.full_name
              AND r6.ts = (SELECT MAX(ts) FROM realtime_snapshots
                            WHERE full_name = r1.full_name
                              AND ts < datetime('now','-6 hours'))
        LEFT JOIN realtime_snapshots r24
               ON r24.full_name = r1.full_name
              AND r24.ts = (SELECT MAX(ts) FROM realtime_snapshots
                             WHERE full_name = r1.full_name
                               AND ts < datetime('now','-24 hours'))
        JOIN repos ON repos.full_name = r1.full_name
        WHERE r1.ts = (SELECT MAX(ts) FROM realtime_snapshots WHERE full_name = r1.full_name)
        ORDER BY delta_1h DESC, r1.stars DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    con.close()

    results = []
    for i, row in enumerate(rows):
        r = dict(row)
        r["rank"] = i + 1
        if isinstance(r.get("categories"), str):
            try:
                r["categories"] = _json.loads(r["categories"])
            except Exception:
                r["categories"] = []
        r["why"] = _why(r)
        results.append(r)

    _set(key, results)
    updated = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {"data": results, "cached": False, "count": len(results), "updated_at": updated}


@app.get("/api/stats")
def stats():
    try:
        s = get_stats()
        s["db_path"] = str(DB_PATH)
        s["db_exists"] = DB_PATH.exists()
        return s
    except Exception as e:
        return {"error": str(e), "db_exists": DB_PATH.exists()}


@app.get("/api/cache/clear")
def clear_cache():
    _cache.clear()
    return {"status": "ok"}


@app.get("/api/admin/collect")
def admin_collect(secret: str = Query(...), mode: str = Query("daily")):
    """Trigger collection from GitLab CI scheduled pipeline or manual call."""
    expected = os.getenv("ADMIN_SECRET", "")
    if not expected or secret != expected:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    if mode not in ("daily", "bootstrap", "papers"):
        return JSONResponse({"error": "invalid mode"}, status_code=400)

    import subprocess, sys
    proc = subprocess.Popen(
        [sys.executable, "collect.py", "--mode", mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    _cache.clear()
    return {"status": "started", "mode": mode, "pid": proc.pid}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=False)
