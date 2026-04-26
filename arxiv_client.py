import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Dict

ARXIV_API = "http://export.arxiv.org/api/query"

CATEGORY_ARXIV = {
    "llm": "cs.CL",
    "video": "cs.CV",
    "cyber": "cs.CR",
    "graphics": "cs.GR",
    "qa": "cs.SE",
    "memory": "cs.IR",
    "gtm": "cs.AI",
    "softeng": "cs.SE",
    "all": "cs.AI",
    "papers": "cs.AI",
}

PERIOD_DAYS = {
    "yesterday": 1,
    "today": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def search_papers(category: str = "all", period: str = "week", max_results: int = 10) -> List[Dict]:
    arxiv_cat = CATEGORY_ARXIV.get(category, "cs.AI")
    days = PERIOD_DAYS.get(period, 7)
    cutoff = datetime.utcnow() - timedelta(days=days)

    try:
        resp = requests.get(
            ARXIV_API,
            params={
                "search_query": f"cat:{arxiv_cat}",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": max_results * 2,  # fetch extra to filter by date
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"arXiv API error: {e}")
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return []

    papers = []
    for entry in root.findall("a:entry", ns):
        published_el = entry.find("a:published", ns)
        if published_el is None:
            continue

        try:
            published = datetime.fromisoformat(published_el.text.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue

        if published < cutoff:
            continue

        title_el = entry.find("a:title", ns)
        summary_el = entry.find("a:summary", ns)
        id_el = entry.find("a:id", ns)

        title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
        summary = (summary_el.text or "").strip().replace("\n", " ") if summary_el is not None else ""
        paper_url = (id_el.text or "").strip() if id_el is not None else ""
        arxiv_id = paper_url.split("/abs/")[-1] if "/abs/" in paper_url else ""

        authors = []
        for author in entry.findall("a:author", ns):
            name_el = author.find("a:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text)

        author_str = ", ".join(authors[:3])
        if len(authors) > 3:
            author_str += " et al."

        papers.append({
            "source": "arxiv",
            "title": title,
            "arxiv_id": arxiv_id,
            "authors": author_str,
            "summary": summary[:300] + ("..." if len(summary) > 300 else ""),
            "why_notable": "",
            "url": paper_url,
            "published": published_el.text,
        })

        if len(papers) >= max_results:
            break

    return papers
