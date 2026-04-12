import os
import json
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
from flask import Flask

load_dotenv()

KIJIJI_URL = "https://www.kijiji.ca/b-cars-trucks/toronto-gta/c174l1700272"
DB_PATH = "kijiji_seen.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SLEEP_SECONDS = 300

# ---------------------------------------------------------------------------
# DB abstraction: PostgreSQL when DATABASE_URL is set, else SQLite (local dev)
# ---------------------------------------------------------------------------

def _pg_conn():
    import psycopg2
    # Railway may give postgres:// but psycopg2 needs postgresql://
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def _sqlite_conn():
    return sqlite3.connect(DB_PATH)


def _use_pg():
    return bool(DATABASE_URL)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
]

BLOCK_KEYWORDS = ("WANTED", "REBUILT", "SALVAGE", "PARTS ONLY")
DEALER_KEYWORDS = ("dealer", "dealership", "financing available", "omvic", "trade-in", "car lot")

app = Flask(__name__)
monitor_state = {"running": False, "last_scrape": None, "last_error": None, "new_today": 0}
state_lock = threading.Lock()


def init_db():
    if _use_pg():
        conn = _pg_conn()
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS seen (
                listing_id TEXT PRIMARY KEY,
                title TEXT,
                price INTEGER,
                location TEXT,
                posted_time TEXT,
                link TEXT,
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        print("[db] Using PostgreSQL")
    else:
        conn = _sqlite_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                listing_id TEXT PRIMARY KEY,
                title TEXT,
                price INTEGER,
                location TEXT,
                posted_time TEXT,
                link TEXT,
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        print("[db] Using SQLite (local)")


def is_seen(listing_id):
    if _use_pg():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM seen WHERE listing_id=%s LIMIT 1", (listing_id,))
        row = cur.fetchone()
        conn.close()
    else:
        conn = _sqlite_conn()
        row = conn.execute("SELECT 1 FROM seen WHERE listing_id=? LIMIT 1", (listing_id,)).fetchone()
        conn.close()
    return row is not None


def mark_seen(listing):
    if _use_pg():
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO seen (listing_id, title, price, location, posted_time, link, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (listing_id) DO NOTHING""",
            (listing["listing_id"], listing["title"], listing.get("price"), listing.get("location"),
             listing.get("posted_time"), listing["link"], datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
    else:
        conn = _sqlite_conn()
        conn.execute(
            "INSERT OR IGNORE INTO seen (listing_id, title, price, location, posted_time, link, created_at) VALUES (?,?,?,?,?,?,?)",
            (listing["listing_id"], listing["title"], listing.get("price"), listing.get("location"),
             listing.get("posted_time"), listing["link"], datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()


def parse_price(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def parse_year(title):
    m = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    if m:
        y = int(m.group(1))
        if 1980 <= y <= datetime.now().year + 1:
            return y
    return None


def parse_mileage(text):
    if not text or "km" not in text.lower():
        return None
    digits = re.sub(r"[^\d]", "", text.lower())
    return int(digits) if digits and int(digits) < 1500000 else None


def classify_seller(text):
    t = (text or "").lower()
    return "DEALER" if any(k in t for k in DEALER_KEYWORDS) else "PRIVATE"


def analyze_listing(title, description, price, mileage, year):
    text = f"{title} {description}".upper()
    flags = []
    highlights = []

    risk_words = ["ACCIDENT", "REBUILT", "SALVAGE", "AS-IS", "NO SAFETY", "DAMAGED", "FLOOD"]
    good_words = ["NO ACCIDENT", "ONE OWNER", "LOW KM", "SAFETY INCLUDED", "CERTIFIED"]

    for w in risk_words:
        if w in text:
            flags.append(w)
    for w in good_words:
        if w in text:
            highlights.append(w)

    if flags:
        rating = "🚨 HIGH RISK"
    elif highlights:
        rating = "⭐ LOOKS GOOD"
    else:
        rating = "⚠️ CHECK IT"

    return rating, flags, highlights


def get_autotrader_price(year, title):
    try:
        words = title.lower().split()
        makes = ["honda", "toyota", "ford", "chevrolet", "nissan", "hyundai", "kia", "mazda",
                 "bmw", "mercedes", "audi", "volkswagen", "jeep", "ram", "dodge", "subaru",
                 "lexus", "acura", "infiniti", "volvo", "gmc", "buick", "cadillac"]
        make = next((w for w in words if w in makes), None)
        if not make or not year:
            return None
        url = f"https://www.autotrader.ca/cars/{make}/?rcp=15&rcs=0&prx=100&loc=Toronto&year={year}&year2={year}"
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        prices = re.findall(r'"price":(\d+)', r.text)
        prices = [int(p) for p in prices if 2000 < int(p) < 500000]
        if len(prices) >= 3:
            return int(sum(sorted(prices)[:5]) / min(5, len(prices)))
    except Exception:
        pass
    return None


def scrape_listings():
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    r = requests.get(KIJIJI_URL, headers=headers, timeout=30)
    print(f"[scrape] status={r.status_code} url={r.url} content_length={len(r.text)}")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Parse JSON-LD ItemList — far more reliable than CSS selectors
    items = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
                break
        except Exception:
            continue
    print(f"[scrape] JSON-LD items found: {len(items)}")

    listings = []
    seen_ids = set()

    for i, element in enumerate(items):
        try:
            item = element.get("item", {})

            title = item.get("name", "").strip()
            if not title:
                print(f"[item {i}] SKIP empty title")
                continue

            text_upper = (title + " " + item.get("description", "")).upper()
            hit = next((k for k in BLOCK_KEYWORDS if k in text_upper), None)
            if hit:
                print(f"[item {i}] SKIP block_keyword={hit!r} title={title[:40]!r}")
                continue

            price = parse_price(item.get("offers", {}).get("price", ""))
            if not price:
                print(f"[item {i}] SKIP no_price title={title[:40]!r}")
                continue

            link = item.get("url", "")
            if not link:
                print(f"[item {i}] SKIP no_link title={title[:40]!r}")
                continue

            # Extract numeric listing ID from URL path (last path segment before query)
            m = re.search(r'/(\d{6,})', link)
            listing_id = m.group(1) if m else link

            if listing_id in seen_ids:
                print(f"[item {i}] SKIP dup_id={listing_id!r}")
                continue
            seen_ids.add(listing_id)

            mileage_val = item.get("mileageFromOdometer", {}).get("value")
            mileage = int(mileage_val) if mileage_val else None
            year = int(item.get("vehicleModelDate", 0)) or parse_year(title)
            description = item.get("description", "")
            seller = classify_seller(description)
            image_url = item.get("image", "")
            # location lives in the URL path segment, e.g. /v-cars-trucks/toronto-gta/...
            loc_m = re.search(r'/v-cars-trucks/([^/]+)/', link)
            location = loc_m.group(1).replace("-", " ").title() if loc_m else "N/A"

            listings.append({
                "listing_id": listing_id,
                "title": title,
                "price": price,
                "mileage": mileage,
                "year": year,
                "seller": seller,
                "location": location,
                "posted_time": "N/A",
                "link": link,
                "image_url": image_url,
                "description": description,
            })
        except Exception as e:
            print(f"[item {i}] SKIP exception={e}")
            continue

    return listings


def send_telegram(listing):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    year = listing.get("year") or "N/A"
    mileage = f"{listing['mileage']:,} km" if listing.get("mileage") else "N/A"
    price = f"${listing['price']:,}" if listing.get("price") else "N/A"

    rating, flags, highlights = analyze_listing(
        listing["title"], listing.get("description", ""),
        listing.get("price"), listing.get("mileage"), listing.get("year")
    )

    analysis_text = rating
    if flags:
        analysis_text += f" | ⚠️ {', '.join(flags)}"
    if highlights:
        analysis_text += f" | ✅ {', '.join(highlights)}"

    market_price = get_autotrader_price(listing.get("year"), listing["title"])
    if market_price:
        suggested = int(market_price * 0.9)
        price_lines = f"💰 Market ref: ${market_price:,}\n💰 Suggested offer: ${suggested:,}"
    else:
        price_lines = "💰 Market ref: N/A"

    text = (
        f"🚗 New Listing\n"
        f"Title: {listing['title']}\n"
        f"Price: {price}\n"
        f"Year: {year} | Mileage: {mileage}\n"
        f"Seller: {listing.get('seller', 'N/A')}\n"
        f"Location: {listing.get('location', 'N/A')}\n"
        f"Posted: {listing.get('posted_time', 'N/A')}\n"
        f"---\n"
        f"📊 Analysis: {analysis_text}\n"
        f"{price_lines}\n"
        f"🔗 {listing['link']}"
    )

    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    img = listing.get("image_url", "")

    def send_message():
        resp = requests.post(f"{base}/sendMessage", json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4096]
        }, timeout=20)
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram sendMessage error: {data}")

    try:
        if img and img.startswith("http"):
            resp = requests.post(f"{base}/sendPhoto", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": img,
                "caption": text[:1024]
            }, timeout=20)
            data = resp.json()
            if not data.get("ok"):
                print(f"Telegram sendPhoto failed ({data.get('description')}), falling back to sendMessage")
                send_message()
        else:
            send_message()
    except Exception as e:
        print(f"Telegram error: {e}")
        try:
            send_message()
        except Exception as e2:
            print(f"Telegram fallback error: {e2}")


def scrape_cycle():
    listings = scrape_listings()
    print(f"[cycle] total listings after scrape: {len(listings)}")
    new = 0
    for l in listings:
        lid = l["listing_id"]
        seen = is_seen(lid)
        print(f"[cycle] id={lid!r} seen={seen} title={l['title'][:40]!r}")
        if not seen:
            mark_seen(l)
            send_telegram(l)
            new += 1
    print(f"[cycle] done: {new} new, {len(listings) - new} already seen")
    return new


def monitor_loop():
    with state_lock:
        monitor_state["running"] = True
    while True:
        try:
            new = scrape_cycle()
            with state_lock:
                monitor_state["last_scrape"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                monitor_state["last_error"] = None
                monitor_state["new_today"] = monitor_state.get("new_today", 0) + new
            print(f"[{datetime.now().isoformat(timespec='seconds')}] New listings: {new}")
        except Exception as e:
            with state_lock:
                monitor_state["last_error"] = str(e)
            print(f"Error: {e}")
        time.sleep(SLEEP_SECONDS)


@app.route("/")
def dashboard():
    with state_lock:
        status = "Running" if monitor_state["running"] else "Stopped"
        last = monitor_state["last_scrape"] or "Never"
        error = monitor_state["last_error"] or "None"
        new_today = monitor_state.get("new_today", 0)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Car Monitor</title>
<style>
body{{font-family:Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px}}
.wrap{{max-width:700px;margin:0 auto}}
h1{{font-size:24px;margin-bottom:20px}}
.card{{background:#161b22;border-radius:10px;padding:18px;margin-bottom:14px;border:1px solid #30363d}}
.label{{color:#8b949e;font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.value{{font-size:20px;margin-top:6px;font-weight:600}}
.ok{{color:#3fb950}}.err{{color:#f85149}}
</style></head>
<body><div class="wrap">
<h1>🚗 Kijiji GTA Car Monitor</h1>
<div class="card"><div class="label">Status</div>
<div class="value {'ok' if status=='Running' else 'err'}">{status}</div></div>
<div class="card"><div class="label">Last Scrape</div><div class="value">{last}</div></div>
<div class="card"><div class="label">New Today</div><div class="value">{new_today}</div></div>
<div class="card"><div class="label">Last Error</div>
<div class="value {'err' if error!='None' else 'ok'}">{error}</div></div>
</div></body></html>"""
    return html


def main():
    init_db()
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
