#!/usr/bin/env python3
"""
Fetches top-100 movie torrents from The Pirate Bay (apibay.org),
enriches data via OMDB, and sends an HTML email with a sorted table.
"""

import os
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from dotenv import load_dotenv
import requests

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

TPB_TOP100   = "https://apibay.org/precompiled/data_top100_201.json"  # cat 201 = Movies
TOP_N        = 20       # how many movies to include in the email
OMDB_API_KEY = os.environ["OMDB_API_KEY"]

SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587
SMTP_USER  = os.environ["GMAIL_USER"]
SMTP_PASS  = (os.environ.get("GMAIL_APP_PASS") or os.environ.get("GMAIL_PASSWORD", "")).replace("\xa0", " ").strip()
EMAIL_FROM = SMTP_USER
EMAIL_TO   = os.environ.get("EMAIL_TO") or SMTP_USER

OMDB_DELAY = 0.25   # seconds between OMDB requests (free tier: 1 000 req/day)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Fetch popular movies ──────────────────────────────────────────────────────

def fetch_popular_movies() -> List[Dict]:
    """
    Fetches the TPB top-100 movies list (apibay.org), deduplicates by IMDb ID,
    and returns the TOP_N most-seeded entries for OMDB enrichment.
    """
    resp = requests.get(TPB_TOP100, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    torrents = resp.json()

    # Keep only entries with a valid IMDb ID, sort by seeders descending
    torrents = [t for t in torrents if t.get("imdb") and t["imdb"] != "None"]
    torrents.sort(key=lambda t: -int(t.get("seeders", 0)))

    movies: List[Dict] = []
    seen: set = set()
    for t in torrents:
        imdb_id = t["imdb"]
        if imdb_id in seen:
            continue          # same movie, different torrent quality
        seen.add(imdb_id)
        movies.append({
            "title":       t.get("name", ""),   # raw torrent name; OMDB will replace it
            "year":        "",
            "imdb_id":     imdb_id,
            "imdb_rating": "N/A",
            "genre":       "",
        })
        if len(movies) >= TOP_N:
            break
    return movies

# ── OMDB enrichment (fills in missing ratings / genre) ───────────────────────

def enrich_with_omdb(movie: Dict) -> Dict:
    """Query OMDB by IMDb ID to fill in title, year, rating, and genre."""
    if not movie["imdb_id"]:
        return movie
    try:
        data = requests.get(
            "https://www.omdbapi.com/",
            params={"i": movie["imdb_id"], "apikey": OMDB_API_KEY},
            headers=HEADERS,
            timeout=10,
        ).json()
        if data.get("Response") == "True":
            movie["title"]       = data.get("Title",      movie["title"])
            movie["year"]        = data.get("Year",        movie["year"])
            movie["imdb_rating"] = data.get("imdbRating",  movie["imdb_rating"])
            movie["genre"]       = data.get("Genre",       movie["genre"])
    except Exception:
        pass
    return movie

# ── HTML email ────────────────────────────────────────────────────────────────

def _rating_color(rating: str) -> str:
    try:
        r = float(rating)
        if r >= 7.0:
            return "#27ae60"
        if r >= 5.0:
            return "#e67e22"
        return "#e74c3c"
    except ValueError:
        return "#888888"


def build_html(movies: List[Dict]) -> str:
    rows = ""
    for i, m in enumerate(movies, 1):
        color     = _rating_color(m["imdb_rating"])
        bg        = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        imdb_cell = (
            f'<a href="https://www.imdb.com/title/{m["imdb_id"]}/" '
            f'style="color:{color};font-weight:bold;text-decoration:none;">'
            f'{m["imdb_rating"]} &#9733;</a>'
            if m["imdb_id"]
            else f'<span style="color:{color};">{m["imdb_rating"]}</span>'
        )
        rows += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:10px 14px;color:#777;">{i}</td>'
            f'<td style="padding:10px 14px;font-weight:500;">{m["title"]}</td>'
            f'<td style="padding:10px 14px;color:#888;">{m["year"]}</td>'
            f'<td style="padding:10px 14px;color:#999;font-size:12px;">{m["genre"]}</td>'
            f'<td style="padding:10px 14px;text-align:center;">{imdb_cell}</td>'
            f'</tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Movie Ratings</title></head>
<body style="font-family:Arial,sans-serif;background:#f0f2f5;padding:24px;margin:0;">
  <div style="max-width:840px;margin:0 auto;background:#fff;border-radius:10px;
              box-shadow:0 2px 12px rgba(0,0,0,.12);overflow:hidden;">

    <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);padding:28px 32px;">
      <h1 style="margin:0;color:#e94560;font-size:22px;">&#127909; Popular Movies This Week</h1>
      <p style="margin:6px 0 0;color:#aaa;font-size:13px;">
        Most active torrents on YTS &mdash; IMDb ratings &mdash; sorted by rating
      </p>
    </div>

    <div style="padding:20px 24px;overflow-x:auto;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#1a1a2e;color:#e94560;">
            <th style="padding:10px 14px;text-align:left;">#</th>
            <th style="padding:10px 14px;text-align:left;">Title</th>
            <th style="padding:10px 14px;text-align:left;">Year</th>
            <th style="padding:10px 14px;text-align:left;">Genre</th>
            <th style="padding:10px 14px;text-align:center;">IMDb</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>

    <div style="padding:14px 24px;background:#f9f9f9;border-top:1px solid #eee;
                font-size:11px;color:#bbb;text-align:center;">
      Generated automatically &middot; Source: YTS &middot; Ratings: IMDb via OMDB
    </div>
  </div>
</body>
</html>"""

# ── Email sending ─────────────────────────────────────────────────────────────

def send_email(html: str, subject: str = "🎬 Popular Movies This Week") -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"Email sent to {EMAIL_TO}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Fetching popular movies from YTS...")
    movies = fetch_popular_movies()
    print(f"  Got {len(movies)} movies\n")

    print("Enriching with OMDB ratings...")
    for m in movies:
        print(f"  {m['title']} ({m['year']})  imdb_id={m['imdb_id']}")
        enrich_with_omdb(m)
        time.sleep(OMDB_DELAY)

    movies.sort(key=lambda m: (
        m["imdb_rating"] in ("N/A", "0.0", ""),
        -float(m["imdb_rating"]) if m["imdb_rating"] not in ("N/A", "0.0", "") else 0,
    ))

    print("\nBuilding email...")
    html = build_html(movies)

    with open("preview.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Preview saved → preview.html")

    send_email(html)


if __name__ == "__main__":
    main()
