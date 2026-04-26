import os
import time
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

from github_client import search_repos, scrape_trending
from gitlab_client import search_gitlab_repos
from perplexity_client import enrich_repos, trending_papers as pplx_papers
from arxiv_client import search_papers

app = FastAPI(title="StarPulse")

_cache: dict = {}
CACHE_TTL = int(os.getenv("CACHE_TTL", 600))
PPLX_TTL = int(os.getenv("PPLX_CACHE_TTL", 3600))

TRENDING_PERIOD_MAP = {"now": "daily", "today": "daily", "week": "weekly", "month": "monthly"}


def _get(key: str, ttl: int):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    return None


def _set(key: str, data):
    _cache[key] = (time.time(), data)


@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/trending")
def trending(
    period: str = Query("week"),
    category: str = Query("all"),
    limit: int = Query(25, le=50),
    platforms: str = Query("github,gitlab"),
):
    key = f"trending:{period}:{category}:{platforms}"
    cached = _get(key, CACHE_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

    use_github = "github" in platforms
    use_gitlab = "gitlab" in platforms

    repos = []

    # ── GitHub ──────────────────────────────────────────────
    if use_github:
        gh_repos = search_repos(period, category, per_page=min(limit, 25))
        repos.extend(gh_repos)

        # Add velocity-based trending for broad categories
        if category in ("all",) and period in TRENDING_PERIOD_MAP:
            scraped = scrape_trending(since=TRENDING_PERIOD_MAP[period])
            existing = {r.full_name for r in repos}
            for r in scraped:
                if r.full_name not in existing:
                    repos.append(r)

    # ── GitLab ──────────────────────────────────────────────
    if use_gitlab:
        gl_repos = search_gitlab_repos(period, category, per_page=10)
        existing = {r.full_name for r in repos}
        for r in gl_repos:
            if r.full_name not in existing:
                repos.append(r)

    # Sort by stars, assign rank
    repos.sort(key=lambda r: r.stars, reverse=True)
    repos = repos[:limit]
    for i, r in enumerate(repos):
        r.rank = i + 1

    # ── Perplexity enrichment (top 10, cached 1h) ───────────
    pplx_key = f"pplx:{period}:{category}"
    insight_map: dict = _get(pplx_key, PPLX_TTL) or {}

    to_enrich = [r for r in repos[:10] if r.full_name not in insight_map]
    if to_enrich:
        enriched = enrich_repos(to_enrich, category)
        for e in enriched:
            insight_map[e["full_name"]] = e.get("insight", "")
        _set(pplx_key, insight_map)

    result = []
    for r in repos:
        d = r.to_dict()
        d["insight"] = insight_map.get(r.full_name, "")
        result.append(d)

    _set(key, result)
    return {"data": result, "cached": False, "count": len(result)}


@app.get("/api/papers")
def papers(
    category: str = Query("all"),
    period: str = Query("week"),
):
    key = f"papers:{category}:{period}"
    cached = _get(key, PPLX_TTL)
    if cached:
        return {"data": cached, "cached": True, "count": len(cached)}

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
    return {"data": combined, "cached": False, "count": len(combined)}


@app.get("/api/cache/clear")
def clear_cache():
    _cache.clear()
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=False)
