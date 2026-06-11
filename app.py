#!/usr/bin/env python3
"""
Concert Finder — no API credentials required.

1. Reads artists from any public Spotify playlist (or Liked Songs) via headless Chrome.
2. Searches Last.fm (server-rendered, no auth) for upcoming shows in
   New York City, Washington DC, and Prince Edward Island.
3. Streams results live to the browser via Server-Sent Events.
4. Sends HTML email reports and SMS texts to a configurable recipient list.

Run:
    pip install -r requirements_concerts.txt
    python app.py            # production (debug off)
    FLASK_DEBUG=1 python app.py   # development
Then open http://localhost:5001
"""

import concurrent.futures
import html
import json
import logging
import os
import re
import smtplib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Constants

# Scroll behaviour
SCROLL_PX          = 500     # pixels per scroll step
SCROLL_DELAY_S     = 0.8     # seconds between scroll steps
STABLE_ROUNDS      = 8       # consecutive unchanged harvests before stopping
MAX_SCROLLS_SHORT  = 1200     # playlist scrape cap (very large playlists)
MAX_SCROLLS_LONG   = 1200     # liked-songs scrape cap (very large libraries)

# Timing
PAGE_SETTLE_S      = 3       # wait after initial navigation
POST_LOGIN_S       = 2       # wait after login completes
ARTIST_WAIT_S      = 20      # max seconds to wait for first artist link
LOGIN_TIMEOUT_S    = 180     # max seconds for user to log in
HTTP_TIMEOUT_S     = 12      # requests.get timeout
CONCERT_WORKERS    = 8       # concurrent Last.fm lookups during a scan

# Email / SMS
MAX_SMS_SHOWS      = 8       # cap on shows included in a text message

# Validation
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_SPOTIFY_PLAYLIST_PREFIX = "https://open.spotify.com/playlist/"

# App setup

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB — blocks oversized POSTs

EMAILS_FILE   = Path(__file__).parent / "emails.json"
PHONES_FILE   = Path(__file__).parent / "phones.json"
MAIL_CFG_FILE = Path(__file__).parent / "mail_config.json"
SPOTIFY_PROFILE = Path.home() / ".concert-finder-spotify-profile"

SMS_GATEWAYS = {
    "AT&T":        "@txt.att.net",
    "Verizon":     "@vtext.com",
    "T-Mobile":    "@tmomail.net",
    "Sprint":      "@messaging.sprintpcs.com",
    "Boost":       "@sms.myboostmobile.com",
    "Cricket":     "@sms.cricketwireless.net",
    "Metro PCS":   "@mymetropcs.com",
    "US Cellular": "@email.uscc.net",
    "Rogers":      "@pcs.rogers.com",
    "Bell":        "@txt.bell.ca",
    "Telus":       "@msg.telus.com",
}

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}

# Shared session for Last.fm/Spotify HTTP lookups — reuses connections (TLS
# handshake + TCP) across the hundreds of requests a large-library scan
# makes instead of opening a fresh connection every time. The pool is sized
# to comfortably cover CONCERT_WORKERS concurrent threads. requests.Session
# + urllib3's connection pool are thread-safe to share this way.
_HTTP = requests.Session()
_HTTP.headers.update(BROWSER_HEADERS)
_HTTP_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
_HTTP.mount("https://", _HTTP_ADAPTER)
_HTTP.mount("http://", _HTTP_ADAPTER)

WATCH_CITIES = [
    {
        "label": "New York City",
        "keywords": ["new york", "brooklyn", "bronx", "queens", "manhattan", "staten island", "nyc"],
        "country": "united states",
    },
    {
        "label": "Prince Edward Island",
        "keywords": ["charlottetown", "prince edward island", "pei"],
        "country": "canada",
    },
    {
        "label": "Washington DC",
        "keywords": ["washington dc", "washington, d.c.", "washington", "arlington", "alexandria"],
        "country": "united states",
    },
]

DEFAULT_PLAYLIST = "https://open.spotify.com/playlist/3eyYxErnxrMTDE6m8zy57w"


# Security headers + error handlers

@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"]  = "nosniff"
    resp.headers["X-Frame-Options"]         = "DENY"
    resp.headers["X-XSS-Protection"]        = "1; mode=block"
    resp.headers["Referrer-Policy"]         = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'"
    )
    return resp


@app.errorhandler(413)
def request_too_large(_):
    return jsonify({"error": "Request body too large (max 1 MB)"}), 413


# SSE helper

def sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _friendly_error(exc: Exception) -> str:
    """
    Turn a raw exception into a short, user-facing message.

    Selenium/chromedriver errors often come with multi-line stack traces
    embedded in the message (e.g. "no such window: target window already
    closed\nfrom unknown error...\nStacktrace:\n0  chromedriver ..."). Strip
    those down to something readable, and recognise the common "the user
    closed the browser window mid-login" case specifically.
    """
    text = str(exc)
    first_line = text.splitlines()[0] if text else type(exc).__name__
    if "no such window" in text or "target window already closed" in text:
        return "The browser window was closed before login finished — click 'Log in with Spotify' to try again."
    if "chrome not reachable" in text or "disconnected" in text.lower():
        return "Lost connection to the browser window — click 'Log in with Spotify' to try again."
    return first_line


# Selenium — shared driver factory + scroll helper

# Spotify injects artist links inside these containers (most specific first)
_ARTIST_SELECTORS = [
    "[data-testid='tracklist-row'] a[href*='/artist/']",
    "[data-testid='track-list-row'] a[href*='/artist/']",
    "div[aria-rowindex] a[href*='/artist/']",
    "a[href*='/artist/']",
]

# The scrollable tracklist itself — used to scope artist-link searches so
# unrelated artist links elsewhere on the page (recommended tracks, the
# now-playing bar, "fans also like" sidebars, etc.) are never picked up.
_TRACKLIST_CONTAINER_SELECTORS = [
    "[data-testid='playlist-tracklist']",
    "[data-testid='track-list']",
    "section[data-testid='playlist-page'] [role='grid']",
    "[role='grid']",
]

# Names that show up as "artist" links but aren't real performers, so we
# never bother searching Last.fm for them.
_NON_ARTIST_NAMES = {"various artists"}


def _make_driver(headless: bool = True, profile_dir: Optional[Path] = None) -> webdriver.Chrome:
    """
    Return a Chrome WebDriver configured to look like a normal browser.
    headless=False opens a visible window (needed for Spotify login).
    profile_dir persists cookies/session so login survives restarts.
    """
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
    if profile_dir:
        opts.add_argument(f"--user-data-dir={profile_dir}")
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


def _find_tracklist_scope(driver):
    """
    Return the element containing the playlist/library tracklist, or
    `driver` itself if none of the known containers can be found.

    Used both to scope artist-link searches (so unrelated parts of the page
    don't leak in) and to find the right element to scroll — the same
    container must be used for both, otherwise scrolling one part of the
    page while reading another causes the harvest to "stabilize" early.
    """
    for sel in _TRACKLIST_CONTAINER_SELECTORS:
        try:
            candidates = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            candidates = []
        # A page can have several elements matching a generic selector (e.g.
        # "[role='grid']" also matches recommendation grids). Only scope to
        # one that actually contains artist links — otherwise an empty/wrong
        # match would silently zero out the whole harvest. Virtualized rows
        # can also detach mid-scroll, so a stale candidate is skipped rather
        # than letting the exception bubble up and abort the whole scan.
        for c in candidates:
            try:
                if c.find_elements(By.CSS_SELECTOR, "a[href*='/artist/']"):
                    return c
            except Exception:
                continue
    return driver


def _tracklist_ready(driver) -> bool:
    """
    True once the real tracklist (`[data-testid='track-list']` /
    `[data-testid='playlist-tracklist']`) has rendered its rows.

    If neither of those elements exists in the DOM at all (some page
    layouts may not use them), fall back to whatever `_find_tracklist_scope`
    would pick. But if the element exists and is just still empty
    (virtualized rows haven't rendered yet), keep waiting rather than
    falling back to a generic `[role='grid']` — a "Jump back in"/recommendation
    rail can render artist links first and get mistaken for the tracklist,
    causing the harvest to scope to (and scroll) the wrong, much smaller list.
    """
    for sel in _TRACKLIST_CONTAINER_SELECTORS[:2]:
        try:
            candidates = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            candidates = []
        for c in candidates:
            try:
                if c.find_elements(By.CSS_SELECTOR, "a[href*='/artist/']"):
                    return True
            except Exception:
                continue
        if candidates:
            return False
    return _find_tracklist_scope(driver) is not driver


def _wait_for_tracklist(driver, timeout: int = ARTIST_WAIT_S):
    """
    Wait until the tracklist container (with at least one artist link) is
    present in the DOM.

    Waiting for *any* `a[href*='/artist/']` on the page (the previous
    approach) is unreliable: recommendation rails, "fans also like"
    sidebars, and concert-promo widgets often render artist links before
    the actual tracklist does. That made the wait succeed too early, so the
    harvest started before the tracklist existed, scoped to the wrong
    (empty) container, and returned zero artists.
    """
    try:
        WebDriverWait(driver, timeout).until(_tracklist_ready)
    except Exception:
        log.debug("Timed out waiting for tracklist container")


def _harvest_artists(driver, scope=None) -> dict:
    """
    Return {name: spotify_url} for every artist link in the playlist's
    tracklist that's currently in the DOM.

    The search is scoped to the tracklist container whenever we can find
    one, so artist links from unrelated parts of the page (recommendations,
    the now-playing bar, related-artist sidebars, etc.) never leak into the
    results — only artists actually in this playlist/library are returned.

    `scope` can be passed in by callers that already located the tracklist
    container this round (e.g. _scroll_and_harvest_iter), avoiding a second
    round-trip through _find_tracklist_scope.
    """
    if scope is None:
        scope = _find_tracklist_scope(driver)

    for selector in _ARTIST_SELECTORS:
        try:
            elements = scope.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            elements = []
        if elements:
            artists = {}
            for a in elements:
                # Virtualized rows can detach mid-scroll, making a previously
                # found element stale by the time we read its attributes —
                # just skip it rather than aborting the whole harvest.
                try:
                    name = (a.text or "").strip()
                    href = (a.get_attribute("href") or "")
                except Exception:
                    continue
                if not name or "/artist/" not in href:
                    continue
                if name.lower() in _NON_ARTIST_NAMES:
                    continue
                if name not in artists:
                    artists[name] = href
            return artists
    return {}


# Scrolls the actual scrollable list container when present (Spotify renders
# Liked Songs / playlists inside a custom OverlayScrollbars viewport, not
# the window) and falls back to window scrolling otherwise. Returns the
# container's scrollHeight so callers can detect when more rows have
# lazy-loaded below the fold.
#
# A `scope` element (the tracklist/grid being harvested) can optionally be
# passed in as the second argument — we then climb its ancestors to find the
# nearest actually-scrollable one. The page can have several
# [data-overlayscrollbars-viewport] elements at once (sidebar, "made for
# you" rails, etc.), so picking the first one blindly (as
# document.querySelector does) can scroll the wrong one entirely, leaving
# the tracklist's height unchanged forever and causing the harvest to
# "stabilize" (and stop) after only the first screenful of rows.
_SCROLL_JS = """
const px = arguments[0];
const scope = arguments[1];
let el = null;
if (scope) {
    let node = scope;
    while (node && node !== document.body) {
        const style = getComputedStyle(node);
        const scrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
        if (scrollable && node.scrollHeight > node.clientHeight + 4) {
            el = node;
            break;
        }
        node = node.parentElement;
    }
}
if (!el) {
    el = document.querySelector('[data-overlayscrollbars-viewport]')
      || document.querySelector('.main-view-container__scroll-node')
      || document.scrollingElement;
}
el.scrollBy(0, px);
return el.scrollHeight;
"""


def _scroll_and_harvest_iter(driver, harvest_fn=_harvest_artists, max_scrolls: int = MAX_SCROLLS_LONG):
    """
    Scroll the list in SCROLL_PX steps, harvesting items via harvest_fn after each step.
    Stops once both the item count AND the scroll height are stable for
    STABLE_ROUNDS consecutive rounds — checking scroll height too avoids
    quitting early during a brief lazy-load pause on large libraries.

    Yields the running item count after each scroll step so callers can
    surface live progress (useful for very large libraries/playlists).
    Returns the final {name: ...} dict (via StopIteration.value).
    """
    all_items: dict = {}
    last_count = last_height = stable_rounds = 0

    for _ in range(max_scrolls):
        if harvest_fn is _harvest_artists:
            # Find the tracklist container once and reuse it for both the
            # harvest and the scroll target, instead of querying the DOM
            # twice per round (each find_elements() call is a Selenium
            # round-trip, which adds up over hundreds of scroll steps).
            scope = _find_tracklist_scope(driver)
            all_items.update(_harvest_artists(driver, scope))
            scroll_scope = None if scope is driver else scope
        else:
            all_items.update(harvest_fn(driver))
            scroll_scope = None
        try:
            height = driver.execute_script(_SCROLL_JS, SCROLL_PX, scroll_scope)
        except Exception:
            # The scoped element can detach between being found and being
            # passed to execute_script (virtualized rows re-render mid-scroll).
            # Fall back to the generic viewport/window scroll for this round
            # rather than aborting the whole harvest.
            height = driver.execute_script(_SCROLL_JS, SCROLL_PX, None)
        yield len(all_items)

        if len(all_items) == last_count and height == last_height:
            stable_rounds += 1
            if stable_rounds >= STABLE_ROUNDS:
                break
        else:
            stable_rounds = 0
            last_count  = len(all_items)
            last_height = height

        time.sleep(SCROLL_DELAY_S)

    all_items.update(harvest_fn(driver))  # final sweep after scroll settles
    return all_items


def _scroll_and_harvest(driver, harvest_fn=_harvest_artists, max_scrolls: int = MAX_SCROLLS_LONG) -> dict:
    """Non-streaming wrapper around _scroll_and_harvest_iter for callers that don't need progress."""
    gen = _scroll_and_harvest_iter(driver, harvest_fn, max_scrolls)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


def _harvest_with_progress(driver, label: str, harvest_fn=_harvest_artists, max_scrolls: int = MAX_SCROLLS_LONG):
    """
    Generator: drives _scroll_and_harvest_iter and yields SSE "status" events
    with a live running count, so very large libraries/playlists show
    visible progress instead of appearing to hang.
    Returns the final {name: ...} dict via StopIteration.value — use with
    `result = yield from _harvest_with_progress(...)`.
    """
    last_emitted = -1
    gen = _scroll_and_harvest_iter(driver, harvest_fn, max_scrolls)
    while True:
        try:
            count = next(gen)
        except StopIteration as stop:
            return stop.value
        if count and count != last_emitted:
            yield sse("status", {"message": f"{label}... {count} found so far"})
            last_emitted = count


# Spotify injects playlist links inside the library/collection list
_PLAYLIST_SELECTORS = [
    "[data-testid='playlist-row'] a[href*='/playlist/']",
    "div[role='row'] a[href*='/playlist/']",
    "a[href*='/playlist/']",
]


def _harvest_playlists(driver) -> dict:
    """Return {name: {"url": spotify_url, "image": cover_image_url}} for playlist rows in the DOM."""
    for selector in _PLAYLIST_SELECTORS:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            playlists = {}
            for a in elements:
                name = (a.text or "").strip()
                href = (a.get_attribute("href") or "").split("?")[0]
                if not name or "/playlist/" not in href or name in playlists:
                    continue
                image = ""
                try:
                    row = a.find_element(
                        By.XPATH,
                        "./ancestor::div[@role='row' or @data-testid='playlist-row'][1]",
                    )
                    image = row.find_element(By.CSS_SELECTOR, "img").get_attribute("src") or ""
                except Exception:
                    pass
                playlists[name] = {"url": href, "image": image}
            return playlists
    return {}


# Spotify — playlist scraper

def scrape_spotify_playlist(playlist_url: str, authed: bool = False):
    """
    Generator: yields SSE "status" events with live progress while scraping,
    and returns (playlist_name, cover_image_url, {artist_name: spotify_url})
    via StopIteration.value — use with `result = yield from scrape_spotify_playlist(...)`.

    Raises ValueError for non-Spotify URLs.
    authed=True reuses the saved Spotify login so private playlists from the
    user's own library can be read too.
    """
    if not playlist_url.startswith(_SPOTIFY_PLAYLIST_PREFIX):
        raise ValueError("URL must start with https://open.spotify.com/playlist/")

    profile_dir = SPOTIFY_PROFILE if authed else None
    driver = _make_driver(headless=True, profile_dir=profile_dir)
    try:
        driver.get(playlist_url)
        _wait_for_tracklist(driver)

        artists = yield from _harvest_with_progress(
            driver, "Reading playlist", max_scrolls=MAX_SCROLLS_SHORT
        )

        title = driver.title
        name  = title.split(" - playlist")[0].strip() if " - playlist" in title else title

        image = ""
        for sel in ("img[data-testid='playlist-image']", ".cover-art img", "img[src*='mosaic']"):
            try:
                image = driver.find_element(By.CSS_SELECTOR, sel).get_attribute("src") or ""
                if image:
                    break
            except Exception:
                pass

        return name, image, artists

    finally:
        driver.quit()


# Concert search — Last.fm

def _city_for_address(address: str) -> Optional[str]:
    """
    Map a Last.fm venue address string to a watched-city label, or None.

    Matching requires the address to also mention the watched city's country
    (so e.g. "Brisbane, Queensland, Australia" can't match "queens"), and
    keywords are matched as whole words (so "queens" doesn't match inside
    "queensland", and "pei" doesn't match inside an unrelated word).
    """
    text = address.lower()
    for city in WATCH_CITIES:
        if city["country"] not in text:
            continue
        for kw in city["keywords"]:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                return city["label"]
    return None


def _artist_slugs(artist_name: str) -> list:
    """
    Return URL-encoded Last.fm slug variants to try, most specific first.
    Handles feat./ft. credits and dual-artist names (A & B, A and B).
    """
    def clean(name: str) -> str:
        name = re.sub(r"\s*(feat\.?|ft\.?|featuring)\s+.*", "", name, flags=re.I)
        return re.sub(r"\s*\(.*?\)", "", name).strip()

    cleaned   = clean(artist_name)
    variants  = [artist_name]
    if cleaned != artist_name:         # only add if stripping actually changed something
        variants.append(cleaned)
    for sep in (" & ", " and "):
        if sep.lower() in artist_name.lower():
            first = re.split(sep, artist_name, maxsplit=1, flags=re.I)[0].strip()
            if first not in variants:
                variants.append(first)

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
    Return upcoming concerts for artist_name in any watched city,
    sorted earliest first and deduplicated by (date, venue).
    Tries multiple slug variants until one returns event rows.
    """
    for slug in _artist_slugs(artist_name):
        try:
            resp = _HTTP.get(
                f"https://www.last.fm/music/{slug}/+events",
                timeout=HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            log.debug("Last.fm request failed for %s: %s", artist_name, exc)
            continue
        if resp.status_code != 200:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.events-list-item[itemprop='event']")
        if not rows:
            continue  # this slug returned no events — try next variant

        events = []
        for row in rows:
            addr_el = row.select_one(".events-list-item-venue--address")
            address = addr_el.get_text(strip=True) if addr_el else ""
            city    = _city_for_address(address)
            if not city:
                continue

            time_el  = row.select_one("time[datetime]")
            date_raw = time_el.get("datetime", "") if time_el else ""

            # Skip shows that have already happened — we only want upcoming dates.
            if date_raw:
                try:
                    event_dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                    if event_dt < datetime.now(timezone.utc):
                        continue
                except Exception:
                    pass

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
                "city":        city,
                "tickets_url": url,
            })

        # Deduplicate by (date, normalised venue) and return sorted by date
        seen: set = set()
        deduped   = []
        for e in events:
            key = (e["date_raw"][:10], re.sub(r"\W", "", e["venue"].lower())[:15])
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        return sorted(deduped, key=lambda e: e["date_raw"])

    return []


# Concert stream helpers — shared by /api/concerts and /api/liked-songs

_OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')


def _artist_image(spotify_url: str) -> str:
    """Best-effort fetch of an artist photo via the og:image tag on their Spotify page."""
    if not spotify_url:
        return ""
    try:
        resp = _HTTP.get(spotify_url, timeout=HTTP_TIMEOUT_S)
        if resp.status_code == 200:
            m = _OG_IMAGE_RE.search(resp.text)
            if m:
                return m.group(1)
    except requests.RequestException:
        pass
    return ""


def _stream_concerts(artists_map: dict, playlist_name: str, playlist_image: str):
    """
    Generator that yields SSE events for the concert-search phase.
    Shared between the playlist and liked-songs routes.

    Last.fm lookups are I/O-bound (network round-trips that dwarf any local
    work), so they're run CONCERT_WORKERS at a time in a thread pool instead
    of one-by-one — this cuts wall-clock time for large libraries from tens
    of minutes down to a few. Progress/results are streamed as each lookup
    completes, which may be a different order than `artists` since faster
    lookups finish first; the frontend doesn't depend on ordering.
    """
    artists = sorted(artists_map)
    yield sse("playlist_info", {"name": playlist_name, "image": playlist_image})
    yield sse("artists_found", {"count": len(artists)})
    yield sse("status", {"message": f"Searching {len(artists)} artists on Last.fm..."})

    found_count = 0
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCERT_WORKERS) as executor:
        future_to_artist = {executor.submit(find_concerts, artist): artist for artist in artists}
        for future in concurrent.futures.as_completed(future_to_artist):
            artist = future_to_artist[future]
            completed += 1
            yield sse("progress", {"current": completed, "total": len(artists), "artist": artist})
            try:
                concerts = future.result()
            except Exception as exc:
                log.debug("Concert lookup failed for %s: %s", artist, exc)
                concerts = []
            if concerts:
                found_count += 1
                spotify_url = artists_map.get(artist, "")
                yield sse("result", {
                    "artist":      artist,
                    "spotify_url": spotify_url,
                    "image":       _artist_image(spotify_url),
                    "concerts":    concerts,
                })

    yield sse("done", {"total_artists": len(artists), "artists_with_shows": found_count})


def _sse_response(generator) -> Response:
    return Response(
        stream_with_context(generator),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Routes — pages

@app.route("/")
def index():
    return render_template("index.html", default_playlist=DEFAULT_PLAYLIST)


# Routes — /api/concerts

@app.route("/api/concerts")
def concerts_stream():
    playlist_url = request.args.get("playlist_url", DEFAULT_PLAYLIST).strip()
    authed = request.args.get("authed") == "1"

    def generate():
        try:
            yield sse("status", {"message": "Reading playlist from Spotify..."})
            try:
                gen = scrape_spotify_playlist(playlist_url, authed=authed)
                while True:
                    try:
                        yield next(gen)
                    except StopIteration as stop:
                        name, image, artists_map = stop.value
                        break
            except ValueError as e:
                yield sse("error", {"message": str(e)})
                return
            except Exception as e:
                log.error("Playlist scrape failed: %s", e)
                yield sse("error", {"message": f"Could not load playlist: {e}"})
                return

            yield from _stream_concerts(artists_map, name, image)

        except Exception as e:
            log.error("Unexpected error in concerts_stream: %s", e)
            yield sse("error", {"message": _friendly_error(e)})

    return _sse_response(generate())


# Routes — /api/liked-songs

_LIKED_SONGS_URL = "https://open.spotify.com/collection/tracks"
_LOGIN_URL = (
    "https://accounts.spotify.com/login"
    "?continue=https%3A%2F%2Fopen.spotify.com%2Fcollection%2Ftracks"
)


def _harvest_liked_songs():
    """
    Generator: opens a visible Spotify browser window (handling login if
    needed) and harvests the Liked Songs artist list with live progress.

    Yields SSE strings ("status"/"login_required") and returns
    {artist_name: spotify_url} via StopIteration.value.
    Raises RuntimeError with a user-facing message on failure.
    """
    SPOTIFY_PROFILE.mkdir(parents=True, exist_ok=True)
    driver = None
    try:
        yield sse("status", {"message": "Opening Spotify in a browser window..."})
        try:
            driver = _make_driver(headless=False, profile_dir=SPOTIFY_PROFILE)
        except Exception as exc:
            log.error("Could not open visible browser for Liked Songs: %s", exc)
            raise RuntimeError(
                "Liked Songs needs a desktop browser window and only works "
                "when running this app on your own computer — not in the cloud."
            )

        driver.get(_LIKED_SONGS_URL)
        time.sleep(PAGE_SETTLE_S)

        if "collection/tracks" not in driver.current_url:
            driver.get(_LOGIN_URL)
            yield sse("login_required", {
                "message": "Please log into Spotify in the browser window. "
                           "Once logged in you will be taken straight to your Liked Songs."
            })
            try:
                WebDriverWait(driver, LOGIN_TIMEOUT_S).until(
                    lambda d: "collection/tracks" in d.current_url
                )
                time.sleep(POST_LOGIN_S)
            except Exception:
                raise RuntimeError("Login timed out — please try again.")

        yield sse("status", {"message": "Reading your Liked Songs..."})
        _wait_for_tracklist(driver)

        artists_map = yield from _harvest_with_progress(
            driver, "Reading your Liked Songs", max_scrolls=MAX_SCROLLS_LONG
        )
        return artists_map

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


@app.route("/api/liked-songs")
def liked_songs_stream():
    def generate():
        try:
            try:
                gen = _harvest_liked_songs()
                while True:
                    try:
                        yield next(gen)
                    except StopIteration as stop:
                        artists_map = stop.value
                        break
            except RuntimeError as e:
                yield sse("error", {"message": str(e)})
                return

            if not artists_map:
                yield sse("error", {"message": "No artists found — are you logged into Spotify?"})
                return

            yield from _stream_concerts(artists_map, "Liked Songs", "")

        except Exception as e:
            log.error("Unexpected error in liked_songs_stream: %s", e)
            yield sse("error", {"message": _friendly_error(e)})

    return _sse_response(generate())


# Routes — /api/spotify-playlists

_PLAYLISTS_URL = "https://open.spotify.com/collection/playlists"
_LOGIN_URL_PLAYLISTS = (
    "https://accounts.spotify.com/login"
    "?continue=https%3A%2F%2Fopen.spotify.com%2Fcollection%2Fplaylists"
)


@app.route("/api/spotify-playlists")
def spotify_playlists_stream():
    """Log into Spotify (if needed) and return the user's playlists for picking."""
    SPOTIFY_PROFILE.mkdir(parents=True, exist_ok=True)

    def generate():
        driver = None
        try:
            yield sse("status", {"message": "Opening Spotify in a browser window..."})
            try:
                driver = _make_driver(headless=False, profile_dir=SPOTIFY_PROFILE)
            except Exception as exc:
                log.error("Could not open visible browser for Spotify login: %s", exc)
                yield sse("error", {
                    "message": "Logging in needs a desktop browser window and only works "
                               "when running this app on your own computer — not in the cloud."
                })
                return

            driver.get(_PLAYLISTS_URL)
            time.sleep(PAGE_SETTLE_S)

            if "collection/playlists" not in driver.current_url:
                driver.get(_LOGIN_URL_PLAYLISTS)
                yield sse("login_required", {
                    "message": "Please log into Spotify in the browser window. "
                               "Once logged in your playlists will load automatically."
                })
                try:
                    WebDriverWait(driver, LOGIN_TIMEOUT_S).until(
                        lambda d: "collection/playlists" in d.current_url
                    )
                    time.sleep(POST_LOGIN_S)
                except Exception:
                    yield sse("error", {"message": "Login timed out — please try again."})
                    return

            yield sse("status", {"message": "Loading your playlists..."})
            try:
                WebDriverWait(driver, ARTIST_WAIT_S).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/playlist/']"))
                )
            except Exception:
                log.debug("Timed out waiting for playlist rows")

            playlists_map = yield from _harvest_with_progress(
                driver, "Loading your playlists", harvest_fn=_harvest_playlists, max_scrolls=MAX_SCROLLS_SHORT
            )
            driver.quit()
            driver = None

            playlists = [{"name": name, **info} for name, info in playlists_map.items()]
            yield sse("playlists", {"playlists": playlists})
            yield sse("done", {})

        except Exception as e:
            log.error("Unexpected error in spotify_playlists_stream: %s", e)
            yield sse("error", {"message": _friendly_error(e)})
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    return _sse_response(generate())


# Routes — /api/scan-multi

@app.route("/api/scan-multi")
def scan_multi_stream():
    """
    Scan several sources at once and combine their artists into a single
    concert search. `sources` is a "|"-separated list of URL-encoded values,
    where each value is either "liked" (for Liked Songs) or a Spotify
    playlist URL.
    """
    raw = request.args.get("sources", "")
    sources = [urllib.parse.unquote(s) for s in raw.split("|") if s]

    if not sources:
        def empty():
            yield sse("error", {"message": "No playlists selected."})
        return _sse_response(empty())

    def generate():
        combined_artists: dict = {}
        names: list = []
        image = ""

        try:
            for source in sources:
                try:
                    if source == "liked":
                        gen = _harvest_liked_songs()
                        while True:
                            try:
                                yield next(gen)
                            except StopIteration as stop:
                                combined_artists.update(stop.value)
                                names.append("Liked Songs")
                                break
                    else:
                        gen = scrape_spotify_playlist(source, authed=True)
                        while True:
                            try:
                                yield next(gen)
                            except StopIteration as stop:
                                playlist_name, playlist_image, artists = stop.value
                                combined_artists.update(artists)
                                names.append(playlist_name)
                                if not image and playlist_image:
                                    image = playlist_image
                                break
                except (RuntimeError, ValueError) as e:
                    yield sse("error", {"message": str(e)})
                    return
                except Exception as e:
                    log.error("Scan failed for source %s: %s", source, e)
                    yield sse("error", {"message": f"Could not scan a selected playlist: {e}"})
                    return

            if not combined_artists:
                yield sse("error", {"message": "No artists found in the selected playlists."})
                return

            if len(names) <= 3:
                label = ", ".join(names)
            else:
                label = f"{names[0]} + {len(names) - 1} more"

            yield from _stream_concerts(combined_artists, label, image)

        except Exception as e:
            log.error("Unexpected error in scan_multi_stream: %s", e)
            yield sse("error", {"message": _friendly_error(e)})

    return _sse_response(generate())


# Storage helpers

def _read_json(path: Path, default):
    """Read a JSON file, returning default on any error."""
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception as exc:
        log.warning("Could not read %s: %s", path.name, exc)
        return default


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


def _load_emails() -> list:
    return _read_json(EMAILS_FILE, [])

def _save_emails(emails: list):
    _write_json(EMAILS_FILE, sorted(set(emails)))

def _load_phones() -> list:
    return _read_json(PHONES_FILE, [])

def _save_phones(phones: list):
    _write_json(PHONES_FILE, phones)

def _load_mail_cfg() -> dict:
    return _read_json(MAIL_CFG_FILE, {})

def _save_mail_cfg(cfg: dict):
    _write_json(MAIL_CFG_FILE, cfg)


# Routes — /api/emails

@app.route("/api/emails", methods=["GET"])
def get_emails():
    return jsonify(_load_emails())


@app.route("/api/emails", methods=["POST"])
def add_email():
    email = (request.json or {}).get("email", "").strip().lower()
    if not _EMAIL_RE.match(email):
        return jsonify({"error": "Invalid email address"}), 400
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


# Routes — /api/phones

@app.route("/api/phones", methods=["GET"])
def get_phones():
    return jsonify(_load_phones())


@app.route("/api/phones", methods=["POST"])
def add_phone():
    data    = request.json or {}
    number  = re.sub(r"\D", "", data.get("number", ""))
    carrier = data.get("carrier", "").strip()
    if not (10 <= len(number) <= 15):
        return jsonify({"error": "Number must be 10–15 digits"}), 400
    if carrier not in SMS_GATEWAYS:
        return jsonify({"error": "Unknown carrier"}), 400
    phones = _load_phones()
    entry  = {"number": number, "carrier": carrier}
    if entry not in phones:
        phones.append(entry)
        _save_phones(phones)
    return jsonify(_load_phones())


@app.route("/api/phones/<number>/<carrier>", methods=["DELETE"])
def remove_phone(number, carrier):
    phones = [p for p in _load_phones()
              if not (p["number"] == number and p["carrier"] == carrier)]
    _save_phones(phones)
    return jsonify(phones)


@app.route("/api/carriers", methods=["GET"])
def get_carriers():
    return jsonify(list(SMS_GATEWAYS.keys()))


# Routes — /api/mail-config

def _smtp_password(cfg: dict) -> str:
    """
    Return the SMTP password, preferring the SMTP_PASS environment variable
    so the credential can be kept off the filesystem entirely.
    """
    return os.environ.get("SMTP_PASS") or cfg.get("smtp_pass", "")


@app.route("/api/mail-config", methods=["GET"])
def get_mail_cfg():
    cfg = _load_mail_cfg()
    return jsonify({
        "smtp_host":  cfg.get("smtp_host", "smtp.gmail.com"),
        "smtp_port":  cfg.get("smtp_port", 587),
        "smtp_user":  cfg.get("smtp_user", ""),
        "from_name":  cfg.get("from_name", "Concert Finder"),
        "configured": bool(cfg.get("smtp_user") and _smtp_password(cfg)),
    })


@app.route("/api/mail-config", methods=["POST"])
def save_mail_cfg():
    data = request.json or {}
    cfg  = _load_mail_cfg()
    for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "from_name"):
        if key in data and str(data[key]).strip():
            cfg[key] = data[key]
    _save_mail_cfg(cfg)
    return jsonify({"ok": True, "env_override": bool(os.environ.get("SMTP_PASS"))})


# Email + SMS builders

def _safe_url(url: str) -> str:
    """
    Allow only http/https URLs in HTML output.
    Blocks javascript:, data:, and other executable schemes.
    """
    try:
        if urlparse(url).scheme.lower() in ("http", "https"):
            return html.escape(url, quote=True)
    except Exception:
        pass
    return "#"


def _flatten_results(results: list) -> list:
    """Return all concerts as a flat list sorted by date, earliest first."""
    flat = [{"artist": r["artist"], **c} for r in results for c in r["concerts"]]
    return sorted(flat, key=lambda x: x.get("date_raw", ""))


def _week_start(d: datetime) -> datetime:
    """Return the Monday (midnight) of the week containing `d`."""
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def _format_week_heading(d: datetime) -> str:
    """Mirror the frontend's weekly headings: THIS WEEK / NEXT WEEK / WEEK OF ..."""
    start = _week_start(d)
    end = start + timedelta(days=6)
    today_start = _week_start(datetime.now())
    diff_weeks = (start - today_start).days // 7

    rng = f"{start.strftime('%b %-d').upper()} - {end.strftime('%b %-d').upper()}"
    year_suffix = f", {end.year}" if end.year != datetime.now().year else ""

    if diff_weeks == 0:
        return f"THIS WEEK · {rng}{year_suffix}"
    if diff_weeks == 1:
        return f"NEXT WEEK · {rng}{year_suffix}"
    return f"WEEK OF {rng}{year_suffix}"


def _build_email_html(flat: list, playlist_name: str) -> str:
    """Build a styled HTML email from a pre-flattened, sorted concert list."""
    cities       = " &amp; ".join(c["label"] for c in WATCH_CITIES)
    report_date  = datetime.now().strftime("%B %-d, %Y")
    safe_pl      = html.escape(playlist_name)
    shows        = len(flat)
    artist_count = len({c["artist"] for c in flat})

    def row(c: dict) -> str:
        show_date = html.escape(c.get("date", "TBA"))
        time_str  = (" &bull; " + html.escape(c["time"])) if c.get("time") else ""
        return (
            '<tr style="border-bottom:1px solid #f0f0f0">'
            f'<td style="padding:10px 12px;font-weight:600">{html.escape(c.get("artist",""))}</td>'
            f'<td style="padding:10px 12px">{show_date}{time_str}</td>'
            f'<td style="padding:10px 12px">{html.escape(c.get("venue",""))}</td>'
            f'<td style="padding:10px 12px">'
            f'  <span style="background:#a855f722;color:#a855f7;padding:2px 8px;'
            f'border-radius:4px;font-size:12px;font-weight:700">{html.escape(c.get("city",""))}</span>'
            f'</td>'
            f'<td style="padding:10px 12px">'
            f'  <a href="{_safe_url(c.get("tickets_url",""))}" style="background:linear-gradient(120deg,#a855f7,#ec4899);'
            f'color:#fff;padding:5px 12px;border-radius:5px;text-decoration:none;font-size:12px;font-weight:700">Tickets</a>'
            f'</td>'
            '</tr>'
        )

    def week_heading_row(c: dict) -> str:
        try:
            heading = _format_week_heading(datetime.fromisoformat(c["date_raw"].replace("Z", "+00:00")))
        except Exception:
            heading = "DATE TBA"
        return (
            '<tr><td colspan="5" style="padding:16px 12px 6px;font-size:12px;'
            f'font-weight:800;letter-spacing:0.04em;color:#a855f7">{html.escape(heading)}</td></tr>'
        )

    rows_html_parts: list = []
    last_group = None
    for c in flat:
        group_key = c["date_raw"][:10] if c.get("date_raw") else "tba"
        # Re-bucket by week-start so any day within the same Mon-Sun week shares a heading.
        if group_key != "tba":
            try:
                group_key = _week_start(datetime.fromisoformat(c["date_raw"].replace("Z", "+00:00"))).isoformat()
            except Exception:
                group_key = "tba"
        if group_key != last_group:
            last_group = group_key
            rows_html_parts.append(week_heading_row(c))
        rows_html_parts.append(row(c))
    rows_html = "".join(rows_html_parts)
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:700px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">
  <div style="background:linear-gradient(120deg,#a855f7,#ec4899);padding:28px 32px">
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:800">Concert Report</h1>
    <p style="margin:6px 0 0;color:rgba(255,255,255,.85);font-size:14px">{safe_pl} &bull; {cities} &bull; {report_date}</p>
  </div>
  <div style="padding:24px 32px">
    <p style="color:#555;font-size:14px;margin:0 0 20px">
      Found <strong style="color:#a855f7">{shows} upcoming show{'s' if shows != 1 else ''}</strong>
      across <strong>{artist_count} artist{'s' if artist_count != 1 else ''}</strong> — sorted earliest first.
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
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="padding:16px 32px;background:#fafafa;border-top:1px solid #eee;font-size:12px;color:#aaa;text-align:center">
    Concert Finder &bull; Data from Last.fm
  </div>
</div>
</body></html>"""


def _build_sms_text(flat: list, playlist_name: str) -> str:
    """
    Compact plain-text for SMS — capped at MAX_SMS_SHOWS entries.
    Sticks to plain ASCII (no em-dash/middle-dot) since some carrier
    email-to-SMS gateways mangle non-ASCII characters.
    """
    lines = [f"Concert Alert - {playlist_name}", ""]
    for c in flat[:MAX_SMS_SHOWS]:
        try:
            dt       = datetime.fromisoformat(c["date_raw"].replace("Z", "+00:00"))
            show_date = dt.strftime("%b %-d")
        except Exception:
            show_date = c.get("date", "TBA").split(",")[0]
        venue_short = c.get("venue", c.get("city", ""))[:30]
        lines.append(c["artist"])
        lines.append(f"  {show_date} - {venue_short}, {c.get('city', '')}")
    if len(flat) > MAX_SMS_SHOWS:
        lines.append(f"...and {len(flat) - MAX_SMS_SHOWS} more")
    return "\n".join(lines)


# Routes — /api/send-report

@app.route("/api/send-report", methods=["POST"])
def send_report():
    data          = request.json or {}
    results       = data.get("results", [])
    playlist_name = data.get("playlist_name", "My Playlist")

    emails = _load_emails()
    phones = _load_phones()
    if not emails and not phones:
        return jsonify({"error": "No email or phone recipients configured"}), 400

    cfg       = _load_mail_cfg()
    smtp_pass = _smtp_password(cfg)
    if not cfg.get("smtp_user") or not smtp_pass:
        return jsonify({"error": "SMTP not configured — open Email Settings"}), 400

    if not results:
        return jsonify({"error": "No concert results to send"}), 400

    # Build both message formats from a single flat list (avoids recomputing twice)
    flat      = _flatten_results(results)
    html_body = _build_email_html(flat, playlist_name)
    sms_body  = _build_sms_text(flat, playlist_name)
    show_count = len(flat)
    subject    = f"{show_count} upcoming show{'s' if show_count != 1 else ''} — {playlist_name}"

    sent, errors = [], []
    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", 587))
    server = None
    try:
        # Support both STARTTLS (587) and SMTPS/SSL (465)
        if port == 465:
            server = smtplib.SMTP_SSL(host, port)
        else:
            server = smtplib.SMTP(host, port)
            server.starttls()
        server.login(cfg["smtp_user"], smtp_pass)

        for to_addr in emails:
            msg            = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{cfg.get('from_name','Concert Finder')} <{cfg['smtp_user']}>"
            msg["To"]      = to_addr
            msg.attach(MIMEText(html_body, "html"))
            try:
                server.sendmail(cfg["smtp_user"], to_addr, msg.as_string())
                sent.append(to_addr)
            except smtplib.SMTPException as exc:
                log.warning("Failed to send email to %s: %s", to_addr, exc)
                errors.append({"to": to_addr, "error": str(exc)})

        for phone in phones:
            gateway  = SMS_GATEWAYS.get(phone["carrier"], "")
            sms_addr = f"{phone['number']}{gateway}"
            msg            = MIMEText(sms_body, "plain")
            msg["Subject"] = ""
            msg["From"]    = cfg["smtp_user"]
            msg["To"]      = sms_addr
            try:
                server.sendmail(cfg["smtp_user"], sms_addr, msg.as_string())
                sent.append(f"{phone['number']} ({phone['carrier']})")
            except smtplib.SMTPException as exc:
                log.warning("Failed to send SMS to %s: %s", sms_addr, exc)
                errors.append({"to": sms_addr, "error": str(exc)})

    except (smtplib.SMTPException, OSError) as exc:
        # Connection-level failures (bad host, refused connection, DNS
        # failure, TLS errors) raise OSError subclasses, not SMTPException —
        # catch both so the user always gets a JSON error instead of a
        # raw 500 page.
        log.error("SMTP connection failed: %s", exc)
        return jsonify({"error": f"SMTP connection failed: {exc}"}), 500
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass

    return jsonify({"sent": sent, "errors": errors})


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port  = int(os.environ.get("PORT", 5001))
    app.run(debug=debug, host="0.0.0.0", port=port)
