#!/usr/bin/env python3
"""
NYC Concert Finder — zero credentials required.

Artists are scraped from Spotify using headless Chrome (Selenium).
NYC concert dates are scraped from Songkick.

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
from datetime import datetime, timezone
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

app = Flask(__name__)

DEFAULT_PLAYLIST = "https://open.spotify.com/playlist/3eyYxErnxrMTDE6m8zy57w"

# Local files (never committed — add to .gitignore)
EMAILS_FILE     = Path(__file__).parent / "emails.json"
MAIL_CFG_FILE   = Path(__file__).parent / "mail_config.json"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

def sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Spotify — headless browser scrape (no API key needed)
# ---------------------------------------------------------------------------

# Selectors for track-row artist links (most specific first)
_ARTIST_SELECTORS = [
    "[data-testid='tracklist-row'] a[href*='/artist/']",   # standard playlist row
    "[data-testid='track-list-row'] a[href*='/artist/']",
    "div[aria-rowindex] a[href*='/artist/']",
    "a[href*='/artist/']",                                  # broad fallback
]

def _harvest_artists(driver) -> dict:
    """Pull every unique artist link visible in the current DOM state."""
    artists = {}
    for selector in _ARTIST_SELECTORS:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            for a in elements:
                name = (a.text or "").strip()
                href = (a.get_attribute("href") or "")
                if name and "/artist/" in href and name not in artists:
                    artists[name] = href
            break   # use the first selector that actually returns something
    return artists


def scrape_spotify_playlist(playlist_url: str) -> tuple:
    """
    Returns (playlist_name, playlist_image_url, {artist_name: spotify_url}).
    Scrolls through the full playlist so virtual-DOM lazy-loading is triggered.
    """
    driver = _make_driver()
    try:
        driver.get(playlist_url)

        # 1. Wait for the tracklist to appear (up to 25s)
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/artist/']"))
            )
        except Exception:
            pass

        # 2. Incremental scroll: move 600 px at a time, harvest after each step.
        #    Stop when the artist count is stable for 4 consecutive scrolls.
        all_artists: dict = {}
        stable_rounds = 0
        last_count    = 0

        for _ in range(80):                         # hard cap: 80 scroll steps
            all_artists.update(_harvest_artists(driver))

            if len(all_artists) == last_count:
                stable_rounds += 1
                if stable_rounds >= 4:
                    break                           # nothing new — we're done
            else:
                stable_rounds = 0
                last_count = len(all_artists)

            driver.execute_script("window.scrollBy(0, 600)")
            time.sleep(0.7)

        # 3. Final sweep after scroll settles
        all_artists.update(_harvest_artists(driver))

        # 4. Playlist name from page title
        title         = driver.title
        playlist_name = title.split(" - playlist")[0].strip() if " - playlist" in title else title

        # 5. Cover image
        playlist_image = ""
        for img_sel in (
            "img[data-testid='playlist-image']",
            ".cover-art img",
            "img[src*='mosaic']",
        ):
            try:
                el = driver.find_element(By.CSS_SELECTOR, img_sel)
                playlist_image = el.get_attribute("src") or ""
                if playlist_image:
                    break
            except Exception:
                pass

        return playlist_name, playlist_image, all_artists

    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Songkick scraper — multi-city concert search
# ---------------------------------------------------------------------------

# Cities we watch — keywords matched against the Songkick location string
WATCH_CITIES = [
    {"label": "New York City",       "keywords": ["new york", "brooklyn", "bronx", "queens", "staten island", "nyc"]},
    {"label": "Prince Edward Island","keywords": ["charlottetown", "prince edward island", "pei"]},
    {"label": "Washington DC",       "keywords": ["washington", "washington dc", "arlington", "alexandria"]},
]


def _soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=12)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _city_label(location_text: str) -> Optional[str]:
    """Return the city label if the location matches any watched city, else None."""
    text = location_text.lower()
    for city in WATCH_CITIES:
        if any(k in text for k in city["keywords"]):
            return city["label"]
    return None


def _clean_name(name: str) -> str:
    """Strip featuring credits, punctuation noise, and normalise for search."""
    # Remove feat./ft. suffixes:  "Artist feat. Other" → "Artist"
    name = re.sub(r'\s*(feat\.?|ft\.?|featuring)\s+.*', '', name, flags=re.I)
    # Remove parenthetical suffixes: "Artist (Official)" → "Artist"
    name = re.sub(r'\s*\(.*?\)', '', name)
    # Collapse extra whitespace
    return name.strip()


def _name_variants(name: str) -> list:
    """Return a list of search queries to try, most specific first."""
    cleaned = _clean_name(name)
    variants = [name]
    if cleaned != name:
        variants.append(cleaned)
    # If name has & or and, try just the first part
    for sep in [" & ", " and "]:
        if sep in name.lower():
            first = re.split(sep, name, maxsplit=1, flags=re.I)[0].strip()
            if first not in variants:
                variants.append(first)
    return variants


def _name_matches(result_text: str, artist_name: str) -> bool:
    """Check whether a Songkick result name is a plausible match."""
    result = result_text.lower().strip()
    target = _clean_name(artist_name).lower().strip()
    # Exact or near-exact match
    if target in result or result in target:
        return True
    # At least half the significant words match
    words = [w for w in target.split() if len(w) > 2]
    if not words:
        return True
    matches = sum(1 for w in words if w in result)
    return matches / len(words) >= 0.5


def _songkick_artist_path(artist_name: str) -> Optional[str]:
    """
    Search Songkick for the artist.
    Search results live in <li class="artist"> items; the name is in the
    nested <p class="summary"> — NOT in the <a> element itself.
    """
    for variant in _name_variants(artist_name):
        query = urllib.parse.quote_plus(variant)
        try:
            soup = _soup(
                f"https://www.songkick.com/search?utf8=%E2%9C%93&query={query}&type=artists"
            )
        except Exception:
            continue

        # Songkick search results: <li class="artist"> … <p class="summary">Name</p>
        for li in soup.find_all("li", class_="artist"):
            # Get the artist link (href contains /artists/NNN-slug)
            a = li.find("a", href=re.compile(r"/artists/\d+"))
            if not a:
                continue
            href = a.get("href", "").split("?")[0]

            # Get the displayed name from the summary paragraph
            summary = li.find("p", class_="summary")
            displayed = summary.get_text(strip=True) if summary else a.get_text(strip=True)

            if _name_matches(displayed, artist_name):
                return href

    return None


def _parse_events(artist_path: str) -> list:
    """Scrape the artist's Songkick calendar; return events in any watched city."""
    try:
        soup = _soup(f"https://www.songkick.com{artist_path}/calendar")
    except Exception:
        return []

    events = []

    for li in soup.select(
        "li.event-listing, "
        "li[class*='event-listing'], "
        "li.concert, "
        "li[class*='concert']"
    ):
        # ── Location ────────────────────────────────────────────────
        loc_el = li.select_one(
            ".location, .venue-location, [class*='location'], "
            ".summary .location, em.location"
        )
        location_text = loc_el.get_text(" ", strip=True) if loc_el else ""
        city = _city_label(location_text)
        if not city:
            continue

        # ── Event name ───────────────────────────────────────────────
        name_el = (
            li.select_one(".event-description strong")
            or li.select_one(".summary strong")
            or li.select_one("h3 a")
            or li.select_one("a[href*='/concerts/']")
            or li.select_one("a[href*='/events/']")
        )
        event_name = name_el.get_text(strip=True) if name_el else ""

        # ── Date & time ──────────────────────────────────────────────
        time_el  = li.select_one("time")
        date_raw = time_el.get("datetime", "") if time_el else ""
        date_display = time_display = ""
        if date_raw:
            try:
                dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                date_display = dt.strftime("%A, %B %-d, %Y")
                time_display = dt.strftime("%-I:%M %p")
            except Exception:
                date_display = date_raw

        # ── Venue ────────────────────────────────────────────────────
        venue_el = li.select_one(
            ".venue-name, strong.venue, "
            "[class*='venue-name'], .summary .venue"
        )
        venue = venue_el.get_text(strip=True) if venue_el else ""

        # ── Ticket URL ───────────────────────────────────────────────
        link_el    = name_el or li.select_one("a[href]")
        raw_href   = (link_el.get("href", "") if link_el else "")
        ticket_url = ("https://www.songkick.com" + raw_href) if raw_href.startswith("/") else raw_href

        events.append({
            "event_name":  event_name or city,
            "date":        date_display or "TBA",
            "date_raw":    date_raw,
            "time":        time_display,
            "venue":       venue or city,
            "city":        city,
            "tickets_url": ticket_url,
        })

    return events


def find_concerts(artist_name: str) -> list:
    """Find upcoming shows for one artist across all watched cities."""
    try:
        path = _songkick_artist_path(artist_name)
        return _parse_events(path) if path else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", default_playlist=DEFAULT_PLAYLIST)


@app.route("/api/concerts")
def concerts_stream():
    playlist_url = request.args.get("playlist_url", DEFAULT_PLAYLIST).strip()

    def generate():
        try:
            # Step 1 — scrape Spotify with headless Chrome
            yield sse("status", {"message": "Opening Spotify playlist in headless browser..."})
            try:
                playlist_name, playlist_image, artists_map = scrape_spotify_playlist(playlist_url)
            except Exception as e:
                yield sse("error", {"message": f"Could not load playlist: {e}"})
                return

            yield sse("playlist_info", {"name": playlist_name, "image": playlist_image})

            artists = sorted(artists_map)
            yield sse("artists_found", {"count": len(artists)})

            # Step 2 — search Songkick for NYC concerts per artist
            found_count = 0
            for i, artist in enumerate(artists):
                yield sse("progress", {
                    "current": i + 1,
                    "total":   len(artists),
                    "artist":  artist,
                })
                concerts = find_concerts(artist)
                if concerts:
                    found_count += 1
                    yield sse("result", {
                        "artist":      artist,
                        "spotify_url": artists_map.get(artist, ""),
                        "concerts":    concerts,
                    })
                time.sleep(0.4)

            yield sse("done", {
                "total_artists":      len(artists),
                "artists_with_shows": found_count,
            })

        except Exception as e:
            yield sse("error", {"message": str(e)})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Email recipients management
# ---------------------------------------------------------------------------

def _load_emails() -> list:
    if EMAILS_FILE.exists():
        try:
            return json.loads(EMAILS_FILE.read_text())
        except Exception:
            pass
    return []


def _save_emails(emails: list):
    EMAILS_FILE.write_text(json.dumps(sorted(set(emails)), indent=2))


def _load_mail_cfg() -> dict:
    if MAIL_CFG_FILE.exists():
        try:
            return json.loads(MAIL_CFG_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_mail_cfg(cfg: dict):
    MAIL_CFG_FILE.write_text(json.dumps(cfg, indent=2))


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


@app.route("/api/mail-config", methods=["GET"])
def get_mail_cfg():
    cfg = _load_mail_cfg()
    # Never return the password — just whether it's set
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
# Send report by email
# ---------------------------------------------------------------------------

def _build_html_email(results: list, playlist_name: str) -> str:
    now = datetime.now().strftime("%B %-d, %Y")
    cities = " &amp; ".join(c["label"] for c in WATCH_CITIES)

    rows_html = ""
    # Sort all concerts by date_raw across all artists
    flat = []
    for r in results:
        for c in r["concerts"]:
            flat.append({"artist": r["artist"], **c})
    flat.sort(key=lambda x: x.get("date_raw", ""))

    for c in flat:
        tix = c.get("tickets_url", "#")
        rows_html += f"""
        <tr>
          <td style="padding:10px 12px;font-weight:600">{c['artist']}</td>
          <td style="padding:10px 12px">{c.get('date','TBA')}{' &bull; ' + c['time'] if c.get('time') else ''}</td>
          <td style="padding:10px 12px">{c.get('venue','')}</td>
          <td style="padding:10px 12px">
            <span style="background:#a855f722;color:#a855f7;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:700">{c.get('city','')}</span>
          </td>
          <td style="padding:10px 12px">
            <a href="{tix}" style="background:linear-gradient(120deg,#a855f7,#ec4899);color:#fff;padding:5px 12px;border-radius:5px;text-decoration:none;font-size:12px;font-weight:700">Tickets</a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:700px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">
  <div style="background:linear-gradient(120deg,#a855f7,#ec4899);padding:28px 32px">
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:800">🎵 Concert Report</h1>
    <p style="margin:6px 0 0;color:rgba(255,255,255,.85);font-size:14px">
      {playlist_name} &bull; {cities} &bull; {now}
    </p>
  </div>
  <div style="padding:24px 32px">
    <p style="color:#555;font-size:14px;margin:0 0 20px">
      Found <strong style="color:#a855f7">{len(flat)} upcoming show{'s' if len(flat)!=1 else ''}</strong>
      across <strong>{len(results)} artist{'s' if len(results)!=1 else ''}</strong> — sorted earliest first.
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
      <tbody>
        {''.join(f'<tr style="border-bottom:1px solid #f0f0f0">{r}</tr>' for r in rows_html.strip().split('<tr>')[1:])}
      </tbody>
    </table>
  </div>
  <div style="padding:16px 32px;background:#fafafa;border-top:1px solid #eee;font-size:12px;color:#aaa;text-align:center">
    Sent by Concert Finder &bull; Data from Songkick
  </div>
</div>
</body></html>"""


@app.route("/api/send-report", methods=["POST"])
def send_report():
    data         = request.json or {}
    results      = data.get("results", [])
    playlist_name = data.get("playlist_name", "My Playlist")

    emails = _load_emails()
    if not emails:
        return jsonify({"error": "No email recipients configured"}), 400

    cfg = _load_mail_cfg()
    if not cfg.get("smtp_user") or not cfg.get("smtp_pass"):
        return jsonify({"error": "SMTP not configured — open Email Settings and save your credentials"}), 400

    if not results:
        return jsonify({"error": "No concert results to send"}), 400

    html_body = _build_html_email(results, playlist_name)
    flat_count = sum(len(r["concerts"]) for r in results)
    subject    = f"🎵 {flat_count} upcoming show{'s' if flat_count!=1 else ''} — {playlist_name}"

    sent = []
    errors = []
    try:
        server = smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"),
                              int(cfg.get("smtp_port", 587)))
        server.starttls()
        server.login(cfg["smtp_user"], cfg["smtp_pass"])

        for to_addr in emails:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{cfg.get('from_name','Concert Finder')} <{cfg['smtp_user']}>"
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
