#!/usr/bin/env python3
"""
Concert Finder — no API credentials required.

1. Reads artists from any public Spotify playlist (or Liked Songs) via headless Chrome.
2. Searches Last.fm (server-rendered, no auth) for upcoming shows in
   New York City, Washington DC, Charlottetown (Prince Edward Island),
   and Halifax, Nova Scotia.
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
import threading
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
from selenium.webdriver.support.ui import WebDriverWait

# Logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class ConcertLookupError(Exception):
    """
    Raised by find_concerts() when Last.fm couldn't be reached/parsed for an
    artist at all (every slug variant errored or returned a non-200), as
    opposed to a successful lookup that simply found no upcoming shows.
    Lets callers tell "checked, nothing found" apart from "couldn't check —
    results may be incomplete" so the latter can be surfaced to the user.
    """


# Constants

# Scroll behaviour
SCROLL_PX          = 500     # pixels per scroll step
SCROLL_DELAY_S     = 0.8     # seconds between scroll steps
STABLE_ROUNDS      = 8       # consecutive unchanged harvests before stopping
MAX_SCROLLS        = 1200    # scrape cap shared by playlists/Liked Songs/library (very large libraries)

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

EMAILS_FILE    = Path(__file__).parent / "emails.json"
PHONES_FILE    = Path(__file__).parent / "phones.json"
MAIL_CFG_FILE  = Path(__file__).parent / "mail_config.json"
AUTOSCAN_FILE  = Path(__file__).parent / "autoscan.json"
SPOTIFY_PROFILE = Path.home() / ".concert-finder-spotify-profile"

# Weekly auto-scan schedule — Sunday morning, local server time.
AUTOSCAN_WEEKDAY = 6   # Monday=0 ... Sunday=6
AUTOSCAN_HOUR    = 11  # 11:15 AM
AUTOSCAN_MINUTE  = 15

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
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-US,en;q=0.9",
    # A real browser navigating to a page sends a full Accept header (not
    # "*/*") and a Referer from in-site navigation — matching that reduces
    # the odds of tripping Last.fm's bot detection (which returns a
    # "Rate Limited" 406 page) during scans.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.last.fm/",
}

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

# Status codes worth a brief retry: Last.fm occasionally returns a transient
# "Rate Limited" 406 page (esp. under the concurrent lookups a large-library
# scan performs) or a 429/5xx — without a retry, that single blip would
# silently read as "no concerts found" for that artist.
_RETRY_STATUS_CODES = {406, 429, 500, 502, 503, 504}
_HTTP_MAX_RETRIES   = 3
_HTTP_RETRY_DELAY_S = 1.5


def _http_get(url: str, **kwargs):
    """
    requests.Session.get() with a couple of short retries on transient
    rate-limit / server-error responses (see _RETRY_STATUS_CODES). Returns
    the final Response (which may still carry a bad status code if every
    attempt was rate-limited) or raises the last RequestException if every
    attempt failed at the connection level.
    """
    kwargs.setdefault("timeout", HTTP_TIMEOUT_S)
    last_exc = None
    resp = None
    for attempt in range(_HTTP_MAX_RETRIES + 1):
        try:
            resp = _HTTP.get(url, **kwargs)
            last_exc = None
            if resp.status_code not in _RETRY_STATUS_CODES:
                return resp
        except requests.RequestException as exc:
            last_exc = exc
            resp = None
        if attempt < _HTTP_MAX_RETRIES:
            time.sleep(_HTTP_RETRY_DELAY_S * (attempt + 1))
    if last_exc:
        raise last_exc
    return resp

WATCH_CITIES = [
    {
        "label": "New York City",
        "keywords": ["new york", "brooklyn", "bronx", "queens", "manhattan", "staten island", "nyc"],
        "country": "united states",
    },
    {
        "label": "Charlottetown, Prince Edward Island",
        "keywords": ["charlottetown", "prince edward island", "pei"],
        "country": "canada",
    },
    {
        "label": "Washington DC",
        # Includes nearby Maryland/Virginia suburbs that regularly host
        # shows close to DC (Merriweather Post Pavilion in Columbia MD,
        # Jiffy Lube Live in Bristow VA, Wolf Trap in Vienna VA, EagleBank
        # Arena in Fairfax VA, FedExField/Northwest Stadium in Landover MD,
        # etc.). Several of these names collide with same-named places
        # elsewhere in the US (Springfield, Columbia, Vienna, Fairfax,
        # Sterling, College Park, Rockville, Largo, Leesburg, Woodbridge,
        # Bowie...), so they're listed in _AMBIGUOUS_CITY_KEYWORDS and
        # verified via the venue's ZIP code (_event_page_extras) before being
        # accepted — see _DC_AREA_ZIP_PREFIXES.
        "keywords": [
            "washington dc", "arlington", "alexandria",
            # Maryland suburbs
            "columbia", "colombia", "silver spring", "bethesda", "rockville",
            "gaithersburg", "landover", "largo", "hyattsville", "college park",
            "greenbelt", "bowie", "national harbor",
            # Northern Virginia suburbs
            "vienna", "fairfax", "tysons", "reston", "herndon", "manassas",
            "woodbridge", "bristow", "springfield", "sterling", "ashburn",
            "leesburg",
        ],
        "country": "united states",
    },
    {
        "label": "Halifax, Nova Scotia",
        "keywords": ["halifax", "nova scotia", "dartmouth"],
        "country": "canada",
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

# How close the highest rendered row index needs to get to the grid's total
# row count (aria-rowcount) for the scroll loop to be considered "reached
# the bottom". Spotify's virtualized list keeps a handful of rows rendered
# above/below the viewport, so the max rendered index lags the true total by
# a small, variable amount even once scrolling is fully done.
_ROW_COUNT_TOLERANCE = 15


def _grid_row_count(driver) -> Optional[int]:
    """
    Return the playlist tracklist's total row count via the grid's
    `aria-rowcount` attribute (Spotify sets this on the `[role='grid']`
    container to the track count, often +1 for the header row), or None if
    it can't be determined. Used as a sanity check that the scroll loop
    actually reached the end of a large playlist rather than stopping early.
    """
    for sel in _TRACKLIST_CONTAINER_SELECTORS:
        try:
            el  = driver.find_element(By.CSS_SELECTOR, sel)
            val = el.get_attribute("aria-rowcount")
            if val and val.isdigit():
                return int(val)
        except Exception:
            continue
    return None


def _grid_max_row_index(driver) -> Optional[int]:
    """
    Return the highest `aria-rowindex` currently rendered in the tracklist,
    or None if no indexed rows are present. Paired with _grid_row_count to
    confirm the harvest scrolled all the way through the playlist.
    """
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, "[aria-rowindex]")
        indices = [int(v) for v in (e.get_attribute("aria-rowindex") for e in elements)
                   if v and v.isdigit()]
        return max(indices) if indices else None
    except Exception:
        return None


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


def _find_scroll_scope(driver, link_selector="a[href*='/artist/']"):
    """
    Return the element containing the relevant list (a tracklist of artist
    links, or a library list of playlist links), or `driver` itself if none
    of the known containers can be found.

    Used both to scope link searches (so unrelated parts of the page don't
    leak in) and to find the right element to scroll — the same container
    must be used for both, otherwise scrolling one part of the page while
    reading another causes the harvest to "stabilize" early and miss rows
    that would otherwise lazy-load further down (e.g. only the first
    screenful of a long playlist library).

    `link_selector` lets callers scope to whichever kind of link identifies
    "the real list" on their page — `a[href*='/artist/']` for a track list,
    `a[href*='/playlist/']` for the playlist library.
    """
    for sel in _TRACKLIST_CONTAINER_SELECTORS:
        try:
            candidates = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            candidates = []
        # A page can have several elements matching a generic selector (e.g.
        # "[role='grid']" also matches recommendation grids). Only scope to
        # one that actually contains the links we care about — otherwise an
        # empty/wrong match would silently zero out the whole harvest.
        # Virtualized rows can also detach mid-scroll, so a stale candidate
        # is skipped rather than letting the exception bubble up and abort
        # the whole scan.
        for c in candidates:
            try:
                if c.find_elements(By.CSS_SELECTOR, link_selector):
                    return c
            except Exception:
                continue
    return driver


def _find_tracklist_scope(driver):
    """Backwards-compatible alias: scope to the track list (artist links)."""
    return _find_scroll_scope(driver, "a[href*='/artist/']")


# The "Your Library" sidebar on the Spotify home page — playlists, Liked
# Songs, followed artists/albums, and saved concerts/podcasts all live here
# as a single virtualized list. Unlike the tracklist, rows have no <a href>,
# so they need their own scope/scroll handling (see _harvest_playlists).
_LIBRARY_SCOPE_SELECTOR = ".YourLibraryX"


def _find_library_scope(driver):
    """Return the 'Your Library' sidebar element, or None if not present
    (e.g. the home page hasn't finished loading)."""
    try:
        return driver.find_element(By.CSS_SELECTOR, _LIBRARY_SCOPE_SELECTOR)
    except Exception:
        return None


def _spotify_logged_in(driver) -> bool:
    """
    True if the Spotify web player shows a logged-in session.

    The "Your Library" sidebar (.YourLibraryX) is present in the DOM even
    when logged out (with a couple of empty placeholder rows), so it can't
    be used to detect login on its own. A signed-out home page instead
    shows a "Log in" button (data-testid='login-button'), which disappears
    once a session is active — that's the reliable signal.
    """
    return not driver.find_elements(By.CSS_SELECTOR, "[data-testid='login-button']")


def _library_has_playlists(driver) -> bool:
    """True once the library sidebar has at least one real (non-placeholder)
    row — i.e. a row carrying an onClickHint helper for an actual entity."""
    library = _find_library_scope(driver)
    if library is None:
        return False
    return bool(library.find_elements(By.CSS_SELECTOR, "[id^='onClickHint']"))


# Spotify sometimes shows new/logged-out visitors an "Open in the Spotify
# app?" interstitial instead of the web player (e.g. on open.spotify.com or
# right after logging in). If that happens, the library sidebar never
# appears and the harvest would silently come back empty. This is a
# best-effort, no-op-if-absent click-through to keep scraping in the browser.
_CONTINUE_IN_BROWSER_TEXTS = (
    "continue in browser", "use web player", "open web player",
    "continue here", "stay in browser", "open in browser", "no thanks",
)


def _dismiss_app_prompt(driver) -> None:
    """If a visible 'open the Spotify app?' prompt is on screen, click
    whichever option keeps the session in the browser. Safe no-op if no
    such prompt is showing."""
    try:
        candidates = driver.find_elements(By.CSS_SELECTOR, "button, a, [role='button']")
    except Exception:
        return
    for el in candidates:
        try:
            if not el.is_displayed():
                continue
            text = (el.text or el.get_attribute("aria-label") or "").strip().lower()
            if any(t in text for t in _CONTINUE_IN_BROWSER_TEXTS):
                el.click()
                time.sleep(0.5)
                return
        except Exception:
            continue


# The library sidebar's outer element doesn't itself scroll — find the
# nearest scrollable descendant (the actual virtualized viewport).
_FIND_SCROLLER_JS = """
const root = arguments[0];
function find(el, depth) {
    if (!el || depth > 6) return null;
    const style = getComputedStyle(el);
    const scrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
    if (scrollable && el.scrollHeight > el.clientHeight + 4) return el;
    for (const c of el.children) {
        const r = find(c, depth + 1);
        if (r) return r;
    }
    return null;
}
return find(root, 0);
"""


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


def _scroll_and_harvest_playlists_iter(driver, max_scrolls: int = MAX_SCROLLS):
    """
    Scroll the "Your Library" sidebar to load every saved playlist.

    The sidebar is a virtualized list (only ~20-25 rows are ever in the DOM
    at once, covering playlists, Liked Songs, followed artists/albums,
    concerts, etc.), so it needs its own scroll loop rather than the
    tracklist's scrollBy-on-the-page approach: we find the sidebar's actual
    scrollable viewport and step its scrollTop directly.

    Yields the running playlist count and returns the final
    {name: {...}} dict (via StopIteration.value), same contract as
    _scroll_and_harvest_iter.
    """
    all_items: dict = {}
    last_count = last_top = stable_rounds = 0

    library = _find_library_scope(driver)
    if library is None:
        return all_items

    try:
        scroller = driver.execute_script(_FIND_SCROLLER_JS, library)
    except Exception:
        scroller = None

    for _ in range(max_scrolls):
        all_items.update(_harvest_playlists(driver, library))
        if scroller is None:
            break
        try:
            top = driver.execute_script(
                "arguments[0].scrollTop += arguments[1]; return arguments[0].scrollTop;",
                scroller, SCROLL_PX,
            )
        except Exception:
            break
        yield len(all_items)

        if len(all_items) == last_count and top == last_top:
            stable_rounds += 1
            if stable_rounds >= STABLE_ROUNDS:
                break
        else:
            stable_rounds = 0
            last_count = len(all_items)
            last_top = top

        time.sleep(SCROLL_DELAY_S)

    all_items.update(_harvest_playlists(driver, library))  # final sweep after scroll settles
    return all_items


def _scroll_and_harvest_iter(driver, harvest_fn=_harvest_artists, max_scrolls: int = MAX_SCROLLS):
    """
    Scroll the list in SCROLL_PX steps, harvesting items via harvest_fn after each step.
    Stops once both the item count AND the scroll height are stable for
    STABLE_ROUNDS consecutive rounds — checking scroll height too avoids
    quitting early during a brief lazy-load pause on large libraries.

    Yields the running item count after each scroll step so callers can
    surface live progress (useful for very large libraries/playlists).
    Returns the final {name: ...} dict (via StopIteration.value).
    """
    if harvest_fn is _harvest_playlists:
        # The library sidebar has its own structure/scroll mechanics —
        # delegate to a dedicated loop instead of forcing it through the
        # tracklist-shaped logic below.
        return (yield from _scroll_and_harvest_playlists_iter(driver, max_scrolls))

    all_items: dict = {}
    last_count = last_height = stable_rounds = 0

    for _ in range(max_scrolls):
        # Find the tracklist container once and reuse it for both the
        # harvest and the scroll target, instead of querying the DOM twice
        # per round (each find_elements() call is a Selenium round-trip,
        # which adds up over hundreds of scroll steps). Scoping both to the
        # same container also avoids scrolling one part of the page while
        # reading another, which would make the harvest "stabilize" (and
        # stop) before every row has loaded.
        scope = _find_scroll_scope(driver, "a[href*='/artist/']")
        all_items.update(harvest_fn(driver, scope))
        scroll_scope = None if scope is driver else scope
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


def _harvest_with_progress(driver, label: str, harvest_fn=_harvest_artists, max_scrolls: int = MAX_SCROLLS):
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
            result = stop.value
            break
        if count and count != last_emitted:
            yield sse("status", {"message": f"{label}... {count} found so far"})
            last_emitted = count

    # Sanity-check that the scroll loop actually reached the bottom of a
    # large tracklist rather than stopping early — e.g. if the page momentarily
    # stopped lazy-loading rows for STABLE_ROUNDS in a row well before the end.
    # This only ever adds an informational note; if the grid's row count can't
    # be read (different Spotify layout, Liked Songs, etc.) it's silently skipped.
    if harvest_fn is _harvest_artists:
        total   = _grid_row_count(driver)
        reached = _grid_max_row_index(driver)
        if total and reached and reached < total - _ROW_COUNT_TOLERANCE:
            yield sse("status", {
                "message": f"Note: scrolled through row {reached} of {total} — "
                           f"re-run the scan if this playlist looks incomplete."
            })

    return result


def _harvest_playlists(driver, scope=None) -> dict:
    """
    Return {name: {"url": spotify_url, "image": cover_image_url}} for the
    user's saved playlists, read from the "Your Library" sidebar
    (`.YourLibraryX`) on the Spotify home page.

    The sidebar is a single virtualized list mixing playlists, Liked Songs,
    followed artists/albums, podcasts, and saved concerts/events — and its
    rows are `div[role="row"]` elements with no `<a href>` (navigation is
    click/JS-driven). Each row instead carries a hidden helper div whose id
    encodes the entity's Spotify URI, e.g.
    `id="onClickHintspotify:playlist:<id>"`, which we use to build the
    playlist URL and to filter out everything that isn't a playlist.

    Row title text also isn't reported via Selenium's `.text` for
    off-screen virtualized rows, so it's read via `textContent` instead.
    """
    root = scope if scope is not None else _find_library_scope(driver)
    if root is None:
        return {}
    playlists = {}
    for row in root.find_elements(By.CSS_SELECTOR, "[role='row']"):
        try:
            hint = row.find_element(By.CSS_SELECTOR, "[id^='onClickHintspotify:playlist:']")
        except Exception:
            continue
        playlist_id = hint.get_attribute("id").rsplit(":", 1)[-1]
        if not playlist_id:
            continue
        try:
            title_el = row.find_element(By.CSS_SELECTOR, "[data-encore-id='listRowTitle']")
            name = (driver.execute_script("return arguments[0].textContent", title_el) or "").strip()
        except Exception:
            continue
        if not name or name == "Liked Songs" or name in playlists:
            continue
        image = ""
        try:
            image = row.find_element(By.CSS_SELECTOR, "img[data-testid='entity-image']").get_attribute("src") or ""
        except Exception:
            pass
        playlists[name] = {"url": f"https://open.spotify.com/playlist/{playlist_id}", "image": image}
    return playlists


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
        _dismiss_app_prompt(driver)
        _wait_for_tracklist(driver)

        artists = yield from _harvest_with_progress(
            driver, "Reading playlist", max_scrolls=MAX_SCROLLS
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

# Last.fm venue addresses are just "<city>, <country>" with no state/region,
# so a handful of city keywords collide with same-named places elsewhere
# (e.g. "Arlington, United States" could be Arlington, VA — near DC — or
# Arlington, TX, home of AT&T Stadium; "Columbia, United States" could be
# Columbia, MD or Columbia, SC/MO). Matches on these keywords are
# provisional and must be confirmed via _event_page_extras() before acceptance.
_AMBIGUOUS_CITY_KEYWORDS = {
    "arlington", "alexandria",      # also TX / LA, etc.
    "columbia", "colombia",         # also SC/MO; "Colombia" is a Last.fm typo seen for Columbia, MD
    "springfield",                  # MA/IL/MO/OH...
    "vienna",                       # also Austria / GA / IL / WV
    "fairfax",                      # also CA
    "sterling",                     # also CO/IL
    "college park",                 # also College Park, GA (Atlanta suburb)
    "rockville",                    # also Rockville, FL ("Welcome to Rockville" festival, Daytona)
    "largo",                        # also Largo, FL
    "leesburg",                     # also Leesburg, FL
    "woodbridge",                   # also Woodbridge, NJ
    "bowie",                        # also Bowie, TX / AZ
}

# US ZIP code prefixes covering DC and its nearby Maryland/Virginia suburbs,
# for "Washington DC" matches:
#   20x        DC itself, plus Montgomery/PG county MD suburbs (Silver
#               Spring, Bethesda, Rockville, Landover, National Harbor) and
#               several NoVA suburbs that share DC's 20xxx block (Reston,
#               Herndon, Manassas, Sterling, Ashburn, Leesburg, Bristow)
#   210x/211x  Columbia, MD (Merriweather Post Pavilion)
#   220x       Fairfax, VA (EagleBank Arena)
#   221x       Springfield / Tysons / Vienna, VA (Wolf Trap)
#   222x       Arlington, VA
#   223x       Alexandria, VA
_DC_AREA_ZIP_PREFIXES = ("20", "210", "211", "220", "221", "222", "223")

# Last.fm's /+events page caps each page at 30 rows and paginates the rest
# (?page=2, ?page=3, ...). Heavily-touring artists (e.g. Metallica) routinely
# have 30+ upcoming shows, so without following pagination, shows on later
# pages — including ones in watched cities — would silently never be seen.
_MAX_EVENT_PAGES = 5


def _city_for_address(address: str) -> tuple:
    """
    Map a Last.fm venue address string to (city_label, ambiguous_keyword).
    Returns (None, None) if the address doesn't match any watched city.

    Matching requires the address to also mention the watched city's country
    (so e.g. "Brisbane, Queensland, Australia" can't match "queens"), and
    keywords are matched as whole words (so "queens" doesn't match inside
    "queensland", and "pei" doesn't match inside an unrelated word).

    Commas and periods are stripped first so "Washington, D.C., United
    States" and "Washington, DC, United States" both normalise to
    "washington dc united states" and match the "washington dc" keyword
    consistently (the punctuation otherwise breaks the trailing \\b in
    "washington, d.c."). This also lets us drop a bare "washington"
    keyword, which used to misclassify shows in Seattle, WASHINGTON
    (state) — "The Showbox, Seattle, Washington, United States" — as
    Washington DC.

    `ambiguous_keyword` is set when the match relied on a keyword that's
    shared with a same-named place outside the watched area (see
    _AMBIGUOUS_CITY_KEYWORDS); the caller should verify such matches before
    trusting them.
    """
    text = address.lower().replace(".", "").replace(",", " ")
    text = re.sub(r"\s+", " ", text)
    for city in WATCH_CITIES:
        if city["country"] not in text:
            continue
        for kw in city["keywords"]:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                ambiguous = kw if kw in _AMBIGUOUS_CITY_KEYWORDS else None
                return city["label"], ambiguous
    return None, None


# Resale marketplaces known for steep markups over face value and/or
# sketchy business practices (chargeback disputes, fake-listing
# complaints, etc.). If an event page's "official" link points at one of
# these, it's skipped in favor of the next candidate (or the StubHub
# search fallback) — the goal is to land users on the venue's own site or
# a primary ticketing vendor selling at face value, not an inflated resale
# listing.
_RESALE_MARKUP_DOMAINS = {
    "viagogo.com",
    "ticketnetwork.com",
    "ticketsmarter.com",
    "seatsnet.com",
    "gigsberg.com",
    "ticketcity.com",
    "costcentral.com",
    "centralfanclub.com",
    "concertpass.com",
    "vipticketplace.com",
    "stadiumtix.com",
    "ticketsupply.com",
}


def _ticket_domain(url: str) -> str:
    """Lowercased registrable-ish domain for a URL, e.g.
    'https://www.viagogo.com/x' -> 'viagogo.com'. Returns '' on failure."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    host = host.split("@")[-1].split(":")[0]  # strip userinfo/port
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_safe_ticket_link(url: str) -> bool:
    """
    True unless `url` points at a known resale-markup/scalper domain
    (_RESALE_MARKUP_DOMAINS) or its registrable domain otherwise looks like
    one of those (e.g. "tickets.viagogo.com").
    """
    if not url:
        return False
    domain = _ticket_domain(url)
    if not domain:
        return False
    return not any(domain == d or domain.endswith("." + d) for d in _RESALE_MARKUP_DOMAINS)


def _event_page_extras(event_url: str):
    """
    Fetch a Last.fm event page once and pull out two things from it:

      1. postal_code — the venue's ZIP code (itemprop="postalCode"), used to
         confirm shows matched only via an ambiguous city keyword (e.g.
         "Arlington") are actually in the DC area.
      2. official_url — a link to the venue's own site or an official ticket
         vendor (AXS, etc.), taken from
         a.event-detail-long-link.external-link[href^="http"], skipping any
         that point at a known resale-markup site (_is_safe_ticket_link).

    Deliberately NOT used as a source: Last.fm's
    a.js-stubhub-link.stubhub-button "Buy Tickets" affiliate link. That link
    is generated by fuzzy-matching the artist/venue against StubHub's
    catalog and has been observed to point at a *completely different event*
    (e.g. a "Wilco" show resolving to a "Rick Wilcox Magic Show" listing), so
    it's not safe to send users there.

    Returns (postal_code, official_url, fetch_ok). fetch_ok is False if the
    page couldn't be fetched at all (request error, rate-limited, etc.) —
    callers should treat that as "couldn't verify" rather than "verification
    failed", so a Last.fm rate-limit blip doesn't make a real show vanish
    from the results.
    """
    if not event_url:
        return None, None, False
    try:
        resp = _http_get(event_url)
    except requests.RequestException:
        return None, None, False
    if resp.status_code != 200:
        return None, None, False

    postal_code = None
    m = re.search(r'itemprop="postalCode">\s*(\d{5})', resp.text)
    if m:
        postal_code = m.group(1)

    official_url = None
    soup  = BeautifulSoup(resp.text, "html.parser")
    links = soup.select("a.event-detail-long-link.external-link[href^='http']")
    # When multiple links are present (e.g. a general venue page link
    # followed by the specific show's ticket page), the last one tends to
    # be the most specific/relevant — but skip over any resale-markup
    # domains so we never recommend an inflated listing as "official".
    for link in reversed(links):
        href = link.get("href")
        if _is_safe_ticket_link(href):
            official_url = href
            break

    return postal_code, official_url, True


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


def _normalize_artist_name(name: str) -> str:
    """Lowercase, drop punctuation/'the'/and-equivalents, collapse whitespace —
    so "Florence + the Machine" and "florence and machine" compare equal."""
    name = name.lower()
    name = re.sub(r"[&+]", " and ", name)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\bthe\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _artist_name_matches(page_name: str, requested: str) -> bool:
    """
    True if a Last.fm artist page's <h1> name plausibly corresponds to the
    artist we searched for.

    Last.fm creates an artist page for *any* slug, even ones with no real
    listings — so a slug variant (especially the cleaned/first-of-duo
    variants from _artist_slugs) can land on a completely different,
    unrelated artist's page. Comparing the page's actual name against what
    we searched for catches that case before its events get attributed to
    the wrong artist.

    The comparison is deliberately lenient (exact match, or one name being a
    word-prefix of the other) to allow for "Florence" matching "Florence +
    the Machine" — the truncated slug variant for dual-artist names is
    expected to land on the full act's page.
    """
    a, b = _normalize_artist_name(page_name), _normalize_artist_name(requested)
    if not a or not b:
        return True  # can't tell — don't block on it
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def _fallback_ticket_url(artist_name: str, venue: str) -> str:
    """
    Best-effort ticket search link for a show when Last.fm's event page
    doesn't provide an official venue/vendor link (official_url).

    Points to a Google search for "<artist> <venue> tickets". Ticket-vendor
    search pages (Ticketmaster, AXS, etc.) sometimes return 403/"Access
    Denied" depending on the visitor's IP/browser (bot-detection), which we
    can't predict or control. A plain Google search always loads, is safe
    (google.com), and surfaces face-value vendors (Ticketmaster, AXS, venue
    box office, etc.) alongside resale options so the user can pick the
    cheapest legitimate listing themselves — while the date/venue/city shown
    in this app still comes from the verified Last.fm match.
    """
    query = " ".join(part for part in (artist_name, venue, "tickets") if part).strip()
    return "https://www.google.com/search?q=" + urllib.parse.quote(query)


def _fmt_datetime(iso: str) -> tuple:
    """
    Parse an ISO datetime string into (display_date, display_time).

    Last.fm's events list never carries an actual show time — every row's
    datetime is midnight (e.g. "2026-07-19T00:00:00") regardless of when
    the show actually starts. Showing that as "12:00 AM" would be a fake,
    misleading time, so a midnight time component is treated as "no time
    given" and time_disp is left blank.
    """
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        date_disp = dt.strftime("%A, %B %-d, %Y")
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            return date_disp, ""
        return date_disp, dt.strftime("%-I:%M %p")
    except Exception:
        return iso, ""


def find_concerts(artist_name: str) -> list:
    """
    Return upcoming concerts for artist_name in any watched city,
    sorted earliest first and deduplicated by (date, venue).
    Tries multiple slug variants until one returns event rows.

    Raises ConcertLookupError if every slug variant failed to load at all
    (request error or non-200 on the first page) — i.e. Last.fm couldn't be
    checked for this artist, as distinct from a clean "no events" result.
    """
    any_page_loaded = False
    for slug in _artist_slugs(artist_name):
        rows = []
        for page in range(1, _MAX_EVENT_PAGES + 1):
            url = f"https://www.last.fm/music/{slug}/+events"
            if page > 1:
                url += f"?page={page}"
            try:
                resp = _http_get(url)
            except requests.RequestException as exc:
                log.debug("Last.fm request failed for %s: %s", artist_name, exc)
                break
            if resp.status_code != 200:
                break
            any_page_loaded = True

            soup = BeautifulSoup(resp.text, "html.parser")

            if page == 1:
                h1 = soup.select_one("h1[itemprop='name']")
                if h1 and not _artist_name_matches(h1.get_text(strip=True), artist_name):
                    log.debug(
                        "Last.fm slug %s resolved to a different artist (%r vs %r) — skipping",
                        slug, h1.get_text(strip=True), artist_name,
                    )
                    rows = []
                    break  # this slug landed on an unrelated artist — try next variant

            page_rows = soup.select("tr.events-list-item[itemprop='event']")
            if not page_rows:
                break
            rows.extend(page_rows)

            if not soup.select_one(".pagination-next a[data-pagination-link]"):
                break  # no more pages

        if not rows:
            continue  # this slug returned no events — try next variant

        events = []
        for row in rows:
            addr_el = row.select_one(".events-list-item-venue--address")
            address = addr_el.get_text(strip=True) if addr_el else ""
            city, ambiguous_kw = _city_for_address(address)
            if not city:
                continue

            link_el  = row.select_one("a.events-list-item-event-name")
            raw_href = link_el.get("href", "") if link_el else ""
            url      = ("https://www.last.fm" + raw_href) if raw_href.startswith("/") else raw_href

            # Fetch the event page once: it gives us both the venue's ZIP
            # code (needed below for ambiguous-city verification) and an
            # official venue/ticket link to send users to.
            postal_code, official_url, fetch_ok = _event_page_extras(url)

            # "Arlington"/"Alexandria" alone are ambiguous (they also name
            # places far outside the DC area, e.g. AT&T Stadium in
            # Arlington, TX) — confirm via the venue's ZIP code before
            # accepting the match. Only reject when the page actually loaded
            # and showed a non-matching ZIP; if Last.fm couldn't be reached
            # (e.g. rate-limited during a big autoscan), keep the show rather
            # than silently dropping a real match we just couldn't verify.
            if ambiguous_kw and fetch_ok and not (postal_code and postal_code.startswith(_DC_AREA_ZIP_PREFIXES)):
                continue

            time_el  = row.select_one("time[datetime]")
            date_raw = time_el.get("datetime", "") if time_el else ""

            # Skip shows that have already happened — we only want upcoming dates.
            # Compare by date only (not full datetime): Last.fm's events list
            # has no real time-of-day (always midnight, see _fmt_datetime),
            # and comparing that naive midnight against an offset-aware
            # datetime.now(timezone.utc) raises TypeError — which used to be
            # silently swallowed below, so past shows were never filtered.
            if date_raw:
                try:
                    event_date = datetime.fromisoformat(date_raw.replace("Z", "+00:00")).date()
                    if event_date < datetime.now(timezone.utc).date():
                        continue
                except Exception:
                    pass

            date_disp, time_disp = _fmt_datetime(date_raw) if date_raw else ("TBA", "")

            name_el    = row.select_one("[itemprop='name']")
            event_name = name_el.get_text(strip=True) if name_el else artist_name

            venue_el = row.select_one(".events-list-item-venue--title")
            venue    = venue_el.get_text(strip=True) if venue_el else ""

            events.append({
                "event_name":  event_name,
                "date":        date_disp,
                "date_raw":    date_raw,
                "time":        time_disp,
                "venue":       venue or city,
                "city":        city,
                "tickets_url": url,
                # Official venue/ticket-vendor link for this specific show,
                # when Last.fm's event page provides one. Falls back to a
                # Google ticket search (fallback_url) when absent.
                "official_url": official_url or "",
                "fallback_url": _fallback_ticket_url(artist_name, venue),
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

    if not any_page_loaded:
        raise ConcertLookupError(artist_name)
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
    failed: list = []

    def emit_result(artist, concerts):
        nonlocal found_count
        if concerts:
            found_count += 1
            spotify_url = artists_map.get(artist, "")
            return sse("result", {
                "artist":      artist,
                "spotify_url": spotify_url,
                "image":       _artist_image(spotify_url),
                "concerts":    concerts,
            })
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCERT_WORKERS) as executor:
        future_to_artist = {executor.submit(find_concerts, artist): artist for artist in artists}
        for future in concurrent.futures.as_completed(future_to_artist):
            artist = future_to_artist[future]
            completed += 1
            yield sse("progress", {"current": completed, "total": len(artists), "artist": artist})
            try:
                concerts = future.result()
            except ConcertLookupError:
                failed.append(artist)
                continue
            except Exception as exc:
                log.debug("Concert lookup failed for %s: %s", artist, exc)
                concerts = []
            event = emit_result(artist, concerts)
            if event:
                yield event

    # Last.fm couldn't be reached/parsed at all for these artists (as opposed
    # to a clean "no shows" result) — give each one a single retry now that
    # the bulk of the concurrent load has finished, so a transient rate-limit
    # blip doesn't quietly drop an artist from the results.
    if failed:
        yield sse("status", {"message": f"Retrying {len(failed)} artist(s) after rate limiting..."})
        still_failed: list = []
        for artist in failed:
            try:
                concerts = find_concerts(artist)
            except ConcertLookupError:
                still_failed.append(artist)
                continue
            event = emit_result(artist, concerts)
            if event:
                yield event
        failed = still_failed

    yield sse("done", {
        "total_artists":      len(artists),
        "artists_with_shows": found_count,
        "failed_artists":     failed,
    })


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
                name, image, artists_map = yield from scrape_spotify_playlist(playlist_url, authed=authed)
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
        _dismiss_app_prompt(driver)

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
                _dismiss_app_prompt(driver)
            except Exception:
                raise RuntimeError("Login timed out — please try again.")

        yield sse("status", {"message": "Reading your Liked Songs..."})
        _wait_for_tracklist(driver)

        artists_map = yield from _harvest_with_progress(
            driver, "Reading your Liked Songs", max_scrolls=MAX_SCROLLS
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
                artists_map = yield from _harvest_liked_songs()
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

# The playlist library now lives in the "Your Library" sidebar on the
# Spotify home page — /collection/playlists redirects to Liked Songs and no
# longer shows a playlist grid.
_PLAYLISTS_URL = "https://open.spotify.com/"
_LOGIN_URL_PLAYLISTS = (
    "https://accounts.spotify.com/login"
    "?continue=https%3A%2F%2Fopen.spotify.com%2F"
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
            _dismiss_app_prompt(driver)

            if not _spotify_logged_in(driver):
                driver.get(_LOGIN_URL_PLAYLISTS)
                yield sse("login_required", {
                    "message": "Please log into Spotify in the browser window. "
                               "Once logged in your playlists will load automatically."
                })
                try:
                    WebDriverWait(driver, LOGIN_TIMEOUT_S).until(_spotify_logged_in)
                    time.sleep(POST_LOGIN_S)
                    _dismiss_app_prompt(driver)
                except Exception:
                    yield sse("error", {"message": "Login timed out — please try again."})
                    return

            yield sse("status", {"message": "Loading your playlists..."})
            try:
                WebDriverWait(driver, ARTIST_WAIT_S).until(_library_has_playlists)
            except Exception:
                log.debug("Timed out waiting for library sidebar rows")

            playlists_map = yield from _harvest_with_progress(
                driver, "Loading your playlists", harvest_fn=_harvest_playlists, max_scrolls=MAX_SCROLLS
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

        # Spotify harvesting drives a shared Chrome profile — only one scan
        # (this one or the auto-scan) can use it at a time. Fail fast with a
        # clear message instead of letting Chrome crash with a cryptic
        # "session not created" error if both try at once.
        if not _browser_lock.acquire(blocking=False):
            yield sse("error", {"message": "A scan is already running (possibly the weekly auto-scan) — please try again in a few minutes."})
            return

        try:
            # Harvesting (Selenium/Chrome) is the part that needs exclusive
            # access to the shared browser profile — release the lock as
            # soon as it's done, before the (lock-free) Last.fm lookups.
            try:
                for source in sources:
                    try:
                        if source == "liked":
                            artists = yield from _harvest_liked_songs()
                            combined_artists.update(artists)
                            names.append("Liked Songs")
                        else:
                            playlist_name, playlist_image, artists = yield from scrape_spotify_playlist(source, authed=True)
                            combined_artists.update(artists)
                            names.append(playlist_name)
                            if not image and playlist_image:
                                image = playlist_image
                    except (RuntimeError, ValueError) as e:
                        yield sse("error", {"message": str(e)})
                        return
                    except Exception as e:
                        log.error("Scan failed for source %s: %s", source, e)
                        yield sse("error", {"message": f"Could not scan a selected playlist: {e}"})
                        return
            finally:
                _browser_lock.release()

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


# Weekly auto-scan
#
# Runs unattended (no SSE client) every Sunday morning, so it drains the
# same harvest/scrape generators used by /api/scan-multi to completion
# rather than streaming their progress anywhere.

def _drain_generator(gen):
    """
    Exhaust a generator that yields SSE strings (meant for a browser client)
    and return its StopIteration.value. Used to run the harvest/scrape
    generators from the auto-scan job, which has no SSE connection to stream
    progress to.
    """
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


# Selenium drives a single shared Chrome profile (SPOTIFY_PROFILE) so the
# Spotify login persists across scans. Chrome refuses to open a second
# instance against a profile that's already in use ("session not created:
# Chrome instance exited"), so a manual "Scan Selected" and the auto-scan's
# Spotify harvesting step can't run at the same time. This lock serializes
# them: whichever starts first holds it through harvesting, and the other
# fails fast with a clear "try again in a bit" message instead of a cryptic
# Chrome crash.
_browser_lock = threading.Lock()

# Tracks the outcome of the most recent autoscan run (scheduled or manual)
# so the UI can surface whether the weekly notification actually went out,
# and why if it didn't. Protected by _autoscan_status_lock since the
# scheduler thread and a manual "Run Now" request can both update it.
_autoscan_status_lock = threading.Lock()
_autoscan_status: dict = {"state": "idle"}


def _set_autoscan_status(**kwargs):
    with _autoscan_status_lock:
        _autoscan_status.clear()
        _autoscan_status.update(kwargs)
        _autoscan_status["updated_at"] = datetime.now().isoformat()


def get_autoscan_status() -> dict:
    with _autoscan_status_lock:
        return dict(_autoscan_status)


def _run_autoscan_once():
    """
    Run the configured weekly auto-scan: harvest artists from every saved
    source (playlists and/or Liked Songs), search Last.fm for upcoming shows
    in the watched cities, and email/text the combined results to whoever's
    configured in /api/emails and /api/phones.

    Only upcoming shows are ever included — find_concerts() already drops
    anything before today, so "this week and up" falls out naturally; this
    just runs that same search on a weekly schedule instead of on demand.

    Never raises: any failure is logged and the run is skipped, so one bad
    week (Spotify logged out, Last.fm down, SMTP misconfigured, etc.)
    doesn't take down the scheduler thread or prevent next Sunday's run.

    Records its outcome in _autoscan_status (via _set_autoscan_status) at
    every exit point so /api/autoscan/status can explain — to the second —
    why the last run did or didn't send a notification.
    """
    _set_autoscan_status(state="running")

    cfg = _load_autoscan()
    if not cfg.get("enabled"):
        log.info("Autoscan: skipped (not enabled)")
        _set_autoscan_status(state="skipped", reason="Auto-scan is turned off.")
        return
    sources = cfg.get("sources", [])
    if not sources:
        log.info("Autoscan: skipped (no playlists/Liked Songs selected)")
        _set_autoscan_status(state="skipped", reason="No playlists or Liked Songs are selected.")
        return

    log.info("Autoscan: starting weekly scan of %d source(s)", len(sources))
    combined_artists: dict = {}
    names: list = []
    source_errors: list = []

    # Spotify harvesting needs exclusive access to the shared Chrome
    # profile (see _browser_lock) — wait a bit for a manual scan to finish
    # rather than crashing Chrome by colliding with it, but don't block the
    # scheduler thread forever if something's stuck.
    if not _browser_lock.acquire(timeout=300):
        log.warning("Autoscan: skipped (browser busy with another scan for 5+ minutes)")
        _set_autoscan_status(state="skipped", reason="Could not start — a manual scan was still running after 5 minutes.")
        return
    try:
        for source in sources:
            try:
                if source == "liked":
                    artists = _drain_generator(_harvest_liked_songs())
                    combined_artists.update(artists)
                    names.append("Liked Songs")
                else:
                    playlist_name, _, artists = _drain_generator(scrape_spotify_playlist(source, authed=True))
                    combined_artists.update(artists)
                    names.append(cfg.get("labels", {}).get(source) or playlist_name)
            except Exception as exc:
                log.error("Autoscan: source %s failed: %s", source, exc)
                source_errors.append({"source": source, "error": str(exc)})
    finally:
        _browser_lock.release()

    if not combined_artists:
        log.warning("Autoscan: no artists found across configured sources — skipping send")
        _set_autoscan_status(
            state="skipped",
            reason="No artists could be loaded from the selected playlists/Liked Songs "
                   "(Spotify session may be logged out or expired).",
            source_errors=source_errors,
        )
        return

    results: list = []
    failed: list = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCERT_WORKERS) as executor:
        future_to_artist = {executor.submit(find_concerts, a): a for a in combined_artists}
        for future in concurrent.futures.as_completed(future_to_artist):
            artist = future_to_artist[future]
            try:
                concerts = future.result()
            except ConcertLookupError:
                failed.append(artist)
                continue
            except Exception as exc:
                log.debug("Autoscan: lookup failed for %s: %s", artist, exc)
                concerts = []
            if concerts:
                results.append({"artist": artist, "concerts": concerts})

    # Give artists that couldn't be checked at all (rate-limited, etc.) one
    # retry now that the bulk of the concurrent load has finished, so a
    # transient blip doesn't silently drop them from the weekly digest.
    if failed:
        log.info("Autoscan: retrying %d artist(s) after rate limiting", len(failed))
        still_failed = []
        for artist in failed:
            try:
                concerts = find_concerts(artist)
            except ConcertLookupError:
                still_failed.append(artist)
                continue
            if concerts:
                results.append({"artist": artist, "concerts": concerts})
        if still_failed:
            log.warning("Autoscan: could not check %d artist(s): %s", len(still_failed), ", ".join(still_failed))

    flat = _flatten_results(results)
    if not flat:
        log.info("Autoscan: no upcoming shows found this week — skipping send")
        _set_autoscan_status(
            state="skipped",
            reason=f"Checked {len(combined_artists)} artist(s) across {len(sources)} source(s) "
                   "but found no upcoming shows this week.",
            artists_checked=len(combined_artists),
            source_errors=source_errors,
        )
        return

    if len(names) <= 3:
        label = ", ".join(names)
    else:
        label = f"{names[0]} + {len(names) - 1} more"

    result, _ = _send_concert_digest(flat, f"Weekly Digest — {label}")
    if "error" in result:
        log.error("Autoscan: send failed: %s", result["error"])
        _set_autoscan_status(
            state="error",
            reason=result["error"],
            shows_found=len(flat),
            artists_checked=len(combined_artists),
            source_errors=source_errors,
        )
    else:
        log.info(
            "Autoscan: sent weekly digest (%d show%s) to %d recipient(s), %d error(s)",
            len(flat), "" if len(flat) == 1 else "s", len(result["sent"]), len(result["errors"]),
        )
        _set_autoscan_status(
            state="sent",
            shows_found=len(flat),
            artists_checked=len(combined_artists),
            sent_to=result["sent"],
            send_errors=result["errors"],
            source_errors=source_errors,
            label=label,
        )


def _seconds_until_next_autoscan() -> float:
    """Seconds from now until the next AUTOSCAN_WEEKDAY at AUTOSCAN_HOUR:AUTOSCAN_MINUTE local time."""
    now = datetime.now()
    days_ahead = (AUTOSCAN_WEEKDAY - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=AUTOSCAN_HOUR, minute=AUTOSCAN_MINUTE, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def _autoscan_loop():
    """
    Background loop: sleeps until the next scheduled run, performs it, then
    sleeps a minute (so a run that finishes within the same minute it started
    can't immediately re-trigger) before computing the next one.
    """
    while True:
        wait_s = _seconds_until_next_autoscan()
        log.info("Autoscan: next run in %.1f hour(s)", wait_s / 3600)
        time.sleep(wait_s)
        try:
            _run_autoscan_once()
        except Exception:
            log.exception("Autoscan: unexpected failure during scheduled run")
        time.sleep(60)


def _start_autoscan_thread():
    threading.Thread(target=_autoscan_loop, name="autoscan", daemon=True).start()


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

_AUTOSCAN_DEFAULT = {"enabled": False, "sources": [], "labels": {}}

def _load_autoscan() -> dict:
    cfg = _read_json(AUTOSCAN_FILE, _AUTOSCAN_DEFAULT)
    return {**_AUTOSCAN_DEFAULT, **cfg}

def _save_autoscan(cfg: dict):
    _write_json(AUTOSCAN_FILE, cfg)


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
    # Carrier email-to-SMS gateways (e.g. T-Mobile's @tmomail.net) expect a
    # bare 10-digit US/Canada number — a leading "1" country code makes the
    # gateway address invalid and the text silently never arrives. Strip it
    # before storing.
    if len(number) == 11 and number.startswith("1"):
        number = number[1:]
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


# Routes — /api/autoscan
#
# Lets the user pick which playlists / Liked Songs should be scanned
# automatically every Sunday morning, with the results emailed/texted to
# whoever's configured in /api/emails and /api/phones. The actual weekly
# run is driven by _autoscan_loop (a background thread started in
# __main__), which calls _run_autoscan_once.

@app.route("/api/autoscan", methods=["GET"])
def get_autoscan():
    return jsonify(_load_autoscan())


@app.route("/api/autoscan", methods=["POST"])
def save_autoscan():
    data    = request.json or {}
    sources = [s for s in data.get("sources", []) if isinstance(s, str) and s]
    labels  = {
        k: v for k, v in (data.get("labels") or {}).items()
        if isinstance(k, str) and isinstance(v, str)
    }
    cfg = {
        "enabled": bool(data.get("enabled")),
        "sources": sources,
        # Display names for playlist URLs, so the weekly digest's subject
        # line ("Weekly Digest — My Road Trip Mix") doesn't have to re-derive
        # them from a live Spotify page at send time.
        "labels":  {k: v for k, v in labels.items() if k in sources},
    }
    _save_autoscan(cfg)
    return jsonify(cfg)


@app.route("/api/autoscan/status", methods=["GET"])
def autoscan_status():
    """
    Report the outcome of the most recent autoscan run (scheduled or
    triggered via /api/autoscan/run-now), so the UI can show whether the
    weekly notification actually went out and, if not, why.
    """
    return jsonify(get_autoscan_status())


@app.route("/api/autoscan/run-now", methods=["POST"])
def autoscan_run_now():
    """
    Manually trigger the same routine the Sunday scheduler runs, in a
    background thread (a full scan across many playlists can take minutes,
    too long for a single HTTP request). Lets the user verify the weekly
    email/SMS notification actually works without waiting for Sunday.
    Poll /api/autoscan/status for the result.
    """
    status = get_autoscan_status()
    if status.get("state") == "running":
        return jsonify({"error": "An autoscan is already running"}), 409

    cfg = _load_autoscan()
    if not cfg.get("enabled"):
        return jsonify({"error": "Auto-scan is turned off — enable and save it first."}), 400
    if not cfg.get("sources"):
        return jsonify({"error": "No playlists or Liked Songs are selected."}), 400

    threading.Thread(target=_run_autoscan_once, name="autoscan-manual", daemon=True).start()
    return jsonify({"started": True})


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
            # Prefer the official venue/ticket-vendor link for this specific
            # show (from Last.fm's event page); fall back to a Google ticket
            # search if Last.fm didn't provide one.
            f'<td style="padding:10px 12px;white-space:nowrap">'
            f'  <a href="{_safe_url(c.get("official_url") or c.get("fallback_url",""))}" style="background:linear-gradient(120deg,#a855f7,#ec4899);'
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

def _send_concert_digest(flat: list, playlist_name: str) -> tuple:
    """
    Build the email/SMS bodies for `flat` concerts and send them to every
    configured recipient over a single SMTP connection.

    Returns (result_dict, status_code). result_dict is either
    {"sent": [...], "errors": [...]} on success (errors may be non-empty if
    some individual recipients failed), or {"error": "..."} if recipients,
    SMTP, or the concert list aren't set up — paired with the appropriate
    HTTP status code (400 for configuration problems, 500 for a connection
    failure).

    Shared by the manual "Send Report" button (/api/send-report) and the
    weekly auto-scan job (_run_autoscan_once) — neither has to duplicate the
    SMTP/SMS-gateway plumbing.
    """
    emails = _load_emails()
    phones = _load_phones()
    if not emails and not phones:
        return {"error": "No email or phone recipients configured"}, 400

    cfg       = _load_mail_cfg()
    smtp_pass = _smtp_password(cfg)
    if not cfg.get("smtp_user") or not smtp_pass:
        return {"error": "SMTP not configured — open Email Settings"}, 400

    if not flat:
        return {"error": "No concert results to send"}, 400

    html_body  = _build_email_html(flat, playlist_name)
    sms_body   = _build_sms_text(flat, playlist_name)
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
        # catch both so the caller always gets a clean error instead of a
        # raw exception.
        log.error("SMTP connection failed: %s", exc)
        return {"error": f"SMTP connection failed: {exc}"}, 500
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass

    return {"sent": sent, "errors": errors}, 200


@app.route("/api/send-report", methods=["POST"])
def send_report():
    data          = request.json or {}
    results       = data.get("results", [])
    playlist_name = data.get("playlist_name", "My Playlist")

    if not results:
        return jsonify({"error": "No concert results to send"}), 400

    # `results` is client-supplied JSON — guard against malformed shapes
    # (missing "artist"/"concerts" keys) so a bad payload returns a 400
    # instead of a 500.
    try:
        flat = _flatten_results(results)
    except (KeyError, TypeError) as exc:
        log.warning("Malformed results payload in send-report: %s", exc)
        return jsonify({"error": "Malformed results data"}), 400

    if not flat:
        return jsonify({"error": "No concert results to send"}), 400

    result, status = _send_concert_digest(flat, playlist_name)
    return jsonify(result), status


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port  = int(os.environ.get("PORT", 5001))
    # Skip starting the autoscan thread in the debug reloader's parent
    # process — only the child (WERKZEUG_RUN_MAIN=true) actually serves
    # requests, and we'd otherwise end up with two competing schedulers.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_autoscan_thread()
    app.run(debug=debug, host="0.0.0.0", port=port)
