import os
import json
import requests
from typing import List, Dict

PPLX_KEY = os.getenv("PERPLEXITY_API_KEY", "")
PPLX_URL = "https://api.perplexity.ai/chat/completions"
PPLX_HEADERS = {
    "Authorization": f"Bearer {PPLX_KEY}",
    "Content-Type": "application/json",
}

RECENCY_MAP = {
    "yesterday": "day",
    "today": "day",
    "week": "week",
    "month": "month",
    "year": "year",
}

CATEGORY_LABELS = {
    "llm": "large language models, AI frameworks, and NLP",
    "video": "AI video generation and computer vision",
    "cyber": "cybersecurity and penetration testing",
    "graphics": "3D graphics, rendering, and visual computing",
    "qa": "software testing, QA automation, and test generation",
    "memory": "AI memory systems, RAG, and vector databases",
    "gtm": "AI-powered sales, marketing automation, and GTM",
    "softeng": "developer tools, IDEs, and software engineering",
    "all": "software development and artificial intelligence",
}


def _call(messages: List[Dict], recency: str = "week", model: str = "sonar-pro") -> str:
    payload = {
        "model": model,
        "messages": messages,
        "search_recency_filter": recency,
        "max_tokens": 1500,
        "temperature": 0.2,
    }
    try:
        resp = requests.post(PPLX_URL, headers=PPLX_HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Perplexity API error: {e}")
        return "[]"


def _extract_json(content: str) -> list:
    start = content.find("[")
    end = content.rfind("]") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(content[start:end])
        except json.JSONDecodeError:
            pass
    return []


def enrich_repos(repos: List, category: str = "all") -> List[Dict]:
    """Batch-enrich repos with 'why trending' insight using real-time web search."""
    if not repos:
        return []

    repo_list = "\n".join(
        f"{i+1}. {r.full_name} — {r.description or 'No description'}"
        for i, r in enumerate(repos[:10])
    )

    prompt = f"""You are a GitHub trend analyst with access to real-time web data. For each GitHub repository below, write exactly 1 sentence (max 18 words) explaining WHY it is trending RIGHT NOW — cite a specific recent event, viral post, release, or community buzz that caused the spike.

Repositories:
{repo_list}

Respond ONLY with a valid JSON array, no markdown, no explanation:
[{{"full_name": "owner/repo", "insight": "..."}}]"""

    recency = "week"
    content = _call([{"role": "user", "content": prompt}], recency=recency)
    insights = _extract_json(content)
    insights_map = {item.get("full_name", ""): item.get("insight", "") for item in insights}

    result = []
    for r in repos[:10]:
        d = r.to_dict()
        d["insight"] = insights_map.get(r.full_name, "")
        result.append(d)

    return result


def trending_papers(category: str = "all", period: str = "week") -> List[Dict]:
    """Find trending research papers using Perplexity's live web search."""
    cat_desc = CATEGORY_LABELS.get(category, "AI/ML")
    period_desc = {"yesterday": "yesterday", "today": "today", "week": "this week", "month": "this month", "year": "this year"}.get(period, "this week")
    recency = RECENCY_MAP.get(period, "week")

    prompt = f"""What are the top 5 most viral or widely-discussed research papers in {cat_desc} published or gaining attention {period_desc}? Look for papers trending on Twitter/X, HuggingFace, ArXiv, or mentioned in major AI newsletters and blogs.

Return ONLY valid JSON array (no markdown, no explanation):
[{{"title": "Full paper title", "arxiv_id": "XXXX.XXXXX or empty string", "authors": "First Author et al.", "why_notable": "1 sentence on why researchers are excited", "url": "full URL to arxiv or paper"}}]"""

    content = _call([{"role": "user", "content": prompt}], recency=recency)
    papers = _extract_json(content)
    return [{"source": "perplexity", **p} for p in papers if isinstance(p, dict)]
