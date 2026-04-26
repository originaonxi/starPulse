import os
import re
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import List, Optional
from bs4 import BeautifulSoup

TOKEN = os.getenv("GITHUB_TOKEN", "")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "StarPulse/1.0",
}
SEARCH_URL = "https://api.github.com/search/repositories"

# ---------- Category → GitHub topic tags ----------
# Use exactly 2 topics per category to minimise API calls (rate limit: 30/min).
# OR is broken for topic: qualifiers — we call each topic separately and merge.
CATEGORY_TOPICS = {
    "llm":      ["llm", "large-language-model"],
    "agents":   ["ai-agent", "llm-agent"],
    "video":    ["video-generation", "text-to-video"],
    "image":    ["stable-diffusion", "image-generation"],
    "audio":    ["text-to-speech", "voice-cloning"],
    "memory":   ["rag", "vector-database"],
    "cyber":    ["cybersecurity", "security-tools"],   # avoid topic:hacking (403)
    "qa":       ["playwright", "test-automation"],
    "graphics": ["3d", "webgl"],
    "gtm":      ["crm", "sales-automation"],
    "softeng":  ["developer-tools", "cli"],
    "data":     ["data-engineering", "analytics"],
    "web3":     ["blockchain", "defi"],
    "robotics": ["robotics", "ros"],
}

# pushed: window per period — wide enough to catch actively maintained repos
PUSHED_DAYS = {
    "now": 3, "today": 7, "yesterday": 7,
    "week": 30, "month": 90, "year": 400,
}

# Min stars — generous so we always get results
CAT_MIN_STARS = {
    "now": 50, "today": 50, "yesterday": 50,
    "week": 50, "month": 100, "year": 500,
}

# Min stars for "all" trending (new repos gaining stars fast)
ALL_MIN_STARS = {
    "now": 5, "today": 5, "yesterday": 5,
    "week": 10, "month": 50, "year": 200,
}


@dataclass
class Repo:
    name: str
    full_name: str
    description: str
    url: str
    stars: int
    forks: int
    language: str
    topics: List[str]
    created_at: str
    owner_avatar: str
    category: str = "all"
    insight: str = ""
    stars_gained: int = 0
    platform: str = "github"
    rank: int = 0

    def to_dict(self):
        return asdict(self)


def _gh_item_to_repo(i: dict, category: str) -> Repo:
    return Repo(
        name=i["name"],
        full_name=i["full_name"],
        description=(i.get("description") or "")[:200],
        url=i["html_url"],
        stars=i["stargazers_count"],
        forks=i["forks_count"],
        language=i.get("language") or "",
        topics=i.get("topics", [])[:6],
        created_at=i["created_at"],
        owner_avatar=i["owner"]["avatar_url"],
        category=category,
        platform="github",
    )


def _search(q: str, per_page: int = 25) -> List[dict]:
    try:
        resp = requests.get(
            SEARCH_URL,
            headers=HEADERS,
            params={"q": q, "sort": "stars", "order": "desc", "per_page": per_page},
            timeout=15,
        )
        if resp.status_code == 422:
            return []  # invalid query, not an error
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception as e:
        print(f"GitHub Search error [{q[:60]}]: {e}")
        return []


def _period_date_filter(period: str) -> str:
    """Return 'created:' date filter for All-trending mode."""
    if period == "today":
        return f"created:{datetime.utcnow().strftime('%Y-%m-%d')}"
    if period in ("now", "yesterday"):
        d1 = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = datetime.utcnow().strftime("%Y-%m-%d")
        return f"created:{d1}..{d2}"
    days = PUSHED_DAYS.get(period, 7)
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    return f"created:>{since}"


def search_repos(period: str = "week", category: str = "all", per_page: int = 25) -> List[Repo]:
    """
    Category mode  → topic-based search, pushed: filter, sort by stars.
    All mode       → created: filter (newly trending), sort by stars.
    OR is broken for topic: qualifiers so we query each topic separately.
    """
    topics = CATEGORY_TOPICS.get(category)

    if not topics:
        # "All" trending: brand-new repos gaining stars
        min_s = ALL_MIN_STARS.get(period, 10)
        date_q = _period_date_filter(period)
        items = _search(f"{date_q} stars:>{min_s}", per_page)
        return [_gh_item_to_repo(i, category) for i in items]

    # Category: top repos sorted by stars, recently pushed
    days = PUSHED_DAYS.get(period, 14)
    min_s = CAT_MIN_STARS.get(period, 100)
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    seen: dict = {}  # full_name → item
    # Request enough per topic so merged result covers per_page after dedup
    fetch_per_topic = max(per_page, 15)

    for topic in topics:
        q = f"topic:{topic} pushed:>{since} stars:>{min_s}"
        for item in _search(q, fetch_per_topic):
            fn = item["full_name"]
            if fn not in seen or item["stargazers_count"] > seen[fn]["stargazers_count"]:
                seen[fn] = item

    # Fallback: widen pushed window if not enough results
    if len(seen) < 8:
        wider = (datetime.utcnow() - timedelta(days=max(days * 4, 180))).strftime("%Y-%m-%d")
        for topic in topics[:1]:  # only primary topic in fallback (save rate limit)
            q = f"topic:{topic} pushed:>{wider} stars:>{min_s}"
            for item in _search(q, fetch_per_topic):
                fn = item["full_name"]
                if fn not in seen:
                    seen[fn] = item

    sorted_items = sorted(seen.values(), key=lambda x: x["stargazers_count"], reverse=True)
    return [_gh_item_to_repo(i, category) for i in sorted_items[:per_page]]


def scrape_trending(since: str = "daily") -> List[Repo]:
    """Scrape github.com/trending for velocity-based trending (star gains)."""
    try:
        resp = requests.get(
            "https://github.com/trending",
            params={"since": since},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        repos = []

        for article in soup.select("article.Box-row"):
            link = article.select_one("h2 a")
            if not link:
                continue
            full_name = link.get("href", "").strip("/")
            if full_name.count("/") != 1:
                continue

            desc_el = article.select_one("p")
            lang_el = article.select_one("[itemprop='programmingLanguage']")
            avatar_el = article.select_one("img.avatar")
            gained_el = article.select_one(".float-sm-right")

            stars = 0
            for a in article.select("a.Link--muted"):
                if "stargazers" in a.get("href", ""):
                    try:
                        stars = int(a.get_text(strip=True).replace(",", ""))
                    except Exception:
                        pass
                    break

            stars_gained = 0
            if gained_el:
                nums = re.findall(r"[\d,]+", gained_el.get_text())
                if nums:
                    try:
                        stars_gained = int(nums[0].replace(",", ""))
                    except Exception:
                        pass

            repos.append(Repo(
                name=full_name.split("/")[1],
                full_name=full_name,
                description=(desc_el.get_text(strip=True) if desc_el else "")[:200],
                url=f"https://github.com/{full_name}",
                stars=stars,
                forks=0,
                language=(lang_el.get_text(strip=True) if lang_el else ""),
                topics=[],
                created_at="",
                owner_avatar=(avatar_el.get("src", "") if avatar_el else ""),
                category="trending",
                stars_gained=stars_gained,
                platform="github",
            ))

        return repos

    except Exception as e:
        print(f"GitHub trending scrape failed: {e}")
        return []
