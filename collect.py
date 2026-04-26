"""
StarPulse collector — populates and maintains the local repo library.

Usage:
    python3 collect.py --mode bootstrap   # first-run seed (~1000+ repos)
    python3 collect.py --mode daily       # incremental update (run via cron)
    python3 collect.py --mode papers      # papers only
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from db import (
    init_db, upsert_repo, record_snapshot, get_stars_on,
    upsert_paper, rebuild_contributors, get_stats,
)
from score import compute_score, is_breakout
from github_client import _search as gh_search
from gitlab_client import search_gitlab_repos
from arxiv_client import search_papers
from perplexity_client import enrich_repos as pplx_enrich, trending_papers as pplx_papers

# ── Expanded 25+ categories ───────────────────────────────────────────────────
CATEGORY_TOPICS = {
    # AI / ML
    "llm":          ["llm", "large-language-model"],
    "agents":       ["ai-agent", "llm-agent"],
    "video":        ["video-generation", "text-to-video"],
    "image":        ["stable-diffusion", "image-generation"],
    "audio":        ["text-to-speech", "voice-cloning"],
    "memory":       ["rag", "vector-database"],
    "open-models":  ["open-source-llm", "llama"],
    # AI Ecosystems
    "claude":       ["claude-ai", "anthropic"],
    "mcp":          ["model-context-protocol", "mcp-server"],
    "codex":        ["openai-codex", "github-copilot"],
    "gemini":       ["google-gemini", "gemini-api"],
    # Engineering
    "cyber":        ["cybersecurity", "security-tools"],
    "qa":           ["playwright", "test-automation"],
    "softeng":      ["developer-tools", "cli"],
    "data":         ["data-engineering", "analytics"],
    # Creative
    "graphics":     ["3d", "webgl"],
    "animations":   ["animation", "motion-graphics"],
    # GTM / Business
    "gtm":          ["crm", "sales-automation"],
    "marketing":    ["seo", "marketing-automation"],
    "finance":      ["algorithmic-trading", "fintech"],
    # Other
    "web3":         ["blockchain", "defi"],
    "robotics":     ["robotics", "ros"],
    "research":     ["research", "academic"],
}

# arXiv subject areas per category
ARXIV_CATS = {
    "llm": "cs.CL", "agents": "cs.AI", "video": "cs.CV", "image": "cs.CV",
    "audio": "eess.AS", "memory": "cs.IR", "open-models": "cs.LG",
    "cyber": "cs.CR", "qa": "cs.SE", "softeng": "cs.SE",
    "data": "cs.DB", "graphics": "cs.GR", "finance": "q-fin.TR",
    "robotics": "cs.RO",
}


def _sleep(secs: float):
    """Polite sleep between API calls."""
    time.sleep(secs)


def _gh_items_to_db_rows(items: list, categories: list) -> list:
    rows = []
    for i in items:
        rows.append({
            "full_name":   i["full_name"],
            "platform":    "github",
            "name":        i["name"],
            "description": (i.get("description") or "")[:300],
            "url":         i["html_url"],
            "language":    i.get("language") or "",
            "topics":      json.dumps(i.get("topics", [])[:8]),
            "categories":  json.dumps(categories),
            "owner_avatar": i["owner"]["avatar_url"],
            "stars":       i["stargazers_count"],
            "forks":       i["forks_count"],
            "open_issues": i.get("open_issues_count", 0),
            "delta_1d":    0,
            "delta_7d":    0,
            "delta_30d":   0,
            "score":       compute_score(
                               i["stargazers_count"],
                               i["forks_count"],
                               i.get("open_issues_count", 0),
                               0,
                           ),
            "is_breakout": 0,
            "insight":     "",
            "pushed_at":   i.get("pushed_at", ""),
            "created_at":  i.get("created_at", ""),
        })
    return rows


def collect_github_category(category: str, per_topic: int = 100) -> list:
    topics = CATEGORY_TOPICS.get(category, [])
    if not topics:
        return []

    seen: dict = {}
    for topic in topics:
        q = f"topic:{topic} stars:>50"
        items = gh_search(q, per_page=per_topic)
        for item in items:
            fn = item["full_name"]
            if fn not in seen or item["stargazers_count"] > seen[fn]["stargazers_count"]:
                seen[fn] = item
        _sleep(2.5)  # stay inside 30 req/min

    return _gh_items_to_db_rows(list(seen.values()), [category])


def collect_gitlab_category(category: str, per_page: int = 25) -> list:
    from dataclasses import asdict
    gl_repos = search_gitlab_repos("month", category, per_page=per_page)
    rows = []
    for r in gl_repos:
        rows.append({
            "full_name":   r.full_name,
            "platform":    "gitlab",
            "name":        r.name,
            "description": r.description[:300],
            "url":         r.url,
            "language":    r.language,
            "topics":      json.dumps(r.topics),
            "categories":  json.dumps([category]),
            "owner_avatar": r.owner_avatar,
            "stars":       r.stars,
            "forks":       r.forks,
            "open_issues": 0,
            "delta_1d":    0,
            "delta_7d":    0,
            "delta_30d":   0,
            "score":       compute_score(r.stars, r.forks, 0, 0),
            "is_breakout": 0,
            "insight":     "",
            "pushed_at":   "",
            "created_at":  "",
        })
    return rows


def apply_velocity(rows: list) -> list:
    """Fill delta_1d / delta_7d / delta_30d from snapshot history."""
    updated = []
    for r in rows:
        fn = r["full_name"]
        stars_now = r["stars"]

        s1 = get_stars_on(fn, 1)
        s7 = get_stars_on(fn, 7)
        s30 = get_stars_on(fn, 30)

        d1  = max(stars_now - s1,  0) if s1  is not None else 0
        d7  = max(stars_now - s7,  0) if s7  is not None else 0
        d30 = max(stars_now - s30, 0) if s30 is not None else 0

        r["delta_1d"]  = d1
        r["delta_7d"]  = d7
        r["delta_30d"] = d30
        r["score"]     = compute_score(stars_now, r["forks"], r["open_issues"], d7)
        r["is_breakout"] = int(is_breakout(stars_now, d7))
        updated.append(r)
    return updated


def enrich_with_insights(rows: list, category: str) -> list:
    """Batch-enrich top repos with Perplexity insights (max 20)."""
    from github_client import Repo
    top = rows[:20]
    fake_repos = []
    for r in top:
        fake_repos.append(Repo(
            name=r["name"],
            full_name=r["full_name"],
            description=r["description"],
            url=r["url"],
            stars=r["stars"],
            forks=r["forks"],
            language=r["language"],
            topics=json.loads(r.get("topics", "[]")),
            created_at=r.get("created_at", ""),
            owner_avatar=r.get("owner_avatar", ""),
            category=category,
        ))
    try:
        enriched = pplx_enrich(fake_repos, category)
        insight_map = {e["full_name"]: e.get("insight", "") for e in enriched}
    except Exception as ex:
        print(f"  [warn] Perplexity enrichment failed: {ex}")
        insight_map = {}

    for r in rows:
        if r["full_name"] in insight_map:
            r["insight"] = insight_map[r["full_name"]]
    return rows


def collect_papers(category: str):
    # arXiv
    arxiv_cat = ARXIV_CATS.get(category, "cs.AI")
    papers = search_papers(category, "month", max_results=10)
    for p in papers:
        upsert_paper({
            "arxiv_id":    p.get("arxiv_id", p.get("url", "").split("/")[-1]),
            "source":      "arxiv",
            "title":       p.get("title", ""),
            "authors":     p.get("authors", ""),
            "abstract":    p.get("summary", "")[:500],
            "categories":  json.dumps([category]),
            "why_notable": "",
            "url":         p.get("url", ""),
            "published_at": p.get("published", ""),
        })

    # Perplexity papers
    try:
        ppapers = pplx_papers(category, "month")
        for p in ppapers:
            arxiv_id = p.get("arxiv_id") or p.get("url", "").split("/")[-1] or p.get("title", "")[:30]
            upsert_paper({
                "arxiv_id":    arxiv_id,
                "source":      "perplexity",
                "title":       p.get("title", ""),
                "authors":     p.get("authors", ""),
                "abstract":    "",
                "categories":  json.dumps([category]),
                "why_notable": p.get("why_notable", ""),
                "url":         p.get("url", ""),
                "published_at": "",
            })
    except Exception as ex:
        print(f"  [warn] Perplexity papers failed for {category}: {ex}")


# ── Modes ─────────────────────────────────────────────────────────────────────

def run_bootstrap():
    """Seed the full library: ~100 repos per category × 25 categories."""
    print(f"[bootstrap] Starting at {datetime.utcnow().isoformat()}")
    init_db()
    today = datetime.utcnow().date().isoformat()

    total_inserted = 0
    categories = list(CATEGORY_TOPICS.keys())

    for cat in categories:
        print(f"  [{cat}] fetching GitHub…", end=" ", flush=True)
        rows = collect_github_category(cat, per_topic=50)
        rows = enrich_with_insights(rows, cat)

        for r in rows:
            upsert_repo(r)
            record_snapshot(r["full_name"], r["stars"], today)
        total_inserted += len(rows)
        print(f"{len(rows)} repos")

        # GitLab
        print(f"  [{cat}] fetching GitLab…", end=" ", flush=True)
        gl_rows = collect_gitlab_category(cat, per_page=20)
        for r in gl_rows:
            upsert_repo(r)
            record_snapshot(r["full_name"], r["stars"], today)
        total_inserted += len(gl_rows)
        print(f"{len(gl_rows)} repos")

        # Papers
        collect_papers(cat)

        _sleep(3)

    rebuild_contributors()
    stats = get_stats()
    print(f"\n[bootstrap] Done. {stats['total_repos']} repos, {stats['total_papers']} papers in DB.")


def run_daily():
    """Incremental daily update: re-fetch each category, record snapshots, compute velocity."""
    print(f"[daily] Starting at {datetime.utcnow().isoformat()}")
    init_db()
    today = datetime.utcnow().date().isoformat()

    categories = list(CATEGORY_TOPICS.keys())
    total_updated = 0

    for cat in categories:
        print(f"  [{cat}]", end=" ", flush=True)

        rows = collect_github_category(cat, per_topic=30)
        gl_rows = collect_gitlab_category(cat, per_page=10)
        all_rows = rows + gl_rows

        # Record today's snapshot first, then compute velocity
        for r in all_rows:
            record_snapshot(r["full_name"], r["stars"], today)

        all_rows = apply_velocity(all_rows)

        # Only enrich repos without insight yet (save Perplexity quota)
        no_insight = [r for r in all_rows[:10] if not r.get("insight")]
        if no_insight:
            no_insight = enrich_with_insights(no_insight, cat)
            insight_map = {r["full_name"]: r["insight"] for r in no_insight}
            for r in all_rows:
                if r["full_name"] in insight_map:
                    r["insight"] = insight_map[r["full_name"]]

        for r in all_rows:
            upsert_repo(r)
        total_updated += len(all_rows)

        print(f"{len(all_rows)} updated ({sum(1 for r in all_rows if r['is_breakout'])} breakouts)")

        collect_papers(cat)
        _sleep(3)

    rebuild_contributors()
    stats = get_stats()
    print(f"\n[daily] Done. {stats['total_repos']} repos total, {stats['breakouts']} breakouts.")


def run_papers_only():
    init_db()
    for cat in CATEGORY_TOPICS:
        print(f"  [{cat}] papers…")
        collect_papers(cat)
        _sleep(1)
    print("Papers update complete.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["bootstrap", "daily", "papers"], default="daily")
    args = parser.parse_args()

    if args.mode == "bootstrap":
        run_bootstrap()
    elif args.mode == "daily":
        run_daily()
    elif args.mode == "papers":
        run_papers_only()
