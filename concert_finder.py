#!/usr/bin/env python3
"""
Spotify Playlist -> NYC Concert Finder

Reads every artist from a Spotify playlist and searches
Ticketmaster for upcoming New York City shows.

Setup:
  1. pip install -r requirements_concerts.txt
  2. Copy .env.example to .env and fill in your credentials
  3. python concert_finder.py
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
TICKETMASTER_API_KEY  = os.getenv("TICKETMASTER_API_KEY")

PLAYLIST_URL = "https://open.spotify.com/playlist/3eyYxErnxrMTDE6m8zy57w"
PLAYLIST_ID  = PLAYLIST_URL.split("/playlist/")[1].split("?")[0]

OUTPUT_FILE = "nyc_concerts.json"


# ---------------------------------------------------------------------------
# Spotify helpers
# ---------------------------------------------------------------------------

def get_spotify_token() -> str:
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_playlist_artists(token: str, playlist_id: str) -> list[str]:
    headers = {"Authorization": f"Bearer {token}"}
    artists: set[str] = set()
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"

    while url:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("items", []):
            track = item.get("track")
            if not track:
                continue
            for artist in track.get("artists", []):
                name = artist.get("name", "").strip()
                if name:
                    artists.add(name)

        url = data.get("next")
        time.sleep(0.1)

    return sorted(artists)


# ---------------------------------------------------------------------------
# Ticketmaster helpers
# ---------------------------------------------------------------------------

def find_nyc_concerts(artist_name: str) -> list[dict]:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "apikey":             TICKETMASTER_API_KEY,
        "keyword":            artist_name,
        "city":               "New York",
        "stateCode":          "NY",
        "countryCode":        "US",
        "classificationName": "Music",
        "sort":               "date,asc",
        "size":               10,
        "startDateTime":      now_utc,
    }

    resp = requests.get(
        "https://app.ticketmaster.com/discovery/v2/events.json",
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    events = []
    for event in data.get("_embedded", {}).get("events", []):
        start      = event.get("dates", {}).get("start", {})
        venues     = event.get("_embedded", {}).get("venues", [{}])
        venue      = venues[0] if venues else {}
        price_ranges = event.get("priceRanges", [])

        price_str = None
        if price_ranges:
            lo = price_ranges[0].get("min")
            hi = price_ranges[0].get("max")
            currency = price_ranges[0].get("currency", "USD")
            if lo and hi:
                price_str = f"{currency} {lo:.0f} - {hi:.0f}"
            elif lo:
                price_str = f"{currency} {lo:.0f}+"

        events.append({
            "event_name": event.get("name"),
            "date":       start.get("localDate"),
            "time":       start.get("localTime"),
            "venue":      venue.get("name"),
            "address":    venue.get("address", {}).get("line1"),
            "price":      price_str,
            "tickets_url": event.get("url"),
        })

    return events


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def check_env() -> None:
    missing = [v for v in (
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "TICKETMASTER_API_KEY",
    ) if not os.getenv(v)]
    if missing:
        print("ERROR: missing environment variables:", ", ".join(missing))
        print("Copy .env.example to .env and fill in the values.")
        sys.exit(1)


def print_results(results: list[dict]) -> None:
    separator = "-" * 60
    print(f"\n{'=' * 60}")
    print(f"  UPCOMING NYC CONCERTS  ({len(results)} artists with shows)")
    print(f"{'=' * 60}")

    for entry in results:
        print(f"\n  {entry['artist'].upper()}")
        for c in entry["concerts"]:
            print(separator)
            print(f"  Event : {c['event_name']}")
            print(f"  Date  : {c['date']}  {c['time'] or ''}")
            print(f"  Venue : {c['venue']}")
            if c["address"]:
                print(f"  Addr  : {c['address']}, New York, NY")
            if c["price"]:
                print(f"  Price : {c['price']}")
            print(f"  Tix   : {c['tickets_url']}")

    print(f"\n{'=' * 60}\n")


def main() -> None:
    check_env()

    print(f"Fetching artists from playlist: {PLAYLIST_URL}")
    token   = get_spotify_token()
    artists = get_playlist_artists(token, PLAYLIST_ID)
    print(f"Found {len(artists)} unique artist(s).\n")

    results: list[dict] = []
    not_found: list[str] = []

    for i, artist in enumerate(artists, 1):
        print(f"[{i}/{len(artists)}] Searching NYC concerts for: {artist}")
        try:
            concerts = find_nyc_concerts(artist)
        except requests.HTTPError as e:
            print(f"  WARNING: Ticketmaster error for {artist}: {e}")
            concerts = []

        if concerts:
            results.append({"artist": artist, "concerts": concerts})
            print(f"  -> {len(concerts)} show(s) found")
        else:
            not_found.append(artist)
            print("  -> no upcoming NYC shows")

        time.sleep(0.25)   # stay well within Ticketmaster rate limits

    print_results(results)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Full results saved to: {OUTPUT_FILE}")

    if not_found:
        print(f"\nArtists with no NYC shows ({len(not_found)}):")
        for a in not_found:
            print(f"  - {a}")


if __name__ == "__main__":
    main()
