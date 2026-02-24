import os
import re
import requests
import feedparser
import sqlite3
import time
from calendar import timegm
from datetime import datetime, timezone, timedelta

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "MISSING")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "MISSING")

MAX_ARTICLES = 20
RECENCY_HOURS = 48

MOVEMENT_KEYWORDS = [
    "production", "deployment", "commercial", "fleet",
    "contract", "partnership", "expansion",
    "regulatory", "pilot", "validation", "funding"
]

KNOWN_COMPANIES = [
    "Continental", "Rivian", "Bosch", "Hyundai", "Honda",
    "Nvidia", "Zendar", "Sony Honda Mobility", "Apple",
    "Lightwheel", "AEVA", "Waymo", "Magna", "ZF",
    "Valeo", "Aptiv", "Qualcomm", "Innoviz",
    "Baidu", "Agibot", "Tesla",
    "Boston Dynamics", "Figure", "Unitree", "Apptronik"
]

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Level+3+autonomous+driving+production",
    "https://news.google.com/rss/search?q=Level+4+autonomous+vehicle+deployment",
    "https://news.google.com/rss/search?q=LiDAR+production+contract+automotive",
    "https://news.google.com/rss/search?q=Humanoid+robot+commercial+deployment"
]

# ─── Pre-compiled Matching ────────────────────────────────────────────────────
# Built once at module load. Single words → sets, phrases → super regex.

def _build_phrase_regex(terms):
    phrases = [re.escape(t) for t in terms if ' ' in t]
    if not phrases:
        return None
    return re.compile(r'\b(' + '|'.join(phrases) + r')\b', re.IGNORECASE)

MOVEMENT_SET        = {k.lower() for k in MOVEMENT_KEYWORDS if ' ' not in k}
MOVEMENT_PHRASE_RE  = _build_phrase_regex(MOVEMENT_KEYWORDS)

COMPANY_SET         = {c.lower() for c in KNOWN_COMPANIES if ' ' not in c}
COMPANY_PHRASE_RE   = _build_phrase_regex(KNOWN_COMPANIES)


# ─── Dynamic Score Ceiling ────────────────────────────────────────────────────

def compute_max_score(boosted_companies, boosted_keywords):
    """
    Computes the theoretical maximum score based on current lists.
    Self-adjusts as MOVEMENT_KEYWORDS, KNOWN_COMPANIES, and feedback lists grow.
    Assumes every keyword and company hits in both title and full text.
    """
    movement_single  = len(MOVEMENT_SET)
    movement_phrases = len([k for k in MOVEMENT_KEYWORDS if ' ' in k])

    company_single   = len(COMPANY_SET)
    company_phrases  = len([c for c in KNOWN_COMPANIES if ' ' in c])

    movement_score  = (movement_single + movement_phrases) * 2  # full text + title bonus
    company_score   = (company_single + company_phrases) * 2    # × 2 weight
    boost_companies = len(boosted_companies) * 3
    boost_keywords  = len(boosted_keywords) * 2
    source_boost    = 3

    return movement_score + company_score + boost_companies + boost_keywords + source_boost


# ─── SQLite Memory ────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_urls (
            url TEXT PRIMARY KEY,
            fetched_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS source_scores (
            domain TEXT PRIMARY KEY,
            useful_count INTEGER DEFAULT 0,
            total_count INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn

def is_seen(conn, url):
    c = conn.cursor()
    c.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,))
    return c.fetchone() is not None

def mark_seen(conn, url):
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO seen_urls (url, fetched_at) VALUES (?, ?)",
        (url, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()

def update_source_count(conn, domain):
    c = conn.cursor()
    c.execute("""
        INSERT INTO source_scores (domain, total_count) VALUES (?, 1)
        ON CONFLICT(domain) DO UPDATE SET total_count = total_count + 1
    """, (domain,))
    conn.commit()

def get_source_boost(conn, domain):
    """Returns a boost (0.0–1.0) for sources that historically had useful articles."""
    c = conn.cursor()
    c.execute("SELECT useful_count, total_count FROM source_scores WHERE domain = ?", (domain,))
    row = c.fetchone()
    if not row or row[1] == 0:
        return 0.0
    return row[0] / row[1]


# ─── Notion Feedback Loop ─────────────────────────────────────────────────────

def fetch_useful_patterns():
    """
    Reads back articles marked 'Useful' in Notion.
    Extracts boosted companies and keywords from their titles.
    """
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    payload = {
        "filter": {
            "property": "Useful",
            "checkbox": {"equals": True}
        }
    }
    response = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
        headers=headers,
        json=payload
    )
    if response.status_code != 200:
        print(f"Could not fetch useful patterns: {response.text}")
        return set(), set()

    results = response.json().get("results", [])
    boosted_companies = set()
    boosted_keywords  = set()

    for page in results:
        title_prop  = page.get("properties", {}).get("Title", {})
        title_parts = title_prop.get("title", [])
        title       = "".join(t.get("plain_text", "") for t in title_parts).lower()

        for company in KNOWN_COMPANIES:
            if company.lower() in title:
                boosted_companies.add(company.lower())

        for keyword in MOVEMENT_KEYWORDS:
            if keyword in title:
                boosted_keywords.add(keyword)

    print(f"Feedback loop: boosted companies={boosted_companies}, keywords={boosted_keywords}")
    return boosted_companies, boosted_keywords


# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_article(title, summary, domain, conn, boosted_companies, boosted_keywords):
    title_text = title.lower()
    full_text  = f"{title_text} {summary.lower()}"

    # Tokenize once — sets deduplicate repeated words naturally
    title_tokens = set(re.findall(r'\w+', title_text))
    full_tokens  = set(re.findall(r'\w+', full_text))

    score = 0

    # ── 1. Movement keywords ─────────────────────────────────────────────────
    # Single words: set intersection — word-boundary safe, O(1) per word
    score += len(full_tokens.intersection(MOVEMENT_SET))
    score += len(title_tokens.intersection(MOVEMENT_SET))   # title bonus

    # Multi-word phrases: one regex pass, deduplicated via set
    if MOVEMENT_PHRASE_RE:
        score += len(set(MOVEMENT_PHRASE_RE.findall(full_text)))
        score += len(set(MOVEMENT_PHRASE_RE.findall(title_text)))

    # ── 2. Known companies ───────────────────────────────────────────────────
    score += len(full_tokens.intersection(COMPANY_SET)) * 2

    if COMPANY_PHRASE_RE:
        score += len(set(COMPANY_PHRASE_RE.findall(full_text))) * 2

    # ── 3. Feedback loop boosts (dynamic — word boundaries via re.search) ───
    for b_company in boosted_companies:
        if re.search(r'\b' + re.escape(b_company) + r'\b', full_text, re.IGNORECASE):
            score += 3

    for kw in boosted_keywords:
        if kw in full_tokens:
            score += 2

    # ── 4. Source quality boost (0–3 points) ────────────────────────────────
    source_boost = get_source_boost(conn, domain)
    score += round(source_boost * 3)

    return score  # raw, uncapped — ceiling computed dynamically per run


# ─── Helpers ─────────────────────────────────────────────────────────────────

def is_recent(entry, cutoff):
    """Returns True if the article was published at or after the cutoff timestamp."""
    parsed = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if parsed is None:
        return False
    published_utc = datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)
    return published_utc >= cutoff

def get_domain(url):
    try:
        return url.split("/")[2].replace("www.", "")
    except Exception:
        return "unknown"

def contains_movement_keyword(text):
    """Gate check — uses same set/regex logic as scorer for consistency."""
    tokens = set(re.findall(r'\w+', text.lower()))
    if tokens.intersection(MOVEMENT_SET):
        return True
    if MOVEMENT_PHRASE_RE and MOVEMENT_PHRASE_RE.search(text):
        return True
    return False

def classify_company(text):
    for company in KNOWN_COMPANIES:
        if company.lower() in text.lower():
            return "Known"
    return "Emerging"

def detect_category(text):
    text = text.lower()
    if "lidar" in text:
        return "LiDAR"
    if "humanoid" in text or "robot" in text:
        return "Robotics"
    return "Autonomy"


# ─── Notion Write ─────────────────────────────────────────────────────────────

def send_to_notion(title, url, bucket, category, score, source):
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Title": {"title": [{"text": {"content": title}}]},
            "URL": {"url": url},
            "Bucket": {"select": {"name": bucket}},
            "Category": {"select": {"name": category}},
            "Date Added": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
            "Relevance Score": {"number": score},
            "Source": {"rich_text": [{"text": {"content": source}}]},
            "Useful": {"checkbox": False}
        }
    }
    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=data
    )
    if response.status_code != 200:
        print(f"  ❌ FAILED [{response.status_code}]: {response.text}")
    else:
        print(f"  ✅ [{score}] {title[:70]}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"RUN STARTED: {datetime.now(timezone.utc).isoformat()}")
    db_id = NOTION_DATABASE_ID
    print(f"DB ID length: {len(db_id)} | has dashes: {'-' in db_id}")
    print("=" * 60)

    conn = init_db()
    boosted_companies, boosted_keywords = fetch_useful_patterns()

    # Dynamic ceiling — self-adjusts as lists grow
    max_score = compute_max_score(boosted_companies, boosted_keywords)
    print(f"Dynamic score ceiling this run: {max_score}")

    candidates = []
    stats = {"total": 0, "old": 0, "no_keyword": 0, "seen": 0}

    # Compute once — every article evaluated against the same cutoff
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENCY_HOURS)

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            stats["total"] += 1
            title  = entry.title
            link   = entry.link
            domain = get_domain(link)

            if not is_recent(entry, cutoff):
                stats["old"] += 1
                continue
            if not contains_movement_keyword(title):
                stats["no_keyword"] += 1
                continue
            if is_seen(conn, link):
                stats["seen"] += 1
                continue

            summary = getattr(entry, 'summary', '') or ''
            score   = score_article(title, summary, domain, conn, boosted_companies, boosted_keywords)
            candidates.append((score, title, link, classify_company(title), detect_category(title), domain))

    print(f"\nFETCH SUMMARY")
    print(f"  Total entries seen : {stats['total']}")
    print(f"  Skipped (too old)  : {stats['old']}")
    print(f"  Skipped (keyword)  : {stats['no_keyword']}")
    print(f"  Skipped (seen)     : {stats['seen']}")
    print(f"  Candidates         : {len(candidates)}")

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:MAX_ARTICLES]

    print(f"\nPUSHING TOP {len(top)} TO NOTION  (ceiling: {max_score})")
    for score, title, link, bucket, category, domain in top:
        send_to_notion(title, link, bucket, category, score, domain)
        mark_seen(conn, link)
        update_source_count(conn, domain)
        time.sleep(0.3)

    conn.close()
    print(f"\nDONE. {len(top)} articles pushed.")
    print("=" * 60)

if __name__ == "__main__":
    main()
