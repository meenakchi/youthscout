"""
Opportunity Finder — Scraper (No AI API Required)
Scrapes hackathons, leadership camps, youth programs, and similar opportunities.

Install deps: pip install requests beautifulsoup4
Then run: python scraper.py
"""

import json
import time
import hashlib
import sqlite3
import re
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- CONFIG ---
DB_PATH = "data/opportunities.db"
OUTPUT_JSON = "data/opportunities.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Keywords used to detect category from page text
CATEGORY_KEYWORDS = {
    "Hackathon":     ["hackathon", "hack ", "hacking", "hack fest", "hackers"],
    "Competition":   ["competition", "challenge", "contest", "award", "pitch", "compete"],
    "Fellowship":    ["fellowship", "scholar", "overseas college", "exchange program"],
    "Internship":    ["internship", "intern ", "apprenticeship", "attachment"],
    "Bootcamp":      ["bootcamp", "boot camp", "training program", "accelerator", "summer of code"],
    "Leadership":    ["leadership", "leader", "mentorship", "community program", "volunteer"],
    "Youth Program": ["youth", "young ", "student program", "undergraduate program"],
}

DEADLINE_PATTERNS = [
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"[\s,\-]+\d{1,2}[\s,\-]+20\d{2}\b",
    r"\b\d{1,2}[\s/\-]+\d{1,2}[\s/\-]+20\d{2}\b",
    r"\b20\d{2}[\s/\-]+\d{1,2}[\s/\-]+\d{1,2}\b",
]

# Current year — used everywhere a year needs to stay dynamic
CURRENT_YEAR = datetime.now().year
NEXT_YEAR    = CURRENT_YEAR + 1


# ─── DATABASE ───────────────────────────────────────────────────────────────

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
            date_found  TEXT
        )
    """)
    conn.commit()
    return conn


def save(conn: sqlite3.Connection, opp: dict) -> bool:
    uid = hashlib.md5(opp["url"].encode()).hexdigest()[:12]
    try:
        conn.execute("""
            INSERT OR REPLACE INTO opportunities
            (id, title, source, url, description, deadline, eligibility, category, location, date_found)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))
        conn.commit()
        inserted = conn.execute("SELECT changes()").fetchone()[0]
        return inserted > 0
    except Exception as e:
        print(f"    DB error: {e}")
        return False


def load_all(conn: sqlite3.Connection) -> list:
    cur = conn.execute("SELECT * FROM opportunities ORDER BY date_found DESC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─── HELPERS ────────────────────────────────────────────────────────────────

def get(url: str, timeout=15) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"    GET error ({url[:60]}): {e}")
        return None


def guess_category(text: str) -> str:
    text = text.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return "Other"


def find_deadline(text: str) -> str:
    for pattern in DEADLINE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            # Only return dates that are current year or future
            match_str = m.group(0).strip()
            year_match = re.search(r"20(\d{2})", match_str)
            if year_match:
                year = int("20" + year_match.group(1))
                if year >= CURRENT_YEAR:
                    return match_str
            else:
                return match_str
    if re.search(r"\brolling\b", text, re.IGNORECASE):
        return "Rolling"
    if re.search(r"\bongoing\b", text, re.IGNORECASE):
        return "Ongoing"
    return "Check site"


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def log(title: str, new: bool):
    prefix = "  +" if new else "  ·"
    print(f"{prefix} {title[:70]}")


# ─── SCRAPERS ───────────────────────────────────────────────────────────────

def scrape_devpost(conn: sqlite3.Connection):
    """Devpost — hackathon listings."""
    print("\n[Devpost]")
    r = get("https://devpost.com/hackathons?challenge_type=all&sort_by=Deadline&open_to[]=public")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select(".challenge-listing")[:30]:
        title_el = card.select_one("h2, h3, .challenge-title")
        link_el  = card.select_one("a[href]")
        date_el  = card.select_one(".challenge-stats li, .submission-period, .deadline")
        desc_el  = card.select_one(".challenge-description, p")
        if not title_el or not link_el:
            continue
        href = link_el["href"]
        if not href.startswith("http"):
            href = "https://devpost.com" + href
        opp = {
            "title":       clean(title_el.get_text()),
            "source":      "Devpost",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "",
            "deadline":    clean(date_el.get_text()) if date_el else find_deadline(card.get_text()),
            "category":    "Hackathon",
            "location":    "Online",
            "eligibility": "Open to all",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_mlh(conn: sqlite3.Connection):
    """Major League Hacking events — tries current season, falls back to next."""
    print("\n[MLH]")
    # Try current year season first, then next year
    for season_year in [CURRENT_YEAR, NEXT_YEAR]:
        r = get(f"https://mlh.io/seasons/{season_year}/events")
        if r and r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            events = soup.select(".event")
            if events:
                print(f"    Using season {season_year} ({len(events)} events found)")
                for card in events:
                    title_el = card.select_one("h3")
                    link_el  = card.select_one("a[href]")
                    date_el  = card.select_one(".event-date, p")
                    if not title_el:
                        continue
                    href = (link_el["href"] if link_el else "https://mlh.io")
                    if not href.startswith("http"):
                        href = "https://mlh.io" + href
                    opp = {
                        "title":       clean(title_el.get_text()),
                        "source":      "Major League Hacking",
                        "url":         href,
                        "description": "MLH-sanctioned hackathon for student developers.",
                        "deadline":    clean(date_el.get_text()) if date_el else "Check site",
                        "category":    "Hackathon",
                        "location":    "Various",
                        "eligibility": "Students of all levels",
                    }
                    new = save(conn, opp)
                    log(opp["title"], new)
                return  # Success, stop trying
        time.sleep(1)
    print("    No MLH events found for current or next season")


def scrape_nyc(conn: sqlite3.Connection):
    """Singapore National Youth Council opportunities."""
    print("\n[NYC Singapore]")
    r = get("https://www.nyc.gov.sg/en/opportunities")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select("article, .opp-card, .listing-item, .card"):
        title_el = card.select_one("h2, h3, h4, .title")
        link_el  = card.select_one("a[href]")
        desc_el  = card.select_one("p, .desc, .summary")
        if not title_el:
            continue
        href = link_el["href"] if link_el else "https://www.nyc.gov.sg"
        if href.startswith("/"):
            href = "https://www.nyc.gov.sg" + href
        raw = card.get_text()
        opp = {
            "title":       clean(title_el.get_text()),
            "source":      "NYC Singapore",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "",
            "deadline":    find_deadline(raw),
            "category":    guess_category(raw),
            "location":    "Singapore",
            "eligibility": "Singapore youths aged 15-35",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_astar(conn: sqlite3.Connection):
    """A*STAR scholarships and internships."""
    print("\n[A*STAR]")
    urls = [
        ("https://www.a-star.edu.sg/Scholarships/for-undergraduate-studies", "Internship"),
        ("https://www.a-star.edu.sg/Scholarships/for-graduate-studies", "Fellowship"),
    ]
    for url, default_cat in urls:
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select(".listing-item, article, .scholarship-item, li a")[:15]:
            title_el = item if item.name == "a" else item.select_one("a, h3, h4")
            if not title_el:
                continue
            title = clean(title_el.get_text())
            if len(title) < 8:
                continue
            href = title_el.get("href", url)
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
    """GovTech Singapore internships and programs."""
    print("\n[GovTech]")
    r = get("https://www.tech.gov.sg/careers/students-and-graduates/")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for item in soup.select("article, .program-card, .listing, section"):
        title_el = item.select_one("h2, h3, h4")
        desc_el  = item.select_one("p")
        link_el  = item.select_one("a[href]")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 8:
            continue
        href = (link_el["href"] if link_el else "https://www.tech.gov.sg")
        if href.startswith("/"):
            href = "https://www.tech.gov.sg" + href
        opp = {
            "title":       title,
            "source":      "GovTech Singapore",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "GovTech program for students and graduates.",
            "deadline":    find_deadline(item.get_text()),
            "category":    guess_category(title + (desc_el.get_text() if desc_el else "")),
            "location":    "Singapore",
            "eligibility": "Students / recent graduates",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_startup_sg(conn: sqlite3.Connection):
    """Startup SG programs and grants."""
    print("\n[Startup SG]")
    r = get("https://www.startupsg.gov.sg/programmes")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select(".programme-card, article, .card, .listing-item"):
        title_el = card.select_one("h2, h3, h4, .title")
        desc_el  = card.select_one("p, .desc")
        link_el  = card.select_one("a[href]")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 5:
            continue
        href = (link_el["href"] if link_el else "https://www.startupsg.gov.sg")
        if href.startswith("/"):
            href = "https://www.startupsg.gov.sg" + href
        opp = {
            "title":       title,
            "source":      "Startup SG",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "Startup SG programme for entrepreneurs.",
            "deadline":    find_deadline(card.get_text()),
            "category":    guess_category(title),
            "location":    "Singapore",
            "eligibility": "Entrepreneurs / startups",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_ymca(conn: sqlite3.Connection):
    """YMCA Singapore youth programs."""
    print("\n[YMCA Singapore]")
    r = get("https://www.ymca.org.sg/programmes")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select("article, .programme, .card, .event"):
        title_el = card.select_one("h2, h3, h4")
        desc_el  = card.select_one("p")
        link_el  = card.select_one("a[href]")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 5:
            continue
        href = link_el["href"] if link_el else "https://www.ymca.org.sg"
        if href.startswith("/"):
            href = "https://www.ymca.org.sg" + href
        opp = {
            "title":       title,
            "source":      "YMCA Singapore",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "YMCA youth programme.",
            "deadline":    find_deadline(card.get_text()),
            "category":    "Youth Program",
            "location":    "Singapore",
            "eligibility": "Ages 17-25, Singapore residents",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_pa(conn: sqlite3.Connection):
    """People's Association youth programs."""
    print("\n[People's Association]")
    r = get("https://www.pa.gov.sg/engage/connect-with-government/youth")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for item in soup.select("article, .program, .card, li"):
        title_el = item.select_one("h2, h3, h4, a")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 10 or len(title) > 150:
            continue
        link_el = item.select_one("a[href]")
        href = link_el["href"] if link_el else "https://www.pa.gov.sg"
        if href.startswith("/"):
            href = "https://www.pa.gov.sg" + href
        opp = {
            "title":       title,
            "source":      "People's Association",
            "url":         href,
            "description": "PA youth engagement program.",
            "deadline":    find_deadline(item.get_text()),
            "category":    "Leadership",
            "location":    "Singapore",
            "eligibility": "Singapore citizens/PRs",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_imda(conn: sqlite3.Connection):
    """IMDA tech programs and grants for students."""
    print("\n[IMDA]")
    pages = [
        "https://www.imda.gov.sg/how-we-can-help/digital-skills-for-life",
        "https://www.imda.gov.sg/how-we-can-help/talent-and-professionals",
    ]
    for url in pages:
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("article, .programme, .card, .listing"):
            title_el = card.select_one("h2, h3, h4")
            desc_el  = card.select_one("p")
            link_el  = card.select_one("a[href]")
            if not title_el:
                continue
            title = clean(title_el.get_text())
            if len(title) < 8:
                continue
            href = (link_el["href"] if link_el else url)
            if href.startswith("/"):
                href = "https://www.imda.gov.sg" + href
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


def scrape_google_gsoc(conn: sqlite3.Connection):
    """Google Summer of Code — always relevant, year stays dynamic."""
    print("\n[Google Summer of Code]")
    # GSoC cycle: applications open ~Jan, deadline ~April, for that calendar year
    # If we're past July, next year's cycle is the relevant one
    month = datetime.now().month
    gsoc_year = NEXT_YEAR if month >= 8 else CURRENT_YEAR
    deadline  = f"April {gsoc_year}"
    opp = {
        "title":       f"Google Summer of Code {gsoc_year}",
        "source":      "Google",
        "url":         "https://summerofcode.withgoogle.com",
        "description": "12-week open source internship program. Work on real projects with experienced mentors and earn a stipend.",
        "deadline":    deadline,
        "category":    "Bootcamp",
        "location":    "Online",
        "eligibility": "Students 18+, any university",
    }
    new = save(conn, opp)
    log(opp["title"], new)


def scrape_microsoft_imagine_cup(conn: sqlite3.Connection):
    """Microsoft Imagine Cup."""
    print("\n[Microsoft Imagine Cup]")
    r = get("https://imaginecup.microsoft.com/en-us/Events")
    if not r:
        # Fallback: Imagine Cup cycle is ~Oct–Feb for the following year's finals
        month = datetime.now().month
        cup_year = NEXT_YEAR if month >= 6 else CURRENT_YEAR
        opp = {
            "title":       f"Microsoft Imagine Cup {cup_year}",
            "source":      "Microsoft",
            "url":         "https://imaginecup.microsoft.com",
            "description": "Global student tech competition. Use AI and cloud to solve real-world problems. Win up to $85,000 USD.",
            "deadline":    f"February {cup_year}",
            "category":    "Competition",
            "location":    "Online + Finals worldwide",
            "eligibility": "Students worldwide, teams of 1-4",
        }
        new = save(conn, opp)
        log(opp["title"], new)
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select("article, .event-card, .competition"):
        title_el = card.select_one("h2, h3")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 5:
            continue
        link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else "https://imaginecup.microsoft.com"
        desc_el = card.select_one("p")
        opp = {
            "title":       title,
            "source":      "Microsoft",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "Microsoft Imagine Cup event.",
            "deadline":    find_deadline(card.get_text()),
            "category":    "Competition",
            "location":    "Online + Finals worldwide",
            "eligibility": "Students worldwide",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_ai_singapore(conn: sqlite3.Connection):
    """AI Singapore — AIAP, AI for Kids, AI bootcamps."""
    print("\n[AI Singapore]")
    r = get("https://aisingapore.org/research/aiap/")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for section in soup.select("section, .program, article"):
        title_el = section.select_one("h2, h3")
        desc_el  = section.select_one("p")
        link_el  = section.select_one("a[href]")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 5:
            continue
        href = link_el["href"] if link_el else "https://aisingapore.org"
        if href.startswith("/"):
            href = "https://aisingapore.org" + href
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


def scrape_nus_noc(conn: sqlite3.Connection):
    """NUS Overseas Colleges — deadline is always Dec of current year for next intake."""
    print("\n[NUS Overseas Colleges]")
    month = datetime.now().month
    # Applications for next year typically close Dec; if past Dec, point to next year
    intake_year = NEXT_YEAR if month == 12 else CURRENT_YEAR
    opp = {
        "title":       "NUS Overseas Colleges Program",
        "source":      "NUS",
        "url":         "https://overseas.nus.edu.sg/noc",
        "description": "Live and work in global startup hubs (Silicon Valley, Stockholm, Shanghai, etc). 6-12 month entrepreneurship program combining startup internship with coursework.",
        "deadline":    f"December {intake_year}",
        "category":    "Fellowship",
        "location":    "Various (global)",
        "eligibility": "NUS undergraduates, Year 2-3",
    }
    new = save(conn, opp)
    log(opp["title"], new)


def scrape_lky_competition(conn: sqlite3.Connection):
    """Lee Kuan Yew Global Business Plan Competition."""
    print("\n[LKY GBPC]")
    r = get("https://lkygbpc.smu.edu.sg")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    full_text = soup.get_text()
    # Try to find a real deadline on the page first; fallback to dynamic year
    deadline = find_deadline(full_text)
    if deadline == "Check site":
        month = datetime.now().month
        comp_year = NEXT_YEAR if month >= 9 else CURRENT_YEAR
        deadline = f"August {comp_year}"
    title_el = soup.select_one("h1, h2")
    desc_el  = soup.select_one("p")
    opp = {
        "title":       clean(title_el.get_text()) if title_el else "Lee Kuan Yew Global Business Plan Competition",
        "source":      "SMU",
        "url":         "https://lkygbpc.smu.edu.sg",
        "description": clean(desc_el.get_text()) if desc_el else "Asia's premier startup competition for student entrepreneurs.",
        "deadline":    deadline,
        "category":    "Competition",
        "location":    "Singapore",
        "eligibility": "Students worldwide, teams of 2-5",
    }
    new = save(conn, opp)
    log(opp["title"], new)


def scrape_smu_events(conn: sqlite3.Connection):
    """SMU student events and competitions."""
    print("\n[SMU Events]")
    r = get("https://www.smu.edu.sg/events")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select("article, .event-item, .listing"):
        title_el = card.select_one("h2, h3, h4")
        link_el  = card.select_one("a[href]")
        desc_el  = card.select_one("p")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 8:
            continue
        raw = card.get_text()
        cat = guess_category(raw)
        if cat == "Other":
            continue  # Skip non-opportunity events
        href = link_el["href"] if link_el else "https://www.smu.edu.sg"
        if href.startswith("/"):
            href = "https://www.smu.edu.sg" + href
        opp = {
            "title":       title,
            "source":      "SMU",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "",
            "deadline":    find_deadline(raw),
            "category":    cat,
            "location":    "Singapore",
            "eligibility": "Students",
        }
        new = save(conn, opp)
        log(opp["title"], new)


def scrape_eventbrite_sg(conn: sqlite3.Connection):
    """Eventbrite Singapore — tech and youth events."""
    print("\n[Eventbrite Singapore]")
    urls = [
        "https://www.eventbrite.sg/d/singapore--singapore/hackathon/",
        "https://www.eventbrite.sg/d/singapore--singapore/youth-program/",
    ]
    for url in urls:
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("[data-testid='event-card'], article, .eds-event-card"):
            title_el = card.select_one("h2, h3, [data-testid='event-card-title']")
            link_el  = card.select_one("a[href]")
            date_el  = card.select_one("time, [data-testid='event-card-date']")
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
                "deadline":    clean(date_el.get_text()) if date_el else "Check site",
                "category":    guess_category(title),
                "location":    "Singapore",
                "eligibility": "Open to all",
            }
            new = save(conn, opp)
            log(opp["title"], new)


def scrape_mccy(conn: sqlite3.Connection):
    """MCCY youth programs."""
    print("\n[MCCY]")
    r = get("https://www.mccy.gov.sg/sector-involvement/youth")
    if not r:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    for item in soup.select("article, .programme, .card"):
        title_el = item.select_one("h2, h3, h4")
        if not title_el:
            continue
        title = clean(title_el.get_text())
        if len(title) < 8:
            continue
        link_el = item.select_one("a[href]")
        href = link_el["href"] if link_el else "https://www.mccy.gov.sg"
        if href.startswith("/"):
            href = "https://www.mccy.gov.sg" + href
        desc_el = item.select_one("p")
        opp = {
            "title":       title,
            "source":      "MCCY",
            "url":         href,
            "description": clean(desc_el.get_text()) if desc_el else "MCCY youth programme.",
            "deadline":    find_deadline(item.get_text()),
            "category":    "Youth Program",
            "location":    "Singapore",
            "eligibility": "Singapore youths",
        }
        new = save(conn, opp)
        log(opp["title"], new)


# ─── PURGE EXPIRED ──────────────────────────────────────────────────────────

def purge_expired(conn: sqlite3.Connection):
    """
    Remove entries whose deadline is clearly in the past.
    Only removes entries with a parseable year strictly less than CURRENT_YEAR.
    Entries with 'Check site', 'Rolling', 'Ongoing', or future dates are kept.
    """
    print("\n[Purge expired]")
    rows = conn.execute("SELECT id, title, deadline FROM opportunities").fetchall()
    removed = 0
    for row_id, title, deadline in rows:
        if not deadline:
            continue
        year_match = re.search(r"\b(20\d{2})\b", deadline or "")
        if year_match:
            year = int(year_match.group(1))
            if year < CURRENT_YEAR:
                conn.execute("DELETE FROM opportunities WHERE id = ?", (row_id,))
                print(f"  - Removed (expired {year}): {title[:60]}")
                removed += 1
    conn.commit()
    print(f"    Purged {removed} expired entries")


# ─── EXPORT ─────────────────────────────────────────────────────────────────

def export_json(conn: sqlite3.Connection):
    opps = load_all(conn)
    Path("data").mkdir(exist_ok=True)
    clean_opps = [{k: v for k, v in o.items() if k != "raw_text"} for o in opps]
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(clean_opps, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Exported {len(clean_opps)} opportunities → {OUTPUT_JSON}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'═'*55}")
    print(f"  Opportunity Finder  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*55}")

    conn = init_db()

    scrapers = [
        scrape_devpost,
        scrape_mlh,
        scrape_nyc,
        scrape_astar,
        scrape_govtech,
        scrape_startup_sg,
        scrape_ymca,
        scrape_pa,
        scrape_imda,
        scrape_google_gsoc,
        scrape_microsoft_imagine_cup,
        scrape_ai_singapore,
        scrape_nus_noc,
        scrape_lky_competition,
        scrape_smu_events,
        scrape_eventbrite_sg,
        scrape_mccy,
    ]

    for scraper in scrapers:
        try:
            scraper(conn)
        except Exception as e:
            print(f"  !! {scraper.__name__} crashed: {e}")
        time.sleep(1.5)

    purge_expired(conn)
    export_json(conn)
    print("\nDone! 🎉\n")


if __name__ == "__main__":
    run()