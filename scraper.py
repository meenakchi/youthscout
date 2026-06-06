"""
Opportunity Finder - Main Scraper
Scrapes hackathons, leadership camps, youth programs, and similar opportunities.

Install deps: pip install requests beautifulsoup4 playwright anthropic schedule
Then run: python scraper.py
"""

import json
import time
import hashlib
import sqlite3
import os
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- CONFIG ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_API_KEY_HERE")
DB_PATH = "data/opportunities.db"
SEARCH_KEYWORDS = [
    "hackathon 2025 singapore students applications open",
    "youth leadership camp 2025 singapore",
    "leadership program students 2025 apply",
    "YMCA youth program 2025",
    "student competition 2025 singapore",
    "fellowship program undergraduates 2025",
    "bootcamp students free 2025 singapore",
    "internship program 2025 students apply",
]

# Add your own target sites here
TARGET_SITES = [
    {"name": "Devpost", "url": "https://devpost.com/hackathons?challenge_type=all&sort_by=Deadline"},
    {"name": "Major League Hacking", "url": "https://mlh.io/seasons/2025/events"},
    {"name": "Singapore Youth Council", "url": "https://www.nyc.gov.sg/en/opportunities"},
]


# --- DATABASE ---
def init_db():
    Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            url TEXT,
            description TEXT,
            deadline TEXT,
            eligibility TEXT,
            category TEXT,
            location TEXT,
            date_found TEXT,
            raw_text TEXT
        )
    """)
    conn.commit()
    return conn


def save_opportunity(conn, opp: dict):
    uid = hashlib.md5(opp["url"].encode()).hexdigest()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO opportunities
            (id, title, source, url, description, deadline, eligibility, category, location, date_found, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            uid,
            opp.get("title", "Untitled"),
            opp.get("source", "Unknown"),
            opp.get("url", ""),
            opp.get("description", ""),
            opp.get("deadline", ""),
            opp.get("eligibility", ""),
            opp.get("category", "Other"),
            opp.get("location", ""),
            datetime.now().isoformat(),
            opp.get("raw_text", ""),
        ))
        conn.commit()
        return uid
    except Exception as e:
        print(f"  DB error: {e}")
        return None


def load_all(conn) -> list:
    cur = conn.execute("SELECT * FROM opportunities ORDER BY date_found DESC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# --- SCRAPERS ---
def scrape_devpost(conn):
    """Scrape Devpost hackathon listings."""
    print("Scraping Devpost...")
    try:
        resp = requests.get(
            "https://devpost.com/hackathons?challenge_type=all&sort_by=Deadline",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".hackathon-tile")
        for card in cards[:20]:
            title_el = card.select_one("h3")
            link_el = card.select_one("a[href]")
            date_el = card.select_one(".submission-period")
            prize_el = card.select_one(".prizes")
            if not title_el or not link_el:
                continue
            opp = {
                "title": title_el.get_text(strip=True),
                "source": "Devpost",
                "url": link_el["href"] if link_el["href"].startswith("http") else "https://devpost.com" + link_el["href"],
                "description": prize_el.get_text(strip=True) if prize_el else "",
                "deadline": date_el.get_text(strip=True) if date_el else "",
                "category": "Hackathon",
                "raw_text": card.get_text(strip=True),
            }
            uid = save_opportunity(conn, opp)
            if uid:
                print(f"  + {opp['title'][:60]}")
    except Exception as e:
        print(f"  Devpost error: {e}")


def scrape_generic_site(conn, name: str, url: str):
    """Generic scraper — pulls all text, sends to AI to extract opportunities."""
    print(f"Scraping {name}...")
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove scripts/styles
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)[:6000]  # Limit for AI
        results = ai_extract_opportunities(text, name, url)
        for opp in results:
            opp["source"] = name
            uid = save_opportunity(conn, opp)
            if uid:
                print(f"  + {opp['title'][:60]}")
    except Exception as e:
        print(f"  {name} error: {e}")


def scrape_google_search(conn, query: str):
    """
    Uses SerpAPI (free tier) or Google Custom Search to find opportunities.
    Sign up at https://serpapi.com for a free API key (100 searches/month free).
    """
    SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
    if not SERPAPI_KEY:
        print(f"  [Skip] No SERPAPI_KEY set. Query: {query}")
        return

    print(f"Google search: {query[:50]}...")
    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SERPAPI_KEY, "num": 10},
            timeout=15,
        )
        data = resp.json()
        results = data.get("organic_results", [])
        for r in results:
            raw = f"{r.get('title','')} {r.get('snippet','')}"
            opp = ai_classify_single(raw, r.get("title", ""), r.get("link", ""))
            if opp:
                opp["source"] = "Google Search"
                opp["url"] = r.get("link", "")
                save_opportunity(conn, opp)
                print(f"  + {opp['title'][:60]}")
    except Exception as e:
        print(f"  Search error: {e}")


# --- AI LAYER (Claude) ---
def ai_extract_opportunities(page_text: str, source: str, source_url: str) -> list:
    """Send scraped page text to Claude, extract structured opportunity data."""
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "YOUR_API_KEY_HERE":
        return []

    prompt = f"""You are an assistant that extracts youth opportunity listings from web page text.

Source: {source}
URL: {source_url}

Page text:
---
{page_text}
---

Extract all opportunities (hackathons, competitions, leadership camps, fellowships, bootcamps, internships, youth programs) from this text.
Return a JSON array. Each item must have:
- title (string)
- description (string, 1-2 sentences)
- deadline (string, e.g. "15 Jan 2025" or "Rolling" or "Unknown")
- eligibility (string, e.g. "Students 18-25" or "Open to all")
- category (one of: Hackathon, Leadership, Fellowship, Bootcamp, Internship, Competition, Youth Program, Other)
- location (string, e.g. "Singapore", "Online", "Worldwide")
- url (string, full URL if found, else use "{source_url}")

Return ONLY the JSON array, no explanation. If nothing found, return [].
"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        text = resp.json()["content"][0]["text"]
        # Strip markdown code fences if present
        text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  AI extract error: {e}")
        return []


def ai_classify_single(raw_text: str, title: str, url: str) -> dict | None:
    """Classify a single search result — is it an opportunity?"""
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "YOUR_API_KEY_HERE":
        return None

    prompt = f"""Is this a real youth opportunity (hackathon, leadership camp, fellowship, bootcamp, competition, program)? If yes, return a JSON object. If no, return null.

Title: {title}
Text: {raw_text}
URL: {url}

JSON fields: title, description, deadline, eligibility, category, location
Return ONLY JSON or null."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        text = resp.json()["content"][0]["text"].strip()
        if text.lower() == "null" or not text.startswith("{"):
            return None
        return json.loads(text)
    except Exception:
        return None


# --- EXPORT ---
def export_json(conn):
    """Export all opportunities to data/opportunities.json for the website."""
    opps = load_all(conn)
    Path("data").mkdir(exist_ok=True)
    with open("data/opportunities.json", "w") as f:
        json.dump(opps, f, indent=2)
    print(f"\nExported {len(opps)} opportunities to data/opportunities.json")


# --- MAIN ---
def run_scraper():
    print(f"\n{'='*50}")
    print(f"Opportunity Finder — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*50)

    conn = init_db()

    # 1. Scrape known sites
    scrape_devpost(conn)

    for site in TARGET_SITES:
        scrape_generic_site(conn, site["name"], site["url"])
        time.sleep(2)  # Be polite

    # 2. Google search (needs SERPAPI_KEY env var)
    for query in SEARCH_KEYWORDS[:3]:  # Limit to save API quota
        scrape_google_search(conn, query)
        time.sleep(1)

    # 3. Export for website
    export_json(conn)

    print("\nDone!")


if __name__ == "__main__":
    run_scraper()