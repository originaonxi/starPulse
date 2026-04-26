# StarPulse — Claude Context

## What This Is
TradingView-style GitHub/GitLab trending intelligence library. SQLite-backed, self-updating, deployed on Railway. Shows Top 10 categories × Top 10 repos each (100 repos total), with breakout detection, contributor rankings, and research papers.

## Architecture

```
collect.py ──► db.py (SQLite) ──► server.py (FastAPI) ──► static/index.html
     │                │
     ▼                ▼
github_client.py   score.py
gitlab_client.py   snapshots table (velocity)
perplexity_client.py
arxiv_client.py
```

## Scoring Formula (`score.py`)

```python
def compute_score(stars, forks, open_issues, delta_7d):
    base = (
        math.log10(max(stars, 1)) * 1.0
        + math.log10(max(forks, 1)) * 0.5
        + math.log10(max(open_issues, 1)) * 0.25
    )
    vel_bonus = (delta_7d / max(stars, 1)) * 5.0
    return round(base + vel_bonus, 4)

def is_breakout(stars, delta_7d):
    prior = max(stars - delta_7d, 1)
    return (delta_7d / prior) > 0.15   # >15% growth in 7d
```

## Topic Ranking Formula (used in /api/top10)

```sql
ORDER BY (total_stars * avg_score) + (velocity_7d * 1000) DESC
```

Velocity × 1000 gives breakout categories meaningful weight without overwhelming established ones.

## Top 10 Categories

| ID | Label | Icon |
|----|-------|------|
| softeng | Dev Tools | ⚙️ |
| research | AI Research | 🔬 |
| finance | Finance / Trading | 💰 |
| codex | OpenAI / Codex | 🧩 |
| cyber | Cybersecurity | 🔐 |
| qa | QA / Testing | 🧪 |
| data | Data Engineering | 📊 |
| claude | Claude / AI | 🟣 |
| animations | Animation / 3D | ✨ |
| memory | RAG / Memory | 🧠 |

## DB Schema (key tables)

- `repos` — full_name, platform, stars, forks, open_issues, delta_7d, delta_30d, score, is_breakout, categories (JSON), topics (JSON), insight
- `snapshots` — full_name, ts, stars (for velocity calc)
- `papers` — arxiv + Perplexity research papers
- `contributors` — top GitHub orgs/users by commit count
- `repos_fts` — FTS5 virtual table (auto-synced via triggers)

## Key Files

| File | Purpose |
|------|---------|
| `collect.py` | Data collection agent — bootstrap (23 cats × 50 repos) + daily incremental |
| `db.py` | SQLite init, upsert, query, FTS5 search |
| `score.py` | Scoring and breakout detection |
| `server.py` | FastAPI — /api/trending, /api/top10, /api/search, /api/breakouts, /api/contributors, /api/papers, /api/stats |
| `github_client.py` | GitHub Search API + trending scrape (BeautifulSoup) |
| `gitlab_client.py` | GitLab API search |
| `perplexity_client.py` | Perplexity sonar-pro for "why trending" insights |
| `arxiv_client.py` | ArXiv API for research papers |
| `static/index.html` | Full SPA — Top 10×10 default, search, breakouts, contributors, papers tabs |

## Environment Variables

```
GITHUB_TOKEN=         # Required — GitHub personal access token
GITLAB_TOKEN=         # Optional — GitLab PAT for gitlab.com API
PERPLEXITY_API_KEY=   # Optional — for WHY insights (sonar-pro model)
GOOGLE_API_KEY=        # Optional — reserved for future use
ADMIN_SECRET=          # Secret for /api/admin/collect endpoint
CACHE_TTL=600          # In-memory cache TTL seconds (default 600)
PPLX_CACHE_TTL=3600   # Perplexity cache TTL (default 3600)
PORT=8080
```

## Collection Modes

```bash
python collect.py --mode bootstrap   # One-time: 23 categories × 50 repos
python collect.py --mode daily       # Incremental: refresh stars, compute velocity, detect breakouts
python collect.py --mode papers      # Fetch ArXiv + Perplexity research papers
```

## Daily Cron (macOS launchd)

Plist at: `~/Library/LaunchAgents/io.starpulse.daily-collect.plist`
Runs: `collect.py --mode daily` at 3:00 AM daily

```bash
launchctl load ~/Library/LaunchAgents/io.starpulse.daily-collect.plist
```

## GitLab CI

`.gitlab-ci.yml` — 3 stages:
- `test`: import checks on every push
- `deploy`: Railway deploy on main (skips gracefully if RAILWAY_TOKEN not set)
- `daily-collect`: hits `/api/admin/collect` only when `CI_PIPELINE_SOURCE == "schedule"` (set in GitLab → Settings → CI/CD → Schedules → 0 3 * * *)

## Railway Deployment

- Config: `railway.toml`
- Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Health: `/api/stats`
- Volume: mount at `/data` for DB persistence (set `DB_PATH=/data/starpulse.db`)
- Token: generate at railway.app → Account Settings → Tokens (NOT a UUID — longer string)

## GitLab Repo

`gitlab.com/origin24/starPulse`

## DB Stats (as of 2026-04-26)

- 3,756+ repos across 23 categories
- Platforms: GitHub + GitLab
- Top categories by signal: Finance (FinceptTerminal breakout ▲10k/wk), AI Research, Dev Tools

## WHY Explanation Priority Chain

1. Perplexity insight (if available)
2. Breakout: "🚀 Breakout — ▲X stars this week"
3. Forks > 20k: "🍴 X forks — devs build on it"
4. Forks > 5k: "🍴 X forks"
5. Stars > 100k: "⭐ X stars — industry standard"
6. Stars > 50k: "⭐ X stars — widely adopted"
7. Fallback: "Top-ranked in category by score X"

## Known Issues / Gotchas

- SQLite "database is locked": never open two `sqlite3.connect()` on same DB simultaneously — always reuse a single connection or use `upsert_repo()` from db.py
- GitLab CI runners require credit card verification on free tier — use local launchd cron as alternative
- Control chars in repo descriptions (from GitLab) can break JSON — strip with `''.join(c for c in d if ord(c) >= 32 or c == '\t')`
- In-memory `_cache` doesn't persist across server restarts — Bootstrap must complete before cache fills with real data
- Railway token format: long string (NOT UUID format)
