#!/usr/bin/env python3
"""
Scrapes the 'Most Popular Torrents' section from 1337x homepage,
queries OMDB for IMDb ratings, and sends an HTML email.
"""

import json
import os
import platform
import re
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple

import undetected_chromedriver as uc
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

SCRAPE_URL   = "https://1337x.unblockninja.st"
OMDB_API_KEY = os.environ["OMDB_API_KEY"]

SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587
SMTP_USER  = os.environ["GMAIL_USER"]
SMTP_PASS  = (os.environ.get("GMAIL_APP_PASS") or os.environ.get("GMAIL_PASSWORD", "")).replace("\xa0", " ").strip()
EMAIL_FROM = SMTP_USER
EMAIL_TO   = os.environ.get("EMAIL_TO") or SMTP_USER

OMDB_DELAY = 0.25

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

# ── ChromeDriver path ─────────────────────────────────────────────────────────
# On Linux CI: the workflow copies chromedriver to /tmp/uc_driver before running.
# On macOS dev: use the manually downloaded binary.

if platform.system() == "Linux":
    CHROMEDRIVER_PATH = "/tmp/uc_driver"
else:
    CHROMEDRIVER_PATH = "/tmp/chromedriver-mac-x64/chromedriver"

COOKIES_FILE = ".cf_cookies.json"

# ── Title cleaning ────────────────────────────────────────────────────────────

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_STOP_RE = re.compile(
    r"\b("
    r"1080p|720p|2160p|4[Kk]|480p|"
    r"BluRay|BDRip|BRRip|WEB[.\-]?DL|WEBRip|HDRip|DVDRip|HDTV|AMZN|NF|HULU|DSNP|IMAX|"
    r"x264|x265|HEVC|AVC|H\.?264|H\.?265|XviD|DivX|"
    r"AC3|DTS(?:[-.]HD)?|AAC|MP3|DD5\.1|TrueHD|Atmos|EAC3|DDP5\.1|"
    r"EXTENDED|THEATRICAL|REMASTERED|PROPER|REPACK|UNRATED|DUBBED|SUBBED|"
    r"HDR10\+?|SDR|DoVi"
    r")\b",
    re.IGNORECASE,
)


def clean_title(raw: str) -> Tuple[str, Optional[str]]:
    text = raw.replace(".", " ").replace("_", " ")
    year_match = _YEAR_RE.search(text)
    year       = year_match.group() if year_match else None
    year_pos   = year_match.start() if year_match else len(text)
    stop_match = _STOP_RE.search(text)
    stop_pos   = stop_match.start() if stop_match else len(text)
    cut        = min(year_pos, stop_pos)
    title      = re.sub(r"\s+", " ", text[:cut]).strip()
    if not title:
        title = " ".join(text.split()[:5])
    return title, year

# ── Selenium scraping ─────────────────────────────────────────────────────────

def _make_driver(headless: bool) -> uc.Chrome:
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    kwargs: Dict = {
        "options":              options,
        "use_subprocess":       True,
        "headless":             headless,
    }
    if os.path.exists(CHROMEDRIVER_PATH):
        kwargs["driver_executable_path"] = CHROMEDRIVER_PATH
    return uc.Chrome(**kwargs)


def _cf_resolved(driver: uc.Chrome) -> bool:
    t = driver.title.lower()
    return "moment" not in t and "checking" not in t and "verify" not in t


def _wait_cf(driver: uc.Chrome, timeout: int = 35) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cf_resolved(driver):
            time.sleep(1.5)
            return True
        time.sleep(1)
    return False


def _load_cookies() -> List[Dict]:
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE) as f:
            return json.load(f)
    return []


def _save_cookies(driver: uc.Chrome) -> None:
    with open(COOKIES_FILE, "w") as f:
        json.dump(driver.get_cookies(), f)


def scrape_titles(url: str) -> List[str]:
    """
    Opens 1337x with undetected Chrome, waits for Cloudflare to clear,
    and extracts movie titles from the 'Most Popular Torrents' section.
    Tries headless first (using saved cookies); falls back to visible window.
    """
    base_url = "/".join(url.split("/")[:3])
    saved    = _load_cookies()
    html     = ""

    for headless in ([True, False] if saved else [False]):
        driver = _make_driver(headless)
        try:
            if saved and headless:
                driver.get(base_url)
                time.sleep(1)
                for c in saved:
                    try:
                        driver.add_cookie(c)
                    except Exception:
                        pass

            driver.get(url)

            if not _wait_cf(driver, timeout=35):
                print("  [!] Cloudflare not resolved" + (" — retrying visibly" if headless else ""))
                continue

            _save_cookies(driver)
            html = driver.page_source
            break
        finally:
            driver.quit()

    if not html:
        raise RuntimeError("Could not load page — Cloudflare challenge not resolved.")

    soup   = BeautifulSoup(html, "html.parser")
    strong = soup.find("strong", string=lambda t: t and "Most Popular Torrents" in t)
    if not strong:
        raise RuntimeError("'Most Popular Torrents' section not found on page.")
    featured = strong.parent.parent

    titles = []
    for td in featured.select("td.name"):
        links = td.find_all("a")
        if len(links) < 2:
            continue
        if "/sub/movies/" not in links[0].get("href", ""):
            continue
        titles.append(links[1].get_text(strip=True))
    return titles

# ── OMDB lookup ───────────────────────────────────────────────────────────────

def get_omdb_info(title: str, year: Optional[str]) -> Dict:
    params: dict = {"t": title, "apikey": OMDB_API_KEY}
    if year:
        params["y"] = year
    try:
        data = requests.get("https://www.omdbapi.com/", params=params, headers=HEADERS, timeout=10).json()
        if data.get("Response") == "True":
            return {
                "imdb_rating": data.get("imdbRating", "N/A"),
                "imdb_id":     data.get("imdbID", ""),
                "genre":       data.get("Genre", ""),
                "year":        data.get("Year", year or ""),
            }
    except Exception:
        pass
    return {"imdb_rating": "N/A", "imdb_id": "", "genre": "", "year": year or ""}

# ── HTML email ────────────────────────────────────────────────────────────────

def _rating_color(rating: str) -> str:
    try:
        r = float(rating)
        return "#27ae60" if r >= 7.0 else "#e67e22" if r >= 5.0 else "#e74c3c"
    except ValueError:
        return "#888888"


def build_html(movies: List[Dict]) -> str:
    rows = ""
    for i, m in enumerate(movies, 1):
        color = _rating_color(m["imdb_rating"])
        bg    = "#f9f9f9" if i % 2 == 0 else "#ffffff"
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
<head><meta charset="UTF-8"><title>Popular Movies</title></head>
<body style="font-family:Arial,sans-serif;background:#f0f2f5;padding:24px;margin:0;">
  <div style="max-width:840px;margin:0 auto;background:#fff;border-radius:10px;
              box-shadow:0 2px 12px rgba(0,0,0,.12);overflow:hidden;">
    <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);padding:28px 32px;">
      <h1 style="margin:0;color:#e94560;font-size:22px;">&#127909; Most Popular Torrents</h1>
      <p style="margin:6px 0 0;color:#aaa;font-size:13px;">
        Source: 1337x &mdash; Ratings: IMDb via OMDB &mdash; sorted by rating
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
      Generated automatically &middot; Data from OMDB API
    </div>
  </div>
</body>
</html>"""

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(html: str, subject: str = "🎬 Most Popular Torrents") -> None:
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
    print(f"Scraping: {SCRAPE_URL}")
    raw_titles = scrape_titles(SCRAPE_URL)
    print(f"  Found {len(raw_titles)} movies\n")

    movies = []
    for raw in raw_titles:
        title, year = clean_title(raw)
        print(f"  [{year or '????'}] {title!r}")
        info = get_omdb_info(title, year)
        movies.append({
            "title":       title,
            "year":        info["year"] or year or "—",
            "imdb_rating": info["imdb_rating"],
            "imdb_id":     info["imdb_id"],
            "genre":       info["genre"],
        })
        time.sleep(OMDB_DELAY)

    movies.sort(key=lambda m: (
        m["imdb_rating"] == "N/A",
        -float(m["imdb_rating"]) if m["imdb_rating"] != "N/A" else 0,
    ))

    html = build_html(movies)
    with open("preview.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Preview saved → preview.html")
    send_email(html)


if __name__ == "__main__":
    main()
