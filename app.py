import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

KIJIJI_URL = "https://www.kijiji.ca/b-cars-trucks/toronto/c174l1700273"
DB_PATH = "kijiji_seen.db"
SCRAPE_INTERVAL_SECONDS = 300  # 5 minutes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

app = Flask(__name__)

monitor_state = {
    "running": False,
    "last_scrape": None,
    "last_error": None,
    "new_today": 0,
    "today_date": datetime.now().strftime("%Y-%m-%d"),
}
state_lock = threading.Lock()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT UNIQUE NOT NULL,
            title TEXT,
            link TEXT NOT NULL,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def parse_price(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    return int(digits)


def parse_year(text: str) -> Optional[int]:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
    if not match:
        return None
    year = int(match.group(1))
    if 1980 <= year <= datetime.now().year + 1:
        return year
    return None


def parse_mileage(text: str) -> Optional[int]:
    if not text:
        return None
    if "km" not in text.lower():
        return None
    match = re.search(r"([\d,\. ]{3,})\s*km", text.lower())
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    if not digits:
        return None
    value = int(digits)
    if value <= 0 or value > 2_000_000:
        return None
    return value


def extract_listing(card) -> Optional[dict]:
    listing_id = card.get("data-listing-id") or card.get("data-vip-url")
    title_el = card.select_one('[data-testid="listing-title"], .title, a.title')
    price_el = card.select_one('[data-testid="listing-price"], .price, .price-wrapper')
    link_el = card.select_one('a[data-testid="listing-link"], a.title, a')
    image_el = card.select_one("img")
    desc_el = card.select_one('[data-testid="listing-description"], .description')

    if not title_el or not link_el:
        return None

    title = title_el.get_text(" ", strip=True)
    href = link_el.get("href", "").strip()
    if not title or not href:
        return None

    if href.startswith("/"):
        link = f"https://www.kijiji.ca{href}"
    else:
        link = href

    if not listing_id:
        listing_id = link

    full_text = card.get_text(" ", strip=True)
    description = desc_el.get_text(" ", strip=True) if desc_el else full_text

    price = parse_price(price_el.get_text(" ", strip=True)) if price_el else None
    year = parse_year(title)
    mileage = parse_mileage(full_text)

    image_url = image_el.get("src") if image_el else None
    if image_url and image_url.startswith("//"):
        image_url = f"https:{image_url}"

    return {
        "listing_id": str(listing_id),
        "title": title,
        "price": price,
        "year": year,
        "mileage": mileage,
        "description": description,
        "link": link,
        "image_url": image_url,
    }


def scrape_kijiji() -> list[dict]:
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    response = requests.get(KIJIJI_URL, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select(
        '[data-testid="listing-card"], [data-listing-id], .search-item, .regular-ad'
    )

    listings = []
    seen = set()
    for card in cards:
        data = extract_listing(card)
        if not data:
            continue
        if data["listing_id"] in seen:
            continue
        seen.add(data["listing_id"])
        listings.append(data)
    return listings


def is_seen(conn: sqlite3.Connection, listing_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_listings WHERE listing_id = ? LIMIT 1", (listing_id,)
    ).fetchone()
    return row is not None


def save_seen(conn: sqlite3.Connection, listing: dict) -> None:
    conn.execute(
        """
        INSERT INTO seen_listings (listing_id, title, link, first_seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            listing["listing_id"],
            listing["title"],
            listing["link"],
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )


def build_message(listing: dict) -> str:
    price_text = f"${listing['price']:,}" if listing.get("price") else "N/A"
    mileage_text = (
        f"{listing['mileage']:,} km" if listing.get("mileage") is not None else "N/A"
    )
    year_text = str(listing["year"]) if listing.get("year") else "N/A"
    return (
        "🚗 New Kijiji Listing\n"
        f"Title: {listing['title']}\n"
        f"Price: {price_text}\n"
        f"Mileage: {mileage_text}\n"
        f"Year: {year_text}\n"
        f"Link: {listing['link']}"
    )


def send_telegram(listing: dict) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    text = build_message(listing)
    image_url = listing.get("image_url")

    if image_url:
        requests.post(
            f"{base_url}/sendPhoto",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": image_url,
                "caption": text[:1000],
            },
            timeout=20,
        )
    else:
        requests.post(
            f"{base_url}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=20,
        )


def scrape_and_notify() -> int:
    listings = scrape_kijiji()
    conn = get_db_connection()
    new_count = 0
    try:
        for listing in listings:
            if is_seen(conn, listing["listing_id"]):
                continue
            save_seen(conn, listing)
            send_telegram(listing)
            new_count += 1
        conn.commit()
    finally:
        conn.close()
    return new_count


def monitor_loop() -> None:
    with state_lock:
        monitor_state["running"] = True

    while True:
        try:
            now_date = datetime.now().strftime("%Y-%m-%d")
            with state_lock:
                if monitor_state["today_date"] != now_date:
                    monitor_state["today_date"] = now_date
                    monitor_state["new_today"] = 0

            new_count = scrape_and_notify()
            with state_lock:
                monitor_state["last_scrape"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                monitor_state["last_error"] = None
                monitor_state["new_today"] += new_count
            print(f"[{datetime.now().isoformat(timespec='seconds')}] New listings: {new_count}")
        except Exception as exc:
            with state_lock:
                monitor_state["last_error"] = str(exc)
            print(f"Scrape error: {exc}")

        time.sleep(SCRAPE_INTERVAL_SECONDS)


@app.get("/")
def health() -> tuple:
    with state_lock:
        return (
            jsonify(
                {
                    "status": "running" if monitor_state["running"] else "starting",
                    "last_scrape": monitor_state["last_scrape"],
                    "last_error": monitor_state["last_error"],
                    "new_today": monitor_state["new_today"],
                    "interval_seconds": SCRAPE_INTERVAL_SECONDS,
                }
            ),
            200,
        )


def main() -> None:
    init_db()
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
