#!/usr/bin/env python3
"""
StarPulse Real-Time AI Tracker
Runs every 5 minutes via launchd.
Sources: GitHub Trending scrape + GitHub Search API (AI topics + frontier labs).
Records star counts into realtime_snapshots table; detects breakouts live.
"""
import os, re, sys, time, json, sqlite3, requests
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

TOKEN   = os.getenv("GITHUB_TOKEN", "")
DB_PATH = Path(__file__).parent / "starpulse.db"

GH_H = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "StarPulse/2.0",
}

# AI-focused GitHub Search topics (rotate to stay within rate limits)
AI_TOPICS = [
    "llm", "large-language-models", "machine-learning",
    "ai-agent", "deep-learning", "transformers",
    "rag", "generative-ai", "foundation-models",
]

# Frontier labs — always track these orgs
FRONTIER_ORGS = [
    ("openai",           "OpenAI"),
    ("anthropics",       "Anthropic"),
    ("google-deepmind",  "DeepMind"),
    ("facebookresearch", "Meta AI"),
    ("mistralai",        "Mistral"),
    ("deepseek-ai",      "DeepSeek"),
    ("xai-org",          "xAI"),
    ("huggingface",      "HuggingFace"),
    ("EleutherAI",       "EleutherAI"),
    ("allenai",          "AllenAI"),
    ("stabilityai",      "StabilityAI"),
    ("cohere-ai",        "Cohere"),
]

# Keywords that mark a repo as AI-relevant (for trending filter)
AI_KEYWORDS = {
    "llm", "gpt", "claude", "gemini", "mistral", "deepseek", "llama",
    "transformer", "neural", "diffusion", "embedding", "rag", "agent",
    "inference", "fine-tun", "langchain", "openai", "anthropic",
    "machine learning", "deep learning", "ai ", " ai", "model",
    "whisper", "stable diffusion", "midjourney", "multimodal",
}


# ── DB helpers ──────────────────────────────────────────────────────────────

def init_db(con: sqlite3.Connection):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS realtime_snapshots (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT    NOT NULL,
            stars     INTEGER NOT NULL,
            ts        TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rt_fn_ts
            ON realtime_snapshots (full_name, ts);
    """)
    con.commit()


def record_snapshots(con: sqlite3.Connection, repos: list[dict]):
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    rows = [(r["full_name"], r.get("stars", 0), ts) for r in repos]
    con.executemany(
        "INSERT INTO realtime_snapshots (full_name, stars, ts) VALUES (?,?,?)", rows
    )
    con.commit()
    return len(rows)


def upsert_repos(con: sqlite3.Connection, repos: list[dict]):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for r in repos:
        fn    = r["full_name"]
        owner = fn.split("/")[0]
        row   = con.execute("SELECT stars FROM repos WHERE full_name=?", (fn,)).fetchone()
        if row:
            old   = row[0] or 0
            new   = r.get("stars", old)
            delta = max(new - old, 0)
            con.execute(
                "UPDATE repos SET stars=?, delta_7d=delta_7d+?, last_updated=?, "
                "description=COALESCE(NULLIF(?,''),description) WHERE full_name=?",
                (new, delta, ts, r.get("description", ""), fn),
            )
        else:
            cats = json.dumps(["research"])
            desc = (r.get("description") or "").lower()
            if any(k in desc for k in ("llm","language model","gpt","claude","gemini","rag")):
                cats = json.dumps(["research","codex"])
            con.execute(
                """INSERT OR IGNORE INTO repos
                   (full_name,platform,name,description,url,language,categories,topics,
                    owner_avatar,stars,forks,open_issues,delta_7d,score,is_breakout,last_updated)
                   VALUES (?,?,?,?,?,?,?,'[]',?,?,?,0,0,0,0,?)""",
                (fn, "github", fn.split("/")[-1],
                 r.get("description",""),
                 r.get("url", f"https://github.com/{fn}"),
                 r.get("language",""), cats,
                 r.get("owner_avatar", f"https://github.com/{owner}.png"),
                 r.get("stars",0), r.get("forks",0), ts),
            )
    con.commit()


def cleanup(con: sqlite3.Connection, keep_hours: int = 48):
    con.execute(
        f"DELETE FROM realtime_snapshots WHERE ts < datetime('now','-{keep_hours} hours')"
    )
    con.commit()


# ── Collectors ───────────────────────────────────────────────────────────────

def scrape_trending() -> list[dict]:
    """Scrape GitHub Trending for AI repos (daily since)."""
    urls = [
        "https://github.com/trending?since=daily",
        "https://github.com/trending/python?since=daily",
        "https://github.com/trending/jupyter-notebook?since=daily",
    ]
    seen, results = set(), []
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    for url in urls:
        try:
            resp = requests.get(url, headers={"User-Agent": ua}, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for art in soup.select("article.Box-row"):
                h2 = art.select_one("h2 a")
                if not h2:
                    continue
                fn = h2["href"].strip("/")
                if fn in seen:
                    continue

                desc_el = art.select_one("p")
                desc    = desc_el.get_text(strip=True) if desc_el else ""
                lang_el = art.select_one("[itemprop='programmingLanguage']")
                lang    = lang_el.get_text(strip=True) if lang_el else ""

                # Filter: only keep AI-relevant repos
                haystack = (fn + " " + desc + " " + lang).lower()
                if not any(kw in haystack for kw in AI_KEYWORDS):
                    continue

                stars_el = art.select_one("a[href$='/stargazers']")
                stars_raw = re.sub(r"[^\d]", "", (stars_el.get_text(strip=True) if stars_el else "0") or "0")
                stars = int(stars_raw) if stars_raw else 0

                gain_el  = art.select_one("span.d-inline-block.float-sm-right")
                gain_raw = re.sub(r"[^\d]", "", (gain_el.get_text(strip=True) if gain_el else "0") or "0")
                gain = int(gain_raw) if gain_raw else 0

                seen.add(fn)
                results.append({
                    "full_name":    fn,
                    "stars":        stars,
                    "stars_today":  gain,
                    "description":  desc,
                    "language":     lang,
                    "url":          f"https://github.com/{fn}",
                    "owner_avatar": f"https://github.com/{fn.split('/')[0]}.png",
                    "source":       "trending",
                })
            time.sleep(1)
        except Exception as exc:
            print(f"  [trending] error {url}: {exc}")

    return results


def gh_search(q: str, n: int = 30) -> list[dict]:
    if not TOKEN:
        return []
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": q, "sort": "updated", "order": "desc", "per_page": n},
            headers=GH_H, timeout=15,
        )
        items = r.json().get("items", [])
        return [{
            "full_name":    i["full_name"],
            "stars":        i["stargazers_count"],
            "forks":        i.get("forks_count", 0),
            "description":  i.get("description", ""),
            "language":     i.get("language", ""),
            "url":          i.get("html_url", ""),
            "owner_avatar": i.get("owner", {}).get("avatar_url", ""),
            "source":       "api",
        } for i in items]
    except Exception as exc:
        print(f"  [gh_search] error '{q}': {exc}")
        return []


def gh_org(org: str, n: int = 5) -> list[dict]:
    return gh_search(f"org:{org} stars:>100", n)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    t0  = time.time()
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] StarPulse real-time collect starting…")

    con = sqlite3.connect(str(DB_PATH))
    init_db(con)

    bucket: dict[str, dict] = {}

    # 1. GitHub Trending
    print("  [1/3] Scraping GitHub Trending (AI filter)…")
    for r in scrape_trending():
        bucket[r["full_name"]] = r
    print(f"        {len(bucket)} AI repos from trending")
    time.sleep(1)

    # 2. API topic searches (4 topics per run, rotate each call)
    print("  [2/3] GitHub API — AI topics…")
    # Pick 4 topics based on minute of hour to rotate through all 9
    minute = datetime.now().minute
    topics = AI_TOPICS[minute % len(AI_TOPICS):minute % len(AI_TOPICS) + 4]
    for topic in topics:
        for r in gh_search(f"topic:{topic} stars:>200", 25):
            bucket.setdefault(r["full_name"], r)
        time.sleep(0.6)
    print(f"        {len(bucket)} unique repos after topics")

    # 3. Frontier lab orgs
    print("  [3/3] Frontier lab orgs…")
    for org, label in FRONTIER_ORGS:
        for r in gh_org(org, 5):
            r["lab"] = label
            bucket.setdefault(r["full_name"], r)
        time.sleep(0.5)

    repos = list(bucket.values())
    print(f"\n  Total unique AI repos this run: {len(repos)}")

    n_snap = record_snapshots(con, repos)
    print(f"  Snapshots recorded: {n_snap}")

    upsert_repos(con, repos)
    cleanup(con)

    # Top movers (delta vs ~60 min ago)
    movers = con.execute("""
        SELECT r1.full_name, r1.stars,
               COALESCE(r1.stars - r2.stars, 0) AS delta_1h
        FROM realtime_snapshots r1
        LEFT JOIN realtime_snapshots r2
            ON r2.full_name = r1.full_name
           AND r2.ts = (
                SELECT MAX(ts) FROM realtime_snapshots
                 WHERE full_name = r1.full_name
                   AND ts < datetime('now','-55 minutes')
               )
        WHERE r1.ts = (SELECT MAX(ts) FROM realtime_snapshots WHERE full_name = r1.full_name)
          AND COALESCE(r1.stars - r2.stars, 0) > 0
        ORDER BY delta_1h DESC
        LIMIT 10
    """).fetchall()

    if movers:
        print("\n  🔥 Top movers right now:")
        for m in movers:
            print(f"     {m[0]:<55} ⭐{m[1]:,}  ▲{m[2]:,}/hr")

    con.close()
    elapsed = round(time.time() - t0, 1)
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Done in {elapsed}s")


if __name__ == "__main__":
    main()
