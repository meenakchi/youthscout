"""
YouthScout SG — Scraper v2
Strategy: RSS/JSON APIs first (JS-agnostic), BeautifulSoup for static HTML only.
Telegram digest optional — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars.

Install: pip install requests beautifulsoup4 feedparser
Run:     python scraper.py
"""

import json
import time
import hashlib
import sqlite3
import re
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False
    print("⚠ feedparser not installed — RSS scrapers will be skipped. Run: pip install feedparser")

# ─── CONFIG ──────────────────────────────────────────────────────────────────

DB_PATH     = "data/opportunities.db"
OUTPUT_JSON = "data/opportunities.json"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CURRENT_YEAR = datetime.now().year
NEXT_YEAR    = CURRENT_YEAR + 1
CURRENT_MONTH = datetime.now().month

# ─── CATEGORY KEYWORDS ───────────────────────────────────────────────────────

CATEGORY_KEYWORDS = {
    "Hackathon":     ["hackathon", "hack ", "hacking", "hackfest"],
    "Competition":   ["competition", "challenge", "contest", "award", "pitch", "compete"],
    "Fellowship":    ["fellowship", "scholar", "overseas college", "exchange program"],
    "Internship":    ["internship", "intern ", "apprenticeship", "attachment", "sipga"],
    "Bootcamp":      ["bootcamp", "boot camp", "training program", "accelerator", "summer of code", "aiap"],
    "Leadership":    ["leadership", "leader", "mentorship", "community program", "volunteer", "yclp"],
    "Youth Program": ["youth", "young ", "student program", "undergraduate program"],
}

# More comprehensive deadline patterns — handles "30 Jun 2026", "Jun 30, 2026", "2026-06-30"
MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

DEADLINE_PATTERNS = [
    # "30 June 2026" or "30 Jun 2026"
    rf"\b(\d{{1,2}})\s+({MONTHS})\s+(20\d{{2}})\b",
    # "June 30, 2026" or "Jun 30 2026"
    rf"\b({MONTHS})\s+(\d{{1,2}})[,\s]+(20\d{{2}})\b",
    # "2026-06-30" or "2026/06/30"
    r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b",
    # "30/06/2026" or "30-06-2026"
    r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b",
    # Just "June 2026" — month + year
    rf"\b({MONTHS})\s+(20\d{{2}})\b",
    # "Applications close 30 Jun" (no year — we'll inject current/next year)
    rf"\b(\d{{1,2}})\s+({MONTHS})\b",
]

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            source      TEXT,
            url         TEXT UNIQUE,
            description TEXT,
            deadline    TEXT,
            eligibility TEXT,
            category    TEXT,
            location    TEXT,
            date_found  TEXT,
            is_new      INTEGER DEFAULT 1
        )
    """)
    # Add is_new column if upgrading from v1
    try:
        conn.execute("ALTER TABLE opportunities ADD COLUMN is_new INTEGER DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    return conn


def make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def save(conn: sqlite3.Connection, opp: dict) -> bool:
    """Returns True if this is a NEW entry (not an update)."""
    uid = make_id(opp["url"])
    existing = conn.execute("SELECT id FROM opportunities WHERE id = ?", (uid,)).fetchone()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO opportunities
            (id, title, source, url, description, deadline, eligibility, category, location, date_found, is_new)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            uid,
            opp.get("title", "Untitled")[:200],
            opp.get("source", "Unknown"),
            opp.get("url", ""),
            opp.get("description", "")[:500],
            opp.get("deadline", "Check site"),
            opp.get("eligibility", "See site"),
            opp.get("category", "Other"),
            opp.get("location", ""),
            datetime.now().isoformat(),
            0 if existing else 1,
        ))
        conn.commit()
        return existing is None  # True = new
    except Exception as e:
        print(f"    DB error: {e}")
        return False


def load_all(conn: sqlite3.Connection) -> list:
    cur = conn.execute("SELECT * FROM opportunities ORDER BY date_found DESC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_new_this_run(conn: sqlite3.Connection) -> list:
    cur = conn.execute(
        "SELECT title, url, category, source FROM opportunities WHERE is_new = 1 ORDER BY date_found DESC"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_all_seen(conn: sqlite3.Connection):
    conn.execute("UPDATE opportunities SET is_new = 0")
    conn.commit()

# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────

def get(url: str, timeout: int = 15, retries: int = 2) -> requests.Response | None:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 404, 410):
                return None  # Don't retry permanent errors
            if attempt < retries:
                time.sleep(2 ** attempt)
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                print(f"    GET failed ({url[:60]}): {e}")
    return None

# ─── TEXT HELPERS ─────────────────────────────────────────────────────────────

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()


def guess_category(text: str) -> str:
    text = text.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return "Other"


def find_deadline(text: str) -> str:
    """
    Tries multiple patterns. Returns a human-readable deadline string,
    or 'Check site' / 'Rolling' / 'Ongoing' as fallbacks.
    """
    if not text:
        return "Check site"

    text_lower = text.lower()

    # Check rolling/ongoing first
    if re.search(r"\brolling\b", text_lower):
        return "Rolling"
    if re.search(r"\bongoing\b", text_lower):
        return "Ongoing"

    # Pattern 1: "30 June 2026"
    m = re.search(rf"\b(\d{{1,2}})\s+({MONTHS})\s+(20\d{{2}})\b", text, re.IGNORECASE)
    if m:
        year = int(m.group(3))
        if year >= CURRENT_YEAR:
            return m.group(0).strip()

    # Pattern 2: "June 30, 2026"
    m = re.search(rf"\b({MONTHS})\s+(\d{{1,2}})[,\s]+(20\d{{2}})\b", text, re.IGNORECASE)
    if m:
        year = int(m.group(3))
        if year >= CURRENT_YEAR:
            return m.group(0).strip()

    # Pattern 3: ISO "2026-06-30"
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if m:
        year = int(m.group(1))
        if year >= CURRENT_YEAR:
            try:
                dt = datetime(year, int(m.group(2)), int(m.group(3)))
                return dt.strftime("%-d %b %Y")
            except ValueError:
                pass

    # Pattern 4: "30/06/2026"
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", text)
    if m:
        year = int(m.group(3))
        if year >= CURRENT_YEAR:
            return f"{m.group(1)}/{m.group(2)}/{year}"

    # Pattern 5: "June 2026" — month + year only
    m = re.search(rf"\b({MONTHS})\s+(20\d{{2}})\b", text, re.IGNORECASE)
    if m:
        year = int(m.group(2))
        if year >= CURRENT_YEAR:
            return m.group(0).strip()

    # Pattern 6: "30 Jun" — no year, infer year
    m = re.search(rf"\b(\d{{1,2}})\s+({MONTHS})\b", text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        month_str = m.group(2)
        # Parse month number
        month_map = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        mn = month_map.get(month_str[:3].lower(), 0)
        if mn:
            infer_year = CURRENT_YEAR if mn >= CURRENT_MONTH else NEXT_YEAR
            try:
                dt = datetime(infer_year, mn, day)
                return dt.strftime("%-d %b %Y")
            except ValueError:
                pass

    return "Check site"


def log(title: str, new: bool):
    print(f"  {'+ ' if new else '· '}{title[:72]}")

# ─── RSS SCRAPER (works on any RSS feed — JS-agnostic) ───────────────────────

def scrape_rss(conn: sqlite3.Connection, feed_url: str, source: str,
               default_category: str, location: str, eligibility: str):
    if not HAS_FEEDPARSER:
        return
    feed = feedparser.parse(feed_url)
    if not feed.entries:
        print(f"    No entries in RSS feed: {feed_url[:60]}")
        return
    for entry in feed.entries[:25]:
        title = clean(entry.get("title", ""))
        url   = entry.get("link", "")
        desc  = clean(BeautifulSoup(entry.get("summary", ""), "html.parser").get_text())
        if not title or not url:
            continue
        combined = title + " " + desc
        opp = {
            "title":       title,
            "source":      source,
            "url":         url,
            "description": desc[:500],
            "deadline":    find_deadline(combined),
            "category":    guess_category(combined) if default_category == "auto" else default_category,
            "location":    location,
            "eligibility": eligibility,
        }
        new = save(conn, opp)
        log(title, new)

# ─── INDIVIDUAL SCRAPERS ──────────────────────────────────────────────────────

def scrape_devpost(conn: sqlite3.Connection):
    """
    Devpost JSON API — actual structured data, no HTML parsing needed.
    """
    print("\n[Devpost]")
    # Devpost has an undocumented JSON endpoint used by their own frontend
    url = "https://devpost.com/hackathons.json?challenge_type=all&sort_by=Deadline&open_to[]=public&page=1"
    r = get(url)
    if not r:
        # Fallback to HTML scraping
        r = get("https://devpost.com/hackathons?challenge_type=all&sort_by=Deadline&open_to[]=public")
        if not r:
            return
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".challenge-listing")[:30]:
            title_el = card.select_one("h2, h3, .challenge-title")
            link_el  = card.select_one("a[href]")
            if not title_el or not link_el:
                continue
            href = link_el["href"]
            if not href.startswith("http"):
                href = "https://devpost.com" + href
            desc_el = card.select_one(".challenge-description, p")
            raw = card.get_text()
            opp = {
                "title":       clean(title_el.get_text()),
                "source":      "Devpost",
                "url":         href,
                "description": clean(desc_el.get_text()) if desc_el else "",
                "deadline":    find_deadline(raw),
                "category":    "Hackathon",
                "location":    "Online",
                "eligibility": "Open to all",
            }
            new = save(conn, opp)
            log(opp["title"], new)
        return

    try:
        data = r.json()
        hackathons = data.get("hackathons", [])
        for h in hackathons[:30]:
            title = clean(h.get("title", ""))
            url   = h.get("url", "")
            if not title or not url:
                continue
            # Devpost gives submission_period_dates like "Jun 01 – Jul 31, 2026"
            deadline_raw = h.get("submission_period_dates", "") or ""
            # Take the end date (after "–" or "-")
            if "–" in deadline_raw or "-" in deadline_raw:
                deadline_raw = re.split(r"[–-]", deadline_raw)[-1].strip()
            opp = {
                "title":       title,
                "source":      "Devpost",
                "url":         url if url.startswith("http") else "https://devpost.com" + url,
                "description": clean(h.get("displayed_location", {}).get("location", "") or
                                     h.get("tagline", "") or "Hackathon on Devpost."),
                "deadline":    find_deadline(deadline_raw) if deadline_raw else "Check site",
                "category":    "Hackathon",
                "location":    h.get("displayed_location", {}).get("location", "Online") or "Online",
                "eligibility": "Open to all",
            }
            new = save(conn, opp)
            log(opp["title"], new)
    except Exception as e:
        print(f"    JSON parse error: {e}")


def scrape_mlh(conn: sqlite3.Connection):
    """MLH — static HTML, works with requests."""
    print("\n[MLH]")
    for season_year in [CURRENT_YEAR, NEXT_YEAR]:
        r = get(f"https://mlh.io/seasons/{season_year}/events")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        events = soup.select(".event")
        if not events:
            continue
        print(f"    Season {season_year}: {len(events)} events")
        for card in events:
            title_el = card.select_one("h3")
            link_el  = card.select_one("a[href]")
            date_el  = card.select_one(".event-date, p, .date")
            if not title_el:
                continue
            href = link_el["href"] if link_el else "https://mlh.io"
            if not href.startswith("http"):
                href = "https://mlh.io" + href
            deadline = find_deadline(date_el.get_text() if date_el else card.get_text())
            opp = {
                "title":       clean(title_el.get_text()),
                "source":      "Major League Hacking",
                "url":         href,
                "description": "MLH-sanctioned hackathon for student developers.",
                "deadline":    deadline,
                "category":    "Hackathon",
                "location":    "Various",
                "eligibility": "Students of all levels",
            }
            new = save(conn, opp)
            log(opp["title"], new)
        return  # Got results, stop


def scrape_nyc_api(conn: sqlite3.Connection):
    """
    NYC Singapore — try their API endpoints first.
    The main site is JS-rendered but they expose some REST endpoints.
    """
    print("\n[NYC Singapore]")
    # Try API endpoint (discovered from network tab)
    api_url = "https://www.nyc.gov.sg/api/opportunity/list?page=1&pageSize=20"
    r = get(api_url)
    if r:
        try:
            data = r.json()
            items = data.get("items", data.get("data", data if isinstance(data, list) else []))
            for item in items:
                title = clean(item.get("title", item.get("name", "")))
                url   = item.get("url", item.get("link", "https://www.nyc.gov.sg/en/opportunities"))
                if not title:
                    continue
                raw = json.dumps(item)
                opp = {
                    "title":       title,
                    "source":      "NYC Singapore",
                    "url":         url if url.startswith("http") else "https://www.nyc.gov.sg" + url,
                    "description": clean(item.get("description", item.get("summary", "NYC youth opportunity."))),
                    "deadline":    find_deadline(raw),
                    "category":    guess_category(title + " " + item.get("description", "")),
                    "location":    "Singapore",
                    "eligibility": "Singapore youths aged 15-35",
                }
                new = save(conn, opp)
                log(opp["title"], new)
            return
        except Exception:
            pass

    # Fallback: static HTML (may be empty if JS-rendered, but worth trying)
    r = get("https://www.nyc.gov.sg/en/opportunities")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select("article, .opp-card, .listing-item, .card, [class*='opportunity']"):
        title_el = card.select_one("h2, h3, h4, .title")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 8:
            continue
        link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else "https://www.nyc.gov.sg/en/opportunities"
        if href.startswith("/"):
            href = "https://www.nyc.gov.sg" + href
        opp = {
            "title":       title,
            "source":      "NYC Singapore",
            "url":         href,
            "description": clean(card.select_one("p, .desc, .summary").get_text()
                                 if card.select_one("p, .desc, .summary") else ""),
            "deadline":    find_deadline(card.get_text()),
            "category":    guess_category(card.get_text()),
            "location":    "Singapore",
            "eligibility": "Singapore youths aged 15-35",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_astar(conn: sqlite3.Connection):
    """A*STAR — static HTML pages, generally works fine."""
    print("\n[A*STAR]")
    pages = [
        ("https://www.a-star.edu.sg/Scholarships/for-undergraduate-studies", "Internship"),
        ("https://www.a-star.edu.sg/Scholarships/for-graduate-studies", "Fellowship"),
        ("https://www.a-star.edu.sg/Scholarships/for-graduate-studies/industrial-postgraduate-programme-ipp", "Fellowship"),
    ]
    for url, default_cat in pages:
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # A*STAR pages usually have scholarship items in <li> or <article> blocks
        for item in soup.select(".listing-item, article, li, .scholarship-item")[:20]:
            link_el  = item.select_one("a[href]")
            title_el = link_el or item.select_one("h3, h4, strong")
            if not title_el:
                continue
            title = clean(title_el.get_text())
            if len(title) < 8 or len(title) > 180:
                continue
            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.a-star.edu.sg" + href
            opp = {
                "title":       title,
                "source":      "A*STAR",
                "url":         href,
                "description": "A*STAR research/study opportunity for students.",
                "deadline":    find_deadline(item.get_text()),
                "category":    default_cat,
                "location":    "Singapore",
                "eligibility": "Undergraduates/graduates",
            }
            new = save(conn, opp)
            log(opp["title"], new)


def scrape_govtech(conn: sqlite3.Connection):
    """GovTech — static HTML."""
    print("\n[GovTech]")
    r = get("https://www.tech.gov.sg/careers/students-and-graduates/")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for item in soup.select("article, .program-card, section, .listing"):
        title_el = item.select_one("h2, h3, h4")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 8:
            continue
        link_el = item.select_one("a[href]")
        href = link_el["href"] if link_el else "https://www.tech.gov.sg"
        if href.startswith("/"):
            href = "https://www.tech.gov.sg" + href
        desc_el = item.select_one("p")
        opp = {
            "title":       title,
            "source":      "GovTech Singapore",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "GovTech program for students.",
            "deadline":    find_deadline(item.get_text()),
            "category":    guess_category(title),
            "location":    "Singapore",
            "eligibility": "Students / recent graduates",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_ai_singapore(conn: sqlite3.Connection):
    """AI Singapore — AIAP and related programs."""
    print("\n[AI Singapore]")
    pages = [
        "https://aisingapore.org/research/aiap/",
        "https://aisingapore.org/industryinnovation/aiforsci/",
    ]
    for url in pages:
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for section in soup.select("section, article, .program"):
            title_el = section.select_one("h1, h2, h3")
            if not title_el:
                continue
            title = clean(title_el.get_text())
            if len(title) < 5 or len(title) > 160:
                continue
            link_el = section.select_one("a[href]")
            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://aisingapore.org" + href
            desc_el = section.select_one("p")
            opp = {
                "title":       title,
                "source":      "AI Singapore",
                "url":         href,
                "description": clean(desc_el.get_text()) if desc_el else "AI Singapore talent program.",
                "deadline":    find_deadline(section.get_text()),
                "category":    "Bootcamp",
                "location":    "Singapore",
                "eligibility": "Fresh grads & career switchers",
            }
            new = save(conn, opp)
            log(opp["title"], new)


def scrape_imda(conn: sqlite3.Connection):
    """IMDA — static HTML."""
    print("\n[IMDA]")
    urls = [
        "https://www.imda.gov.sg/how-we-can-help/digital-skills-for-life",
        "https://www.imda.gov.sg/how-we-can-help/talent-and-professionals",
    ]
    for url in urls:
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("article, .programme, .card, .listing"):
            title_el = card.select_one("h2, h3, h4")
            if not title_el:
                continue
            title = clean(title_el.get_text())
            if len(title) < 8:
                continue
            link_el = card.select_one("a[href]")
            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.imda.gov.sg" + href
            desc_el = card.select_one("p")
            opp = {
                "title":       title,
                "source":      "IMDA",
                "url":         href,
                "description": clean(desc_el.get_text()) if desc_el else "IMDA digital skills/talent programme.",
                "deadline":    find_deadline(card.get_text()),
                "category":    guess_category(title),
                "location":    "Singapore",
                "eligibility": "Students and professionals",
            }
            new = save(conn, opp)
            log(opp["title"], new)


def scrape_eventbrite_api(conn: sqlite3.Connection):
    """
    Eventbrite — use their public search endpoint (no auth needed for basic search).
    Falls back to HTML if the endpoint 403s.
    """
    print("\n[Eventbrite Singapore]")
    search_url = (
        "https://www.eventbriteapi.com/v3/events/search/"
        "?location.address=Singapore&q=hackathon+youth+competition"
        "&expand=venue&sort_by=date&token=public"
    )
    # Most of the time, Eventbrite API requires auth. Use RSS instead.
    # Eventbrite publishes RSS for keyword searches:
    rss_url = "https://www.eventbrite.sg/e/hackathons-singapore/?format=json"  # Usually 200
    # Actually use their public sitemap / search pages (static-ish)
    pages = [
        "https://www.eventbrite.sg/d/singapore--singapore/hackathon/",
        "https://www.eventbrite.sg/d/singapore--singapore/competition/",
    ]
    found = 0
    for url in pages:
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # Eventbrite server-side renders some content into a <script id="__SERVER_DATA__">
        script = soup.find("script", {"id": "__SERVER_DATA__"})
        if script and script.string:
            try:
                data = json.loads(script.string)
                # Navigate the nested structure
                events = (
                    data.get("search_data", {})
                        .get("events", {})
                        .get("results", [])
                )
                for ev in events:
                    title = clean(ev.get("name", ""))
                    ev_url = ev.get("url", "")
                    if not title or not ev_url:
                        continue
                    start = ev.get("start_date", "") or ev.get("start", {}).get("local", "")
                    opp = {
                        "title":       title,
                        "source":      "Eventbrite",
                        "url":         ev_url,
                        "description": clean(ev.get("summary", ev.get("description", {}).get("text", "Event in Singapore."))),
                        "deadline":    find_deadline(start or title),
                        "category":    guess_category(title),
                        "location":    "Singapore",
                        "eligibility": "Open to all",
                    }
                    new = save(conn, opp)
                    log(opp["title"], new)
                    found += 1
                continue
            except (json.JSONDecodeError, AttributeError):
                pass
        # Fallback: scrape visible HTML cards
        for card in soup.select("[data-testid='event-card'], article.eds-event-card"):
            title_el = card.select_one("h2, h3, [data-testid='event-card-title']")
            link_el  = card.select_one("a[href]")
            if not title_el or not link_el:
                continue
            title = clean(title_el.get_text())
            if len(title) < 6:
                continue
            href = link_el["href"]
            if not href.startswith("http"):
                href = "https://www.eventbrite.sg" + href
            opp = {
                "title":       title,
                "source":      "Eventbrite",
                "url":         href,
                "description": "Event in Singapore.",
                "deadline":    find_deadline(card.get_text()),
                "category":    guess_category(title),
                "location":    "Singapore",
                "eligibility": "Open to all",
            }
            new = save(conn, opp)
            log(opp["title"], new)
            found += 1
    if found == 0:
        print("    No Eventbrite results extracted (likely JS-rendered)")


def scrape_startup_sg(conn: sqlite3.Connection):
    """Startup SG — static HTML."""
    print("\n[Startup SG]")
    r = get("https://www.startupsg.gov.sg/programmes")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select(".programme-card, article, .card, .listing-item"):
        title_el = card.select_one("h2, h3, h4, .title")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 5:
            continue
        link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else "https://www.startupsg.gov.sg"
        if href.startswith("/"):
            href = "https://www.startupsg.gov.sg" + href
        desc_el = card.select_one("p, .desc")
        opp = {
            "title":       title,
            "source":      "Startup SG",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "Startup SG programme.",
            "deadline":    find_deadline(card.get_text()),
            "category":    guess_category(title),
            "location":    "Singapore",
            "eligibility": "Entrepreneurs / startups",
        }
        new = save(conn, opp)
        log(opp["title"], new)


# ─── STATIC / WELL-KNOWN OPPORTUNITIES ───────────────────────────────────────
# For recurring programmes where the website is JS-heavy or rarely changes,
# we maintain them directly with dynamic year logic.

def scrape_static_known(conn: sqlite3.Connection):
    """
    Hardcoded well-known recurring opportunities.
    Dates are computed dynamically — no stale hardcoding.
    """
    print("\n[Well-known recurring opportunities]")

    # GSoC: applications ~Jan–Apr of the year it runs
    gsoc_year = NEXT_YEAR if CURRENT_MONTH >= 8 else CURRENT_YEAR
    # Imagine Cup: registration opens ~Oct, deadline ~Feb of competition year
    cup_year = NEXT_YEAR if CURRENT_MONTH >= 6 else CURRENT_YEAR
    # NUS NOC: applications close ~Dec for next academic year
    noc_intake = NEXT_YEAR if CURRENT_MONTH == 12 else CURRENT_YEAR
    # SIPGA: typically March for the following intake
    sipga_year = NEXT_YEAR if CURRENT_MONTH >= 4 else CURRENT_YEAR
    # LKY GBPC: submissions ~Aug
    lky_year = NEXT_YEAR if CURRENT_MONTH >= 9 else CURRENT_YEAR
    # Youth Action Challenge: opens ~Sep
    yac_year = NEXT_YEAR if CURRENT_MONTH >= 10 else CURRENT_YEAR

    static_opps = [
        {
            "title":       f"Google Summer of Code {gsoc_year}",
            "source":      "Google",
            "url":         "https://summerofcode.withgoogle.com",
            "description": "12-week open source internship program. Work on real projects with experienced mentors and earn a stipend.",
            "deadline":    f"April {gsoc_year}",
            "category":    "Bootcamp",
            "location":    "Online",
            "eligibility": "Students 18+, any university",
        },
        {
            "title":       f"Microsoft Imagine Cup {cup_year}",
            "source":      "Microsoft",
            "url":         "https://imaginecup.microsoft.com",
            "description": "Global student tech competition. Use AI and cloud to solve real-world problems. Win up to USD 85,000.",
            "deadline":    f"February {cup_year}",
            "category":    "Competition",
            "location":    "Online + Finals worldwide",
            "eligibility": "Students worldwide, teams of 1-4",
        },
        {
            "title":       "NUS Overseas Colleges Program",
            "source":      "NUS",
            "url":         "https://overseas.nus.edu.sg/noc",
            "description": "Live and work in global startup hubs (Silicon Valley, Stockholm, Shanghai, etc). 6-12 month entrepreneurship program combining startup internship with coursework.",
            "deadline":    f"December {noc_intake}",
            "category":    "Fellowship",
            "location":    "Various (global)",
            "eligibility": "NUS undergraduates, Year 2-3",
        },
        {
            "title":       "Singapore International Pre-Graduate Award (SIPGA)",
            "source":      "A*STAR",
            "url":         "https://www.a-star.edu.sg/Scholarships/for-undergraduate-studies/sipga",
            "description": "Research internship at A*STAR institutes. Work alongside world-class researchers on cutting-edge projects.",
            "deadline":    f"March {sipga_year}",
            "category":    "Internship",
            "location":    "Singapore",
            "eligibility": "Penultimate/final year undergrads, min CAP 4.0",
        },
        {
            "title":       f"Lee Kuan Yew Global Business Plan Competition {lky_year}",
            "source":      "SMU",
            "url":         "https://lkygbpc.smu.edu.sg",
            "description": "Asia's premier startup competition for student entrepreneurs. Pitch to top VCs and win up to $100,000 in prizes.",
            "deadline":    f"August {lky_year}",
            "category":    "Competition",
            "location":    "Singapore",
            "eligibility": "Students worldwide, teams of 2-5",
        },
        {
            "title":       f"Youth Action Challenge {yac_year}",
            "source":      "MCCY / NYC",
            "url":         "https://www.nyc.gov.sg",
            "description": "National platform for youths to co-create solutions to social issues. Get mentorship, funding, and support.",
            "deadline":    f"Applications open Sep {yac_year}",
            "category":    "Youth Program",
            "location":    "Singapore",
            "eligibility": "Singapore youths aged 15-35",
        },
        {
            "title":       "AIAP — AI Apprenticeship Programme",
            "source":      "AI Singapore",
            "url":         "https://aisingapore.org/research/aiap/",
            "description": "9-month applied AI programme. Build production-grade ML models with mentorship from senior AI engineers.",
            "deadline":    "Rolling intake",
            "category":    "Bootcamp",
            "location":    "Singapore",
            "eligibility": "Fresh grads / career switchers with coding background",
        },
        {
            "title":       "YMCA Youth Leadership Academy",
            "source":      "YMCA Singapore",
            "url":         "https://www.ymca.org.sg",
            "description": "Develop leadership through workshops, community projects, and mentorship from industry leaders. 3-month cohort.",
            "deadline":    "Rolling intake",
            "category":    "Leadership",
            "location":    "Singapore",
            "eligibility": "Ages 17-25, Singapore residents",
        },
    ]

    for opp in static_opps:
        new = save(conn, opp)
        log(opp["title"], new)


# ─── PURGE EXPIRED ────────────────────────────────────────────────────────────

def purge_expired(conn: sqlite3.Connection):
    """Remove entries whose deadline year is strictly in the past."""
    print("\n[Purge expired]")
    rows = conn.execute("SELECT id, title, deadline FROM opportunities").fetchall()
    removed = 0
    for row_id, title, deadline in rows:
        if not deadline or deadline in ("Check site", "Rolling", "Ongoing"):
            continue
        year_match = re.search(r"\b(20\d{2})\b", deadline)
        if year_match and int(year_match.group(1)) < CURRENT_YEAR:
            conn.execute("DELETE FROM opportunities WHERE id = ?", (row_id,))
            print(f"  - Removed (expired): {title[:60]}")
            removed += 1
    conn.commit()
    print(f"    Purged {removed} expired entries")


# ─── TELEGRAM DIGEST ──────────────────────────────────────────────────────────

def send_telegram_digest(new_opps: list):
    """
    Send a Telegram message with newly found opportunities.
    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars to enable.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n[Telegram] Skipped — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return
    if not new_opps:
        print("\n[Telegram] No new opps to send")
        return

    print(f"\n[Telegram] Sending digest: {len(new_opps)} new opps")

    # Build message — Telegram supports basic MarkdownV2
    lines = [f"*YouthScout SG* — {len(new_opps)} new opportunit{'y' if len(new_opps) == 1 else 'ies'} 🎯\n"]
    for opp in new_opps[:10]:  # Cap at 10 so message doesn't exceed 4096 chars
        cat_emoji = {
            "Hackathon":    "💻",
            "Competition":  "🏆",
            "Fellowship":   "🌏",
            "Internship":   "💼",
            "Bootcamp":     "🚀",
            "Leadership":   "🌟",
            "Youth Program": "🎓",
        }.get(opp["category"], "📌")
        # Escape special chars for MarkdownV2
        def esc(s):
            return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(s))
        lines.append(f"{cat_emoji} [{esc(opp['title'])}]({opp['url']})")
        lines.append(f"   _{esc(opp['source'])}_ · {esc(opp['category'])}\n")

    if len(new_opps) > 10:
        lines.append(f"_...and {len(new_opps) - 10} more at youthscout\\.sg_")

    message = "\n".join(lines)

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.ok:
            print("    Telegram message sent ✓")
        else:
            print(f"    Telegram error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"    Telegram send failed: {e}")


# ─── EXPORT ───────────────────────────────────────────────────────────────────

def export_json(conn: sqlite3.Connection):
    opps = load_all(conn)
    # Strip internal-only columns from the public JSON
    public_keys = {"id", "title", "source", "url", "description", "deadline",
                   "eligibility", "category", "location", "date_found"}
    clean_opps = [{k: v for k, v in o.items() if k in public_keys} for o in opps]
    Path("data").mkdir(exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(clean_opps, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Exported {len(clean_opps)} opportunities → {OUTPUT_JSON}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'═' * 55}")
    print(f"  YouthScout Scraper v2  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * 55}")

    conn = init_db()

    # Reset is_new flags from previous run before scraping
    mark_all_seen(conn)

    scrapers = [
        scrape_devpost,
        scrape_mlh,
        scrape_nyc_api,
        scrape_astar,
        scrape_govtech,
        scrape_startup_sg,
        scrape_imda,
        scrape_ai_singapore,
        scrape_eventbrite_api,
        scrape_static_known,
    ]

    for scraper in scrapers:
        try:
            scraper(conn)
        except Exception as e:
            print(f"  !! {scraper.__name__} crashed: {e}")
        time.sleep(1.5)

    purge_expired(conn)

    # Collect newly added opps for Telegram digest
    new_opps = get_new_this_run(conn)
    send_telegram_digest(new_opps)

    export_json(conn)

    print(f"\n{'═' * 55}")
    print(f"  Done! {len(load_all(conn))} total · {len(new_opps)} new this run")
    print(f"{'═' * 55}\n")


if __name__ == "__main__":
    run()