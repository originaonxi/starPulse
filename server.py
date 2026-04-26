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
    """Generate a one-line human explanation for why this repo ranks here."""
    parts = []
    if r.get("insight"):
        return r["insight"]
    if r.get("is_breakout"):
        parts.append(f"🚀 Breakout — ▲{r['delta_7d']:,} stars this week")
    if r.get("delta_7d", 0) > 500:
        parts.append(f"▲{r['delta_7d']:,}/wk momentum")
    if r.get("forks", 0) > 20000:
        parts.append(f"🍴 {r['forks']:,} forks — devs build on it")
    elif r.get("forks", 0) > 5000:
        parts.append(f"🍴 {r['forks']:,} forks")
    if r.get("stars", 0) > 100000:
        parts.append(f"⭐ {r['stars']:,} stars — industry standard")
    elif r.get("stars", 0) > 50000:
        parts.append(f"⭐ {r['stars']:,} stars — widely adopted")
    if not parts:
        parts.append(f"Top-ranked in category by score {r.get('score', 0):.1f}")
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
