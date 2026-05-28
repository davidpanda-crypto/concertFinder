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
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template, request, stream_with_context
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

app = Flask(__name__)

DEFAULT_PLAYLIST = "https://open.spotify.com/playlist/3eyYxErnxrMTDE6m8zy57w"

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

def scrape_spotify_playlist(playlist_url: str) -> tuple:
    """
    Returns (playlist_name, playlist_image_url, {artist_name: spotify_url}).
    Uses a headless Chrome session so no Spotify credentials are required.
    """
    driver = _make_driver()
    try:
        driver.get(playlist_url)

        # Wait for at least one artist link to appear
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/artist/']"))
            )
        except Exception:
            pass  # fall through and collect whatever loaded

        # Scroll down repeatedly to trigger lazy-loading of all tracks
        prev_count = 0
        for _ in range(30):
            links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/artist/']")
            if len(links) == prev_count:
                break
            prev_count = len(links)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.2)

        # Playlist name from page title  ("Playlist name - playlist by … | Spotify")
        title = driver.title
        playlist_name = title.split(" - playlist")[0].strip() if " - playlist" in title else title

        # Playlist cover image
        playlist_image = ""
        try:
            img = driver.find_element(By.CSS_SELECTOR, "img[data-testid='playlist-image'], .cover-art img")
            playlist_image = img.get_attribute("src") or ""
        except Exception:
            pass

        # Collect unique artists
        artists: dict = {}
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='/artist/']"):
            name = a.text.strip()
            href = a.get_attribute("href") or ""
            if name and name not in artists:
                artists[name] = href

        return playlist_name, playlist_image, artists

    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Songkick scraper — NYC concerts
# ---------------------------------------------------------------------------

def _soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=12)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _songkick_artist_path(artist_name: str) -> Optional[str]:
    query = urllib.parse.quote_plus(artist_name)
    soup = _soup(f"https://www.songkick.com/search?utf8=%E2%9C%93&query={query}&type=artists")

    for a in soup.select("ul.artists-search-results li a, .search-results .artist a"):
        href = a.get("href", "")
        if "/artists/" in href:
            return href.split("?")[0]

    m = re.search(r'href="(/artists/\d+-[^"?]+)"', soup.decode_contents())
    return m.group(1) if m else None


def _parse_nyc_events(artist_path: str) -> list:
    soup = _soup(f"https://www.songkick.com{artist_path}/calendar")

    nyc_keywords = {"new york", "brooklyn", "queens", "bronx", "staten island", "nyc"}
    events = []

    for li in soup.select("li.event-listing, li[class*='event']"):
        loc_el = li.select_one(".location, .venue-location, [class*='location']")
        location_text = loc_el.get_text(" ", strip=True).lower() if loc_el else ""
        if not any(k in location_text for k in nyc_keywords):
            continue

        name_el = (
            li.select_one(".event-description strong, .primary-detail, h3 a, .summary a")
            or li.select_one("a[href*='/concerts/']")
            or li.select_one("a[href*='/events/']")
        )
        event_name = name_el.get_text(strip=True) if name_el else artist_path.split("-", 1)[-1].title()

        time_el = li.select_one("time")
        date_raw = time_el.get("datetime", "") if time_el else ""
        date_display = time_display = ""
        if date_raw:
            try:
                dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                date_display = dt.strftime("%A, %B %-d, %Y")
                time_display = dt.strftime("%-I:%M %p")
            except Exception:
                date_display = date_raw

        venue_el = li.select_one(".venue-name, .venue, [class*='venue']")
        venue = venue_el.get_text(strip=True) if venue_el else "New York, NY"

        link_el = name_el or li.select_one("a[href]")
        href = (link_el.get("href", "") if link_el else "")
        ticket_url = ("https://www.songkick.com" + href) if href.startswith("/") else href

        events.append({
            "event_name":  event_name,
            "date":        date_display or "TBA",
            "date_raw":    date_raw,
            "time":        time_display,
            "venue":       venue,
            "tickets_url": ticket_url,
        })

    return events


def find_nyc_concerts(artist_name: str) -> list:
    try:
        path = _songkick_artist_path(artist_name)
        return _parse_nyc_events(path) if path else []
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
                concerts = find_nyc_concerts(artist)
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


if __name__ == "__main__":
    app.run(debug=True, port=5001)
