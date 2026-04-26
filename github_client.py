import os
import re
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from typing import List
from bs4 import BeautifulSoup

TOKEN = os.getenv("GITHUB_TOKEN", "")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "StarPulse/1.0",
}

PERIOD_DAYS = {
    "now": 1,
    "today": 0,
    "yesterday": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}

# Keyword search in name+description — far wider coverage than sparse topic tags
CATEGORY_KEYWORDS = {
    "all": [],
    "llm": ["llm", "langchain", "ollama", "openai", "claude", "gemini", "mistral", "vllm"],
    "agents": ["ai agent", "autogpt", "autonomous agent", "multi-agent", "agentic"],
    "video": ["video generation", "text-to-video", "video diffusion", "sora", "video ai"],
    "image": ["stable-diffusion", "diffusion model", "comfyui", "image generation", "flux"],
    "audio": ["text-to-speech", "voice clone", "whisper", "music generation", "tts"],
    "memory": ["rag", "vector database", "knowledge graph", "memory agent", "embeddings"],
    "cyber": ["cybersecurity", "pentest", "ctf", "vulnerability", "exploit", "malware"],
    "qa": ["playwright", "selenium", "pytest", "cypress", "test automation", "e2e"],
    "graphics": ["3d rendering", "webgl", "vulkan", "shader", "blender", "opengl", "threejs"],
    "gtm": ["sales automation", "outreach", "lead generation", "marketing automation", "crm"],
    "softeng": ["developer tools", "code editor", "lsp", "devtools", "cli tool"],
    "data": ["data pipeline", "analytics", "etl", "data warehouse", "dbt", "airflow"],
    "web3": ["blockchain", "defi", "smart contract", "nft", "solidity", "web3"],
    "robotics": ["robotics", "ros", "robot arm", "autonomous vehicle", "drone"],
}

MIN_STARS = {
    "now": 5,
    "today": 5,
    "yesterday": 5,
    "week": 10,
    "month": 50,
    "year": 200,
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


def _period_filter(period: str) -> str:
    if period == "today":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return f"created:{today}"
    if period in ("now", "yesterday"):
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return f"created:{yesterday}..{today}"
    days = PERIOD_DAYS.get(period, 7)
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    return f"created:>{since}"


def search_repos(period: str = "week", category: str = "all", per_page: int = 25) -> List[Repo]:
    keywords = CATEGORY_KEYWORDS.get(category, [])
    date_q = _period_filter(period)
    min_stars = MIN_STARS.get(period, 10)

    if keywords:
        kw_q = " OR ".join(f'"{k}"' if " " in k else k for k in keywords[:4])
        q = f"({kw_q}) in:name,description {date_q} stars:>{min_stars}"
    else:
        q = f"{date_q} stars:>{min_stars}"

    try:
        resp = requests.get(
            "https://api.github.com/search/repositories",
            headers=HEADERS,
            params={"q": q, "sort": "stars", "order": "desc", "per_page": per_page},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"GitHub Search API error: {e}")
        return []

    return [
        Repo(
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
        for i in resp.json().get("items", [])
    ]


def scrape_trending(since: str = "daily") -> List[Repo]:
    """Scrape github.com/trending — velocity-based (star gains), all ages of repos."""
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
