import os
import requests
from datetime import datetime, timedelta
from typing import List
from github_client import Repo

GL_TOKEN = os.getenv("GITLAB_TOKEN", "")
GL_HEADERS = {"PRIVATE-TOKEN": GL_TOKEN, "User-Agent": "StarPulse/1.0"}
GL_BASE = "https://gitlab.com/api/v4"

CATEGORY_SEARCH = {
    "all": [""],
    "llm": ["llm", "langchain", "ollama"],
    "agents": ["ai-agent", "autonomous-agent", "autogpt"],
    "video": ["video-generation", "text-to-video"],
    "image": ["stable-diffusion", "diffusion", "image-generation"],
    "audio": ["tts", "voice-clone", "whisper"],
    "memory": ["rag", "vector-database", "embeddings"],
    "cyber": ["pentest", "ctf", "vulnerability"],
    "qa": ["playwright", "selenium", "pytest", "test-automation"],
    "graphics": ["rendering", "webgl", "vulkan", "shader"],
    "gtm": ["sales-automation", "crm", "outreach"],
    "softeng": ["developer-tools", "cli-tool", "ide"],
    "data": ["data-pipeline", "analytics", "etl"],
    "web3": ["blockchain", "defi", "smart-contract"],
    "robotics": ["robotics", "ros", "autonomous-vehicle"],
}

PERIOD_DAYS = {
    "now": 1, "today": 1, "yesterday": 1,
    "week": 7, "month": 30, "year": 365,
}


def search_gitlab_repos(period: str = "week", category: str = "all", per_page: int = 10) -> List[Repo]:
    days = PERIOD_DAYS.get(period, 7)
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    terms = CATEGORY_SEARCH.get(category, [""])
    repos: List[Repo] = []
    seen: set = set()

    for term in terms[:2]:  # max 2 searches per category to stay within rate limits
        params = {
            "order_by": "star_count",
            "sort": "desc",
            "visibility": "public",
            "archived": "false",
            "per_page": per_page,
            "last_activity_after": since,
        }
        if term:
            params["search"] = term

        try:
            resp = requests.get(f"{GL_BASE}/projects", headers=GL_HEADERS, params=params, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"GitLab API error ({term}): {e}")
            continue

        for item in resp.json():
            gid = item.get("id")
            if gid in seen:
                continue
            seen.add(gid)

            ns = item.get("namespace", {})
            owner = ns.get("full_path") or ns.get("name", "")
            name = item.get("path") or item.get("name", "")
            full_name = f"{owner}/{name}"
            web_url = item.get("web_url") or f"https://gitlab.com/{full_name}"

            avatar = item.get("avatar_url") or ns.get("avatar_url") or ""

            repos.append(Repo(
                name=name,
                full_name=full_name,
                description=(item.get("description") or "")[:200],
                url=web_url,
                stars=item.get("star_count", 0),
                forks=item.get("forks_count", 0),
                language="",
                topics=item.get("topics", [])[:6],
                created_at=item.get("created_at", ""),
                owner_avatar=avatar,
                category=category,
                platform="gitlab",
            ))

    return repos
