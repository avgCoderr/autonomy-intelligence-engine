import os
import requests
import feedparser
from datetime import datetime, timezone

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

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

def contains_movement_keyword(text):
    text = text.lower()
    return any(keyword in text for keyword in MOVEMENT_KEYWORDS)

def classify_company(text):
    for company in KNOWN_COMPANIES:
        if company.lower() in text.lower():
            return "Known"
    return "Emerging"

def send_to_notion(title, url, bucket, category):
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Title": {
                "title": [{"text": {"content": title}}]
            },
            "URL": {
                "url": url
            },
            "Bucket": {
                "select": {"name": bucket}
            },
            "Category": {
                "select": {"name": category}
            },
            "Date Added": {
                "date": {"start": datetime.now(timezone.utc).isoformat()}
            }
        }
    }

    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=data
    )

    print(response.status_code, response.text)

def detect_category(text):
    text = text.lower()
    if "lidar" in text:
        return "LiDAR"
    if "humanoid" in text or "robot" in text:
        return "Robotics"
    return "Autonomy"

def main():
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            title = entry.title
            link = entry.link

            if not contains_movement_keyword(title):
                continue

            bucket = classify_company(title)
            category = detect_category(title)

            send_to_notion(title, link, bucket, category)

if __name__ == "__main__":
    main()
