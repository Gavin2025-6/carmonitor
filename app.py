import os
import json
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, render_template_string

load_dotenv()

KIJIJI_URL = "https://www.kijiji.ca/b-cars-trucks/toronto/c174l1700273"
DB_PATH = "kijiji_monitor.db"
MARKET_REFERENCE_PATH = "market_reference.json"
SLEEP_MIN_SECONDS = 280
SLEEP_MAX_SECONDS = 320
BLOCK_KEYWORDS = ("WANTED", "REBUILT", "ACCIDENT", "PARTS")
DEALER_KEYWORDS = (
    "dealer",
    "dealership",
    "financing available",
    "omvic",
    "trade-in",
    "car lot",
    "sales representative",
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

app = Flask(__name__)

monitor_state = {
    "running": False,
    "last_scrape": None,
    "last_error": None,
}
state_lock = threading.Lock()
MARKET_REFERENCE: dict[str, int] = {}


@dataclass
class Listing:
    listing_id: str
    title: str
    price: int
    mileage: Optional[int]
    year: Optional[int]
    seller_type: str
    link: str
    image_url: Optional[str]
    key: str


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            mileage INTEGER,
            year INTEGER,
            seller_type TEXT NOT NULL,
            link TEXT NOT NULL,
            image_url TEXT,
            key TEXT,
            is_hot_deal INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def load_market_reference() -> dict[str, int]:
    try:
        with open(MARKET_REFERENCE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        normalized = {}
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, (int, float)):
                normalized[key.lower().strip()] = int(value)
        return normalized
    except Exception:
        return {}


def parse_price(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    return int(digits)


def parse_year(title: str) -> Optional[int]:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    if not match:
        return None
    year = int(match.group(1))
    if 1980 <= year <= datetime.now().year + 1:
        return year
    return None


def parse_mileage(text: str) -> Optional[int]:
    if not text:
        return None
    lower = text.lower()
    if "km" not in lower:
        return None
    digits = re.sub(r"[^\d]", "", lower)
    if not digits:
        return None
    value = int(digits)
    return value if value < 1_500_000 else None


def is_blocked_listing(text: str) -> bool:
    upper_text = text.upper()
    return any(keyword in upper_text for keyword in BLOCK_KEYWORDS)


def normalize_model_key(title: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", " ", title.lower())
    tokens = [t for t in cleaned.split() if t]
    tokens = [t for t in tokens if t not in {"automatic", "manual", "awd", "fwd", "rwd", "4wd"}]
    return " ".join(tokens[:3]) if tokens else "unknown-model"


def classify_seller(description: str) -> str:
    haystack = (description or "").lower()
    if any(keyword in haystack for keyword in DEALER_KEYWORDS):
        return "DEALER"
    if "private seller" in haystack or "private" in haystack:
        return "PRIVATE"
    return "PRIVATE"


def market_price_for_title(title: str) -> Optional[int]:
    lowered_title = title.lower()
    for model_name, market_price in MARKET_REFERENCE.items():
        if model_name in lowered_title:
            return market_price
    return None


def extract_listing(card) -> Optional[Listing]:
    listing_id = card.get("data-listing-id") or card.get("data-vip-url")
    title_el = card.select_one('[data-testid="listing-title"], .title, a.title')
    price_el = card.select_one('[data-testid="listing-price"], .price, .price-wrapper')
    link_el = card.select_one('a[data-testid="listing-link"], a.title, a')
    image_el = card.select_one("img")

    if not title_el or not price_el or not link_el:
        return None

    title = title_el.get_text(" ", strip=True)
    if not title:
        return None

    price = parse_price(price_el.get_text(" ", strip=True))
    if price is None:
        return None

    href = link_el.get("href", "")
    if href.startswith("/"):
        link = f"https://www.kijiji.ca{href}"
    else:
        link = href
    if not link:
        return None

    if not listing_id:
        listing_id = link

    desc_el = card.select_one('[data-testid="listing-description"], .description')
    details_text = card.get_text(" ", strip=True)
    if desc_el:
        details_text += " " + desc_el.get_text(" ", strip=True)

    if is_blocked_listing(f"{title} {details_text}"):
        return None

    mileage = parse_mileage(details_text)
    year = parse_year(title)
    seller_type = classify_seller(details_text)
    image_url = image_el.get("src") if image_el else None
    if image_url and image_url.startswith("//"):
        image_url = f"https:{image_url}"

    key = f"{year or 'unknown-year'}-{normalize_model_key(title)}"
    return Listing(
        listing_id=str(listing_id),
        title=title,
        price=price,
        mileage=mileage,
        year=year,
        seller_type=seller_type,
        link=link,
        image_url=image_url,
        key=key,
    )


def scrape_listings() -> list[Listing]:
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    response = requests.get(KIJIJI_URL, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select(
        '[data-testid="listing-card"], [data-listing-id], .search-item, .regular-ad'
    )

    listings: list[Listing] = []
    seen_ids = set()
    for card in cards:
        listing = extract_listing(card)
        if listing and listing.listing_id not in seen_ids:
            seen_ids.add(listing.listing_id)
            listings.append(listing)
    return listings


def is_seen_listing(conn: sqlite3.Connection, listing_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM listings WHERE listing_id = ? LIMIT 1", (listing_id,)
    ).fetchone()
    return row is not None


def store_listing(conn: sqlite3.Connection, listing: Listing, is_hot_deal: bool) -> None:
    conn.execute(
        """
        INSERT INTO listings (
            listing_id, title, price, mileage, year, seller_type, link, image_url, key, is_hot_deal, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing.listing_id,
            listing.title,
            listing.price,
            listing.mileage,
            listing.year,
            listing.seller_type,
            listing.link,
            listing.image_url,
            listing.key,
            1 if is_hot_deal else 0,
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )


def format_alert(listing: Listing, is_hot_deal: bool, market_price: Optional[float]) -> str:
    deal_tag = "🔥 HOT DEAL\n" if is_hot_deal else ""
    market_line = f"\nMarket ref: ${int(market_price):,}" if market_price else ""
    mileage_text = f"{listing.mileage:,} km" if listing.mileage is not None else "N/A"
    year_text = str(listing.year) if listing.year else "N/A"
    return (
        f"{deal_tag}[{listing.seller_type}]\n"
        f"{listing.title}\n"
        f"Price: ${listing.price:,}{market_line}\n"
        f"Year: {year_text}\n"
        f"Mileage: {mileage_text}\n"
        f"Link: {listing.link}"
    )


def send_telegram_alert(listing: Listing, is_hot_deal: bool, market_price: Optional[float]) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    text = format_alert(listing, is_hot_deal, market_price)
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if listing.image_url:
        requests.post(
            f"{base_url}/sendPhoto",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": listing.image_url,
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


def process_scrape_cycle() -> int:
    scraped = scrape_listings()
    conn = get_db_connection()
    new_listings: list[tuple[Listing, bool, Optional[float]]] = []

    for listing in scraped:
        if is_seen_listing(conn, listing.listing_id):
            continue
        market_price = market_price_for_title(listing.title)
        is_hot_deal = market_price is not None and listing.price < market_price * 0.9
        store_listing(conn, listing, is_hot_deal)
        new_listings.append((listing, is_hot_deal, market_price))

    conn.commit()
    conn.close()

    # Private sellers are prioritized in notifications.
    new_listings.sort(key=lambda item: 0 if item[0].seller_type == "PRIVATE" else 1)
    for listing, is_hot_deal, market_price in new_listings:
        send_telegram_alert(listing, is_hot_deal, market_price)

    return len(new_listings)


def monitor_loop() -> None:
    with state_lock:
        monitor_state["running"] = True

    while True:
        try:
            new_count = process_scrape_cycle()
            with state_lock:
                monitor_state["last_scrape"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                monitor_state["last_error"] = None
            print(f"[{datetime.now().isoformat(timespec='seconds')}] New listings: {new_count}")
        except Exception as exc:
            with state_lock:
                monitor_state["last_error"] = str(exc)
            print(f"Scrape error: {exc}")
        sleep_seconds = random.randint(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS)
        time.sleep(sleep_seconds)


@app.route("/")
def dashboard():
    conn = get_db_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    deals_found_today = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM listings
        WHERE is_hot_deal = 1 AND substr(created_at, 1, 10) = ?
        """,
        (today,),
    ).fetchone()["count"]
    conn.close()

    with state_lock:
        running = monitor_state["running"]
        last_scrape = monitor_state["last_scrape"] or "Never"
        last_error = monitor_state["last_error"] or "None"

    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Kijiji Car Monitor</title>
        <style>
          body { font-family: Arial, sans-serif; background: #f7f9fc; margin: 0; padding: 24px; }
          .wrap { max-width: 760px; margin: 0 auto; }
          .title { font-size: 28px; margin-bottom: 20px; font-weight: 700; }
          .grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
          .card { background: #fff; border-radius: 10px; padding: 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
          .label { color: #667085; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; }
          .value { font-size: 21px; margin-top: 6px; }
          .ok { color: #0f9d58; }
          .err { color: #d93025; }
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="title">Kijiji Toronto Car Monitor</div>
          <div class="grid">
            <div class="card">
              <div class="label">System Status</div>
              <div class="value {{ 'ok' if running else 'err' }}">{{ 'Running' if running else 'Stopped' }}</div>
            </div>
            <div class="card">
              <div class="label">Last Scrape Time</div>
              <div class="value">{{ last_scrape }}</div>
            </div>
            <div class="card">
              <div class="label">Deals Found Today</div>
              <div class="value">{{ deals_found_today }}</div>
            </div>
            <div class="card">
              <div class="label">Last Error</div>
              <div class="value {{ 'err' if last_error != 'None' else '' }}">{{ last_error }}</div>
            </div>
          </div>
        </div>
      </body>
    </html>
    """
    return render_template_string(
        html,
        running=running,
        last_scrape=last_scrape,
        deals_found_today=deals_found_today,
        last_error=last_error,
    )


def main() -> None:
    global MARKET_REFERENCE
    MARKET_REFERENCE = load_market_reference()
    init_db()
    scraper_thread = threading.Thread(target=monitor_loop, daemon=True)
    scraper_thread.start()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
