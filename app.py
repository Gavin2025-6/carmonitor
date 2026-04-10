import os
import json
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, render_template_string

load_dotenv()

KIJIJI_URL = "https://www.kijiji.ca/b-cars-trucks/toronto-gta/c174l1700272"
DB_PATH = "kijiji_seen.db"
SCRAPE_INTERVAL_SECONDS = 300  # 5 minutes
AUTOTRADER_REQUEST_TIMEOUT = 15

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

RISK_KEYWORD_PATTERNS = [
    (r"\baccident(s)?\b", "accident"),
    (r"\brebuilt\b", "rebuilt"),
    (r"\bsalvage\b", "salvage"),
    (r"\bas-is\b", "as-is"),
    (r"\bas is\b", "as is"),
    (r"\bno safety\b", "no safety"),
    (r"\bnot safet(y|ied)\b", "not safety"),
    (r"\bflood(ed)?\b", "flood"),
    (r"\blem(on)?\b", "lemon"),
    (r"\bwrite[- ]?off\b", "write-off"),
    (r"\bframe damage\b", "frame damage"),
    (r"\bstructural\b", "structural"),
    (r"\bparts only\b", "parts only"),
    (r"\bstolen\b", "stolen"),
]

HIGHLIGHT_PATTERNS = [
    (r"\bone owner\b", "one owner"),
    (r"\bsingle owner\b", "single owner"),
    (r"\bno accident(s)?\b", "no accidents"),
    (r"\bclean carfax\b", "clean carfax"),
    (r"\bcarfax\b", "carfax"),
    (r"\bcertified\b", "certified"),
    (r"\bwarranty\b", "warranty"),
    (r"\bsafety certified\b", "safety certified"),
    (r"\bpassed safety\b", "passed safety"),
    (r"\bwith safety\b", "with safety"),
    (r"\blow km\b", "low km"),
    (r"\blow mileage\b", "low mileage"),
]

DEALER_KEYWORDS = (
    "dealer",
    "dealership",
    "omvic",
    "financing",
    "car lot",
    "inventory",
    "showroom",
    "commercial",
)


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


def extract_price(card, full_text: str) -> Optional[int]:
    # Kijiji selectors can change, so use multiple selectors + regex fallback.
    selector_candidates = [
        '[data-testid="listing-price"]',
        '[data-testid="price"]',
        ".price",
        ".price-wrapper",
        '[aria-label*="$"]',
    ]
    for selector in selector_candidates:
        element = card.select_one(selector)
        if not element:
            continue
        value = parse_price(element.get_text(" ", strip=True))
        if value:
            return value

    regex_candidates = [
        r"\$\s?([\d]{1,3}(?:[,\s]\d{3})+|\d{4,6})",
        r"([\d]{1,3}(?:[,\s]\d{3})+|\d{4,6})\s?\$",
    ]
    for pattern in regex_candidates:
        match = re.search(pattern, full_text)
        if not match:
            continue
        value = parse_price(match.group(0))
        if value:
            return value
    return None


def extract_location(card) -> str:
    selector_candidates = [
        '[data-testid="listing-location"]',
        '[data-testid="location"]',
        ".location",
        ".item-location",
    ]
    for selector in selector_candidates:
        element = card.select_one(selector)
        if not element:
            continue
        text = element.get_text(" ", strip=True)
        if text:
            return text
    return "N/A"


def extract_posted_time(card) -> str:
    selector_candidates = [
        '[data-testid="listing-date-value"]',
        '[data-testid="listing-date"]',
        '[data-testid="date-posted"]',
        '[data-testid="listing-date-and-location"]',
        '[class*="date-posted"]',
        '[class*="time"]',
        "time",
        ".date-posted",
    ]
    for selector in selector_candidates:
        element = card.select_one(selector)
        if not element:
            continue
        text = element.get_text(" ", strip=True)
        if text:
            # Prefer relative time text like "< 1 minute ago", "2 hours ago".
            match = re.search(
                r"(<\s*1\s*minute\s*ago|\d+\s*(?:minute|minutes|hour|hours|day|days)\s*ago|yesterday)",
                text.lower(),
            )
            if match:
                return match.group(1)

    full_text = card.get_text(" ", strip=True).lower()
    match = re.search(
        r"(<\s*1\s*minute\s*ago|\d+\s*(?:minute|minutes|hour|hours|day|days)\s*ago|yesterday)",
        full_text,
    )
    if match:
        return match.group(1)
    return "N/A"


def build_posted_time_map(html: str) -> dict[str, str]:
    posted_map: dict[str, str] = {}
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    if not match:
        return posted_map
    try:
        data = json.loads(unescape(match.group(1)))
        apollo_state = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
        for key, value in apollo_state.items():
            if not isinstance(key, str) or not key.startswith("AutosListing:"):
                continue
            if not isinstance(value, dict):
                continue
            listing_id = str(value.get("id") or "")
            sorting_date = value.get("sortingDate")
            if not listing_id or not sorting_date:
                continue
            posted_map[listing_id] = iso_to_relative_posted_time(sorting_date)
    except Exception:
        return posted_map
    return posted_map


def iso_to_relative_posted_time(iso_value: str) -> str:
    try:
        posted_dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        if posted_dt.tzinfo is None:
            posted_dt = posted_dt.replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        delta_seconds = max(0, int((now_dt - posted_dt).total_seconds()))
        minutes = delta_seconds // 60
        hours = minutes // 60
        if minutes < 1:
            return "< 1 minute ago"
        if minutes == 1:
            return "1 minute ago"
        if minutes < 60:
            return f"{minutes} minutes ago"
        if hours == 1:
            return "1 hour ago"
        return f"{hours} hours ago"
    except Exception:
        return "N/A"


def is_kijiji_posted_within_10_minutes(posted_text: str) -> bool:
    """Only '< 1 minute ago' or 'X minutes ago' with X<=10 (Kijiji-style). All else skip."""
    if not posted_text or posted_text == "N/A":
        return False
    text = posted_text.strip().lower()
    if re.search(r"<\s*1\s*minute\s*ago", text):
        return True
    minute_match = re.search(r"(\d+)\s*minutes?\s*ago", text)
    if minute_match:
        return int(minute_match.group(1)) <= 10
    return False


def parse_make_model_year(title: str) -> tuple[Optional[int], Optional[str], Optional[str]]:
    if not title:
        return None, None, None
    parts = title.strip().split()
    if len(parts) < 3:
        return None, None, None
    if not (parts[0].isdigit() and len(parts[0]) == 4):
        return None, None, None
    year = int(parts[0])
    make = parts[1]
    model = parts[2]
    return year, make, model


def analyze_vehicle(listing: dict) -> dict[str, Any]:
    title = listing.get("title") or ""
    description = listing.get("description") or ""
    combined = f"{title} {description}".lower()
    mileage = listing.get("mileage")

    seller_type = "私人"
    if any(keyword in combined for keyword in DEALER_KEYWORDS):
        seller_type = "经销商"

    risk_hits: list[str] = []
    for pattern, label in RISK_KEYWORD_PATTERNS:
        if re.search(pattern, combined, flags=re.IGNORECASE):
            if label not in risk_hits:
                risk_hits.append(label)

    highlight_hits: list[str] = []
    for pattern, label in HIGHLIGHT_PATTERNS:
        if re.search(pattern, combined, flags=re.IGNORECASE):
            if label not in highlight_hits:
                highlight_hits.append(label)
    if mileage is not None and mileage < 100_000:
        if "low mileage" not in highlight_hits:
            highlight_hits.insert(0, "low mileage (<100k km)")

    if risk_hits:
        rating = "🚨风险"
    elif highlight_hits or (mileage is not None and mileage < 100_000):
        rating = "⭐好"
    else:
        rating = "⚠️注意"

    risk_text = ",".join(risk_hits) if risk_hits else "无"
    highlight_text = ",".join(highlight_hits) if highlight_hits else "无"
    detail = (
        f"卖家:{seller_type}｜风险词:{risk_text}｜亮点:{highlight_text}"
    )

    return {
        "rating": rating,
        "seller_type": seller_type,
        "risk_hits": risk_hits,
        "highlight_hits": highlight_hits,
        "detail": detail,
    }


def extract_prices_from_autotrader_html(html: str) -> list[int]:
    prices: list[int] = []
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, flags=re.DOTALL
    )
    if match:
        try:
            data = json.loads(unescape(match.group(1)))
            blob = json.dumps(data)
            for found in re.finditer(r'"price"\s*:\s*(\d{4,6})', blob):
                value = int(found.group(1))
                if 2_000 <= value <= 500_000:
                    prices.append(value)
        except Exception:
            pass
    for found in re.finditer(r"\$\s*([\d,]+)", html):
        value = parse_price(found.group(0))
        if value and 2_000 <= value <= 500_000:
            prices.append(value)
    ordered: list[int] = []
    seen: set[int] = set()
    for price in prices:
        if price not in seen:
            seen.add(price)
            ordered.append(price)
    return ordered


def fetch_autotrader_reference_price(
    year: Optional[int], make: Optional[str], model: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    if not year or not make or not model:
        return None, None
    params = {
        "rcp": "15",
        "rcs": "0",
        "srt": "4",
        "make": make,
        "model": model,
        "yRange": f"{year},{year}",
    }
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    url = f"https://www.autotrader.ca/cars/on/toronto/?{query}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-CA,en;q=0.9",
    }
    try:
        response = requests.get(
            url, headers=headers, timeout=AUTOTRADER_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        html = response.text
    except Exception:
        return None, None
    if len(html) < 2000 or "Incapsula" in html or "_Incapsula_" in html:
        return None, None
    prices = extract_prices_from_autotrader_html(html)
    if not prices:
        return None, None
    top = prices[:5]
    average = sum(top) // len(top)
    suggested = int(average * 0.9)
    return average, suggested


def enrich_listing_for_push(listing: dict) -> None:
    listing["analysis"] = analyze_vehicle(listing)
    year, make, model = parse_make_model_year(listing.get("title", ""))
    ref, suggested = fetch_autotrader_reference_price(year, make, model)
    listing["autotrader_ref"] = ref
    listing["autotrader_suggested"] = suggested


def extract_listing(card, posted_time_map: dict[str, str]) -> Optional[dict]:
    listing_id = card.get("data-listing-id") or card.get("data-vip-url")
    title_el = card.select_one('[data-testid="listing-title"], .title, a.title')
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

    price = extract_price(card, full_text)
    year = parse_year(title)
    mileage = parse_mileage(full_text)
    location = extract_location(card)
    posted_time = posted_time_map.get(str(listing_id)) or extract_posted_time(card)

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
        "location": location,
        "posted_time": posted_time,
        "link": link,
        "image_url": image_url,
    }


def scrape_kijiji() -> list[dict]:
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    response = requests.get(KIJIJI_URL, headers=headers, timeout=30)
    response.raise_for_status()

    posted_time_map = build_posted_time_map(response.text)
    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select(
        '[data-testid="listing-card"], [data-listing-id], .search-item, .regular-ad'
    )

    listings = []
    seen = set()
    for card in cards:
        data = extract_listing(card, posted_time_map)
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
    mileage_text = f"{listing['mileage']:,}" if listing.get("mileage") is not None else "N/A"
    year_text = str(listing["year"]) if listing.get("year") else "N/A"
    location_text = listing.get("location") or "N/A"
    posted_text = listing.get("posted_time") or "N/A"
    analysis = listing.get("analysis") or {}
    rating = analysis.get("rating", "⚠️注意")
    detail = analysis.get("detail", "")
    ref = listing.get("autotrader_ref")
    suggested = listing.get("autotrader_suggested")
    if ref is not None and suggested is not None:
        ref_line = f"💰 AutoTrader参考价：${ref:,}"
        sug_line = f"💰 建议出价：${suggested:,}"
    else:
        ref_line = "💰 AutoTrader参考价：参考价暂无"
        sug_line = "💰 建议出价：参考价暂无"
    return (
        "🚗 New Listing\n"
        f"Title: {listing['title']}\n"
        f"Price: {price_text}\n"
        f"Year: {year_text} | Mileage: {mileage_text} km\n"
        f"Location: {location_text}\n"
        f"Posted: {posted_text}\n"
        "---\n"
        f"📊 分析：{rating} {detail}\n"
        f"{ref_line}\n"
        f"{sug_line}\n"
        f"Link: {listing['link']}"
    )


def send_telegram(listing: dict) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    text = build_message(listing)
    image_url = listing.get("image_url")

    if image_url and len(text) <= 1024:
        response = requests.post(
            f"{base_url}/sendPhoto",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": image_url,
                "caption": text,
            },
            timeout=20,
        )
        return response.ok
    response = requests.post(
        f"{base_url}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20,
    )
    return response.ok


def scrape_and_notify() -> int:
    listings = scrape_kijiji()
    conn = get_db_connection()
    new_count = 0
    try:
        for listing in listings:
            if not is_kijiji_posted_within_10_minutes(
                listing.get("posted_time") or "N/A"
            ):
                continue
            if is_seen(conn, listing["listing_id"]):
                continue
            enrich_listing_for_push(listing)
            sent = send_telegram(listing)
            if not sent:
                continue
            save_seen(conn, listing)
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
        running = monitor_state["running"]
        last_scrape = monitor_state["last_scrape"] or "Never"
        last_error = monitor_state["last_error"] or "None"
        new_today = monitor_state["new_today"]

    html = """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Kijiji Monitor Dashboard</title>
        <style>
          :root {
            --bg: #0b1220;
            --panel: #121a2b;
            --text: #e5e7eb;
            --muted: #9ca3af;
            --ok: #22c55e;
            --err: #ef4444;
            --border: #1f2937;
          }
          * { box-sizing: border-box; }
          body {
            margin: 0;
            min-height: 100vh;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: radial-gradient(circle at top, #111827 0%, var(--bg) 60%);
            color: var(--text);
            padding: 28px;
          }
          .container {
            max-width: 980px;
            margin: 0 auto;
          }
          .title {
            font-size: 30px;
            font-weight: 700;
            margin: 0 0 20px 0;
          }
          .subtitle {
            color: var(--muted);
            margin: 0 0 28px 0;
          }
          .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
          }
          .card {
            background: linear-gradient(180deg, #172033 0%, var(--panel) 100%);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 18px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
          }
          .label {
            color: var(--muted);
            font-size: 12px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
          }
          .value {
            margin-top: 10px;
            font-size: 24px;
            font-weight: 700;
            line-height: 1.2;
            word-break: break-word;
          }
          .ok { color: var(--ok); }
          .err { color: var(--err); }
        </style>
      </head>
      <body>
        <div class="container">
          <h1 class="title">Railway Dashboard</h1>
          <p class="subtitle">Kijiji GTA Car Monitor Runtime Status</p>
          <div class="grid">
            <div class="card">
              <div class="label">系统状态</div>
              <div class="value {{ 'ok' if running else 'err' }}">{{ 'Running' if running else 'Stopped' }}</div>
            </div>
            <div class="card">
              <div class="label">最后抓取时间</div>
              <div class="value">{{ last_scrape }}</div>
            </div>
            <div class="card">
              <div class="label">今日新车数量</div>
              <div class="value ok">{{ new_today }}</div>
            </div>
            <div class="card">
              <div class="label">最后错误信息</div>
              <div class="value {{ 'err' if last_error != 'None' else 'ok' }}">{{ last_error }}</div>
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
        new_today=new_today,
        last_error=last_error,
    )


def main() -> None:
    init_db()
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
