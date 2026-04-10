# Kijiji Toronto Car Monitor

Professional Python monitor for Toronto car listings on Kijiji with:

- periodic scraping with rotating user-agents
- local SQLite database to dedupe seen listing IDs
- market reference price comparison and HOT DEAL tagging
- PRIVATE vs DEALER tagging and private-first alert priority
- Telegram alerts with listing image + direct link
- web dashboard for monitor status

## Features

- Source: `https://www.kijiji.ca/b-cars-trucks/toronto/c174l1700273`
- Sleep window between scrapes: random `280-320` seconds
- Filtering excludes titles containing:
  - `WANTED`
  - `REBUILT`
  - `ACCIDENT`
  - `PARTS`
- HOT DEAL logic:
  - A listing is tagged `🔥 HOT DEAL` when price `< 90%` of market reference
  - Market reference is loaded from `market_reference.json`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `.env` values:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Run

```bash
python app.py
```

Dashboard:

- [http://localhost:5000](http://localhost:5000)

## Dashboard fields

- **System Status**: Running/Stopped
- **Last Scrape Time**: Timestamp of most recent completed scrape
- **Deals Found Today**: Number of HOT DEAL listings identified today
- **Last Error**: Last exception text if scrape or notifications failed

## Database

The app creates `kijiji_monitor.db` automatically with `listings` table.
`listing_id` is unique to prevent duplicate alerts.

## Market Reference

Edit `market_reference.json` to tune Toronto baseline prices by model.
