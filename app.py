#!/usr/bin/env python3
"""
Concert Finder — no API credentials required.

1. Reads artists from any public Spotify playlist using headless Chrome.
2. Searches Last.fm (server-rendered, no auth) for upcoming shows in
   New York City, Washington DC, and Prince Edward Island.
3. Streams results live to the browser via Server-Sent Events.
4. Sends formatted HTML email reports to a configurable recipient list.

Run:
    pip install -r requirements_concerts.txt
    python app.py
Then open http://localhost:5001
"""

import json
import re
import smtplib
import time
import urllib.parse
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------

app = Flask(__name__)

DEFAULT_PLAYLIST = "https://open.spotify.com/playlist/3eyYxErnxrMTDE6m8zy57w"

EMAILS_FILE   = Path(__file__).parent / "emails.json"
MAIL_CFG_FILE = Path(__file__).parent / "mail_config.json"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}

# Cities to watch — keywords matched against Last.fm address strings
WATCH_CITIES = [
    {"label": "New York City",        "keywords": ["new york", "brooklyn", "bronx", "queens", "staten island", "nyc"]},
    {"label": "Prince Edward Island", "keywords": ["charlottetown", "prince edward island", "pei"]},
    {"label": "Washington DC",        "keywords": ["washington", "washington dc", "arlington", "alexandria"]},
]


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

def sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Spotify — headless Chrome scraper (no API key needed)
# ---------------------------------------------------------------------------

# CSS selectors tried in order, most specific first
_ARTIST_SELECTORS = [
    "[data-testid='tracklist-row'] a[href*='/artist/']",
    "[data-testid='track-list-row'] a[href*='/artist/']",
    "div[aria-rowindex] a[href*='/artist/']",
    "a[href*='/artist/']",
]


def _make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument(f"user-agent={BROWSER_UA}")
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    driver.set_page_load_timeout(30)
    return driver


def _harvest_artists(driver) -> dict:
    """Return {name: spotify_url} for every artist link in the current DOM."""
    for selector in _ARTIST_SELECTORS:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            artists = {}
            for a in elements:
                name = (a.text or "").strip()
                href = (a.get_attribute("href") or "")
                if name and "/artist/" in href and name not in artists:
                    artists[name] = href
            return artists
    return {}


def scrape_spotify_playlist(playlist_url: str) -> tuple:
    """
    Return (playlist_name, cover_image_url, {artist_name: spotify_url}).
    Scrolls 600 px at a time until the artist list stabilises for 4 rounds,
    ensuring all lazy-loaded tracks are captured.
    """
    driver = _make_driver()
    try:
        driver.get(playlist_url)

        # Wait up to 25 s for the first artist link
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/artist/']"))
            )
        except Exception:
            pass

        # Scroll and harvest until stable
        all_artists: dict = {}
        last_count = stable_rounds = 0

        for _ in range(80):
            all_artists.update(_harvest_artists(driver))
            if len(all_artists) == last_count:
                stable_rounds += 1
                if stable_rounds >= 4:
                    break
            else:
                stable_rounds = 0
                last_count = len(all_artists)
            driver.execute_script("window.scrollBy(0, 600)")
            time.sleep(0.7)

        all_artists.update(_harvest_artists(driver))  # final sweep

        # Playlist name
        title = driver.title
        name = title.split(" - playlist")[0].strip() if " - playlist" in title else title

        # Cover image
        image = ""
        for sel in ("img[data-testid='playlist-image']", ".cover-art img", "img[src*='mosaic']"):
            try:
                image = driver.find_element(By.CSS_SELECTOR, sel).get_attribute("src") or ""
                if image:
                    break
            except Exception:
                pass

        return name, image, all_artists

    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Concert search — Last.fm (server-rendered HTML, no credentials)
# ---------------------------------------------------------------------------

def _city_for_address(address: str) -> Optional[str]:
    """Map a Last.fm venue address string to one of our watched city labels."""
    text = address.lower()
    for city in WATCH_CITIES:
        if any(kw in text for kw in city["keywords"]):
            return city["label"]
    return None


def _artist_slugs(artist_name: str) -> list:
    """
    Generate Last.fm URL slugs to try for this artist.
    Strips feat./ft. suffixes and tries the first half of '&' / 'and' names.
    """
    def clean(name: str) -> str:
        name = re.sub(r'\s*(feat\.?|ft\.?|featuring)\s+.*', '', name, flags=re.I)
        return re.sub(r'\s*\(.*?\)', '', name).strip()

    variants = [artist_name, clean(artist_name)]
    for sep in (" & ", " and "):
        if sep.lower() in artist_name.lower():
            first = re.split(sep, artist_name, maxsplit=1, flags=re.I)[0].strip()
            variants.append(first)

    # Dedupe while preserving order, then URL-encode
    seen, slugs = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            slugs.append(urllib.parse.quote(v, safe=""))
    return slugs


def _fmt_datetime(iso: str) -> tuple:
    """Parse an ISO datetime string into (display_date, display_time)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%A, %B %-d, %Y"), dt.strftime("%-I:%M %p")
    except Exception:
        return iso, ""


def find_concerts(artist_name: str) -> list:
    """
    Return upcoming concerts for artist_name in any watched city.
    Tries multiple slug variants until one returns event rows.
    """
    for slug in _artist_slugs(artist_name):
        try:
            resp = requests.get(
                f"https://www.last.fm/music/{slug}/+events",
                headers=BROWSER_HEADERS,
                timeout=12,
            )
        except Exception:
            continue
        if resp.status_code != 200:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.events-list-item[itemprop='event']")
        if not rows:
            continue

        events = []
        for row in rows:
            addr_el  = row.select_one(".events-list-item-venue--address")
            address  = addr_el.get_text(strip=True) if addr_el else ""
            city     = _city_for_address(address)
            if not city:
                continue

            time_el   = row.select_one("time[datetime]")
            date_raw  = time_el.get("datetime", "") if time_el else ""
            date_disp, time_disp = _fmt_datetime(date_raw) if date_raw else ("TBA", "")

            name_el    = row.select_one("[itemprop='name']")
            event_name = name_el.get_text(strip=True) if name_el else artist_name

            venue_el = row.select_one(".events-list-item-venue--title")
            venue    = venue_el.get_text(strip=True) if venue_el else ""

            link_el  = row.select_one("a.events-list-item-event-name")
            raw_href = link_el.get("href", "") if link_el else ""
            url      = ("https://www.last.fm" + raw_href) if raw_href.startswith("/") else raw_href

            events.append({
                "event_name":  event_name,
                "date":        date_disp,
                "date_raw":    date_raw,
                "time":        time_disp,
                "venue":       venue or city,
                "address":     address,
                "city":        city,
                "tickets_url": url,
            })

        if events:
            # Deduplicate by (date, venue) and sort earliest first
            seen, deduped = set(), []
            for e in events:
                key = (e["date_raw"][:10], re.sub(r'\W', '', e["venue"].lower())[:15])
                if key not in seen:
                    seen.add(key)
                    deduped.append(e)
            return sorted(deduped, key=lambda e: e["date_raw"])

        return []  # artist found but no watched-city events

    return []


# ---------------------------------------------------------------------------
# Streaming route — /api/concerts
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", default_playlist=DEFAULT_PLAYLIST)


@app.route("/api/concerts")
def concerts_stream():
    playlist_url = request.args.get("playlist_url", DEFAULT_PLAYLIST).strip()

    def generate():
        try:
            yield sse("status", {"message": "Reading playlist from Spotify..."})
            try:
                playlist_name, playlist_image, artists_map = scrape_spotify_playlist(playlist_url)
            except Exception as e:
                yield sse("error", {"message": f"Could not load playlist: {e}"})
                return

            yield sse("playlist_info", {"name": playlist_name, "image": playlist_image})
            artists = sorted(artists_map)
            yield sse("artists_found", {"count": len(artists)})
            yield sse("status", {"message": f"Searching {len(artists)} artists on Last.fm..."})

            found_count = 0
            for i, artist in enumerate(artists):
                yield sse("progress", {"current": i + 1, "total": len(artists), "artist": artist})
                concerts = find_concerts(artist)
                if concerts:
                    found_count += 1
                    yield sse("result", {
                        "artist":      artist,
                        "spotify_url": artists_map.get(artist, ""),
                        "concerts":    concerts,
                    })
                time.sleep(0.3)

            yield sse("done", {"total_artists": len(artists), "artists_with_shows": found_count})

        except Exception as e:
            yield sse("error", {"message": str(e)})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Email recipients — /api/emails
# ---------------------------------------------------------------------------

def _load_emails() -> list:
    try:
        return json.loads(EMAILS_FILE.read_text()) if EMAILS_FILE.exists() else []
    except Exception:
        return []


def _save_emails(emails: list):
    EMAILS_FILE.write_text(json.dumps(sorted(set(emails)), indent=2))


@app.route("/api/emails", methods=["GET"])
def get_emails():
    return jsonify(_load_emails())


@app.route("/api/emails", methods=["POST"])
def add_email():
    email = (request.json or {}).get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Invalid email"}), 400
    emails = _load_emails()
    if email not in emails:
        emails.append(email)
        _save_emails(emails)
    return jsonify(_load_emails())


@app.route("/api/emails/<path:email>", methods=["DELETE"])
def remove_email(email):
    emails = [e for e in _load_emails() if e != email.strip().lower()]
    _save_emails(emails)
    return jsonify(emails)


# ---------------------------------------------------------------------------
# Mail config — /api/mail-config
# ---------------------------------------------------------------------------

def _load_mail_cfg() -> dict:
    try:
        return json.loads(MAIL_CFG_FILE.read_text()) if MAIL_CFG_FILE.exists() else {}
    except Exception:
        return {}


def _save_mail_cfg(cfg: dict):
    MAIL_CFG_FILE.write_text(json.dumps(cfg, indent=2))


@app.route("/api/mail-config", methods=["GET"])
def get_mail_cfg():
    cfg = _load_mail_cfg()
    return jsonify({
        "smtp_host":  cfg.get("smtp_host", "smtp.gmail.com"),
        "smtp_port":  cfg.get("smtp_port", 587),
        "smtp_user":  cfg.get("smtp_user", ""),
        "from_name":  cfg.get("from_name", "Concert Finder"),
        "configured": bool(cfg.get("smtp_user") and cfg.get("smtp_pass")),
    })


@app.route("/api/mail-config", methods=["POST"])
def save_mail_cfg():
    data = request.json or {}
    cfg  = _load_mail_cfg()
    for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "from_name"):
        if key in data and data[key] != "":
            cfg[key] = data[key]
    _save_mail_cfg(cfg)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Send report — /api/send-report
# ---------------------------------------------------------------------------

def _build_email_html(results: list, playlist_name: str) -> str:
    cities = " &amp; ".join(c["label"] for c in WATCH_CITIES)
    date   = datetime.now().strftime("%B %-d, %Y")

    flat = sorted(
        [{"artist": r["artist"], **c} for r in results for c in r["concerts"]],
        key=lambda x: x.get("date_raw", ""),
    )

    rows = "".join(f"""
        <tr style="border-bottom:1px solid #f0f0f0">
          <td style="padding:10px 12px;font-weight:600">{c['artist']}</td>
          <td style="padding:10px 12px">{c.get('date','TBA')}{' &bull; ' + c['time'] if c.get('time') else ''}</td>
          <td style="padding:10px 12px">{c.get('venue','')}</td>
          <td style="padding:10px 12px">
            <span style="background:#a855f722;color:#a855f7;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:700">{c.get('city','')}</span>
          </td>
          <td style="padding:10px 12px">
            <a href="{c.get('tickets_url','#')}" style="background:linear-gradient(120deg,#a855f7,#ec4899);color:#fff;padding:5px 12px;border-radius:5px;text-decoration:none;font-size:12px;font-weight:700">Tickets</a>
          </td>
        </tr>""" for c in flat)

    shows  = len(flat)
    artists = len(results)
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:700px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">
  <div style="background:linear-gradient(120deg,#a855f7,#ec4899);padding:28px 32px">
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:800">Concert Report</h1>
    <p style="margin:6px 0 0;color:rgba(255,255,255,.85);font-size:14px">{playlist_name} &bull; {cities} &bull; {date}</p>
  </div>
  <div style="padding:24px 32px">
    <p style="color:#555;font-size:14px;margin:0 0 20px">
      Found <strong style="color:#a855f7">{shows} upcoming show{'s' if shows != 1 else ''}</strong>
      across <strong>{artists} artist{'s' if artists != 1 else ''}</strong> — sorted earliest first.
    </p>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f9f9f9;border-bottom:2px solid #eee">
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;text-transform:uppercase">Artist</th>
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;text-transform:uppercase">Date / Time</th>
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;text-transform:uppercase">Venue</th>
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;text-transform:uppercase">City</th>
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;text-transform:uppercase">Tickets</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <div style="padding:16px 32px;background:#fafafa;border-top:1px solid #eee;font-size:12px;color:#aaa;text-align:center">
    Concert Finder &bull; Data from Last.fm
  </div>
</div>
</body></html>"""


@app.route("/api/send-report", methods=["POST"])
def send_report():
    data          = request.json or {}
    results       = data.get("results", [])
    playlist_name = data.get("playlist_name", "My Playlist")

    emails = _load_emails()
    if not emails:
        return jsonify({"error": "No email recipients configured"}), 400

    cfg = _load_mail_cfg()
    if not cfg.get("smtp_user") or not cfg.get("smtp_pass"):
        return jsonify({"error": "SMTP not configured — open Email Settings and save your credentials"}), 400

    if not results:
        return jsonify({"error": "No concert results to send"}), 400

    html_body  = _build_email_html(results, playlist_name)
    show_count = sum(len(r["concerts"]) for r in results)
    subject    = f"{show_count} upcoming show{'s' if show_count != 1 else ''} — {playlist_name}"

    sent, errors = [], []
    try:
        server = smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 587)))
        server.starttls()
        server.login(cfg["smtp_user"], cfg["smtp_pass"])

        for to_addr in emails:
            msg            = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{cfg.get('from_name', 'Concert Finder')} <{cfg['smtp_user']}>"
            msg["To"]      = to_addr
            msg.attach(MIMEText(html_body, "html"))
            try:
                server.sendmail(cfg["smtp_user"], to_addr, msg.as_string())
                sent.append(to_addr)
            except Exception as e:
                errors.append({"email": to_addr, "error": str(e)})

        server.quit()
    except Exception as e:
        return jsonify({"error": f"SMTP connection failed: {e}"}), 500

    return jsonify({"sent": sent, "errors": errors})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
