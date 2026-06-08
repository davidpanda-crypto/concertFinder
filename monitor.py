#!/usr/bin/env python3
"""
Concert Monitor — daily background job for Mac.

Two ways to get your artist list:
  A) Songkick (recommended): set SONGKICK_USERNAME below after connecting
     Spotify in Songkick's settings. No extra credentials needed.
  B) Spotify Liked Songs directly: set SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET
     (free from developer.spotify.com) — only needed once to log in.

The script:
  1. Pulls your artist list from Songkick or Spotify
  2. Searches each artist on Songkick for upcoming shows in NYC, DC, Charlottetown
  3. Pops a macOS notification for any show within NOTIFY_DAYS_AHEAD days
  4. Remembers what it already told you so it won't repeat itself

Install as a daily job:
  python3 monitor.py --install
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
#  CONFIG — edit these
# ─────────────────────────────────────────

SONGKICK_USERNAME = ""          # e.g. "davidpanda"  (fill in after setup)

# Only needed if NOT using Songkick (option B)
SPOTIFY_CLIENT_ID     = ""
SPOTIFY_CLIENT_SECRET = ""

# How far ahead to look (days)
NOTIFY_DAYS_AHEAD = 60

# Cities to watch  (all keywords are matched case-insensitively against venue location)
WATCH_CITIES = [
    {"label": "New York City",    "keywords": ["new york", "brooklyn", "bronx", "queens", "nyc"]},
    {"label": "Washington DC",    "keywords": ["washington", " dc", "arlington", "alexandria"]},
    {"label": "Charlottetown",    "keywords": ["charlottetown", "prince edward island", "pei"]},
]

# State file — tracks concerts already notified so you don't get spammed
STATE_FILE = Path.home() / ".concert_monitor_state.json"

# ─────────────────────────────────────────
#  HTTP helpers
# ─────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _get(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# ─────────────────────────────────────────
#  Artist sources
# ─────────────────────────────────────────

def artists_from_songkick(username: str) -> list:
    """Scrape the artist list from a public Songkick profile."""
    print(f"  Fetching artists from Songkick profile: {username}")
    artists = []
    page = 1
    while True:
        soup = _get(f"https://www.songkick.com/{username}/artists?page={page}")
        items = soup.select("li.artist strong, li.artist a, .tracked-artist .summary strong")
        if not items:
            # Try alternative selector
            items = soup.select("li[class*='artist'] a[href*='/artists/']")
        if not items:
            break
        for el in items:
            name = el.get_text(strip=True)
            if name:
                artists.append(name)
        next_link = soup.select_one("a[rel='next'], .next a, a.next_page")
        if not next_link:
            break
        page += 1
        time.sleep(0.5)
    print(f"  Found {len(artists)} tracked artists on Songkick")
    return list(dict.fromkeys(artists))  # dedupe while preserving order


def artists_from_spotify() -> list:
    """Pull liked-song artists via Spotify OAuth (one-time browser login)."""
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("ERROR: Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in monitor.py")
        sys.exit(1)

    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri="http://localhost:8888/callback",
        scope="user-library-read",
        cache_path=str(Path.home() / ".spotify_token_cache"),
        open_browser=True,
    ))

    print("  Fetching Spotify Liked Songs (browser will open for first-time login)...")
    artists = {}
    offset = 0
    while True:
        results = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        for item in items:
            track = item.get("track") or {}
            for a in track.get("artists", []):
                name = a.get("name", "").strip()
                if name:
                    artists[name] = True
        offset += len(items)
        if not results.get("next"):
            break
        time.sleep(0.1)

    print(f"  Found {len(artists)} unique artists in Liked Songs")
    return list(artists.keys())


# ─────────────────────────────────────────
#  Songkick concert search
# ─────────────────────────────────────────

def _songkick_artist_path(artist_name: str):
    query = urllib.parse.quote_plus(artist_name)
    soup = _get(f"https://www.songkick.com/search?utf8=%E2%9C%93&query={query}&type=artists")
    for a in soup.select("ul.artists-search-results li a, .search-results .artist a"):
        href = a.get("href", "")
        if "/artists/" in href:
            return href.split("?")[0]
    m = re.search(r'href="(/artists/\d+-[^"?]+)"', soup.decode_contents())
    return m.group(1) if m else None


def _city_label(location_text: str):
    text = location_text.lower()
    for city in WATCH_CITIES:
        if any(k in text for k in city["keywords"]):
            return city["label"]
    return None


def upcoming_shows_for_artist(artist_name: str) -> list:
    try:
        path = _songkick_artist_path(artist_name)
        if not path:
            return []
        soup = _get(f"https://www.songkick.com{path}/calendar")
    except Exception:
        return []

    cutoff = datetime.now(timezone.utc) + timedelta(days=NOTIFY_DAYS_AHEAD)
    shows = []

    for li in soup.select("li.event-listing, li[class*='event']"):
        loc_el = li.select_one(".location, .venue-location, [class*='location']")
        location_text = loc_el.get_text(" ", strip=True) if loc_el else ""
        city_label = _city_label(location_text)
        if not city_label:
            continue

        time_el = li.select_one("time")
        date_raw = time_el.get("datetime", "") if time_el else ""
        try:
            dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
            if dt > cutoff:
                continue          # too far away
            date_str = dt.strftime("%a %b %-d, %Y")
        except Exception:
            date_str = date_raw or "Date TBA"

        name_el = (
            li.select_one(".event-description strong, .primary-detail, h3 a, .summary a")
            or li.select_one("a[href*='/concerts/'], a[href*='/events/']")
        )
        event_name = name_el.get_text(strip=True) if name_el else artist_name

        venue_el = li.select_one(".venue-name, .venue, [class*='venue']")
        venue = venue_el.get_text(strip=True) if venue_el else city_label

        link_el = name_el or li.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        url = ("https://www.songkick.com" + href) if href.startswith("/") else href

        shows.append({
            "id":         f"{artist_name}|{date_raw}|{city_label}",
            "artist":     artist_name,
            "event":      event_name,
            "date":       date_str,
            "date_raw":   date_raw,
            "city":       city_label,
            "venue":      venue,
            "url":        url,
        })

    return shows


# ─────────────────────────────────────────
#  macOS notifications
# ─────────────────────────────────────────

def notify(title: str, message: str, url: str = ""):
    script = f'display notification "{message}" with title "{title}" sound name "default"'
    subprocess.run(["osascript", "-e", script], capture_output=True)
    if url:
        print(f"  [NOTIFY] {title} — {message}")
        print(f"           {url}")


# ─────────────────────────────────────────
#  State (avoid duplicate notifications)
# ─────────────────────────────────────────

def load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_state(seen: set):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ─────────────────────────────────────────
#  launchd installer
# ─────────────────────────────────────────

PLIST_LABEL = "com.user.concert-monitor"
PLIST_PATH  = Path.home() / "Library/LaunchAgents" / f"{PLIST_LABEL}.plist"
PYTHON_BIN  = sys.executable
SCRIPT_PATH = Path(__file__).resolve()


def install_launchd():
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>{PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{PYTHON_BIN}</string>
    <string>{SCRIPT_PATH}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>    <integer>9</integer>
    <key>Minute</key>  <integer>0</integer>
  </dict>
  <key>StandardOutPath</key>  <string>{Path.home()}/Library/Logs/concert-monitor.log</string>
  <key>StandardErrorPath</key> <string>{Path.home()}/Library/Logs/concert-monitor.log</string>
  <key>RunAtLoad</key>          <false/>
</dict>
</plist>"""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist)
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"✓ Installed — runs every day at 9:00 AM")
        print(f"  Plist:  {PLIST_PATH}")
        print(f"  Log:    ~/Library/Logs/concert-monitor.log")
    else:
        print(f"✗ launchctl error: {result.stderr}")


def uninstall_launchd():
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print("✓ Uninstalled concert monitor")
    else:
        print("Nothing to uninstall")


# ─────────────────────────────────────────
#  Main
# ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Concert Monitor")
    parser.add_argument("--install",   action="store_true", help="Install as daily launchd job")
    parser.add_argument("--uninstall", action="store_true", help="Remove launchd job")
    parser.add_argument("--spotify",   action="store_true", help="Use Spotify Liked Songs instead of Songkick profile")
    args = parser.parse_args()

    if args.install:
        install_launchd()
        return
    if args.uninstall:
        uninstall_launchd()
        return

    print(f"\n{'='*55}")
    print(f"  Concert Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Watching: {', '.join(c['label'] for c in WATCH_CITIES)}")
    print(f"  Window:   next {NOTIFY_DAYS_AHEAD} days")
    print(f"{'='*55}\n")

    # Get artist list
    if args.spotify or not SONGKICK_USERNAME:
        artists = artists_from_spotify()
    else:
        artists = artists_from_songkick(SONGKICK_USERNAME)

    if not artists:
        print("No artists found. Set SONGKICK_USERNAME or Spotify credentials.")
        return

    seen = load_state()
    new_shows = []

    print(f"\nSearching {len(artists)} artists across {len(WATCH_CITIES)} cities...\n")
    for i, artist in enumerate(artists, 1):
        print(f"[{i:>3}/{len(artists)}] {artist}", end="  ", flush=True)
        shows = upcoming_shows_for_artist(artist)
        fresh = [s for s in shows if s["id"] not in seen]
        if fresh:
            print(f"→ {len(fresh)} show(s) found!")
            new_shows.extend(fresh)
        else:
            print("—")
        time.sleep(0.4)

    print(f"\n{'─'*55}")
    if new_shows:
        # Sort by date, earliest first
        new_shows.sort(key=lambda s: s.get("date_raw", ""))
        print(f"\n🎵 {len(new_shows)} new show(s) — sending notifications:\n")
        for show in new_shows:
            msg = f"{show['date']}  •  {show['venue']}, {show['city']}"
            notify(f"🎵 {show['artist']}", msg, show["url"])
            seen.add(show["id"])
        save_state(seen)
    else:
        print("\nNo new shows found — nothing to notify.")

    print(f"\nDone. Log: ~/Library/Logs/concert-monitor.log\n")


if __name__ == "__main__":
    main()
