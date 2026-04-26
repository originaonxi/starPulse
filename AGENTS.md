# StarPulse — Agent Architecture

## Overview

StarPulse runs 4 autonomous agents on a daily schedule. They are chained: Collector → Scorer → Enricher → Breakout Detector. All state flows through `starpulse.db`.

---

## Agent 1: Collector (`collect.py --mode daily`)

**Trigger:** 3:00 AM daily (macOS launchd) or GitLab CI schedule (0 3 * * *)

**Inputs:** GitHub Search API, GitHub trending scrape, GitLab API

**What it does:**
1. For each of 23 categories, fetches top repos via GitHub topic: search
2. Calls `github_client.scrape_trending()` for breakout velocity from GitHub trending page
3. Calls `gitlab_client.search_gitlab_repos()` for GitLab repos
4. Calls `db.upsert_repo()` for every repo — inserts or updates stars/forks/issues

**Categories collected (23):**
ai, llm, ml, robotics, web3, defi, video, creative, saas, developer-tools, devops, mobile, security, game-dev, education, productivity, research, data, nlp, agent, voice, vision, rag

**GitHub Search API limits:**
- 30 req/min authenticated
- No OR operator in topic: queries — each topic is a separate request
- Use `per_page=50` to maximize per-request yield

**Bootstrap mode (`--mode bootstrap`):**
- Seeds all 23 categories at 50 repos each (~1,150 initial repos)
- Auto-triggered on server startup if DB is empty and GITHUB_TOKEN is set

---

## Agent 2: Velocity Scorer (`collect.py` — runs after collection)

**Inputs:** `repos` table (current stars), `snapshots` table (historical stars)

**What it does:**
1. Calls `db.record_snapshot()` to save today's star count
2. Calls `db.get_stars_on()` to look up stars from 7 days ago
3. Computes `delta_7d = current_stars - stars_7d_ago`
4. Calls `score.compute_score(stars, forks, issues, delta_7d)` → writes `score` column
5. Calls `score.is_breakout(stars, delta_7d)` → writes `is_breakout` column (1/0)

**Breakout threshold:** >15% star growth in 7 days relative to prior star count

**Score formula:**
```
score = log10(stars)×1.0 + log10(forks)×0.5 + log10(issues)×0.25 + (delta_7d/stars)×5.0
```

---

## Agent 3: Enricher (`perplexity_client.enrich_repos`)

**Trigger:** Runs after Scorer, for top 20 repos by score

**Inputs:** Repo name, description, language, stars

**What it does:**
1. Calls Perplexity `sonar-pro` with a structured prompt asking WHY this repo is trending
2. Returns 1-sentence insight per repo
3. Writes to `repos.insight` column

**Prompt pattern:**
```
"Why is {full_name} trending on GitHub right now? 
Stars: {stars:,}. Description: {description}. 
One sentence, factual, no fluff."
```

**Cost control:** Only enriches repos NOT already in insight_map (cached per session, PPLX_TTL=3600s)

---

## Agent 4: Breakout Detector (embedded in `collect.py`)

**Inputs:** GitHub trending page (daily/weekly/monthly)

**What it does:**
1. Scrapes `github.com/trending?since=weekly` via BeautifulSoup
2. Any repo listed there gets `is_breakout=1`
3. Stars gained on trending page captured as `stars_gained` → used as velocity proxy on first bootstrap when snapshot history is empty

**Why this exists:** On first bootstrap, all `delta_7d=0` because there are no 7-day-ago snapshots yet. The trending page provides velocity signal immediately.

---

## Agent 5: Papers Agent (`collect.py --mode papers`)

**Trigger:** Manual or separate schedule

**Sources:**
- ArXiv API: `arxiv_client.search_papers(category, period, max_results=8)`
- Perplexity: `perplexity_client.trending_papers(category, period)` — surfaces papers Perplexity knows are discussed on social/HN

**Dedup:** By title slug (first 40 chars, lowercased)

**Storage:** `papers` table — title, url, summary, category, source

---

## Server Serving Layer (`server.py`)

### /api/top10

The main endpoint. Returns 10 categories × 10 repos each.

**Topic ranking:** `(total_stars × avg_score) + (velocity_7d × 1000)`
- velocity × 1000 ensures breakout categories (Finance with ▲10k/wk) rank above flat mega-categories

**WHY generation:** `_why(r)` — priority chain: Perplexity insight → breakout badge → forks → stars → score fallback

### /api/trending

Filterable by period, category, platform, sort column, limit.

**DB-first:** Serves from SQLite when data exists. Falls back to live GitHub/GitLab API only if DB is empty (pre-bootstrap).

### /api/search

Full-text search via FTS5 across repo name + description + topics.

### /api/breakouts

Repos with `is_breakout=1`, sorted by score.

### /api/admin/collect?secret=&mode=

Protected endpoint for CI to trigger collection. Returns PID of spawned subprocess.

---

## Cache Strategy

In-memory dict `_cache` with TTL:
- Most endpoints: `CACHE_TTL=600s` (10 min)
- Perplexity insights: `PPLX_CACHE_TTL=3600s` (1 hr)
- Cache key always includes all query params (including `limit`) to prevent stale-limit bugs

Clear cache: `GET /api/cache/clear`

---

## FTS5 Auto-Indexing

`db.py` creates 3 SQLite triggers:
- `repos_ai` (after insert) → insert into `repos_fts`
- `repos_au` (after update) → delete + insert into `repos_fts`
- `repos_ad` (after delete) → delete from `repos_fts`

This keeps FTS index always in sync without manual calls.

---

## Deployment Checklist

1. Set env vars: `GITHUB_TOKEN`, `ADMIN_SECRET`, optionally `GITLAB_TOKEN`, `PERPLEXITY_API_KEY`
2. Run `python collect.py --mode bootstrap` once to seed DB (or let server auto-bootstrap on startup)
3. Set up Railway volume at `/data`, set `DB_PATH=/data/starpulse.db`
4. Configure GitLab CI schedule: `0 3 * * *` with `CI_PIPELINE_SOURCE = schedule`
5. Set `RAILWAY_PUBLIC_URL` and `ADMIN_SECRET` as GitLab CI/CD variables
6. Get valid Railway token from railway.app → Account Settings → Tokens (long string, not UUID)
