#!/usr/bin/env python3
"""social-scan: pull public social stats and turn them into a one-page audit.

Backends
  YouTube, TikTok  -> yt-dlp, run here (logged out, no cookies); TikTok
                      followers and photo carousels from the public pages,
                      and comments (opt-in, --tiktok-comments) from TikTok's
                      unofficial web comment list, logged out and unsigned
  Snapchat         -> the public profile page (logged out, no cookies): the
                      newest Spotlights it lists, with no logged-out paging
  Podcast          -> Apple's Search API, chart and review feeds, Spotify's
                      podcast charts and the show's own RSS feed (all free)
  Instagram        -> Apify REST (`scan --instagram` or `collect --instagram`,
                      APIFY_TOKEN from the environment) or Apify MCP dataset
                      JSON via `ingest-ig`

Start here
  doctor     [--get-model]   what's installed, what each missing piece turns off,
                             and the install line for macOS and Linux
  scan       --out DIR [--youtube H] [--tiktok H] [--instagram H] [--snapchat H] [--podcast SHOW] [--name N] [--prev DIR]
             [--tiktok-followers N] [--limit N | --days N] [--yt-enrich N] [--no-hooks] [--no-comments]
             [--tiktok-comments] [--no-prev]
                             collect, hooks, comments and report in one go;
                             a step whose tool is missing is skipped with a note.
                             Use one dated --out folder per account and run
                             (<account>/<YYYY-MM-DD>/): scan reports everything
                             already in it, and compares it with the newest
                             earlier dated folder beside it.

Single steps
  collect    --out DIR [--youtube H] [--tiktok H] [--instagram H] [--snapchat H] [--podcast SHOW] [--limit N | --days N] [--yt-enrich N]
  ingest-ig  --out DIR (--reels reels.json | --posts posts.json) [--profile profile.json]
  meta       --out DIR --platform P --followers N [--following N] [--source TEXT]
  hooks      --out DIR [--top N] [--bottom N] [--seconds S]
  comments   --out DIR [--top N] [--max N] [--tiktok-comments]
  report     --out DIR [--name NAME] [--since YYYY-MM-DD] [--prev DIR | --no-prev]
  diff       OLD NEW         what changed between two scan folders of one account
  compare    DIR DIR ... [--out FILE]   one table across accounts, one scan folder each

Outlier lift tiers and the hooks pass are adapted from ScrapeCreators'
social-media-research-skills @ 64ba7b4 (MIT): outlier-post-finder and
transcript-intelligence, rewired to yt-dlp, Apify and local whisper-cli.

Tools come from PATH; YTDLP, FFMPEG, WHISPER_CLI and WHISPER_MODEL override them.
Everything is read-only. Never add --cookies or --cookies-from-browser.
"""
import argparse
import contextlib
import csv
import datetime as dt
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# overrides may arrive with a quoted, unexpanded ~ ("~/bin/yt-dlp"), so expand them here
YTDLP = os.path.expanduser(os.environ.get("YTDLP") or "yt-dlp")
FFMPEG = os.path.expanduser(os.environ.get("FFMPEG") or "ffmpeg")
WHISPER = os.path.expanduser(os.environ.get("WHISPER_CLI") or "whisper-cli")
WHISPER_MODEL = os.path.expanduser(os.environ.get("WHISPER_MODEL") or "~/.local/share/whisper-cpp/ggml-base.en.bin")
TODAY = dt.date.today()
PULLED_AT = dt.datetime.now().astimezone().isoformat(timespec="seconds")
RUN_DAY = TODAY  # the real date of this run; TODAY moves to a folder's collect day while its report is built

MIN_PY = (3, 8)
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
# sha256 of the model files `doctor --get-model` checks after download (matches Hugging Face's x-linked-etag)
MODEL_SHA256 = {"ggml-base.en.bin": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002"}
YTDLP_STALE_DAYS = 90  # YouTube and TikTok change often; an older yt-dlp is the usual cause of a failed collect

# lift = views / the group's settled median (outlier-post-finder tiers)
TIERS = ((5.0, "huge"), (2.0, "strong"), (1.5, "mild"))
MIN_RATE_N = 10  # under this many posts with likes, engagement lift is not shown

# Instagram source. Logged out, the posts feed (apify/instagram-scraper with
# resultsType posts, and apify/instagram-post-scraper) returns the grid's first
# 12 (pinned + newest) and then a scattered set of older posts: on one test
# account it skipped 29 live posts across ten weeks. The reels tab
# returns the newest N reels in order, so it is the only contiguous sample.
IG_REELS_ACTOR = "apify/instagram-reel-scraper"
IG_MAX_USD = 0.30  # hard cap per Apify call; an Instagram run makes two (about $0.11 for 40 reels)
GAP_FLAG_DAYS = 7  # a gap this long, and 4x the usual gap, gets a "check the profile" line
PLATFORMS = ("youtube", "tiktok", "instagram", "snapchat")
# public pages answer a normal browser the same way they answer anyone logged out
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0.0.0 Safari/537.36")
SNAP_EARN_S = 30  # Snapchat pays only on Spotlights this long or longer (Snap help 14669003687444, checked 2026-10-16)


# ---------- helpers ----------

def ytdlp_json(url, extra=(), flat=True, timeout=300):
    # --ignore-config: a user yt-dlp config could carry cookies; this tool stays logged out
    cmd = [YTDLP, "--ignore-config", "-J", "--socket-timeout", "20", "--no-warnings", "--skip-download"]
    if flat:
        cmd.append("--flat-playlist")
    cmd += list(extra) + [url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"{YTDLP} not found (run `doctor` for the install line)")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"yt-dlp timed out after {timeout} s")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "yt-dlp failed")
    try:
        return json.loads(p.stdout)
    except ValueError:  # json.JSONDecodeError
        raise RuntimeError("yt-dlp returned unreadable output (an old yt-dlp is the usual cause; `doctor` checks its age)")


def fetch(url, accept=None, timeout=30, max_bytes=60_000_000):
    """A public URL's bytes, logged out with no cookies. Every failure becomes RuntimeError."""
    import http.client
    import urllib.error
    import urllib.request
    if not str(url).lower().startswith(("http://", "https://")):  # never a local file from someone's catalog entry
        raise RuntimeError(f"bad URL {url!r} (only http and https are read)")
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if accept:
        headers["Accept"] = accept
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            chunks, n = [], 0
            while n <= max_bytes:  # 1 MB at a time, so no read allocates the whole cap up front
                b = r.read(min(1 << 20, max_bytes + 1 - n))
                if not b:
                    break
                chunks.append(b)
                n += len(b)
            if r.length and n <= max_bytes:  # read(amt) hands back a cut-off body without raising
                raise http.client.IncompleteRead(b"".join(chunks), r.length)
            data = b"".join(chunks)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url}")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:  # incl. a cut-off read
        raise RuntimeError(f"couldn't reach {url} ({getattr(e, 'reason', None) or type(e).__name__})")
    except ValueError as e:  # a malformed URL from a catalog or a redirect (no scheme, non-ASCII)
        raise RuntimeError(f"bad URL {url!r} ({type(e).__name__})")
    if len(data) > max_bytes:
        raise RuntimeError(f"{url} sent more than {max_bytes // 1_000_000} MB")
    return data


def fetch_json(url, accept="application/json"):
    try:
        return json.loads(fetch(url, accept=accept))
    except ValueError:
        raise RuntimeError(f"{url} sent unreadable JSON")


def page_json(url, script_id, timeout=30):
    """The JSON a public page embeds in <script id=script_id>, fetched logged out with no cookies."""
    html = fetch(url, timeout=timeout).decode("utf-8", "replace")
    m = re.search(rf'<script id="{script_id}"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError(f"{url} has no {script_id} data (blocked, changed, or not a public page)")
    try:
        return json.loads(m.group(1))
    except ValueError:
        raise RuntimeError(f"{url} sent unreadable {script_id} data")


def to_date(entry):
    ud = str(entry.get("upload_date") or "")
    if len(ud) == 8 and ud.isdigit():
        return f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"
    ts = entry.get("timestamp")
    if ts:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat()
    return None


def first_line(text, n=110):
    if not text:
        return ""
    line = str(text).strip().splitlines()[0].strip()
    return line if len(line) <= n else line[: n - 1] + "…"


def caption_hook(text, n=160):
    """The caption's opening: first sentence, plus the next one when the first is a short tease."""
    line = first_line(text, 1000)
    # split only where a new sentence visibly starts, then re-join after
    # abbreviations ("Dr.", "Aug.") and inside unclosed parentheses
    parts = []
    for piece in re.split(r"(?<=[.?!])\s+(?=[A-Z\"'(#@])", line):
        if parts and (re.search(r"\b([A-Z][a-z]{0,3}|vs|etc)\.$", parts[-1]) or parts[-1].count("(") > parts[-1].count(")")):
            parts[-1] += " " + piece
        else:
            parts.append(piece)
    hook = parts[0]
    if len(hook) < 40 and len(parts) > 1:
        hook += " " + parts[1]
    return hook if len(hook) <= n else hook[: n - 1] + "…"


def num(v):
    try:
        if v is None or v == "" or int(v) < 0:  # IG returns -1 for hidden likes
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def stamp(out, what):
    """Record when this folder's `what` data was collected. A report written
    later keeps that date, so a re-render can't pass for a fresh scan."""
    path = os.path.join(out, "collected.json")
    d = load(path) or {}
    d[what] = PULLED_AT
    save(path, d)


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def handle_of(h):
    h = h.strip().rstrip("/")
    if "/" in h:
        h = h.split("/")[-1]
    return h.lstrip("@")


# ---------- collectors ----------

def collect_youtube(handle, out, limit, enrich, days=None):
    base = f"https://www.youtube.com/@{handle_of(handle)}"
    posts, followers, errs = [], None, []
    for tab, kind in (("videos", "long"), ("shorts", "short")):
        try:
            d = ytdlp_json(f"{base}/{tab}", ["--playlist-end", str(DAYS_CAP if days else limit),
                                              "--extractor-args", "youtubetab:approximate_date"])
        except RuntimeError as e:
            errs.append(f"{tab}: {e}")
            print(f"  youtube/{tab}: skipped ({e})", file=sys.stderr)
            continue
        save(os.path.join(out, "raw", f"youtube_{tab}.json"), d)
        followers = followers or d.get("channel_follower_count")
        for e in d.get("entries") or []:
            vid = e.get("id")
            posts.append({
                "platform": "youtube", "kind": kind, "id": vid,
                "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                "date": to_date(e), "date_approx": bool(to_date(e)),
                "duration_s": e.get("duration"), "views": num(e.get("view_count")),
                "likes": None, "comments": None, "shares": None, "saves": None,
                "pinned": False, "caption": first_line(e.get("title")),
                "hook_text": first_line(e.get("title"), 300),
            })
    if not posts and errs:  # nothing came back: leave any earlier posts_youtube.json alone
        raise RuntimeError("neither the videos nor the Shorts tab loaded (yt-dlp's errors are above)")

    # Flat listings lack likes, comments and (for Shorts) dates. Fetch full
    # metadata for the newest N so the report can compute rates.
    def full(p):
        try:
            return p, ytdlp_json(f"https://www.youtube.com/watch?v={p['id']}", flat=False, timeout=90)
        except Exception:
            return p, None

    def enrich_all(targets):
        got = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            for p, d in ex.map(full, targets):
                if not d:
                    continue
                got += 1
                p["date"], p["date_approx"] = to_date(d), False
                p["likes"] = num(d.get("like_count"))
                p["comments"] = num(d.get("comment_count"))
                p["duration_s"] = d.get("duration") or p["duration_s"]
                v = num(d.get("view_count"))
                p["views"] = v if v is not None else p["views"]
        return got

    window = None
    if days and posts:
        # a date window: long-form carries an approximate date ("3 weeks ago"), so
        # enrich those within a month's slack of the cutoff; Shorts carry none, so
        # enrich them newest first until one is older than the window
        cutoff, slack = window_cutoff(days), window_cutoff(days + 31)
        all_l = [p for p in posts if p["kind"] == "long"]
        all_s = [p for p in posts if p["kind"] == "short"]
        near = [p for p in all_l if not p["date"] or p["date"] >= slack]
        enrich_all(near)
        # a post whose page didn't answer has only the listing's "3 weeks ago", which can't place it in the window
        longs = [p for p in near if not p["date_approx"]]
        # yt-dlp's "3 weeks ago" is the latest an upload can be, so one dated before the cutoff (a couple of
        # days' margin for month rounding) is out of the window for certain
        edge = window_cutoff(days + 2)
        undated = sum(1 for p in near if p["date_approx"] and (not p["date"] or p["date"] >= edge))
        shorts, reached_s, tried = [], len(all_s) < DAYS_CAP, 0
        for i in range(0, len(all_s), 12):
            batch = all_s[i:i + 12]
            tried += len(batch)
            if not enrich_all(batch):  # YouTube refused every page in the batch: stop asking
                reached_s = False
                undated += len(batch)
                break
            past = [j for j, p in enumerate(batch) if p["date"] and p["date"] < cutoff]
            cut = past[0] if past else len(batch)
            shorts += [p for p in batch[:cut] if p["date"]]
            undated += sum(1 for p in batch[:cut] if not p["date"])
            if past:
                reached_s = True
                break
        reached_l = len(all_l) < DAYS_CAP or len(near) < len(all_l) or any(p["date"] < cutoff for p in longs)
        capped = (len(all_l) >= DAYS_CAP and not reached_l) or (len(all_s) >= DAYS_CAP and reached_s is False
                                                               and tried >= DAYS_CAP)
        newest = max((p["date"] for p in all_l + all_s if p.get("date")), default=None)
        window = (reached_l and reached_s, capped, undated, newest)
        posts = longs + shorts
        print(f"  youtube: looked up {len(near) + tried} to find the last {days} days"
              + (f"; {undated} couldn't be dated and are left out" if undated else ""), file=sys.stderr)
    elif enrich and posts:
        # a third of the budget to the newest long-form, the rest to the newest
        # Shorts, then backfill unused slots from whichever kind has more
        all_l = [p for p in posts if p["kind"] == "long"]
        all_s = [p for p in posts if p["kind"] == "short"]
        longs = all_l[: max(enrich // 3, 1)]
        shorts = all_s[: enrich - len(longs)]
        longs = all_l[: enrich - len(shorts)]
        targets = longs + shorts
        enrich_all(targets)
        print(f"  youtube: enriched {len(targets)} of {len(posts)}", file=sys.stderr)

    save(os.path.join(out, "posts_youtube.json"), posts)
    stamp(out, "youtube")
    if window:
        keep_window(out, "youtube", days, reached=window[0], capped=window[1], undated=window[2], newest=window[3])
        posts = load(os.path.join(out, "posts_youtube.json"), []) or []
        if not posts:
            print(f"  youtube: no posts in the last {days} days", file=sys.stderr)
    else:
        forget_window(out, "youtube")
    if followers is not None:
        save(os.path.join(out, "meta_youtube.json"),
             {"followers": followers, "following": None, "source": "yt-dlp channel_follower_count",
              "pulled": TODAY.isoformat()})
    print(f"  youtube: {len(posts)} posts, "
          + (f"{followers:,} subscribers" if followers is not None else "subscriber count not shown"), file=sys.stderr)


def tiktok_profile(handle):
    """Followers and following from the public profile page. Raises for a handle that doesn't exist."""
    d = page_json(f"https://www.tiktok.com/@{handle}", "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    ud = (d.get("__DEFAULT_SCOPE__") or {}).get("webapp.user-detail") or {}
    if ud.get("statusCode") == 10221:  # TikTok answers 200 either way; this code means no such account
        raise RuntimeError(f"no TikTok account @{handle} (check the handle)")
    ui = ud.get("userInfo") or {}
    if ud.get("statusCode") or not ui:
        raise RuntimeError(f"profile page status {ud.get('statusCode')}")
    st, s2 = ui.get("stats") or {}, ui.get("statsV2") or {}
    return {"followers": num(s2.get("followerCount") or st.get("followerCount")),
            "following": num(s2.get("followingCount") or st.get("followingCount"))}


def tiktok_kind(url):
    """("photo", slide count, first slide URL) for a photo carousel, ("video", None, None), or None when the page won't say."""
    try:
        d = page_json(url, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    except RuntimeError:
        return None
    it = (((d.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}).get("itemInfo") or {}).get("itemStruct")
    if not it:
        return None
    ip = it.get("imagePost")
    if not ip:
        return ("video", None, None)
    images = ip.get("images") or []
    try:
        first = (((images[0] if images else {}).get("imageURL") or {}).get("urlList") or [None])[0]
    except (AttributeError, TypeError, IndexError):
        first = None
    return ("photo", len(images), first)


def collect_tiktok(handle, out, limit, quiet_followers=False, days=None):
    h, info = handle_of(handle), {}
    try:  # first, so a mistyped handle fails before the listing
        prof = tiktok_profile(h)
    except RuntimeError as e:
        if str(e).startswith("no TikTok account"):
            raise
        prof, info["followers_error"] = None, str(e)
        print(f"  tiktok: follower count not fetched ({e})", file=sys.stderr)
    asked = min(max(limit, 60), DAYS_CAP) if days else limit
    failed = False
    d = ytdlp_json(f"https://www.tiktok.com/@{h}", ["--playlist-end", str(asked)])
    window = None
    if days:  # page on until the listing passes the cutoff (pinned posts sit first, so judge by the last)
        cutoff = window_cutoff(days)
        last = lambda d: to_date((d.get("entries") or [{}])[-1]) if d.get("entries") else None
        if len(d.get("entries") or []) >= asked and (last(d) or "") >= cutoff and asked < DAYS_CAP:
            try:
                d, asked = ytdlp_json(f"https://www.tiktok.com/@{h}", ["--playlist-end", str(DAYS_CAP)]), DAYS_CAP
            except RuntimeError as e:  # keep the listing already in hand; the window reads short
                print(f"  tiktok: the longer listing failed ({e}); keeping the first {asked}", file=sys.stderr)
                failed = True
        reached = len(d.get("entries") or []) < asked or (last(d) or "") < cutoff
        newest = max((to_date(e) for e in d.get("entries") or [] if to_date(e)), default=None)
        window = (reached, not reached and not failed and len(d.get("entries") or []) >= DAYS_CAP, newest,
                  f"TikTok's longer listing failed, so only the newest {asked} were read" if failed else None)
    save(os.path.join(out, "raw", "tiktok.json"), d)
    posts = []
    for e in d.get("entries") or []:
        if days and (to_date(e) or "") < window_cutoff(days):
            continue  # older than the window: no carousel check, no row
        posts.append({
            "platform": "tiktok", "kind": "video", "id": e.get("id"), "url": e.get("url"),
            "date": to_date(e), "date_approx": False, "duration_s": e.get("duration"),
            "views": num(e.get("view_count")), "likes": num(e.get("like_count")),
            "comments": num(e.get("comment_count")), "shares": num(e.get("repost_count")),
            "saves": num(e.get("save_count")), "pinned": False,
            "caption": first_line(e.get("description") or e.get("title")),
            "hook_text": first_line(e.get("description") or e.get("title"), 300),
        })
    if not posts and days and d.get("entries"):
        print(f"  tiktok: no posts in the last {days} days (the newest is from {window[2] or '?'})",
              file=sys.stderr)
    elif not posts:
        print("  tiktok: 0 posts (private, empty, or blocked)", file=sys.stderr)
    # a photo carousel's listed length is its song's, so carousels need telling
    # apart: their cover thumbnails sit under a "photomode" CDN path (matched the
    # post pages 19 of 19 carousels and 101 of 101 videos on two accounts,
    # 2026-10-16). Only carousels, and posts with no thumbnail, get a page GET.
    thumbs = {e.get("id"): " ".join(t.get("url") or "" for t in (e.get("thumbnails") or []))
              for e in d.get("entries") or []}
    def classify(q):
        t = thumbs.get(q["id"]) or ""
        if t and "photomode" not in t:
            return ("video", None, None)
        k = tiktok_kind(q["url"]) if q.get("url") else None
        if k is None and "photomode" in t:  # the page didn't answer: the listing still says carousel
            cover = next((x.get("url") for e in d.get("entries") or [] if e.get("id") == q["id"]
                          for x in (e.get("thumbnails") or []) if "photomode" in (x.get("url") or "")), None)
            return ("photo", None, cover)
        return k
    with ThreadPoolExecutor(max_workers=4) as ex:
        kinds = list(ex.map(classify, posts))
    for q, k in zip(posts, kinds):
        if k and k[0] == "photo":
            q.update(kind="photo", slides=k[1], slide_url=k[2], duration_s=None)
        q["kind_checked"] = k is not None
    info["unchecked"] = unchecked = sum(1 for k in kinds if k is None)
    save(os.path.join(out, "posts_tiktok.json"), posts)
    stamp(out, "tiktok")
    if window:
        keep_window(out, "tiktok", days, reached=window[0], capped=window[1], newest=window[2], reason=window[3])
    else:
        forget_window(out, "tiktok")
    if prof and prof["followers"] is not None:
        save(os.path.join(out, "meta_tiktok.json"), {"followers": prof["followers"], "following": prof["following"],
                                                     "source": "tiktok.com profile page, logged out",
                                                     "pulled": TODAY.isoformat()})
    photos = sum(1 for q in posts if q["kind"] == "photo")
    print(f"  tiktok: {len(posts)} posts ({photos} photo carousel{'' if photos == 1 else 's'}"
          + (f", {unchecked} couldn't be checked and count as videos" if unchecked else "") + ")"
          + (f", {prof['followers']:,} followers" if prof and prof["followers"] is not None else
             "" if quiet_followers else f"; add followers with: python3 {here()} meta --out {shlex.quote(out)} "
             "--platform tiktok --followers N"), file=sys.stderr)
    return info


# Apple's top-level podcast genres and their Spotify chart slugs (Spotify has no
# Government or Kids & Family chart). Apple ids from its genre service, Spotify
# slugs from podcastcharts.byspotify.com, both checked 2026-10-16.
APPLE_TO_SPOTIFY = {"1301": "arts", "1321": "business", "1303": "comedy", "1304": "education", "1483": "fiction",
                    "1512": "health-fitness", "1487": "history", "1502": "leisure", "1310": "music", "1489": "news",
                    "1314": "religion-spirituality", "1533": "science", "1324": "society-culture", "1545": "sports",
                    "1309": "tv-film", "1318": "technology", "1488": "true-crime"}


SPOTIFY_NAMES = {"health-fitness": "Health & Fitness", "religion-spirituality": "Religion & Spirituality",
                 "society-culture": "Society & Culture", "tv-film": "TV & Film"}


def norm_name(x):
    """A show name for matching: its letters (with their combining vowel and tone
    marks) and digits in any script, NFKC-folded and lowercased, after dropping
    emoji and symbols first (so "3 Takeaways™" still matches "3 Takeaways", and
    "MBC 라디오" doesn't match "MBC 뉴스"). An empty result never counts as a match."""
    import unicodedata

    def keep(c):  # variation selectors (an emoji's U+FE0F) are marks too, but not part of a name
        return c.isalnum() or (unicodedata.category(c) in ("Mn", "Mc") and not "\ufe00" <= c <= "\ufe0f"
                               and not "\U000e0100" <= c <= "\U000e01ef")
    x = "".join(c for c in str(x or "") if not unicodedata.category(c).startswith("S"))
    return "".join(c for c in unicodedata.normalize("NFKC", x).casefold() if keep(c))


def spotify_rank(rows, name):
    """(rank, row) of a show in a Spotify chart, matched by name: exactly once
    normalized, else with a leading "The" dropped on both sides ("The Dan Bongino
    Show" is "Dan Bongino Show" there). (None, {}) when it isn't listed."""
    key = norm_name(name)
    if not key:
        return None, {}
    for loose in (False, True):
        strip = (lambda k: re.sub(r"^the(?=.)", "", k)) if loose else (lambda k: k)
        for i, r in enumerate(rows):  # the rank is the position in the chart as sent
            k = norm_name(r.get("showName")) if isinstance(r, dict) else ""
            if k and strip(k) == strip(key):
                return i + 1, r
    return None, {}


def apple_id(q):
    """The show ID in a podcasts.apple.com link, an "id123" token or plain ASCII digits; else None."""
    m = re.search(r"(?:^|/)id(\d{1,20})(?:$|[/?#])", q)
    if m:
        return str(int(m.group(1)))  # int() reads any script's digits; the URL gets ASCII
    return q if q.isascii() and q.isdigit() else None


def ld_json(html):
    """The schema.org objects a page embeds in <script type="application/ld+json">."""
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        out += d if isinstance(d, list) else [d]
    return [d for d in out if isinstance(d, dict)]


def app_ratings(html, cid):
    """This show's ratings record in the show page's app data: {"total": N,
    "counts": {"5": .., "1": ..}}, counts None unless they add up to N; None
    when the page has no record for this show."""
    for m in re.finditer(r'\{[^{}]*"totalNumberOfRatings":\d+[^{}]*\}', html):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        total, c = d.get("totalNumberOfRatings"), d.get("ratingCounts")
        if str(d.get("productId")) != cid or not isinstance(total, int) or total < 0:
            continue
        ok = isinstance(c, list) and len(c) == 5 and all(isinstance(x, int) and x >= 0 for x in c) and sum(c) == total
        return {"total": total, "counts": dict(zip(("5", "4", "3", "2", "1"), c)) if ok and total else None}
    return None


def podcast_secs(v):
    """itunes:duration as seconds: "02:17:27", "45:10" or "7244"."""
    try:
        parts = [int(float(x)) for x in str(v).strip().split(":")]
    except (ValueError, OverflowError):  # "inf" and "1e999" overflow int()
        return None
    if not parts or len(parts) > 3 or min(parts) < 0:
        return None
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec or None


def hms(sec):
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m"


def no_dtd(data):
    """Refuse XML with an inline DTD before ElementTree sees it: that's where
    entity declarations (billion laughs) and attribute defaults (copied onto
    every element) live, and podcast feeds don't need one. expat finds the
    prolog itself, so padding can't hide one; an external DTD is never loaded."""
    import xml.parsers.expat

    class Done(Exception):
        pass

    def doctype(name, sysid, pubid, internal):
        if internal:
            raise RuntimeError("the feed has an inline DTD (entities or attribute defaults), so it wasn't parsed")

    def decl(*_):  # a backstop: declarations only occur inside an inline DTD
        raise RuntimeError("the feed declares XML entities, so it wasn't parsed")

    def start(*_):
        raise Done  # the root element has started: no declarations can follow

    p = xml.parsers.expat.ParserCreate()
    p.StartDoctypeDeclHandler, p.EntityDeclHandler, p.StartElementHandler = doctype, decl, start
    try:
        p.Parse(data, True)
    except (Done, xml.parsers.expat.ExpatError):  # ElementTree reports a malformed feed itself
        pass


def podcast_feed(url):
    """Episode cadence and length from the show's own RSS feed."""
    import email.utils
    import io
    import xml.etree.ElementTree as ET
    data = fetch(url, accept="application/rss+xml, application/xml, text/xml")
    no_dtd(data)
    duration = "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration"
    eps = []
    try:
        for _, el in ET.iterparse(io.BytesIO(data)):
            if el.tag != "item":
                continue
            when = None
            try:
                when = email.utils.parsedate_to_datetime(el.findtext("pubDate") or "")
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
            if when and when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            eps.append({"date": when, "secs": podcast_secs(el.findtext(duration) or "")})
            el.clear()  # keep memory flat on a big feed
    except ET.ParseError as e:
        raise RuntimeError(f"the feed isn't readable XML ({e})")
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    dated = sorted((e for e in eps if e["date"]), key=lambda e: e["date"], reverse=True)
    future = sum(1 for e in dated if e["date"] > soon)  # trailers pinned ahead, typo'd years
    dated = [e for e in dated if e["date"] <= soon]
    local = lambda d: d.astimezone().date()  # local dates, like TODAY
    out = {"episodes": len(eps), "newest": local(dated[0]["date"]).isoformat() if dated else None}
    if future:
        out["future_dated"] = future
    if dated:
        # 12 hours off the 8-weeks-ago slot, so a few minutes or a clock change can't add an episode
        cut = dated[0]["date"] - dt.timedelta(weeks=8) + dt.timedelta(hours=12)
        recent = [e for e in dated if e["date"] > cut]
        old = dated[len(recent):]
        if 0 < len(old) <= 2:  # a trailer or two pinned far behind a feed that keeps only recent episodes
            gaps = [(a["date"] - b["date"]).total_seconds() / 86400 for a, b in zip(recent, recent[1:])]
            jump = (recent[-1]["date"] - old[0]["date"]).total_seconds() / 86400
            if jump > max(14, 4 * statistics.median(gaps) if gaps else 0):
                out["left_out_old"], out["left_out_oldest"] = len(old), local(old[-1]["date"]).isoformat()
                out["left_out_dates"] = sorted(local(e["date"]).isoformat() for e in old)
                dated = recent
        lens = [e["secs"] for e in recent if e["secs"]]
        if dated[-1]["date"] > cut:  # the feed starts inside the window: a new show or a trimmed feed
            span = (dated[0]["date"] - dated[-1]["date"]).total_seconds() / 86400
            out["window_from"] = local(dated[-1]["date"]).isoformat()
            # like the posts' cadence: no rate under 14 days, and N episodes span N - 1 gaps
            weeks, per_week = (round(span / 7, 1), round((len(recent) - 1) / (span / 7), 1)) if span >= 14 else (None, None)
        else:
            weeks, per_week = 8, round(len(recent) / 8, 1)
        out.update(window_weeks=weeks, in_window=len(recent), per_week=per_week,
                   median_secs=int(statistics.median(lens)) if lens else None)
    return out


def why(e):
    return str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"


def collect_podcast(query, out):
    """A podcast's Apple and Spotify chart ranks, Apple rating and reviews, and
    episode cadence: about a dozen requests to free public endpoints. Each
    source that fails becomes a line in errors; only finding the show is fatal."""
    import urllib.parse
    q, info, errors = str(query).strip(), {}, []
    aid = apple_id(q)
    if not aid and ("://" in q or "podcasts.apple.com" in q.lower()):
        raise RuntimeError(f"{q!r} is a link with no show ID in it: pass the digits after 'id' in the show's "
                           "podcasts.apple.com link")

    def results(url):
        d = fetch_json(url)
        rows = d.get("results") if isinstance(d, dict) else None
        return [r for r in rows or [] if isinstance(r, dict) and r.get("collectionId")]

    res, matched, also = [], "Apple ID", []
    if aid:
        d = fetch_json(f"https://itunes.apple.com/lookup?id={aid}&entity=podcast")
        rows = [r for r in (d.get("results") if isinstance(d, dict) else None) or [] if isinstance(r, dict)]
        res = [r for r in rows if str(r.get("collectionId")) == str(int(aid))]  # "042" is show 42
        if not res and rows:  # a publisher's ID: Apple lists the publisher, then its shows
            shows = [r for r in rows if r.get("collectionId")]
            pub = rows[0].get("artistName") or (shows[0].get("artistName") if shows else None) or "?"
            raise RuntimeError(f"{aid} is a publisher's Apple ID ({pub}), "
                               "not a show's" + (": its shows include " + ", ".join(
                                   f"{r.get('collectionName')!r} ({r['collectionId']})" for r in shows[:3]) if shows else "")
                               + ". Pass the show's own ID, the digits after 'id' in its podcasts.apple.com/…/podcast/… link")
        if not res and aid != q:
            raise RuntimeError(f"no podcast on Apple Podcasts has the ID {aid}")
    if not res:  # a name, or digits that aren't an ID (a show called "1619")
        found = results("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": q, "entity": "podcast", "limit": 10}))
        key = norm_name(q)
        exact = [r for r in found if key and norm_name(r.get("collectionName")) == key]
        if aid and not exact:
            raise RuntimeError(f"no podcast on Apple Podcasts has the ID or the name {q!r}")
        res, also = exact or found, exact[1:]
        matched = ("exact name" if len(exact) == 1 else f"exact name, shared by {len(exact)} shows") if exact \
            else "search, first result"
    if not res:
        raise RuntimeError(f"no podcast found on Apple Podcasts for {q!r} (check the name, or pass its Apple ID: "
                           "the digits after 'id' in its podcasts.apple.com link)")
    show = res[0]
    cid = str(show["collectionId"])
    if matched.startswith("search"):
        info["note"] = (f"podcast: {q!r} matched {show.get('collectionName')!r} by search; pass --podcast {cid} "
                        "to pin the show")
    elif also:
        info["note"] = (f"podcast: {len(also) + 1} shows on Apple are named {show.get('collectionName')!r}; used {cid} "
                        f"by {show.get('artistName') or '?'}; others: "
                        + ", ".join(f"{r['collectionId']} by {r.get('artistName') or '?'}" for r in also)
                        + ". Pass --podcast ID to pin the right one (Spotify ranks are matched by name too)")
    save(os.path.join(out, "raw", "podcast_lookup.json"), show)
    gids = [str(g) for g in (show.get("genreIds") or []) if str(g) != "26"]  # 26 is "Podcasts" itself
    gnames = dict(zip((str(g) for g in show.get("genreIds") or []), show.get("genres") or []))
    charts = []
    for gid in gids + [""]:  # each of the show's genres, then all podcasts
        label = gnames.get(gid, "All podcasts") if gid else "All podcasts"
        try:
            d = fetch_json("https://itunes.apple.com/us/rss/toppodcasts/limit=200" + (f"/genre={gid}" if gid else "") + "/json")
            entries = ((d.get("feed") or {}).get("entry") if isinstance(d, dict) else None) or []
            entries = [e for e in (entries if isinstance(entries, list) else [entries]) if isinstance(e, dict)]
            if not entries:
                raise RuntimeError("empty or unexpected response")
            ids = [str(((e.get("id") or {}).get("attributes") or {}).get("im:id")) if isinstance(e.get("id"), dict)
                   else None for e in entries]
            charts.append({"source": "Apple", "chart": label, "size": len(entries),
                           "rank": ids.index(cid) + 1 if cid in ids else None, "move": None})
        except Exception as e:  # one chart failing leaves the rest
            errors.append(f"Apple chart {label}: {why(e)}")
    slugs = ["top-podcasts", "trending"] + sorted({APPLE_TO_SPOTIFY[g] for g in gids if g in APPLE_TO_SPOTIFY})
    for slug in slugs:
        label = SPOTIFY_NAMES.get(slug, slug.replace("-", " ").title())
        try:
            rows = fetch_json(f"https://podcastcharts.byspotify.com/api/charts/{slug}?region=us&limit=100")
            if not isinstance(rows, list) or not rows:
                raise RuntimeError("empty or unexpected response")
            rank, row = spotify_rank(rows, show.get("collectionName"))
            move = row.get("chartRankMove")
            charts.append({"source": "Spotify", "chart": label, "size": len(rows), "rank": rank,
                           "move": move.lower() if isinstance(move, str) and move else None})
        except Exception as e:
            errors.append(f"Spotify chart {label}: {why(e)}")
    rating = ratings = counts = None
    featured = []
    try:
        html = fetch(f"https://podcasts.apple.com/us/podcast/id{cid}").decode("utf-8", "replace")
        series = next((d for d in ld_json(html) if d.get("aggregateRating") or d.get("review")), {})
        agg = series.get("aggregateRating")
        agg = agg if isinstance(agg, dict) else {}
        rating, ratings = agg.get("ratingValue"), num(agg.get("reviewCount"))  # reviewCount is the ratings count
        rating = float(rating) if isinstance(rating, (int, float)) else None
        app = app_ratings(html, cid)
        counts = app["counts"] if app and ratings and app["total"] == ratings else None
        reviews_ld = series.get("review")
        for r in reviews_ld if isinstance(reviews_ld, list) else []:  # the ones Apple shows on the page; no names
            if isinstance(r, dict):
                stars = r.get("reviewRating")
                featured.append({"rating": num(stars.get("ratingValue")) if isinstance(stars, dict) else None,
                                 "title": r.get("name"), "text": r.get("reviewBody"),
                                 "date": str(r.get("datePublished") or "")[:10] or None})
        if rating is None:
            series_types = ("CreativeWorkSeries", "PodcastSeries")
            # the page's own ratings record decides; the schema.org data only when there's no record
            if (app["total"] == 0 if app is not None else
                    any(d.get("@type") in series_types and not d.get("aggregateRating") for d in ld_json(html))):
                ratings = 0  # a show nobody has rated yet, not a page the script couldn't read
            else:
                errors.append("Apple show page: no rating found")
    except Exception as e:
        errors.append(f"Apple show page: {why(e)}")
    reviews, empty, last_full, failed, seen = [], [], 0, None, set()
    lab = lambda e, k: e[k].get("label") if isinstance(e.get(k), dict) else None
    for page in range(1, 11):  # 50 a page; Apple serves some pages empty, so stop after two in a row
        try:
            d = fetch_json(f"https://itunes.apple.com/us/rss/customerreviews/page={page}/id={cid}/sortby=mostrecent/json")
            entries = ((d.get("feed") or {}).get("entry") if isinstance(d, dict) else None) or []
            entries = [e for e in (entries if isinstance(entries, list) else [entries])
                       if isinstance(e, dict) and isinstance(e.get("im:rating"), dict)]
        except Exception as e:
            errors.append(f"Apple reviews page {page}: {why(e)}")
            failed = page
            break
        if not entries:
            empty.append(page)
            if empty[-2:] == [page - 1, page]:
                break
            continue
        last_full = page
        for e in entries:  # rating, title, text and date only: no reviewer names
            rid = lab(e, "id")
            if rid and rid in seen:  # one review can sit on two pages
                continue
            seen.add(rid)
            reviews.append({"rating": num(lab(e, "im:rating")), "title": lab(e, "title"),
                            "text": lab(e, "content"), "date": str(lab(e, "updated") or "")[:10] or None})
    holes = [x for x in empty if x < last_full]
    notes = []
    if 1 in holes:  # the newest 50 are missing: the show page's reviews are the recent ones left
        feed_end = max((r["date"] for r in reviews if r["date"]), default="?")
        key = lambda r: (str(r["title"] or "").strip().casefold(), str(r["text"] or "").strip().casefold())
        have = {key(r) for r in reviews}  # not the date: the page's and the feed's differ by a day across zones
        add = [r for r in featured if key(r) not in have]
        reviews += add
        notes.append(f"page 1, its newest 50, came back empty, so its reviews end {feed_end}"
                     + (f"; the {len(add)} review{'' if len(add) == 1 else 's'} on Apple's US show page "
                        f"{'are' if len(add) > 1 else 'is'} added" if add else ""))
    rest = [str(x) for x in holes if x != 1]
    if rest:
        notes.append(f"page{'s' if len(rest) > 1 else ''} {', '.join(rest)} came back empty, so some reviews in "
                     "between are missing")
    if failed and reviews:
        notes.append(f"page {failed} failed")
    reviews.sort(key=lambda r: r["date"] or "", reverse=True)
    source = "Apple's US review feed, newest first" + (f" ({'; '.join(notes)})" if notes else "")
    if not reviews and featured:  # the review feed can answer with nothing at all
        reviews, source = featured, ("the reviews Apple shows on its US show page (its review feed "
                                     + ("failed" if failed else "came back empty") + ")")
    elif not reviews:
        source = None
    feed = None
    if show.get("feedUrl"):
        try:
            feed = podcast_feed(show["feedUrl"])
        except Exception as e:
            errors.append(f"RSS feed: {why(e)}")
    stars = {str(k): sum(1 for r in reviews if r["rating"] == k) for k in range(5, 0, -1)}
    pod = {"pulled": TODAY.isoformat(), "query": q, "matched_by": matched, "apple_id": cid,
           "name": show.get("collectionName"), "publisher": show.get("artistName"),
           "also_named": [{"apple_id": str(r["collectionId"]), "publisher": r.get("artistName"),
                           "episodes": r.get("trackCount")} for r in also],
           "genres": [gnames.get(g) for g in gids], "episodes_listed": show.get("trackCount"),
           "apple_url": f"https://podcasts.apple.com/us/podcast/id{cid}", "feed_url": show.get("feedUrl"),
           "rating": rating, "ratings": ratings, "rating_counts": counts, "charts": charts, "feed": feed,
           "reviews": {"read": len(reviews), "source": source,
                       "newest": max((r["date"] for r in reviews if r["date"]), default=None), "stars": stars},
           "errors": errors}
    save(os.path.join(out, "podcast.json"), pod)
    stamp(out, "podcast")
    save(os.path.join(out, "reviews_apple.json"), reviews)
    ranked = [f"{c['source']} {c['chart']} #{c['rank']}" for c in charts if c["rank"]]
    print(f"  podcast: {pod['name']} ({matched}); " + (", ".join(ranked) if ranked else "not in any chart checked")
          + (f"; {rating} from {ratings:,} US rating{'' if ratings == 1 else 's'}" if rating and ratings else "; no US ratings yet" if ratings == 0 else "") + f"; {len(reviews)} review{'' if len(reviews) == 1 else 's'} read"
          + (f"; {len(errors)} source{'' if len(errors) == 1 else 's'} failed" if errors else ""), file=sys.stderr)
    info["errors"] = errors
    info["name"] = pod["name"]
    return info


def podcast_section(out):
    pod = load(os.path.join(out, "podcast.json"))
    if not pod:
        return []
    try:
        pulled = dt.date.fromisoformat(pod["pulled"])
    except (KeyError, TypeError, ValueError):
        pulled = None
    L = ["## Podcast",
         f"**[{pod['name']}]({pod['apple_url']})**" + (f" by {pod['publisher']}" if pod.get("publisher") else "")
         + (f" ({', '.join(g for g in pod.get('genres') or [] if g)})" if pod.get("genres") else "")
         + (f": rated {pod['rating']} from {pod['ratings']:,} US rating{'' if pod['ratings'] == 1 else 's'} on Apple Podcasts"
            if pod.get("rating") and pod.get("ratings")
            else ": no ratings yet on Apple Podcasts (US store)" if pod.get("ratings") == 0 else "")
         + (f"; {pod['episodes_listed']:,} episode{'' if pod['episodes_listed'] == 1 else 's'} listed"
            if pod.get("episodes_listed") else "") + "."]
    rc = pod.get("rating_counts")
    if rc and sum(rc.values()) > 1:  # with one rating the header already says it
        n = sum(rc.values())
        L.append(f"All {n:,} US Apple ratings: " + " · ".join(f"{k}★ {rc.get(k, 0) / n:.0%}" for k in ("5", "4", "3", "2", "1")) + ".")
    if pulled and pulled != RUN_DAY:
        L.append(f"These podcast numbers are from {pulled}; run scan with --podcast again to refresh them.")
    also = pod.get("also_named") or []
    if also:
        L.append(f"{len(also) + 1} shows on Apple share this name; this is Apple ID {pod['apple_id']}. Others: "
                 + ", ".join(f"{a['apple_id']} by {a.get('publisher') or '?'}" for a in also)
                 + ". Check it's the right show; Spotify ranks are matched by name, so they could belong to another.")
    elif str(pod.get("matched_by", "")).startswith("search"):
        L.append(f"Matched by search for \"{pod['query']}\": check it's the right show (Apple ID {pod['apple_id']}).")
    if pod.get("charts"):
        L += ["", f"**Charts** (US, as of {pod['pulled']}; positions move daily)", "| Chart | Rank |", "|---|---|"]
        for c in pod["charts"]:
            where = (f"#{c['rank']} of {c['size']}" + (f" ({c['move']})" if c.get("move") else "")) if c["rank"] else \
                f"not in the top {c['size']}" if c.get("size") else "couldn't read"
            L.append(f"| {c['source']} {c['chart']} | {where} |")
        L += ["", "Not in a chart means outside its top N, which is weak evidence on its own. Spotify rows are matched "
                  "by show name, so a show Spotify lists under another name reads as not in its charts."]
    f = pod.get("feed") or {}
    if f.get("episodes"):
        ago = ""
        try:
            newest = dt.date.fromisoformat(f["newest"])
        except (KeyError, TypeError, ValueError):
            newest = None
        if newest and pulled:
            days, later = (pulled - newest).days, pulled != RUN_DAY
            if days < 0:  # the feed allows items up to a day ahead
                ago = f" (dated after the {pulled} pull)" if later else " (dated ahead of today)"
            elif days == 0:
                ago = f" (the {pulled} pull day)" if later else " (today)"
            elif later:
                ago = f" ({days} day{'' if days == 1 else 's'} before the {pulled} pull)"
            else:
                ago = " (yesterday)" if days == 1 else f" ({days} days ago)"
        if f.get("per_week") is not None and f.get("window_weeks") == 8:
            rate = f"; {f['per_week']} a week over the 8 weeks before the newest episode"
        elif f.get("per_week") is not None:
            rate = (f"; {f['per_week']} a week over the {f['window_weeks']:g} weeks "
                    + (f"since {f['window_from']}" if f.get("window_from") else "the feed covers"))
        elif f.get("window_from"):
            n = f.get("in_window")
            rate = (f"; no weekly rate from one episode ({f['window_from']})" if n == 1 else
                    f"; no weekly rate: its {n if n is not None else '?'} newest episodes span under two weeks "
                    f"(since {f['window_from']})")
        else:
            rate = ""
        L += ["", f"**Episodes:** {f['episodes']:,} in the feed, newest {f.get('newest') or '?'}{ago}{rate}"
              + (f", median length {hms(f['median_secs'])}" if f.get("median_secs") else "") + "."]
        if f.get("left_out_old"):
            k = f["left_out_old"]
            when = " and ".join(f.get("left_out_dates") or [f.get("left_out_oldest") or "?"])
            L.append(f"{k} older feed item{'s' if k > 1 else ''} (from {when}) "
                     f"{'come' if k > 1 else 'comes'} long before the rest, so the cadence leaves "
                     f"{'them' if k > 1 else 'it'} out.")
        if f.get("future_dated"):
            L.append(f"{f['future_dated']} feed item{'s are' if f['future_dated'] > 1 else ' is'} dated in the future "
                     "and left out of the cadence.")
        listed = pod.get("episodes_listed") or 0
        if listed - f["episodes"] >= 10 and f["episodes"] < 0.8 * listed:  # a trimmed or paywalled public feed
            L.append(f"The public feed carries {f['episodes']:,} of the {listed:,} episodes Apple lists, so cadence and "
                     "length here may not match the show's real schedule.")
    r = pod.get("reviews") or {}
    if r.get("read"):
        st = r.get("stars") or {}
        L += ["", f"**Apple reviews (US):** {r['read']} read from {r.get('source') or 'Apple'}"
              + (f"; newest {r['newest']}" if r.get("newest") else "")
              + f". Stars in {'this one' if r['read'] == 1 else 'these ' + str(r['read'])}: " + " · ".join(f"{k}★ {st.get(k, 0)}" for k in ("5", "4", "3", "2", "1"))
              + ". Most ratings come without a written review, so this is a sample, not the rating. Text and dates "
              "are in reviews_apple.json for comment mining; reviewer names aren't kept."]
    if pod.get("errors"):
        L += ["", "Couldn't read: " + "; ".join(pod["errors"]) + "."]
    L.append("")
    return L


def snap_seo_keywords(m):
    """The keywords from Snap's SEO pass (the one that wrote llmTitle), best first; [] when absent."""
    if m.get("llmKeywords"):
        return list(m["llmKeywords"])[:6]
    for sig in m.get("cuSignals") or []:
        pr = ((sig.get("signal") or {}).get("llmPrediction") or {}).get("llmPromptResult") or {}
        if pr.get("promptName") == "seo_description_and_keywords":
            try:
                raw = json.loads((((pr.get("promptResult") or {}).get("keywords") or {}).get("result") or {}).get("stringValue") or "[]")
                pairs = [(k.rsplit("|", 1)[0], float(k.rsplit("|", 1)[1]) if "|" in k else 0) for k in raw]
            except (ValueError, TypeError, AttributeError):
                return []
            return [k for k, _ in sorted(pairs, key=lambda x: -x[1])][:6]
    return []


def collect_snapchat(handle, out):
    """Spotlights from the public profile page, logged out: one GET, plus one per
    Spotlight whose view count the profile hides. Snapchat lists only the newest
    Spotlights logged out (3 to 26 on accounts tested), with no paging."""
    h = handle_of(handle)
    try:
        pp = page_json(f"https://www.snapchat.com/@{h}", "__NEXT_DATA__")["props"]["pageProps"]
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            raise RuntimeError(f"no public Snapchat profile @{h} (check the handle)")
        raise
    except (KeyError, TypeError):
        raise RuntimeError(f"snapchat.com/@{h} didn't send profile data (changed page, or not a public profile)")
    up = pp.get("userProfile") or {}
    if up.get("$case") != "publicProfileInfo":  # a personal account: no public Spotlights or follower count
        raise RuntimeError(f"@{h} isn't a Snapchat Public Profile, so there are no Spotlights to read")
    pi = up.get("publicProfileInfo") or {}
    posts, post_of_snap = [], {}
    for hl, m in zip(pp.get("spotlightHighlights") or [], pp.get("spotlightStoryMetadata") or []):
        snaps, m = hl.get("snapList") or [], m or {}
        sid, vm = (hl.get("storyId") or {}).get("value"), m.get("videoMetadata") or {}
        if not snaps or not sid or not vm.get("contentUrl"):
            continue  # an empty slot: a removed or unavailable Spotlight
        for sn in snaps:
            post_of_snap[(sn.get("snapId") or {}).get("value") if isinstance(sn.get("snapId"), dict) else sn.get("snapId")] = sid
        es, ms = m.get("engagementStats") or {}, int(vm.get("uploadDateMs") or 0)
        when = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc) if ms else None
        text = m.get("description") or vm.get("embeddedTextCaption") or ""
        photo = snaps[0].get("snapMediaType") == 0  # an image Spotlight: Snap gives it a stock 10 s length
        dur_ms = None if photo else int(vm.get("durationMs") or 0) or None
        posts.append({
            "platform": "snapchat", "kind": "photo" if photo else "spotlight", "id": sid,
            "url": f"https://www.snapchat.com/spotlight/{sid}",
            "date": when.date().isoformat() if when else None, "date_approx": False,
            "posted_at": when.isoformat() if when else None,
            "duration_ms": dur_ms, "duration_s": round(dur_ms / 1000, 1) if dur_ms else None,
            "views": num(es.get("viewCount")), "views_hidden": es.get("viewCount") == "-1",
            "likes": num(es.get("boostCount")), "comments": num(es.get("commentCount")),
            "shares": num(es.get("shareCount")), "saves": None, "recommends": num(es.get("recommendCount")),
            "pinned": False, "caption": first_line(text), "hook_text": first_line(text, 300),
            "has_description": bool(m.get("description")), "hashtags": m.get("hashtags") or [],
            # Snap's SEO pass (title + keywords) and its separate content classifier often disagree
            "snap_ai_title": m.get("llmTitle") or None, "snap_seo_keywords": snap_seo_keywords(m),
            "snap_content_tags": m.get("textMetadataKeywords") or [],
            "media_url": vm.get("contentUrl"),  # signed CDN link, expires
            "slide_url": vm.get("contentUrl") if photo else None,
            "transcript_url": None if photo else (snaps[0].get("audioTranscriptionObjectUrl") or {}).get("value"),
        })
    for q in posts:  # the single Spotlight page sometimes shows a count the profile hides
        if q["views"] is None:
            time.sleep(1)
            try:
                v = num((page_json(q["url"], "__NEXT_DATA__")["props"]["pageProps"].get("videoMetadata") or {}).get("viewCount"))
            except (RuntimeError, KeyError, TypeError):
                v = None
            if v is not None:
                q["views"], q["views_hidden"] = v, False
    inline = {}
    try:  # one JSON string, one list of top comments per Spotlight
        groups = json.loads(pp.get("encodedSpotlightComments") or "[]")
    except ValueError:
        groups = []
    for group in groups if isinstance(groups, list) else []:
        for c in group if isinstance(group, list) else []:
            sid, ts = post_of_snap.get(c.get("snapId")), int(c.get("replyTimestampMs") or 0)
            if sid and c.get("replyText"):  # text, likes, date, replies: no names, ids or avatars
                inline.setdefault(sid, []).append({
                    "text": re.sub(r"@ ?[\w.\-]+", "@…", c["replyText"]),
                    "likes": sum(num(r.get("reactCount")) or 0 for r in (c.get("reactCounts") or [])),
                    "date": dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date().isoformat() if ts else None,
                    "replies": num(c.get("threadedReplyCount")),
                    # the ids are compared here and never saved
                    "by_creator": bool(c.get("snapPosterId")) and c.get("replyPosterId") == c.get("snapPosterId")})
    raw = {k: v for k, v in pp.items() if "comment" not in k.lower()}  # the receipt, minus commenter identities
    save(os.path.join(out, "raw", "snapchat_profile.json"), raw)
    save(os.path.join(out, "posts_snapchat.json"), posts)
    stamp(out, "snapchat")
    merge_snapchat_history(out, posts)
    if inline:
        save(os.path.join(out, "comments_snapchat_inline.json"), inline)
    save(os.path.join(out, "meta_snapchat.json"), {
        "followers": num(pi.get("subscriberCount")) or None,  # "0" means the creator hides it
        "following": None, "source": "snapchat.com profile page, logged out", "pulled": TODAY.isoformat(),
        "spotlights_visible": len(posts), "more_exist": bool(pp.get("spotlightHighlightsCursor")),
        "public_story_snaps": len((pp.get("story") or {}).get("snapList") or []),
        "saved_stories": len(pp.get("curatedHighlights") or [])})
    hidden = sum(1 for q in posts if q["views"] is None)
    print(f"  snapchat: {len(posts)} Spotlight{'' if len(posts) == 1 else 's'} (the newest the profile page lists)"
          + (f", {hidden} with views hidden" if hidden else "")
          + (f", {num(pi.get('subscriberCount')):,} followers" if num(pi.get("subscriberCount")) else ""), file=sys.stderr)


def apify_items(actor, payload, max_items, max_usd=IG_MAX_USD):
    """Run an Apify actor synchronously and return its dataset items.

    The token comes only from the APIFY_TOKEN environment variable, sent as a
    header. It is never printed, logged, or put in the URL. maxItems caps
    pay-per-result actors, maxTotalChargeUsd caps pay-per-event ones.
    """
    import urllib.request
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set (see SKILL.md step 3)")
    if re.search(r"\s", token):  # http.client would quote the whole header, token included, in its error
        raise RuntimeError("APIFY_TOKEN has a space or line break inside it; export it again")
    url = (f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
           f"?timeout=240&maxItems={int(max_items)}&maxTotalChargeUsd={max_usd}&clean=true")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def redact(text):
    """Error text with the Apify token masked, in case a library quoted it."""
    token = os.environ.get("APIFY_TOKEN", "").strip()
    return str(text).replace(token, "<APIFY_TOKEN>") if token else str(text)


def collect_instagram(handle, out, limit):
    h = handle_of(handle)
    # the reels tab, not the posts feed: see IG_REELS_ACTOR. Photos and
    # carousels have no contiguous logged-out source, so they're left out.
    reels = apify_items(IG_REELS_ACTOR, {"username": [h], "resultsLimit": limit}, limit)
    rp, pr = os.path.join(out, "raw", "instagram_reels.json"), os.path.join(out, "raw", "instagram_profile.json")
    save(rp, reels)  # saved first, so a profile failure doesn't lose a paid scrape
    # no retries: a retried run-sync call starts (and bills) a second run
    try:
        profile = apify_items("apify/instagram-profile-scraper", {"usernames": [h]}, 1)
    except Exception as e:  # the follower count is the cheap part; keep the reels in the report
        ingest_ig(rp, None, out, feed="reels")
        raise RuntimeError(f"follower count not pulled ({redact(e)}); the reels are in the report. Add followers with: "
                           f"python3 {here()} meta --out {shlex.quote(out)} --platform instagram --followers N")
    save(pr, profile)
    ingest_ig(rp, pr, out, feed="reels")


def ingest_ig(posts_path, profile_path, out, feed="reels"):
    """feed: "reels" for the reels-tab scrape (contiguous), "posts" for the
    older posts-feed scrape, which is kept readable for past runs but flagged."""
    if not os.path.isfile(posts_path):
        sys.exit(f"{posts_path} not found, so nothing was ingested (the path is read from where you run "
                 "the command, not from --out)")
    raw = load(posts_path, [])
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("data") or []
    if feed == "posts":
        print("  instagram: WARNING the posts feed is not contiguous logged out past the first 12 posts; "
              "medians and cadence from it are unreliable. Use the reels scrape (--reels).", file=sys.stderr)
    posts = []
    for e in raw:
        t = (e.get("type") or "").lower()
        kind = {"video": "reel", "image": "image", "sidecar": "carousel"}.get(t, t or "post")
        views = num(e.get("videoPlayCount"))
        if views is None:
            views = num(e.get("videoViewCount"))
        posts.append({
            "platform": "instagram", "kind": kind, "id": e.get("shortCode") or e.get("id"),
            "url": e.get("url") or (f"https://www.instagram.com/p/{e['shortCode']}/" if e.get("shortCode") else None),
            "date": (e.get("timestamp") or "")[:10] or None, "date_approx": False,
            "duration_s": e.get("videoDuration"), "views": views,
            "likes": num(e.get("likesCount")), "comments": num(e.get("commentsCount")),
            "shares": None, "saves": None, "pinned": bool(e.get("isPinned")),
            "caption": first_line(e.get("caption")),
            "hook_text": first_line(e.get("caption"), 300),
            "media_url": e.get("videoUrl"),  # signed CDN links, expire within hours to days
            "audio_url": e.get("audioUrl"),  # some videoUrls carry no audio stream
            "feed": feed,
        })
    # the posts scrape carries each post's newest comments; keep text, likes and
    # date only (no handles) for the comment-mining pass
    inline = {}
    for e in raw:
        cs = [{"text": c.get("text"), "likes": num(c.get("likesCount")), "date": (c.get("timestamp") or "")[:10] or None}
              for c in (e.get("latestComments") or []) if c.get("text")]
        if cs:
            inline[e.get("shortCode") or e.get("id")] = cs
    if inline:
        save(os.path.join(out, "comments_instagram_inline.json"), inline)
    if not posts:
        print("  instagram: 0 posts (private, empty, or blocked)", file=sys.stderr)
    save(os.path.join(out, "posts_instagram.json"), posts)
    stamp(out, "instagram")
    print(f"  instagram: {len(posts)} posts ingested", file=sys.stderr)
    if profile_path:
        prof = load(profile_path, [])
        prof = prof[0] if isinstance(prof, list) and prof else prof
        if isinstance(prof, dict) and prof:
            save(os.path.join(out, "meta_instagram.json"), {
                "followers": prof.get("followersCount"), "following": prof.get("followsCount"),
                "source": "apify/instagram-profile-scraper", "pulled": TODAY.isoformat()})
            print(f"  instagram: {prof.get('followersCount')} followers", file=sys.stderr)


# ---------- report ----------

def pct(x):
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def fmt(n):
    if n is None:
        return "n/a"
    n = int(round(n))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def median_rate(posts, field):
    rates = [p[field] / p["views"] for p in posts if p.get(field) is not None and p.get("views")]
    return statistics.median(rates) if rates else None


def group_stats(name, posts, meta=None):
    viewed = [p for p in posts if p.get("views") is not None]
    # pinned posts stay in the sample whatever their age, so they sit outside
    # the newest-N window: they get a lift and a row, never the window's dates,
    # cadence, median or tier counts
    window = [p for p in posts if not p.get("pinned")]
    dates = sorted(p["date"] for p in window if p.get("date"))
    # un-enriched YouTube uploads only say "3 months ago", so their dates bunch
    # onto a few days and can be months late: cadence and the gap check use
    # exact dates only (on 2026-10-15, 28 of 60 long-form landed on one day and
    # made a false 59-day gap)
    exact = sorted(p["date"] for p in window if p.get("date") and not p.get("date_approx"))
    approx = sorted(p["date"] for p in window if p.get("date") and p.get("date_approx"))
    s = {"group": name, "posts": len(window), "with_views": sum(1 for p in window if p.get("views") is not None),
         "dates_approx": len(approx)}
    if dates:
        # an approximate date is the latest the upload can be, so it decides
        # the first date on a tie and the last date only when it's later
        s.update(first=dates[0], last=dates[-1], first_approx=bool(approx) and (not exact or approx[0] <= exact[0]),
                 last_approx=bool(approx) and (not exact or approx[-1] > exact[-1]))
    if exact:
        # cadence runs to today, so a quiet stretch after the last post counts
        span = (TODAY - dt.date.fromisoformat(exact[0])).days
        s.update(per_week=round(len(exact) / (span / 7), 1) if span >= 14 else "n/a",
                 cadence_n=len(exact), cadence_from=exact[0])
        s["gap"] = largest_gap(exact)
    # baseline excludes posts under 3 days old, which are still climbing
    in_window = [p for p in viewed if not p.get("pinned")] or viewed
    settled = [p for p in in_window if not is_fresh(p)]
    # a burst of uploads can leave nothing settled (Snapchat lists only its newest
    # Spotlights): use them all and say the numbers will move
    s["all_fresh"] = bool(in_window) and not settled
    if s["all_fresh"]:
        settled = in_window
    s["views_hidden"] = sum(1 for p in window if p.get("views") is None and p.get("views_hidden"))
    base = settled if len(settled) >= 5 else in_window
    s["baseline_n"] = len(base)
    s["confidence"] = "low" if len(base) < 10 else "medium" if len(base) < 20 else "high"
    if base:
        views = [p["views"] for p in base]
        med = statistics.median(views)
        s.update(total_views=sum(p["views"] for p in in_window), median=med,
                 mean=round(statistics.mean(views)), max=max(p["views"] for p in in_window),
                 p90=sorted(views)[int(0.9 * (len(views) - 1))])
        # lift against this group's own median, so a reel is never scored
        # against carousels or a Short against long-form
        eng_base = [engagement(p) for p in base if engagement(p) is not None]
        eng_med = statistics.median(eng_base) if len(eng_base) >= MIN_RATE_N else None
        for p in viewed:
            p["lift"] = round(p["views"] / med, 2) if med > 0 else None
            p["tier"] = tier_of(p["lift"])
            e = engagement(p)
            p["eng_lift"] = round(e / eng_med, 2) if eng_med and e is not None else None
        ranked = sorted(viewed, key=lambda p: -p["views"])
        s["outliers"] = [p for p in ranked if p["tier"]]
        # tier counts leave out posts under 3 days old; the table still shows them, marked fresh
        s["tiers"] = {name: sum(1 for p in settled if p["tier"] == name) for _, name in TIERS}
        s["tier_n"] = len(settled)
        s["duds"] = [p for p in settled if med > 0 and p["views"] < 0.2 * med]
        # top 5 by views, extended to 10 rows only when more than 5 posts reach 1.5x
        s["top"] = s["outliers"][:10] if len(s["outliers"]) > 5 else ranked[:5]
        s["bottom"] = sorted(settled, key=lambda p: p["views"])[:5]
        top3 = sum(sorted((p["views"] for p in in_window), reverse=True)[:3])
        s["top3_share"] = top3 / s["total_views"] if s["total_views"] else None
    # Snapchat withholds the view counts of its weakest Spotlights, so a hidden
    # count is ranked below every visible one instead of dropped; dropping them ran
    # one test account's median about 2x high (2026-10-16). Hidden posts usually,
    # but not always, have fewer likes than visible ones: hidden_not_lowest flags it.
    hidden_all = [p for p in window if p.get("views") is None and p.get("views_hidden")]
    if hidden_all:
        vis_all = sorted((p for p in window if p.get("views") is not None), key=lambda p: -p["views"])
        # posts under 3 days old stay out of the baseline, as on every other platform,
        # unless that leaves too few (a burst of uploads)
        old_vis = [p for p in vis_all if not is_fresh(p)]
        old_hid = [p for p in hidden_all if not is_fresh(p)]
        vis, hidden = (old_vis, old_hid) if len(old_vis) + len(old_hid) >= 5 and old_vis else (vis_all, hidden_all)
        n = len(vis) + len(hidden)
        s["baseline_n"] = n
        s["confidence"] = "low" if n < 10 else "medium" if n < 20 else "high"
        liked = [p for p in window if p.get("likes") is not None]
        eng_pool = [engagement(p) for p in liked if not is_fresh(p)]
        if len(eng_pool) < MIN_RATE_N:
            eng_pool = [engagement(p) for p in liked]
        eng_med = statistics.median(eng_pool) if len(eng_pool) >= MIN_RATE_N else None
        for p in liked:
            p["eng_lift"] = round(engagement(p) / eng_med, 2) if eng_med else None
        s["hidden_not_lowest"] = bool(vis) and any((h.get("likes") or 0) > min((v.get("likes") or 0) for v in vis)
                                                   for h in hidden)
        med, under = censored_median([p["views"] for p in vis], len(hidden))
        if med is not None:  # the median is a visible count: lift and tiers hold
            s["median"] = med
            for p in vis_all:  # fresh posts still get a lift in the tables, marked fresh
                p["lift"] = round(p["views"] / med, 2) if med > 0 else None
                p["tier"] = tier_of(p["lift"])
            s["tiers"] = {name: sum(1 for p in vis if p["tier"] == name) for _, name in TIERS}
            s["tier_n"] = n  # hidden counts sit below every visible one, so they count as not outliers
            s["outliers"] = [p for p in vis_all if p["tier"]]
            s["top"] = s["outliers"][:10] if len(s["outliers"]) > 5 else vis_all[:5]
            s["bottom"] = (sorted(hidden, key=lambda p: p.get("likes") or 0) + vis[::-1])[:5]
            s["top3_upper"] = True  # hidden views are missing from the total, so the share is at most this
            s["duds"] = [p for p in vis if med > 0 and p["views"] < 0.2 * med]
            if vis and med > 0 and vis[-1]["views"] < 0.2 * med:  # every hidden count is lower still
                s["duds"] += hidden
            else:
                s["duds_unknown"] = len(hidden)
        else:  # half or more hidden: the median is only known to be under the lowest visible count
            s["median"], s["median_under"] = None, under
            for p in vis_all:
                p["lift"], p["tier"] = None, None
            s["tiers"], s["top3_share"], s["outliers"], s["duds"] = None, None, [], []
            s["rank_by"] = "likes"
            old = [p for p in liked if not is_fresh(p)] or liked
            s["top"] = sorted(liked, key=lambda p: -p["likes"])[:5]
            s["bottom"] = sorted(old, key=lambda p: p["likes"])[:5]
    elif not base:
        s["tiers"] = None
    s["rate_n"] = sum(1 for p in window if p.get("likes") is not None and p.get("views"))
    s["like_rate"] = median_rate(window, "likes")
    s["comment_rate"] = median_rate(window, "comments")
    s["share_rate"] = median_rate(window, "shares")
    s["fresh"] = sum(1 for p in window if is_fresh(p))
    s["pinned"] = sum(1 for p in posts if p.get("pinned"))
    return s


def censored_median(vis, n_hidden):
    """Median views when some counts are hidden below every visible one (Snapchat).
    Returns (median, under): the median when it lands on a visible count, else
    (None, lowest visible count) because it's only known to be under that."""
    vis = sorted(vis, reverse=True)
    n = len(vis) + n_hidden
    if not n:
        return None, None
    lo, hi = (n - 1) // 2, n // 2  # the median's rank(s), highest first
    if hi < len(vis):
        return (vis[lo] + vis[hi]) / 2, None
    return None, (vis[-1] if vis else None)


def largest_gap(dates):
    """The longest stretch between consecutive posts, flagged when it is at
    least GAP_FLAG_DAYS and 4x the usual gap. A scrape that skips posts shows up
    here (the 2026-10-15 posts feed jumped 12 days on a daily account); so does
    a real break, which is why the report says to check the profile."""
    ds = [dt.date.fromisoformat(d) for d in dates]
    gaps = [((ds[i] - ds[i - 1]).days, ds[i - 1], ds[i]) for i in range(1, len(ds))]
    if not gaps:
        return None
    days, a, b = max(gaps)
    usual = statistics.median(g for g, _, _ in gaps)
    return {"days": days, "from": a.isoformat(), "to": b.isoformat(), "usual": usual,
            "flag": days >= GAP_FLAG_DAYS and days >= 4 * max(usual, 1)}


def is_fresh(p):
    return bool(p.get("date")) and (TODAY - dt.date.fromisoformat(p["date"])).days < 3


def tier_of(lift):
    for floor, name in TIERS:
        if lift is not None and lift >= floor:
            return name
    return None


def engagement(p):
    """Likes + comments, or None when likes are hidden or unknown.

    Shares and saves stay out: only TikTok (shares, saves) and Snapchat (shares)
    expose them logged out, so adding them would make the same number mean
    different things per platform.
    """
    if p.get("likes") is None:
        return None
    return p["likes"] + (p.get("comments") or 0)


def followers_per_1k(posts, meta):
    total = sum(p["views"] for p in posts if p.get("views") and not p.get("pinned"))
    if meta and meta.get("followers") is not None and total:
        return round(meta["followers"] / (total / 1000), 2)
    return None


# ---------- hooks and comments (run after collect / ingest-ig) ----------

HOOK_WINDOW_MS = 10_000  # the spoken hook is what is said in the first 10 seconds
# ♪ marks sung lyrics (whisper and Snap's transcripts both use it): a song isn't a spoken hook
JUNK = re.compile(r"thanks? (you )?for watching|subscribe|subtitles? by|amara\.org|♪|^\W*$|^\s*[\[(].*[\])]\s*$", re.I)


def cell(text):
    return (text or "").replace("|", "/").replace("\n", " ").strip()


def pick_targets(groups, top, bottom):
    """Per group: the top posts by views (outliers first) and the bottom settled posts, for contrast."""
    out = []
    for plat, s in groups:
        if not s.get("top"):
            continue
        chosen = [("top", p) for p in s["top"][:top]]
        ids = {p["id"] for _, p in chosen}
        chosen += [("bottom", p) for p in s["bottom"][:bottom] if p["id"] not in ids]
        out += [(plat, s["group"], role, p) for role, p in chosen]
    return out


def vtt_hook(url):
    """Snap's own transcript for a Spotlight: speech that starts in the hook window, or "" for none."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:  # served as octet-stream: read it as text
        txt = r.read().decode("utf-8", "replace").replace("\r\n", "\n")
    words = []
    for h, m, sec, text in re.findall(r"(\d+):(\d\d):(\d\d)\.\d+ --> [^\n]+\n(.+?)(?:\n\n|\Z)", txt, re.S):
        text = text.replace("\n", " ").strip()
        if (int(h) * 3600 + int(m) * 60 + int(sec)) * 1000 < HOOK_WINDOW_MS and text and not JUNK.search(text):
            words += text.split()
    return " ".join(words[:40]) + (" …" if len(words) > 40 else "")


def save_image(url, path):
    """A photo post's image as the visual hook, scaled to 360 px when ffmpeg is there. True when saved."""
    import http.client
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
            data, ctype = r.read(), r.headers.get("Content-Type") or ""
    except (OSError, ValueError, http.client.HTTPException):
        return False
    tmp = path + ".src"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        if shutil.which(FFMPEG):
            try:
                return subprocess.run([FFMPEG, "-v", "error", "-y", "-i", tmp, "-frames:v", "1", "-vf", "scale=360:-2",
                                       path], capture_output=True, timeout=60).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        if not ctype.startswith("image/"):  # an error page, not a picture
            return False
        os.replace(tmp, path)
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def fetch_clip(plat, p, seconds, path):
    """First `seconds` of the post's video, logged out. Returns an error string or None."""
    if plat in ("instagram", "snapchat"):
        if not p.get("media_url"):
            return "no video url in the scrape (photo, carousel, or an older fixture)"
        cmd = [FFMPEG, "-v", "error", "-y", "-t", str(seconds), "-i", p["media_url"], "-c", "copy", path]
    else:
        cmd = [YTDLP, "--ignore-config", "--no-warnings", "-q", "--no-playlist",
               "-f", "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b", "--merge-output-format", "mp4",
               "--download-sections", f"*0-{seconds}", "--force-overwrites", "-o", path, p["url"]]
    for _ in range(2):  # one retry: section downloads drop now and then (ffmpeg exit 8 on 2026-10-15)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and os.path.exists(path):
            return None
    err = (r.stderr.strip().splitlines() or ["download failed"])[-1]
    return ("video link expired or blocked: " if plat in ("instagram", "snapchat") else "") + err[:160]


def model_problem(path):
    """None when `path` looks like a whisper.cpp model, otherwise what's wrong with it."""
    if not os.path.isfile(path):
        return "not found"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        return f"unreadable ({e.strerror})"
    # ggml model files start with "lmgg"; a failed download often saves an HTML or JSON error page instead
    if magic not in (b"lmgg", b"GGUF"):
        return f"not a whisper.cpp model ({size:,} bytes, starts {magic!r})"
    if size < 10_000_000:  # the smallest published model is about 31 MB
        return f"truncated ({size:,} bytes)"
    return None


def speech_off(detail=False):
    """Why spoken hooks can't run here, or None when they can. The short form
    goes into hooks.json and summary.md, so it never carries a local path."""
    if not shutil.which(FFMPEG):
        return "ffmpeg not installed"
    if not shutil.which(WHISPER):
        return "whisper-cli not installed"
    problem = model_problem(WHISPER_MODEL)
    if not problem:
        return None
    short = "whisper model missing" if problem == "not found" else "whisper model damaged"
    return f"{short} ({problem} at {WHISPER_MODEL})" if detail else short


def spoken_hook(wav, stem):
    """whisper-cli on the clip; keep confident speech segments that start inside the hook window."""
    if not os.path.exists(WHISPER_MODEL):
        return None, "whisper model missing"
    # -nth 0.3 (default 0.6): stricter no-speech cut. On 2026-10-15 the default
    # turned bird calls under a text-overlay reel into "I'm not sure." (p 0.81);
    # 0.3 dropped it and kept every real-speech segment on the same run.
    r = subprocess.run([WHISPER, "-m", WHISPER_MODEL, "-f", wav, "-sns", "-nth", "0.3", "-ojf", "-of", stem, "-np"],
                       capture_output=True, text=True, timeout=180)
    d = load(stem + ".json")
    if r.returncode != 0 or not d:
        return None, "whisper failed"
    words = []
    for seg in d.get("transcription", []):
        if seg["offsets"]["from"] >= HOOK_WINDOW_MS:
            break
        text = seg.get("text", "").strip()
        ps = [t["p"] for t in seg.get("tokens", []) if not t.get("text", "").startswith("[_")]
        # base.en invents "Thanks for watching" or music notes over music; drop
        # non-words and low-confidence segments rather than ship them as quotes
        if JUNK.search(text) or not any(c.isalpha() for c in text) or (ps and sum(ps) / len(ps) < 0.5):
            continue
        words += text.split()
    if not words:
        return "", "no speech found in the hook window (music, text-only, or silent open)"
    return " ".join(words[:40]) + (" …" if len(words) > 40 else ""), "ok"


def hooks(out, top, bottom, seconds):
    all_posts, metas, _ = load_posts(out)
    if not all_posts:
        sys.exit("No posts found. Run collect / ingest-ig first.")
    targets = pick_targets(build_groups(all_posts, metas), top, bottom)
    hdir = os.path.join(out, "hooks")
    os.makedirs(hdir, exist_ok=True)
    # degrade instead of failing: no ffmpeg keeps caption openings only, no
    # whisper keeps captions and frames, no yt-dlp keeps YouTube/TikTok captions only
    no_speech = speech_off()
    no_frames = not shutil.which(FFMPEG)
    no_clips = not shutil.which(YTDLP)
    if no_speech:
        keeps = "caption openings only" if no_frames else "caption openings and first frames"
        extra = [x for x, on in (("Snapchat Spotlights keep Snap's own transcript",
                                  any(t[0] == "snapchat" and t[3].get("transcript_url") for t in targets)),
                                 ("photo posts keep their image", any(t[3].get("slide_url") for t in targets))) if on]
        print(f"  hooks: {speech_off(detail=True)}, so {keeps}, no whisper transcripts"
              + (f" ({'; '.join(extra)})" if extra else "") + " (`doctor` has the fix)", file=sys.stderr)
    # with yt-dlp gone, an earlier hooks.json row for the same post (same views) is kept, not downgraded
    prev = {(e["platform"], e["id"]): e for e in load(os.path.join(out, "hooks.json"), []) or []}

    def better(e, t):  # an earlier row for the same post (same views) wins when it has more
        old = prev.get((e["platform"], e["id"]))
        score = lambda x: (x.get("spoken_status") == "ok") + bool(x.get("frame"))
        photoish = lambda x: (x.get("spoken_status") or "").startswith(("photo", "image link"))
        if old and old.get("views") == t[3].get("views") and photoish(old) == photoish(e) and score(old) > score(e):
            return dict(old, group=e["group"], role=e["role"], lift=e.get("lift"), tier=e.get("tier"))
        return e

    def one(t):  # one post can never sink the batch
        try:
            return better(_one(t), t)
        except Exception as err:
            plat, group, role, p = t
            print(f"    {plat} {p['id']}: {err}", file=sys.stderr)
            return {"platform": plat, "group": group, "role": role, "id": p["id"], "url": p["url"],
                    "views": p.get("views"), "lift": p.get("lift"), "tier": p.get("tier"),
                    "text_hook": caption_hook(p.get("hook_text") or p.get("caption")), "spoken_hook": None,
                    "spoken_status": f"failed: {type(err).__name__}", "frame": None}

    def _one(t):
        plat, group, role, p = t
        stem = os.path.join(hdir, f"{plat}_{p['id']}")
        e = {"platform": plat, "group": group, "role": role, "id": p["id"], "url": p["url"],
             "views": p.get("views"), "lift": p.get("lift"), "tier": p.get("tier"),
             "text_hook": caption_hook(p.get("hook_text") or p.get("caption")), "spoken_hook": None,
             "spoken_status": None, "frame": None}
        if p.get("kind") in ("image", "carousel"):
            e["spoken_status"] = "photo or carousel: text hook only"
            return e
        if p.get("kind") == "photo":  # a TikTok carousel or image Spotlight: no speech, the image is the hook
            e["spoken_status"] = "photo carousel: no speech" if plat == "tiktok" else "photo: no speech"
            if p.get("slide_url") and save_image(p["slide_url"], stem + ".jpg"):
                e["frame"] = os.path.relpath(stem + ".jpg", out)
            elif p.get("slide_url"):  # counted as a failure, so the recap says the visual hook is missing
                e["spoken_status"] = f"image link expired or blocked (re-collect {plat} to refresh it)"
            return e
        old = prev.get((plat, p["id"]))
        if no_clips and plat in ("youtube", "tiktok") and old and old.get("views") == p.get("views") \
                and (old.get("spoken_status") == "ok" or old.get("frame")):
            return dict(old, group=group, role=role, lift=p.get("lift"), tier=p.get("tier"))
        snap_said = None
        if plat == "snapchat" and p.get("transcript_url"):  # Snap's own transcript beats a local guess
            try:
                snap_said = vtt_hook(p["transcript_url"])
                e["spoken_source"] = "snap transcript"
            except Exception:  # falls through to the local whisper path
                snap_said = None
        if no_frames:
            if snap_said is not None:
                e["spoken_hook"], e["spoken_status"] = snap_said, "ok" if snap_said else \
                    "no speech found in the hook window (music, text-only, or silent open)"
            else:
                e["spoken_status"] = f"skipped: {no_speech}"
            return e
        if no_clips and plat in ("youtube", "tiktok"):
            e["spoken_status"] = "skipped: yt-dlp not installed"
            return e
        clip, wav = stem + ".mp4", stem + ".wav"
        try:
            err = fetch_clip(plat, p, seconds, clip)
            if err:
                if snap_said is not None:  # the clip link expired, but Snap's transcript is already in hand
                    e["spoken_hook"], e["spoken_status"] = snap_said, "ok" if snap_said else \
                        "no speech found in the hook window (music, text-only, or silent open)"
                else:
                    e["spoken_status"] = err
                return e
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.5", "-i", clip, "-frames:v", "1",
                            "-vf", "scale=360:-2", stem + ".jpg"], capture_output=True, timeout=60)
            if os.path.exists(stem + ".jpg"):
                e["frame"] = os.path.relpath(stem + ".jpg", out)
            if snap_said is not None:
                e["spoken_hook"], e["spoken_status"] = snap_said, "ok" if snap_said else \
                    "no speech found in the hook window (music, text-only, or silent open)"
                return e
            if no_speech:
                e["spoken_status"] = f"skipped: {no_speech}"
                return e
            for src in (clip, p.get("audio_url")):
                if src and subprocess.run([FFMPEG, "-v", "error", "-y", "-t", str(seconds), "-i", src, "-vn",
                                           "-ar", "16000", "-ac", "1", wav], capture_output=True, timeout=90).returncode == 0:
                    break
            e["spoken_hook"], e["spoken_status"] = spoken_hook(wav, stem + ".whisper") if os.path.exists(wav) \
                else (None, "no audio track")
        except subprocess.TimeoutExpired as err:  # one stuck tool shouldn't sink the batch
            e["spoken_status"] = f"failed: {os.path.basename(str(err.cmd[0]))} timed out after {err.timeout:g} s"
            print(f"    {plat} {p['id']}: {err}", file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as err:  # the report gets no local paths, stderr gets the detail
            e["spoken_status"] = f"failed: {getattr(err, 'strerror', None) or type(err).__name__}"
            print(f"    {plat} {p['id']}: {err}", file=sys.stderr)
        finally:
            for f in (clip, wav):  # keep the frame and transcript as receipts, not the client's video
                if os.path.exists(f):
                    os.remove(f)
        return e

    with ThreadPoolExecutor(max_workers=3) as ex:
        entries = list(ex.map(one, targets))
    if no_clips:
        yt = [e for e in entries if e["platform"] in ("youtube", "tiktok") and
              not (e.get("spoken_status") or "").startswith(("photo", "image link"))]
        bare = sum(1 for e in yt if e.get("spoken_status") == "skipped: yt-dlp not installed")
        if yt:
            print("  hooks: yt-dlp not installed: " + "; ".join(x for x, k in (
                (f"{len(yt) - bare} YouTube/TikTok posts kept from the earlier hooks.json", len(yt) - bare),
                (f"{bare} keep their caption opening only", bare)) if k), file=sys.stderr)
    save(os.path.join(out, "hooks.json"), entries)
    ok = sum(1 for e in entries if e["spoken_status"] == "ok")
    print(f"  hooks: {len(entries)} posts, {ok} spoken hooks -> hooks.json", file=sys.stderr)
    for e in entries:
        if e["spoken_status"] != "ok" and not (e["spoken_status"] or "").startswith(("skipped: ", "photo")):
            print(f"    {e['platform']} {e['id']}: {e['spoken_status']}", file=sys.stderr)


def hooks_stale(out):
    """True when hooks.json came from an earlier collect than the posts now in the
    folder: each entry keeps the view count its post had when it was picked, and
    live counts move between collects. (File times can't be trusted: a copied
    folder gets new ones.)"""
    entries = load(os.path.join(out, "hooks.json"))
    if not entries:
        return False
    views = {(p["platform"], p["id"]): p.get("views") for p in load_posts(out)[0]}
    matched = [e for e in entries if (e["platform"], e["id"]) in views]
    return not matched or any(e.get("views") != views[(e["platform"], e["id"])] for e in matched)


def hooks_section(out, all_posts):
    entries = load(os.path.join(out, "hooks.json"))
    if not entries:
        return []
    live = {(p["platform"], p["id"]): p for p in all_posts}
    entries = [e for e in entries if (e["platform"], e["id"]) in live]  # --since can drop some
    if not entries:
        return []
    # a column nothing filled (tools missing on this run) is left out instead of
    # repeating the reason in every row of a client-facing table
    spoken = any(e.get("spoken_hook") is not None for e in entries)
    frames = any(e.get("frame") for e in entries)
    secs = HOOK_WINDOW_MS // 1000
    intro = "Text hook = caption or title first line, verbatim."
    if spoken:  # name only the sources that filled rows
        srcs = [x for x, on in ((f"whisper-cli (base.en) on the first {secs} seconds",
                                 any(e.get("spoken_hook") is not None and not e.get("spoken_source") for e in entries)),
                                (f"Snapchat's own transcript (first {secs} seconds) for Spotlights",
                                 any(e.get("spoken_hook") is not None and e.get("spoken_source") for e in entries))) if on]
        intro += f" Spoken hook = {' or '.join(srcs)}, machine transcript: check it against the video before quoting."
    if frames:
        intro += (" Frame = " + " or ".join(x for x, on in (
            ("the picture at 0.5 seconds", any(e.get("frame") and not (e.get("spoken_status") or "").startswith("photo") for e in entries)),
            ("a photo post's image", any(e.get("frame") and (e.get("spoken_status") or "").startswith("photo") for e in entries)))
            if on) + ", the visual hook.")
    missing = [w for w, on in (("spoken hooks", spoken), ("first frames", frames)) if not on]
    if missing:
        intro += f" {' and '.join(missing).capitalize()} weren't pulled on this run."
    head = ["Post", "Role", "Lift", "Text hook"] + ([f"Spoken hook (first {secs} s)"] if spoken else []) \
        + (["Frame"] if frames else [])
    L = ["## Hooks: top posts vs bottom posts", intro, ""]
    grp = lambda e: live[(e["platform"], e["id"])].get("group") or e["group"]  # an older hooks.json may use old names
    for group in dict.fromkeys(grp(e) for e in entries):
        L += [f"**{group.title()}**", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for e in (x for x in entries if grp(x) == group):
            p = live[(e["platform"], e["id"])]
            text_hook = caption_hook(p.get("hook_text")) if p.get("hook_text") else e.get("text_hook")
            row = [f"[{fmt(p.get('views'))}]({e['url']})", e["role"], lift_txt(p), cell(text_hook) or "_(no caption)_"]
            if spoken:
                row.append(f'"{cell(e["spoken_hook"])}"' if e.get("spoken_hook") else f"_{cell(e.get('spoken_status'))}_")
            if frames:
                row.append(f"[frame]({e['frame']})" if e.get("frame") else "")
            L.append("| " + " | ".join(row) + " |")
        L.append("")
    return L


def mask_mentions(text, extra=None):
    """Comment text with mentions masked. TikTok's own mention spans (the
    text_extra entries that name a user) go first, each read both as
    characters and as UTF-16 units so either reading is covered; then any
    typed @name and any email address."""
    t = str(text or "")
    drop = [False] * len(t)
    unit = []  # the character each UTF-16 unit belongs to
    for i, ch in enumerate(t):
        unit += [i] * (2 if ord(ch) > 0xFFFF else 1)
    for x in extra or []:
        if not isinstance(x, dict) or not (x.get("user_id") or x.get("sec_uid")):
            continue
        st, en = x.get("start"), x.get("end")
        if not isinstance(st, int) or not isinstance(en, int) or not 0 <= st < en:
            continue
        spans = []
        if st < len(t):
            spans.append((st, min(en, len(t))))
        if st < len(unit):
            spans.append((unit[st], unit[min(en, len(unit)) - 1] + 1))
        at = [sp for sp in spans if t[sp[0]] in "@＠"]
        for a, b in (at[:1] if len(at) == 1 else spans):  # the reading that lands on the "@", else both to be safe
            for i in range(a, b):
                drop[i] = True
    out, i = [], 0
    while i < len(t):
        if drop[i]:
            while i < len(t) and drop[i]:
                i += 1
            out.append("@…")
        else:
            out.append(t[i])
            i += 1
    t = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "…@…", "".join(out))
    return re.sub(r"[@＠]\s?(?!…)[^\s@＠]+", "@…", t)


def tiktok_comments(post, max_n=100):
    """Comments on one TikTok post from TikTok's web comment list, logged out
    and unsigned. The endpoint is unofficial, so it can stop answering at any
    time. Top-level comments only (reply threads aren't read; `replies` is
    their count). Keeps text, likes, replies, creator-liked, language and
    date; the commenter's handle is compared with the creator's and never
    saved. Returns (comments, total, why the read stopped early or None)."""
    import urllib.parse
    m = re.search(r"tiktok\.com/@([^/?#]+)/", post.get("url") or "")
    creator = (m.group(1) if m else "").casefold()
    out, seen, cursor, total, stopped = [], set(), 0, None, None
    for page in range(10):  # 20 a page asked, about 13-14 come back
        time.sleep(0.5)  # a polite gap before every request, the first of each post included
        u = "https://www.tiktok.com/api/comment/list/?" + urllib.parse.urlencode(
            {"aid": 1988, "aweme_id": post["id"], "count": 20, "cursor": cursor})
        try:
            try:
                d = fetch_json(u)
            except RuntimeError as e:
                if "unreadable JSON" in str(e):  # an empty or HTML answer: the usual sign of a blocked request
                    raise RuntimeError("TikTok's comment list sent nothing usable (it may want signed requests now)")
                raise RuntimeError(str(e).replace(u, "TikTok's comment list"))
            if not isinstance(d, dict):
                raise RuntimeError("TikTok's comment list sent an unexpected answer")
            code = d.get("status_code")
            if code not in (None, 0, "0"):
                raise RuntimeError(f"TikTok's comment list refused the request (status {code}"
                                   + (f": {str(d.get('status_msg'))[:80]}" if d.get("status_msg") else "") + ")")
            listed = num(post.get("comments")) or 0
            if page == 0 and not d.get("comments") and listed > 2:  # a soft block: an empty answer for a post with comments
                raise RuntimeError(f"TikTok's comment list sent no comments for a post that lists {listed:,} "
                                   "(it may want signed requests now)")
        except RuntimeError as e:
            if not out:
                raise
            stopped = f"stopped after {len(out)}: {e}"  # keep what was read
            break
        total = num(d.get("total")) if total is None else total
        for c in d.get("comments") or []:
            if len(out) >= max_n:
                break
            if not isinstance(c, dict) or not c.get("text") or c.get("cid") in seen:
                continue
            seen.add(c.get("cid"))
            user = c.get("user") if isinstance(c.get("user"), dict) else {}
            ts = num(c.get("create_time"))
            out.append({"text": mask_mentions(c["text"], c.get("text_extra")), "likes": num(c.get("digg_count")),
                        "replies": num(c.get("reply_comment_total")), "creator_liked": bool(c.get("is_author_digged")),
                        "lang": c.get("comment_language") or None,
                        "date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat() if ts else None,
                        "by_creator": bool(creator) and str(user.get("unique_id") or "").casefold() == creator})
        if len(out) >= max_n or not d.get("has_more") or not d.get("comments"):
            break
        cursor = num(d.get("cursor")) or cursor + 20
    return out, total, stopped


def comments(out, top, max_comments, tiktok=False):
    """Comment text for the top posts: YouTube via yt-dlp, Instagram from the posts
    scrape, Snapchat from the profile page, and TikTok (with tiktok=True) from
    TikTok's unofficial web comment list."""
    all_posts, metas, _ = load_posts(out)
    every = pick_targets(build_groups(all_posts, metas), top, 0)
    targets = [t for t in every if tiktok or t[0] != "tiktok"]
    earlier = load(os.path.join(out, "comments.json"), []) or []
    tk_now = {t[3]["id"]: t for t in every if t[0] == "tiktok"}
    kept_tk = [] if tiktok else [dict(e, group=tk_now[e["id"]][1], lift=tk_now[e["id"]][3].get("lift"),
                                      tier=tk_now[e["id"]][3].get("tier"))
                                 for e in earlier if e.get("platform") == "tiktok" and e.get("comments")
                                 and e.get("id") in tk_now]
    slow = None  # set once TikTok answers 429: no more TikTok requests this run
    prev = {e["id"]: e for e in load(os.path.join(out, "comments.json"), []) or []
            if e.get("platform") == "youtube" and not (e.get("source") or "").startswith("yt-dlp failed")}
    no_ytdlp = not shutil.which(YTDLP)
    if no_ytdlp and any(t[0] == "youtube" for t in targets):
        print("  comments: yt-dlp not installed, so YouTube comments come from the earlier comments.json where it has "
              "them, and are left out otherwise", file=sys.stderr)
        targets = [t for t in targets if t[0] != "youtube" or t[3]["id"] in prev]
    inline = load(os.path.join(out, "comments_instagram_inline.json"), {})
    snap_inline = load(os.path.join(out, "comments_snapchat_inline.json"), {})
    result = []
    for plat, group, _, p in targets:
        e = {"platform": plat, "group": group, "id": p["id"], "url": p["url"], "lift": p.get("lift"),
             "tier": p.get("tier"), "comment_count": p.get("comments"), "source": None, "comments": []}
        if plat == "youtube" and no_ytdlp:
            e = dict(prev[p["id"]], group=group, lift=p.get("lift"), tier=p.get("tier"))
        elif plat == "youtube":
            try:
                d = ytdlp_json(p["url"], ["--write-comments", "--extractor-args",
                                          f"youtube:max_comments={max_comments},all,0,0;comment_sort=top"],
                               flat=False, timeout=180)
            except Exception as err:
                e["source"] = f"yt-dlp failed: {err}"
            else:
                e["source"] = "yt-dlp, top-sorted"
                # handles dropped on purpose; uploader replies flagged so they aren't read as audience
                e["comments"] = [{"text": mask_mentions(c.get("text")), "likes": num(c.get("like_count")),
                                  "date": to_date(c), "reply": c.get("parent") != "root",
                                  "by_creator": bool(c.get("author_is_uploader"))}
                                 for c in d.get("comments") or [] if c.get("text")]
        elif plat == "snapchat":
            e["source"] = "snapchat profile page (top comments only, about 25 per Spotlight)"
            e["comments"] = snap_inline.get(p["id"], [])
        elif plat == "tiktok" and slow:
            e["source"] = f"tiktok comments didn't load: skipped, TikTok asked to slow down on an earlier post ({slow})"
        elif plat == "tiktok":
            try:
                e["comments"], total, stopped = tiktok_comments(p, max_comments)
            except Exception as err:
                e["source"] = f"tiktok comments didn't load: {err}"
                if "HTTP 429" in str(err):
                    slow = "HTTP 429"
            else:
                e["source"] = ("tiktok web comment list (unofficial, logged out; top-level comments in TikTok's own order"
                               + (f", {len(e['comments'])} of {total:,} counting replies" if total else "")
                               + (f"; {stopped}" if stopped else "") + ")")
                if stopped and "HTTP 429" in stopped:
                    slow = "HTTP 429"
        else:
            e["source"] = "instagram posts scrape (newest comments only)"
            e["comments"] = [dict(c, text=mask_mentions(c.get("text"))) if isinstance(c, dict) else c
                             for c in inline.get(p["id"], [])]
        result.append(e)
    result += kept_tk  # an earlier --tiktok-comments read, kept when this run leaves TikTok out
    save(os.path.join(out, "comments.json"), result)
    n = sum(len(e["comments"]) for e in result)
    tk = [e for e in result if e["platform"] == "tiktok" and e not in kept_tk]
    tk_bad = [e for e in tk if (e.get("source") or "").startswith("tiktok comments didn't load")]
    tk_cut = [e for e in tk if "; stopped after " in (e.get("source") or "")]
    skipped = not tiktok and any(t[0] == "tiktok" for t in every)
    print(f"  comments: {n} comments across {len(result)} posts -> comments.json"
          + (f" (TikTok comments are off: scan --tiktok-comments or comments --tiktok turns them on, through an "
             "unofficial endpoint; see references/research-methods.md"
             + (f"; the {len(kept_tk)} TikTok posts read earlier are kept" if kept_tk else "") + ")" if skipped else "")
          + (f"; TikTok: {len(tk) - len(tk_bad)} of {len(tk)} posts loaded" if tk else "")
          + (f", {len(tk_cut)} cut short" if tk_cut else ""), file=sys.stderr)


def load_posts(out, since=None):
    all_posts, metas = [], {}
    for plat in PLATFORMS:
        all_posts += load(os.path.join(out, f"posts_{plat}.json"), [])
        m = load(os.path.join(out, f"meta_{plat}.json"))
        if m:
            metas[plat] = m
    undated_dropped = 0
    if since:
        undated_dropped = sum(1 for p in all_posts if not p.get("date"))
        all_posts = [p for p in all_posts if p.get("date") and p["date"] >= since]
    return all_posts, metas, undated_dropped


def build_groups(all_posts, metas):
    """Platform x format groups, each with its own median baseline and per-post lift."""
    groups = []
    for plat in ("instagram", "tiktok", "youtube", "snapchat"):
        ps = [p for p in all_posts if p["platform"] == plat]
        if not ps:
            continue
        # followers per 1K views is per platform (all its posts in the window),
        # shown on the platform's first row only
        # Snapchat lists a few dozen Spotlights at most, with some views hidden: too thin for a per-1K figure
        fp1k = "n/a" if plat == "snapchat" else followers_per_1k(ps, metas.get(plat))
        if plat == "youtube":
            subs = [(label, [p for p in ps if p["kind"] == kind])
                    for kind, label in (("short", "youtube shorts"), ("long", "youtube long-form"))]
        elif plat == "instagram":
            subs = [("instagram reels", [p for p in ps if p["kind"] == "reel"]),
                    ("instagram photos/carousels", [p for p in ps if p["kind"] != "reel"])]
        elif plat == "tiktok":
            subs = [("tiktok videos", [p for p in ps if p["kind"] != "photo"]),
                    ("tiktok photos", [p for p in ps if p["kind"] == "photo"])]
        elif plat == "snapchat":
            subs = [("snapchat spotlight", ps)]
        else:
            subs = [(plat, ps)]
        first = True
        for label, sub in subs:
            if not sub:
                continue
            g = group_stats(label, sub, None)
            for p in sub:
                p["group"] = label
            g["followers_per_1k"] = fp1k if first else "n/a"
            if plat == "tiktok":  # one feed: a stretch of carousels isn't a gap in the videos
                g.pop("gap", None)
                if first:
                    whole = sorted(p["date"] for p in ps if p.get("date") and not p.get("pinned"))
                    g["gap"] = largest_gap(whole) if len(whole) > 1 else None
            first = False
            groups.append((plat, g))
    return groups


def flags(p):
    f = [x for x, on in (("approx", p.get("date") and p.get("date_approx")), ("pinned", p.get("pinned")),
                         ("fresh", is_fresh(p)), ("views hidden", p.get("views") is None and p.get("views_hidden")),
                         (f"{p.get('slides')} slides", p.get("kind") == "photo" and p.get("slides"))) if on]
    return f" ({', '.join(f)})" if f else ""


def per_week_txt(s):
    pw = s.get("per_week", "n/a")
    return f"{pw} ({s['cadence_n']} exact)" if s["dates_approx"] and s.get("cadence_n") and pw != "n/a" else pw


def window_txt(s):
    def one(key):
        return f"{s[key]} (approx)" if s.get(f"{key}_approx") else s.get(key, "?")
    return f"{one('first')} to {one('last')}"


def approx_note(s):
    """One line for a group whose window holds approximate dates, or None."""
    n = s["dates_approx"]
    if not n:
        return None
    line = (f"**Approximate dates:** {n} of {s['posts']} posts have only a \"3 months ago\"-style date from YouTube (marked approx), "
            "so they bunch onto a few days and the real upload can be months earlier. ")
    if s.get("cadence_n"):
        return line + (f"Per week and the gap check use only the {s['cadence_n']} exact dates, from {s['cadence_from']} on; "
                       "raise --yt-enrich to date more.")
    return line + "No post here has an exact date, so per week and the gap check are left out; raise --yt-enrich to date them."


def lift_txt(p):
    if p.get("lift") is None:
        return "n/a"
    return f"{p['lift']:.1f}x" if p["lift"] >= 0.95 else f"{p['lift']:.2f}x"


def spotlight_checks(posts, meta):
    """Snapchat-specific checks under the Spotlight group: what can earn, how it went up, what Snap reads."""
    n = len(posts)
    if not n:
        return []
    vids = [p for p in posts if p.get("kind") != "photo"]
    photos = n - len(vids)
    # test the raw milliseconds: 29,970 ms rounds to 30.0 s but can't earn
    timed = [p for p in vids if p.get("duration_ms") or p.get("duration_s")]
    ms = lambda p: p.get("duration_ms") or round((p.get("duration_s") or 0) * 1000)
    short = sorted(ms(p) for p in timed if ms(p) < SNAP_EARN_S * 1000)
    secs = lambda v: f"{v / 1000:.2f}".rstrip("0").rstrip(".") + " s"
    L = [f"**Spotlight checks** (Snapchat pays only on video Spotlights of {SNAP_EARN_S} s or more: Snap help article "
         "14669003687444, checked 2026-10-16)",
         f"- {SNAP_EARN_S} s or longer: {len(timed) - len(short)} of {len(timed)} videos"
         + (f" ({len(short)} can't earn: {', '.join(secs(v) for v in short)})" if short else ".")]
    if photos:
        L.append(f"- Photo Spotlights: {photos} (no length, can't earn).")
    buckets = [("under 30 s", 0, 30), ("30-59 s", 30, 60), ("60-179 s", 60, 180), ("180 s+", 180, 10 ** 6)]
    parts = []
    for label, lo, hi in buckets:
        b = [p for p in timed if lo * 1000 <= ms(p) < hi * 1000]
        if b:
            v = [p["views"] for p in b if p.get("views") is not None]
            nh = sum(1 for p in b if p.get("views") is None and p.get("views_hidden"))
            bmed, bunder = censored_median(v, nh)
            txt = ""
            if len(b) >= 3 and (bmed is not None or bunder is not None):  # two posts aren't a comparison
                txt = (f" (median {fmt(bmed)} views" if bmed is not None else f" (median under {fmt(bunder)} views") \
                    + (f", {nh} of {len(b)} hidden)" if nh else ")")
            parts.append(f"{label} {len(b)}{txt}")
    if parts:
        L.append("- Length: " + "; ".join(parts) + ".")
    stamps = sorted(dt.datetime.fromisoformat(p["posted_at"]) for p in posts if p.get("posted_at"))
    burst = sum(1 for i, t in enumerate(stamps)
                if (i and (t - stamps[i - 1]).total_seconds() <= 120)
                or (i + 1 < len(stamps) and (stamps[i + 1] - t).total_seconds() <= 120))
    if burst:
        L.append(f"- Upload bursts: {burst} of {n} went up within 2 minutes of another. Spread them out, and check "
                 "nobody else uploads the same clips: Snapchat drops repeated content from recommendations.")
    L.append(f"- Descriptions on {sum(1 for p in posts if p.get('has_description'))} of {n}; hashtags on "
             f"{sum(1 for p in posts if p.get('hashtags'))} of {n} (Snap uses both to place a Spotlight).")
    if "public_story_snaps" in meta:
        L.append(f"- Public Story: {meta['public_story_snaps']} snaps live now; Saved Stories: "
                 f"{meta.get('saved_stories', 0)}.")
    if meta.get("more_exist"):
        L.append("- The profile has more Spotlights than the page lists; older ones aren't in this scan.")
    read = [p for p in sorted(posts, key=lambda p: -(p.get("likes") or 0)) if p.get("snap_ai_title")][:5]
    if read:
        L += ["", "**Snap's read of the most-liked Spotlights** (Snap's SEO pass writes the title and keywords; a "
              "separate classifier writes the content tags, and the two often disagree. Whether either steers "
              "recommendations isn't public.)",
              "| Spotlight | Likes | Snap's title | Snap's keywords | Content tags |", "|---|---|---|---|---|"]
        for p in read:
            L.append(f"| [{p.get('date') or '?'}]({p['url']}) | {fmt(p.get('likes'))} | {cell(p['snap_ai_title'])} | "
                     f"{cell(', '.join((p.get('snap_seo_keywords') or [])[:5]))} | "
                     f"{cell(', '.join((p.get('snap_content_tags') or p.get('snap_keywords') or [])[:5]))} |")
    return L


# ---------- collect windows (--days) ----------

DAYS_CAP = 300  # posts per tab when collecting by --days


def window_cutoff(days):
    """The first day a --days window keeps: --days 30 is the 30 calendar days ending today."""
    return (TODAY - dt.timedelta(days=days - 1)).isoformat()


def keep_window(out, plat, days, reached=None, capped=False, undated=0, newest=None, reason=None):
    """Trim a platform's collected posts to the last `days` days and record in
    window.json what the window covers. reached: the listing went back past
    the cutoff (None: judge it from the posts' own dates). undated: posts in
    range left out because they couldn't be dated."""
    path = os.path.join(out, f"posts_{plat}.json")
    posts = load(path, []) or []
    cutoff = window_cutoff(days)
    dates = [p["date"] for p in posts if p.get("date") and not p.get("pinned")]
    if reached is None:
        reached = bool(dates and min(dates) < cutoff)
    kept = [p for p in posts if not p.get("pinned") and (not p.get("date") or p["date"] >= cutoff)]
    if kept:  # pinned posts ride along only with a window that has posts of its own
        kept = [p for p in posts if p.get("pinned")] + kept
    save(path, kept)
    w = load(os.path.join(out, "window.json")) or {}
    w[plat] = {"days": days, "cutoff": cutoff, "reached": bool(reached), "capped": bool(capped),
               "from": min((p["date"] for p in kept if p.get("date") and not p.get("pinned")), default=None),
               "newest": max(dates + ([newest] if newest else []), default=None), "posts": len(kept),
               "left_out": len(posts) - len(kept), "undated": undated, "reason": reason}
    save(os.path.join(out, "window.json"), w)


def window_after(out, plat, days):
    """--days for a platform with no paging (Snapchat) or a paid, count-capped scrape (Instagram): trim to the window."""
    if not days:
        forget_window(out, plat)
        return
    reached = None
    if plat == "snapchat" and not (load(os.path.join(out, "meta_snapchat.json")) or {}).get("more_exist", True):
        reached = True  # the profile listed every Spotlight
    keep_window(out, plat, days, reached=reached)


def collect_instagram_window(handle, out, limit, days):
    t0 = time.time()
    try:
        collect_instagram(handle, out, limit)
    finally:
        pi = os.path.join(out, "posts_instagram.json")
        if os.path.exists(pi) and os.path.getmtime(pi) >= t0:
            window_after(out, "instagram", days)


def forget_window(out, plat):
    """A collect by post count replaces any earlier window for that platform."""
    path = os.path.join(out, "window.json")
    w = load(path) or {}
    if plat in w:
        w.pop(plat)
        save(path, w)


def empty_window(v):
    """A window with no posts because the account posted nothing in it (not a
    blocked listing, and not posts YouTube refused to date)."""
    return not v.get("posts") and not v.get("undated") and v.get("newest") is not None


def window_lines(out, platforms):
    """The report's note on a collect by --days, and where a platform falls short of it."""
    w = load(os.path.join(out, "window.json")) or {}
    if not w:
        return []
    ds = sorted({v["days"] for v in w.values()})
    L = [f"Collected by date: the last {' / '.join(str(d) for d in ds)} day{'' if ds == [1] else 's'} "
         f"(--days), from {min(v['cutoff'] for v in w.values())}."
         + ("" if set(platforms) <= set(w) else f" Only {', '.join(sorted(w))} came that way; "
            f"{', '.join(sorted(set(platforms) - set(w)))} came by post count.")]
    empty = [f"{k} (its newest post is from {v['newest']})" for k, v in sorted(w.items()) if empty_window(v)]
    if empty:
        L.append("**No posts in the window:** " + "; ".join(empty) + ".")
    short = [f"{k} reaches back only to {v.get('from') or '?'} ("
             + (v["reason"] if v.get("reason") else "it hit the {}-post cap".format(DAYS_CAP) if v.get("capped") else
                "Snapchat lists only its newest Spotlights logged out" if k == "snapchat" else
                "Instagram's paid scrape stops at --limit" if k == "instagram" else
                "YouTube stopped answering the per-video lookups that date its posts" if k == "youtube" else
                "the listing ends there") + ")"
             for k, v in sorted(w.items()) if v.get("posts") and not v.get("reached")]
    short += [f"{k}: {v['undated']} post{'s' if v['undated'] != 1 else ''} in range couldn't be dated (YouTube refused "
              "their pages) and are left out" for k, v in sorted(w.items()) if v.get("undated")]
    if short:
        L.append("**Short window:** " + "; ".join(short) + ". Those numbers cover less than the window.")
    return L


def group_days(window, g, posts=None):
    """Days a group's posts cover: the --days window when its platform reached
    back that far, else the first-to-last span of its dated posts. None when
    undated posts make the span unknowable (Shorts past --yt-enrich)."""
    plat = g["group"].split()[0]
    w = (window or {}).get(plat)
    if w and w.get("reached") and not w.get("undated"):
        return w["days"]
    if posts is not None and any(not p.get("date") and not p.get("pinned") for p in posts
                                 if p.get("group") == g["group"]):
        return None
    try:
        return (dt.date.fromisoformat(g["last"]) - dt.date.fromisoformat(g["first"])).days + 1
    except (KeyError, TypeError, ValueError):
        return None


def window_cadence(groups, window, since=None):
    """In a --days window that reached back, per week is the posts over the
    window itself (or over a --since range inside it)."""
    for plat, g in groups:
        w = (window or {}).get(plat)
        if w and w.get("reached") and w.get("days"):
            exact = g.get("cadence_n") if g.get("cadence_n") is not None else g.get("posts")
            days, start = w["days"], w.get("cutoff")
            if since and start and since > start:
                days, start = (TODAY - dt.date.fromisoformat(since)).days + 1, since
            g["per_week"] = round(exact / (days / 7), 1)
            g["cadence_from"] = start
    return groups


# ---------- compare ----------

def compare(dirs):
    """One table across accounts (one scan folder each): window, cadence,
    median, outlier rate, top-3 share and followers per 1K per group, each
    folder measured as of the day it was collected. Medians and followers per
    1K are left out where the windows differ too much to compare."""
    states, notes = [], []
    for d in dirs:
        if not load_posts(d)[0]:
            notes.append(f"{d}: no collected posts, left out")
            continue
        st = scan_state(d)
        st["dir"] = d
        base = os.path.basename(os.path.abspath(d))
        st["label"] = st["name"] or (f"{os.path.basename(os.path.dirname(os.path.abspath(d)))}/{base}"
                                     if DATED_DIR.match(base) else base)
        states.append(st)
    if not states:
        raise ValueError("none of those folders holds collected posts")
    seen = {}
    for st in states:  # two scans of one account: tell them apart by folder
        seen[st["label"]] = seen.get(st["label"], 0) + 1
    for st in states:
        if seen[st["label"]] > 1:
            st["label"] += f" ({os.path.basename(os.path.abspath(st['dir']))})"

    days = lambda st, g: group_days(st["window"], g, st["posts"])
    L = ["# Compare: " + ", ".join(st["label"] for st in states), "",
         "| Account | Followers | Collected | Window |", "|---|---|---|---|"]
    for st in states:
        foll = ", ".join(f"{k} {fmt(v.get('followers'))}" for k, v in sorted((st["accounts"] or {}).items())
                         if v.get("followers") is not None) or "n/a"
        ws = sorted({v["days"] for v in st["window"].values()})
        L.append(f"| {st['label']} | {foll} | {st['pulled'] or '?'} | "
                 + (f"last {'/'.join(map(str, ws))} days" if ws else "by post count") + " |")
    pulled = sorted(st["pulled"] for st in states if st["pulled"])
    if len(pulled) > 1 and (dt.date.fromisoformat(pulled[-1]) - dt.date.fromisoformat(pulled[0])).days > 2:
        L += ["", f"**Collected on different days** ({pulled[0]} to {pulled[-1]}): the same --days covers different "
                  "dates, and older scans have had longer to gather views. Scan them within a day or two of each other "
                  "for a fair table."]
    groups = sorted({g["group"] for st in states for g in st["groups"]})
    # per group, the window most accounts share (two or more); a row more than 1.5x off it, or any row when no
    # two accounts share a window, can't compare medians
    near = lambda a, b: max(a, b) / min(a, b) <= 1.5
    ref = {}
    for gr in groups:
        ds = [days(st, g) for st in states for g in st["groups"] if g["group"] == gr]
        if len(ds) < 2:
            continue
        known = [x for x in ds if x]
        best = max(known, key=lambda d: (sum(near(d, x) for x in known), -d), default=None)
        ref[gr] = best if best and sum(near(best, x) for x in known) >= 2 else None
    off = lambda gr, w: gr in ref and (ref[gr] is None or not w or not near(w, ref[gr]))
    def clean(st, plat):  # a --days window that reached back with nothing left undated, else None
        w = st["window"].get(plat)
        return w["days"] if w and w.get("reached") and not w.get("undated") else None

    fp_days = {}
    for plat in {g.split()[0] for g in groups}:
        have = [st for st in states if any(x["group"].split()[0] == plat for x in st["groups"])]
        ds = [clean(st, plat) for st in have if clean(st, plat)]
        if len(have) == 1:  # one account alone: nothing to set it beside
            fp_days[plat] = "alone"
        else:
            top = max(set(ds), key=ds.count) if ds else None
            fp_days[plat] = top if top and ds.count(top) >= 2 else None
    L += ["", "| Group | Account | Window (days) | Posts | Per week | Median views | Outlier rate (2x+) | Top 3 share | "
              "Followers per 1K | Confidence (n) |", "|---|---|---|---|---|---|---|---|---|---|"]
    uneven, short_rows, fresh_rows = set(), [], []
    for gr in groups:
        plat = gr.split()[0]
        for st in states:
            g = next((x for x in st["groups"] if x["group"] == gr), None)
            if not g:
                has = any(x["group"].split()[0] == plat for x in st["groups"])
                win = st["window"].get(plat) or {}
                why = ("none in this scan" if has else
                       f"no posts in the last {win['days']} days" + (f" (newest {win['newest']})" if win.get("newest") else "")
                       if win else f"{plat} not collected")
                L.append(f"| {gr} | {st['label']} | – | 0 | – | – | – | – | – | {why} |")
                continue
            w = days(st, g)
            hide = off(gr, w)
            # followers per 1K covers the whole platform and scales with the window's length, so it shows only
            # where this platform's window is a clean --days of the length the other accounts' clean windows share
            plat_off = fp_days.get(plat) is None or (fp_days[plat] != "alone" and clean(st, plat) != fp_days[plat])
            if hide:
                uneven.add(gr)
            med = "–" if hide else (f"under {fmt(g['median_under'])}" if g.get("median_under") is not None
                                    else fmt(g.get("median")))
            fp = "–" if plat_off else (g.get("followers_per_1k") or "n/a")
            t = g.get("tiers")
            tn = g.get("tier_n", g.get("baseline_n"))
            hit = (t.get("huge", 0) + t.get("strong", 0)) / tn if isinstance(t, dict) and tn and tn >= 5 else None
            t3 = ("≤" if g.get("top3_upper") and g.get("top3_share") is not None else "") + pct(g.get("top3_share"))
            win = st["window"].get(plat) or {}
            short = bool(win) and (not win.get("reached") or win.get("undated"))
            if short:
                short_rows.append(f"{st['label']} {gr} (" + ("didn't reach back the whole --days" if not win.get("reached")
                                                          else f"{win['undated']} in range couldn't be dated") + ")")
            if g.get("all_fresh"):
                fresh_rows.append(f"{st['label']} {gr}")
            L.append(f"| {gr} | {st['label']} | {w or '?'}{' (short)' if short else ''} | {g['posts']} | "
                     f"{g.get('per_week', 'n/a')} | {med} | {'n/a' if hit is None else f'{hit * 100:.0f}%'} | {t3} | "
                     f"{fp} | {g['confidence']} ({g['baseline_n']}){', fresh' if g.get('all_fresh') else ''} |")
    if uneven:
        L += ["", f"**Windows differ** in {', '.join(sorted(uneven))}: where an account's window is more than 1.5 times "
                  "off the one the others share (or no two share one), its median views are left out (–), since median "
                  "views move with how many days a window covers. Scan every account with the same --days to compare "
                  "them; cadence and outlier rate are each account's own and still compare."]
    if short_rows:
        L += ["", "**Short window:** " + "; ".join(short_rows) + ": these rows cover less than the window (the scan's "
                  "report says why)."]
    if fresh_rows:
        L += ["", "**Fresh:** " + "; ".join(fresh_rows) + ": every post is under 3 days old, so the median and outlier "
                  "rate use unsettled posts and will still move. Don't set them beside settled rows."]
    best = []
    for st in states:
        scored = [(g["tiers"].get("huge", 0) + g["tiers"].get("strong", 0), g["group"]) for g in st["groups"]
                  if isinstance(g.get("tiers"), dict)]
        if scored and max(scored)[0]:
            n = max(scored)[0]
            best.append(f"{st['label']}: {max(scored)[1]} ({n} post{'' if n == 1 else 's'} at 2x+)")
    if best:
        L += ["", "**Best format** (most posts at 2x+ their own median): " + "; ".join(best) + "."]
    L += ["", "Outlier rate is the share of each group's settled posts at 2x+ its own median (n/a under 5), so it "
              "compares across accounts of any size; raw views don't. Top 3 share depends on how many posts the window "
              "holds, so compare it only between rows with similar post counts. Followers per 1K scales with the window's "
              "length, so it's shown only where accounts were scanned with the same --days and reached back the whole "
              "window."]
    if notes:
        L += ["", "Left out: " + "; ".join(notes) + "."]
    return "\n".join(L) + "\n"


# ---------- run history ----------

# a scan folder is named for its date, with an optional short run marker:
# 2026-10-16, 2026-10-16-2, 2026-10-16b. Anything longer ("2026-10-16-duolingo")
# is another layout, where siblings can be other accounts.
DATED_DIR = re.compile(r"\d{4}-\d{2}-\d{2}(?:[-_.]?(?:\d{1,2}|[a-z]))?$")
SNAP_HISTORY = "posts_snapchat_history.json"


def run_key(name):
    """Sort key for a dated folder: its date, then its run marker as a number
    or a letter, so 2026-10-16-10 comes after 2026-10-16-9."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[-_.]?(.*)$", name)
    t = m.group(2) if m else ""
    return (m.group(1) if m else name, (1, int(t), "") if t.isdigit() else (2, 0, t) if t else (0, 0, ""))


def older_scans(out, need=("summary.json",)):
    """Earlier dated siblings of a dated --out folder (the documented
    <account>/<YYYY-MM-DD>/ layout) holding one of `need`, newest first."""
    here = os.path.abspath(out)
    name = os.path.basename(here)
    if not DATED_DIR.match(name):
        return []
    parent = os.path.dirname(here)
    try:
        sibs = os.listdir(parent)
    except OSError:
        return []
    older = sorted((s for s in sibs if DATED_DIR.match(s) and run_key(s) < run_key(name)
                    and any(os.path.isfile(os.path.join(parent, s, f)) for f in need)), key=run_key, reverse=True)
    return [os.path.join(parent, s) for s in older]


def prior_scan(out, need=("summary.json",)):
    """The newest earlier dated sibling holding one of `need`; None otherwise."""
    older = older_scans(out, need)
    return older[0] if older else None


def later_than(a, b):
    """True when collect time a is after b (both ISO strings; dates compare as days)."""
    try:
        if len(a) > 10 and len(b) > 10:
            return dt.datetime.fromisoformat(a) > dt.datetime.fromisoformat(b)
    except (TypeError, ValueError):
        pass
    return str(a)[:10] > str(b)[:10]


def group_of(p):
    """The report group a post falls in, for posts an earlier summary.json doesn't list."""
    k, plat = p.get("kind"), p.get("platform")
    return {"youtube": "youtube shorts" if k == "short" else "youtube long-form",
            "instagram": "instagram reels" if k == "reel" else "instagram photos/carousels",
            "tiktok": "tiktok photos" if k == "photo" else "tiktok videos",
            "snapchat": "snapchat spotlight"}.get(plat, plat)


@contextlib.contextmanager
def as_of(day):
    """Measure freshness and cadence to `day` (a folder's collect day) instead of
    the date this runs, and put the real date back afterwards."""
    global TODAY
    saved = TODAY
    try:
        TODAY = dt.date.fromisoformat(str(day)[:10]) if day else saved
    except ValueError:
        pass
    try:
        yield
    finally:
        TODAY = saved


def collected_on(folder, metas=None, pod=None):
    """(date, timestamp) a folder's data was collected, not when its report was
    last written: the collect stamps, else the follower records and the podcast
    pull, else a report from before the stamps (which re-renders restamped)."""
    stamps = [v for v in (load(os.path.join(folder, "collected.json")) or {}).values() if isinstance(v, str) and v]
    if stamps:
        return max(stamps)[:10], max(stamps)
    if metas is None:
        metas = load_posts(folder)[1]
    pod = pod if pod is not None else load(os.path.join(folder, "podcast.json")) or {}
    days = [str(x)[:10] for x in [m.get("pulled") for m in metas.values()] + [pod.get("pulled")] if x]
    if days:
        return max(days), None
    so = load(os.path.join(folder, "summary.json")) or {}
    return so.get("pulled"), so.get("pulled_at")


def scan_state(folder):
    """A scan folder as its report read on the day it was collected: rebuilt
    from its own posts files, with freshness and cadence measured to that day,
    so a later re-render of that folder (a --since window, a restamped date)
    can't change what "then" means."""
    so = load(os.path.join(folder, "summary.json")) or {}
    raw, metas, _ = load_posts(folder)
    pod = load(os.path.join(folder, "podcast.json")) or so.get("podcast")
    day, at = collected_on(folder, metas, pod or {})
    posts = [dict(p) for p in raw]
    window = load(os.path.join(folder, "window.json")) or {}
    with as_of(day):
        groups = [g for _, g in window_cadence(build_groups(posts, metas), window)]
    return {"pulled": day, "pulled_at": at, "name": so.get("name"), "accounts": metas, "groups": groups,
            "posts": posts, "podcast": pod, "window": window}


def step(p):
    """The rounding step of a shown view count (0 when exact): TikTok shows 1M+
    in 100K steps and 10K+ in 100s; YouTube counts outside --yt-enrich (no
    likes) are rounded to about two figures."""
    v = p.get("views")
    if not v:
        return 0
    if p.get("platform") == "tiktok":
        return 100_000 if v >= 1_000_000 and v % 100_000 == 0 else 100 if v >= 10_000 and v % 100 == 0 else 0
    if p.get("platform") == "youtube" and p.get("likes") is None and v >= 1000:
        return 10 ** (len(str(int(v))) - 2)
    return 0


def since_last(prior, now, name, out):
    """The "Since last scan" section: what changed between an earlier scan of
    the account and this one. Raises ValueError when `prior` isn't earlier."""
    then = scan_state(prior)
    if not then["pulled"] and not then["posts"] and not then.get("podcast"):
        return []
    try:
        days = (dt.date.fromisoformat(now["pulled"]) - dt.date.fromisoformat(then["pulled"])).days
    except (TypeError, ValueError):
        days = None
    if days is not None and (days < 0 or (days == 0 and then.get("pulled_at") and now.get("pulled_at")
                                         and later_than(then["pulled_at"], now["pulled_at"]))):
        raise ValueError(f"{os.path.basename(os.path.abspath(prior))} was collected after this folder "
                         f"({then['pulled']}, this one {now['pulled']}), so it isn't an earlier scan")
    rel = os.path.relpath(os.path.join(prior, "summary.md"), out).replace(os.sep, "/")
    L = ["## Since last scan",
         f"Compared with [{os.path.basename(os.path.abspath(prior))}]({f'<{rel}>' if ' ' in rel else rel})"
         + (f", pulled {then['pulled']}" + (f" at {then['pulled_at'][11:16]}" if days == 0 and then.get("pulled_at") else "")
            + (" (earlier the same day)" if days == 0 else f" ({days} day{'' if days == 1 else 's'} earlier)")
            if days is not None else "") + "."]
    if then.get("name") and name and norm_name(then["name"]) != norm_name(name):
        L.append(f"The earlier report is titled \"{then['name']}\": check it's the same account.")

    rows = []
    for plat in PLATFORMS:
        a = ((then["accounts"] or {}).get(plat) or {}).get("followers")
        b = ((now["accounts"] or {}).get(plat) or {}).get("followers")
        if a is None and b is None:
            continue
        ch = f"{b - a:+,} ({(b - a) / a * 100:+.1f}%)" if a and b is not None else "n/a"
        rows.append((plat, f"| {plat} | {fmt(a)} | {fmt(b)} | {ch} |"))
    if rows:
        L += ["", "| Platform | Followers then | Followers now | Change |", "|---|---|---|---|"] + [r for _, r in rows]
        if any(plat in ("youtube", "snapchat") for plat, _ in rows):
            L += ["", "YouTube and Snapchat show rounded follower counts, so a change there can read as 0 or as a "
                      "whole rounding step."]

    pt, pn = then.get("podcast") or {}, now.get("podcast") or {}
    if pt and pn and str(pt.get("apple_id")) == str(pn.get("apple_id")):
        bits = []
        if pt.get("ratings") is not None and pn.get("ratings") is not None:
            bits.append(f"US ratings {pt['ratings']:,} → {pn['ratings']:,} ({pn['ratings'] - pt['ratings']:+,})"
                        + (f", rated {pt['rating']} → {pn['rating']}" if pt.get("rating") and pn.get("rating") else ""))
        ft, fn = pt.get("feed") or {}, pn.get("feed") or {}
        if ft.get("per_week") is not None and fn.get("per_week") is not None:
            bits.append(f"episodes a week {ft['per_week']} → {fn['per_week']}")
        old_c = {(c.get("source"), c.get("chart")): c for c in pt.get("charts") or [] if isinstance(c, dict)}
        rank = lambda c: ("n/a" if not c else f"#{c['rank']}" if c.get("rank") else
                          f"not in the top {c['size']}" if c.get("size") else "couldn't read")
        crow = [f"| {c.get('source')} {c.get('chart')} | {rank(old_c.get((c.get('source'), c.get('chart'))))} | {rank(c)} |"
                for c in pn.get("charts") or [] if isinstance(c, dict)]
        L += ["", f"**Podcast** ({pn.get('name')}): " + ("; ".join(bits) if bits else "no comparable totals") + "."]
        if crow:
            L += ["", "| Chart | Then | Now |", "|---|---|---|"] + crow

    def med(s):
        if not s:
            return "n/a"
        return f"under {fmt(s['median_under'])}" if s.get("median_under") is not None else fmt(s.get("median"))

    def x2(s):
        t = s.get("tiers") if s else None
        return (t.get("huge", 0) + t.get("strong", 0)) if isinstance(t, dict) else "n/a"

    def t3(s):
        if not s:
            return "n/a"
        return ("≤" if s.get("top3_upper") and s.get("top3_share") is not None else "") + pct(s.get("top3_share"))

    now_win = now.get("window") or {}

    tg = {g.get("group"): g for g in then["groups"]}
    ng = [g for g in now["groups"] if isinstance(g, dict)]
    if ng:
        L += ["", "| Group | Posts then | now | Median then | now | Per week then | now | 2x+ then | now | Top 3 share then | now |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        uneven = []
        for g in ng:
            o = tg.get(g["group"], {})
            L.append(f"| {g['group']} | {o.get('posts', 'n/a')} | {g.get('posts')} | {med(o)} | {med(g)} | "
                     f"{o.get('per_week', 'n/a') if o else 'n/a'} | {g.get('per_week', 'n/a')} | {x2(o)} | {x2(g)} | "
                     f"{t3(o)} | {t3(g)} |")
            a, b = group_days(then.get("window"), o, then["posts"]) if o else None, group_days(now_win, g, now["posts"])
            part = a is None or b is None  # undated posts (Shorts past --yt-enrich): fall back to the dated span
            dspan = lambda x: group_days(None, x) if x else None
            a, b = (a or dspan(o)), (b or dspan(g))
            dd = lambda n: f"{n} day{'' if n == 1 else 's'}"
            if a and b and max(a, b) / min(a, b) > 1.5:
                uneven.append((g["group"], f"{g['group']} covered {dd(a)} with {o.get('posts')} posts then, "
                                           f"{dd(b)} with {g.get('posts')} now"
                                           + (" (dated posts only: some had no date)" if part else "")))
            elif o and part and not (a and b):
                uneven.append((g["group"], f"{g['group']}: its window is unknown in one scan (posts with no date)"))
        if uneven:
            snap = any(k.startswith("snapchat") for k, _ in uneven)
            other = any(not k.startswith("snapchat") for k, _ in uneven)
            L += ["", "**Different windows:** " + "; ".join(t for _, t in uneven) + ". A window more than 1.5 times "
                  "longer in one scan means part of these changes is the window, not the content: quote those rows "
                  "with that caveat" + (", or scan both with the same --days" if other else "")
                  + (". Snapchat lists only its newest Spotlights logged out, so compare Snapchat scans taken at a "
                     "similar interval" if snap else "") + "."]
        dropped = [k for k in tg if k not in {g["group"] for g in ng}]
        if dropped:
            L += ["", f"In the earlier scan only: {', '.join(dropped)}."]

    # posts are compared only for platforms collected in both scans
    plats_then = {p.get("platform") for p in then["posts"]}
    plats_now = {p.get("platform") for p in now["posts"]}
    shared = plats_then & plats_now
    one_sided = [f"{plat} (in the earlier scan only: {sum(1 for p in then['posts'] if p.get('platform') == plat)} "
                 "posts, not collected this run)" for plat in sorted(plats_then - plats_now)]
    one_sided += [f"{plat} (new in this scan)" for plat in sorted(plats_now - plats_then)]
    if one_sided:
        L += ["", "**Not compared:** " + "; ".join(one_sided) + "."]
    key = lambda p: (p.get("platform"), p.get("id"))
    old = {key(p): p for p in then["posts"] if p.get("platform") in shared}
    cur = {key(p): p for p in now["posts"] if p.get("platform") in shared}
    if old or cur:
        new = [p for k, p in cur.items() if k not in old]
        both = [(old[k], p) for k, p in cur.items() if k in old]
        gone = [p for k, p in old.items() if k not in cur]
        fresh = [p for p in new if p.get("date") and then["pulled"] and p["date"] >= then["pulled"]]
        undated = [p for p in new if not p.get("date")]
        back = len(new) - len(fresh) - len(undated)
        L += ["", f"**Posts:** {len(fresh)} posted since the last scan"
              + (f", {back} older post{'s' if back != 1 else ''} newly in the sample" if back else "")
              + (f", {len(undated)} new to the sample with no date (YouTube Shorts past --yt-enrich)" if undated else "")
              + f", {len(both)} in both, {len(gone)} only in the earlier scan (aged out of the sample, or deleted: "
              "check before saying deleted)."]
        if fresh:
            top = sorted(fresh, key=lambda p: (p.get("views") if p.get("views") is not None else -1, p.get("likes") or 0),
                         reverse=True)[:10]
            L += ["", "**New posts**" + (f" (top 10 of {len(fresh)})" if len(fresh) > 10 else ""),
                  "| Group | Posted | Views | Lift | Opening line |", "|---|---|---|---|---|"]
            for p in top:
                v = f"[{fmt(p.get('views'))}]({p['url']})" if p.get("url") else fmt(p.get("views"))
                L.append(f"| {p.get('group') or group_of(p)} | {p.get('date') or '?'}{flags(p)} | {v} | {lift_txt(p)} | "
                         f"{cell(p.get('caption')) or '_(no caption)_'} |")
        grew = sorted(((max(step(a), step(b)), b["views"] - a["views"], a, b) for a, b in both
                       if a.get("views") is not None and b.get("views") is not None and b["views"] > a["views"]),
                      key=lambda t: -(t[1] - t[0]))  # ranked by what the gain is at least, so a rounding step can't lead
        grew = [(st and 10 * st > d, d, a, b) for st, d, a, b in grew]
        if grew:
            L += ["", "**Still climbing** (most views gained between the scans)",
                  "| Group | Posted | Views then | Views now | Gained |", "|---|---|---|---|---|"]
            for approx, d, a, b in grew[:8]:
                v = f"[{fmt(b['views'])}]({b['url']})" if b.get("url") else fmt(b["views"])
                L.append(f"| {b.get('group') or group_of(b)} | {b.get('date') or '?'} | {fmt(a['views'])} | {v} | "
                         f"{'~' if approx else ''}+{fmt(d)} ({d / max(a['views'], 1) * 100:+.0f}%) |")
            if any(t[0] for t in grew[:8]):
                L += ["", "~ marks a gain on a rounded count where one rounding step is a tenth of the gain or more "
                          "(TikTok shows views in 100s from 10K and in 100Ks from 1M; YouTube rounds counts outside "
                          "--yt-enrich): read it as approximate, give or take one step."]
        # a fixed yardstick: the earlier scan's own median, so a falling median can't make a flat post an outlier
        crossed, unmeasured = [], {}
        for a, b in both:
            g = b.get("group") or group_of(b)
            o = tg.get(g) or {}
            m0 = o.get("median") if o.get("median_under") is None else None
            if not m0 or not isinstance(o.get("tiers"), dict):
                unmeasured[g] = ("not a group in the earlier scan" if not o else
                                 "too many hidden view counts then" if o.get("median_under") is not None else
                                 "no view counts then" if not o.get("with_views") else "too few posts with views then")
                continue
            if a.get("views") is not None and b.get("views") is not None and a["views"] < 2 * m0 <= b["views"] \
                    and (b.get("lift") or 0) >= 2:  # 2x+ against both medians, so this report agrees
                crossed.append((a, b, m0))
        if crossed:
            L += ["", f"**Became outliers since the last scan** (crossed 2x the earlier median): {len(crossed)}: "
                  + ", ".join(f"[{fmt(b['views'])}]({b['url']}) ({b.get('group') or group_of(b)}: {fmt(a['views'])} → "
                              f"{fmt(b['views'])} views, 2x the earlier median is {fmt(2 * m0)}; now {lift_txt(b)})"
                              for a, b, m0 in sorted(crossed, key=lambda t: -t[1]["views"] / t[2])[:8]) + "."]
        if unmeasured:
            L += ["", "No outlier comparison for " + "; ".join(f"{g} ({why})" for g, why in sorted(unmeasured.items()))
                  + ": the earlier median wasn't measurable."]
    L.append("")
    return L


def seed_snapchat_history(out, prior=None):
    """Start this folder's Snapchat history from the newest earlier dated folder
    that has one (or, from before the history file, that folder's Spotlights),
    so the history spans every run. `prior` names a folder to try first."""
    if os.path.exists(os.path.join(out, SNAP_HISTORY)):
        return
    cands = older_scans(out, (SNAP_HISTORY, "posts_snapchat.json"))
    if not cands and prior and not later_than(collected_on(prior)[0] or "", RUN_DAY.isoformat()):
        cands = [prior]  # an undated layout: the named earlier scan is the only link
    for src in cands:
        try:
            h = load(os.path.join(src, SNAP_HISTORY))
        except (ValueError, OSError) as e:
            print(f"  snapchat: couldn't read the history in {src} ({e}); trying its Spotlights", file=sys.stderr)
            h = None
        if h:
            save(os.path.join(out, SNAP_HISTORY), h)
            return
        try:
            old = load(os.path.join(src, "posts_snapchat.json"))
        except (ValueError, OSError) as e:
            print(f"  snapchat: couldn't read {src}/posts_snapchat.json ({e}); trying an older folder", file=sys.stderr)
            continue
        if old:
            merge_snapchat_history(out, old, at=collected_on(src)[1] or collected_on(src)[0] or "earlier")
            return


def merge_snapchat_history(out, posts, at=None):
    """Every Spotlight this folder (and the scans it was seeded from) has seen,
    by id, with first and last seen and the counts at each pull. Logged out,
    the profile lists only the newest Spotlights, so this is the only record
    of the ones that scroll off."""
    path = os.path.join(out, SNAP_HISTORY)
    if at is None:
        seed_snapchat_history(out)
    try:
        h = load(path) or {}
    except ValueError:
        os.replace(path, path + ".unreadable")
        print(f"  snapchat: {SNAP_HISTORY} was unreadable; kept it as {SNAP_HISTORY}.unreadable and rebuilt it from "
              "the earlier scans", file=sys.stderr)
        if at is None:
            seed_snapchat_history(out)
        try:
            h = load(path) or {}
        except ValueError:
            h = {}
    runs, by_id = list(h.get("runs") or []), dict(h.get("posts") or {})
    at = at or PULLED_AT
    if at not in runs:
        runs.append(at)
    for p in posts:
        e = dict(by_id.get(p["id"]) or {})
        e.update({k: p.get(k) for k in ("url", "kind", "date", "posted_at", "duration_s", "caption")})
        e["first_seen"] = min(e.get("first_seen") or at, at)
        e["last_seen"] = max(e.get("last_seen") or at, at)
        snap = {"at": at, **{k: p.get(k) for k in ("views", "views_hidden", "likes", "comments", "shares")}}
        e["snapshots"] = sorted([s for s in e.get("snapshots") or [] if s.get("at") != at] + [snap],
                                key=lambda s: s.get("at") or "")[-200:]
        by_id[p["id"]] = e
    save(path, {"runs": sorted(runs)[-1000:], "posts": by_id})


def snapchat_history_lines(out):
    try:
        h = load(os.path.join(out, SNAP_HISTORY)) or {}
    except ValueError:
        return []
    allp, runs = h.get("posts") or {}, h.get("runs") or []
    if not allp:
        return []
    latest = max(runs) if runs else None
    listed = {i for i, e in allp.items() if latest and e.get("last_seen") == latest}
    gone = [e for i, e in allp.items() if i not in listed]
    L = [f"**History:** {len(allp)} Spotlights seen across {len(runs)} pull{'' if len(runs) == 1 else 's'}"
         + (f" since {str(runs[0])[:10]}" if runs else "") + f" ({SNAP_HISTORY})"
         + (f"; {len(gone)} no longer listed on the profile, kept with their last counts" if gone else "") + "."]
    last = lambda e: (e.get("snapshots") or [{}])[-1]
    peak = lambda e: max([s.get("views") or 0 for s in e.get("snapshots") or []] or [0])
    top = sorted(gone, key=peak, reverse=True)[:5]
    if top:
        L += ["", "**Most-viewed Spotlights no longer listed** (counts when last seen)",
              "| Posted | Views | Likes | Last seen | Opening line |", "|---|---|---|---|---|"]
        for e in top:
            s = last(e)
            v = f"[{fmt(s.get('views'))}]({e['url']})" if e.get("url") else fmt(s.get("views"))
            L.append(f"| {e.get('date') or '?'} | {v} | {fmt(s.get('likes'))} | {str(e.get('last_seen'))[:10]} | "
                     f"{cell(e.get('caption')) or '_(no caption)_'} |")
    return L


def report(out, name, since, note=None, prior="auto"):
    """summary.md, summary.json and posts.csv, measured as of the day the
    folder's data was collected, so a later re-render reads the same."""
    with as_of(collected_on(out)[0]):
        return build_report(out, name, since, note, prior)


def build_report(out, name, since, note, prior):
    all_posts, metas, undated_dropped = load_posts(out, since)
    if not all_posts:
        total = len(load_posts(out)[0]) if since else 0
        if total:  # the folder has posts, the date window has none: write nothing
            sys.exit(f"No posts since {since} ({total} in the folder), so nothing was written. Check the date, "
                     "or leave out --since.")
        if not os.path.exists(os.path.join(out, "podcast.json")):
            sys.exit("No posts found. Run collect / ingest-ig first.")
    groups = window_cadence(build_groups(all_posts, metas), load(os.path.join(out, "window.json")), since)
    named = prior not in (None, "auto")
    if prior == "auto":
        prior = prior_scan(out)
    if prior and os.path.abspath(prior) == os.path.abspath(out):
        prior = None
    if prior and since:  # a --since window isn't comparable with a whole earlier scan
        if named:
            msg = ("Since last scan left out: a --since report covers a date window, not the whole scan "
                   "(run report without --since to compare)")
            note(msg) if note else print(f"  report: {msg}", file=sys.stderr)
        prior = None

    with open(os.path.join(out, "posts.csv"), "w", newline="") as f:
        cols = ["platform", "group", "kind", "date", "date_approx", "views", "lift", "tier", "eng_lift",
                "likes", "comments", "shares", "saves", "duration_s", "pinned", "caption", "url", "id",
                "views_hidden", "slides", "recommends", "kind_checked"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in sorted(all_posts, key=lambda p: (p["platform"], p.get("date") or ""), reverse=True):
            w.writerow(p)

    if not name and not all_posts:  # a podcast-only folder
        name = (load(os.path.join(out, "podcast.json")) or {}).get("name")
    pulled = collected_on(out, metas)[0] or TODAY.isoformat()
    L = [f"# Social scan: {name or os.path.basename(os.path.abspath(out))}",
         f"Pulled {pulled} from public, logged-out data."
         + ((f" {len(all_posts)} posts" + (f" since {since}." if since else ".")) if all_posts else "")]
    L += window_lines(out, sorted({p["platform"] for p in all_posts})) + [""]

    if metas:
        L += ["## Accounts", "| Platform | Followers | Following | Source |", "|---|---|---|---|"]
        for plat in ("instagram", "tiktok", "youtube", "snapchat"):
            m = metas.get(plat)
            if m:
                src = m.get("source", "") + (f", pulled {m['pulled']}" if m.get("pulled") else "")
                L.append(f"| {plat} | {fmt(m.get('followers'))} | {fmt(m.get('following'))} | {src} |")
        L.append("")

    L += podcast_section(out)
    since_L = []
    if prior:
        try:
            since_L = since_last(prior, {"pulled": pulled, "pulled_at": collected_on(out, metas)[1],
                                         "window": load(os.path.join(out, "window.json")) or {},
                                         "accounts": metas, "groups": [g for _, g in groups],
                                         "posts": all_posts, "podcast": load(os.path.join(out, "podcast.json"))},
                                 name, out)
        except (ValueError, OSError) as e:  # a damaged file in the earlier folder, or a folder that isn't earlier
            msg = f"Since last scan left out: {e}"
            note(msg) if note else print(f"  report: {msg}", file=sys.stderr)
            prior = None
    if not all_posts:
        return finish_report(out, name, L + since_L, groups, all_posts, metas, prior, since)
    L += ["## Scorecard",
          "| Group | Posts | Per week | Median views | Max | Top 3 share | Outliers 5x+ / 2-5x / 1.5-2x | Confidence | Like rate (n) | Comment rate | Followers per 1K views (proxy) |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, s in groups:
        rn = f" ({s['rate_n']})" if s["like_rate"] is not None else ""
        t = s.get("tiers")
        tiers = f"{t['huge']} / {t['strong']} / {t['mild']}" if t else "n/a"
        med = f"under {fmt(s['median_under'])}" if s.get("median_under") is not None else fmt(s.get("median"))
        t3 = ("≤" if s.get("top3_upper") and s.get("top3_share") is not None else "") + pct(s.get("top3_share"))
        L.append(f"| {s['group']} | {s['posts']} | {per_week_txt(s)} | {med} | "
                 f"{fmt(s.get('max'))} | {t3} | {tiers} | {s['confidence']} ({s['baseline_n']}) | "
                 f"{pct(s['like_rate'])}{rn} | {pct(s['comment_rate'])} | {s.get('followers_per_1k') or 'n/a'} |")
    if undated_dropped:
        L.append(f"\n{undated_dropped} undated posts left out by --since (un-enriched YouTube Shorts).")
    approx_kept = sum(1 for p in all_posts if p.get("date") and p.get("date_approx")) if since else 0
    if approx_kept:
        L.append(f"\n{approx_kept} posts kept by --since have approximate dates (un-enriched YouTube uploads), "
                 f"so some may be older than {since}.")
    L.append("")
    L += since_L

    for _, s in groups:
        if not s.get("top"):
            continue
        t = s.get("tiers")
        med = f"under {fmt(s['median_under'])}" if s.get("median_under") is not None else fmt(s.get("median"))
        duds = f"{len(s.get('duds') or [])} under 0.2x" + (
            f", {s['duds_unknown']} hidden counts unknown" if s.get("duds_unknown") else "")
        L += [f"## {s['group'].title()}",
              f"{window_txt(s)}. Baseline: median {med} views across {s['baseline_n']} posts, confidence "
              f"{s['confidence']}. " + (f"Outliers: {t['huge']} at 5x+, {t['strong']} at 2-5x, {t['mild']} at 1.5-2x; "
                                        f"{duds}." if t else
                                        "Outliers can't be measured: half or more of the view counts are hidden.")]
        if s.get("all_fresh"):
            L.append((f"All {s['posts']} posts are" if s["fresh"] == s["posts"] else
                      f"Every post with a view count ({s['with_views']}) is") + " under 3 days old, so the median "
                     + ("and tiers use" if t else "uses") + " them all: every number here will still move.")
        elif s["fresh"] and s.get("rank_by") == "likes":
            L.append(f"{s['fresh']} posts are under 3 days old and stay out of the bottom 5.")
        elif s["fresh"]:
            L.append(f"{s['fresh']} posts are under 3 days old and still collecting views; they show a lift but stay out of the tier counts.")
        if s.get("views_hidden"):
            L.append(f"**Views hidden:** Snapchat shows view counts on {s['posts'] - s['views_hidden']} of {s['posts']} "
                     "Spotlights and hides them on its weakest ones, so the hidden counts rank below every visible one. "
                     + ("With half or more hidden, the median is only known to be under the lowest visible count, lift "
                        "and tiers are left out, and the tables rank by likes." if s.get("rank_by") == "likes" else
                        "The median counts them that way; top 3 share is an upper bound.")
                     + (" One or more hidden Spotlights has more likes than a visible one, so treat the order as "
                        "approximate." if s.get("hidden_not_lowest") else ""))
        mine = [p for p in all_posts if p["platform"] == "tiktok" and p.get("group") == s["group"]]
        unchecked = sum(1 for p in mine if p.get("kind_checked") is False)
        legacy = sum(1 for p in mine if "kind_checked" not in p)
        for k, why in ((unchecked, "couldn't be checked for photo carousels (the listing had no cover image and the "
                                   "post page didn't answer)"),
                       (legacy, "were collected before 1.2.0, so photo carousels weren't separated")):
            if k:
                msg = (f"{k} TikTok post{'' if k == 1 else 's'} {why} and count as videos; re-collect TikTok before "
                       "quoting this median.")
                L.append(msg)
                if note:
                    note("tiktok: " + msg)
        if s["pinned"]:
            L.append(f"{s['pinned']} pinned posts are listed for reference but sit outside the window: they stay out of "
                     "the dates, cadence, median and tier counts. Pinning keeps them in front of visitors, which explains part of their lift.")
        if approx_note(s):
            L.append(approx_note(s))
        g = s.get("gap")
        if g and g["flag"]:
            L.append(f"**Check the profile:** no posts for {g['days']} days between {g['from']} and {g['to']} "
                     f"(usual gap {g['usual']:g} days). Either the account paused or the scrape skipped posts; "
                     "don't quote this group's median or cadence until that's settled.")
        # Instagram rows from before 2026-10-15 have no feed key: they came from the posts feed
        if any(p.get("feed", "posts") == "posts" for p in all_posts
               if p["platform"] == "instagram" and p.get("group") == s["group"]):
            L.append("**Unreliable sample:** this came from the Instagram posts feed, which skips posts past the first 12 "
                     "when logged out. Re-run with the reels scrape before quoting it.")
        L += ["", "**Top posts** (" + ("ranked by likes; " if s.get("rank_by") == "likes" else "")
              + "lift = views / this group's median)",
              "| Date | Views | Lift | Tier | Eng. lift (likes+comments) | Likes | Opening line |",
              "|---|---|---|---|---|---|---|"]
        for p in s["top"]:
            el = f"{p['eng_lift']:.1f}x" if p.get("eng_lift") is not None else "n/a"
            L.append(f"| {p.get('date') or '?'}{flags(p)} | [{fmt(p['views'])}]({p['url']}) | {lift_txt(p)} | "
                     f"{p.get('tier') or ''} | {el} | {fmt(p.get('likes'))} | {p['caption'] or '_(no caption)_'} |")
        L += ["", "**Bottom 5**" + (" (by likes)" if s.get("rank_by") == "likes" else ""),
              "| Date | Views | Lift | Likes | Opening line |", "|---|---|---|---|---|"]
        for p in s["bottom"]:
            L.append(f"| {p.get('date') or '?'}{flags(p)} | [{fmt(p['views'])}]({p['url']}) | {lift_txt(p)} | "
                     f"{fmt(p.get('likes'))} | {p['caption'] or '_(no caption)_'} |")
        if s["group"] == "snapchat spotlight":
            snaps = [p for p in all_posts if p["platform"] == "snapchat"]
            L += [""] + spotlight_checks(snaps, metas.get("snapchat") or {})
            hist = snapchat_history_lines(out)
            if hist:
                L += [""] + hist
        L.append("")

    if hooks_stale(out):
        msg = "Hooks section left out: hooks.json was built from an earlier collect"
        if note:
            note(msg + " (run scan without --no-hooks to rebuild it)")
        else:
            print(f"  report: {msg}; run `hooks` again to add it", file=sys.stderr)
    else:
        L += hooks_section(out, all_posts)

    L += ["## Read this before quoting numbers",
          "- Views are plays from a logged-out view. Instagram plays count replays. None of these are unique viewers.",
          "- Lift compares a post with its own group's median (reels with reels, Shorts with Shorts), never across "
          "platforms or accounts. Tiers: huge 5x+, strong 2-5x, mild 1.5-2x. Confidence is low under 10 posts, "
          "medium at 10-19, high at 20+. Reports before 2026-10-15 counted outliers at 3x instead; per-post lift is "
          "in posts.csv and summary.json for any other cut.",
          "- Eng. lift uses likes plus comments only, against the same group's median, and shows n/a when fewer than "
          f"{MIN_RATE_N} posts in the group have like counts.",
          "- Followers per 1K views divides today's follower count by the platform's views in this window. "
          "It moves with --limit and --since, so compare like-for-like runs only. Follows from each post "
          "only show up in the account's own analytics.",
          "- Top 3 share is the three biggest posts' share of all views. A high number means the account "
          "lives on a few hits, not a repeatable format.",
          "- Median views leave out posts under 3 days old, unless a group has no older posts with views (the group "
          "says so). Like rate (n) shows how many posts had like counts.",
          "- YouTube dates from flat listings are approximate unless enriched (date_approx in posts.csv and "
          "summary.json, approx in the tables): YouTube only says \"3 months ago\", so the real upload can be months "
          "earlier. Per week and the gap check use exact dates only. Shorts without enrichment have no date.",
          "- Posts under 3 days old are still climbing. Don't compare them with older posts.",
          *(["- TikTok photo carousels get their own group (tiktok photos): their listed length is the song's, and "
             "they have no spoken hook. Reports before 1.2.0 counted them in the tiktok row."]
            if any(p["platform"] == "tiktok" and p.get("kind") == "photo" for p in all_posts) else []),
          *([f"- Snapchat listed {(metas.get('snapchat') or {}).get('spotlights_visible', '?')} Spotlights logged out: "
             "only the newest the profile page shows, with no paging, and it hides view counts on its weakest ones. "
             "Saves, watch time, completion and earnings need the account's own Insights."]
            if any(p["platform"] == "snapchat" for p in all_posts) else []),
          "- Instagram comes from the reels tab, the only feed that returns the newest posts in order when logged out. "
          "Photos and carousels aren't sampled. Pinned posts are shown but kept out of every window figure "
          "(reports before 2026-10-15 counted them in the median).",
          "- Reach, follows per post, retention and ad spend need the client's own analytics access."]
    return finish_report(out, name, L, groups, all_posts, metas, prior, since)


def finish_report(out, name, L, groups, all_posts, metas, prior=None, since=None):
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    slim = [{k: v for k, v in s.items() if k not in ("top", "bottom", "outliers", "duds")} for _, s in groups]
    lifts = [{k: p.get(k) for k in ("platform", "group", "id", "url", "date", "date_approx", "views", "views_hidden",
                                    "likes", "lift", "tier", "eng_lift", "pinned")}
             for p in all_posts]  # every post, so the next scan's "Since last scan" knows what this one saw
    pod = load(os.path.join(out, "podcast.json"))
    day, at = collected_on(out, metas, pod or {})
    save(os.path.join(out, "summary.json"), {"pulled": day or TODAY.isoformat(), "pulled_at": at,
                                             "window": load(os.path.join(out, "window.json")) or None,
                                             "rendered_at": PULLED_AT, "since": since, "name": name,
                                             "prior": os.path.basename(os.path.abspath(prior)) if prior else None,
                                             "accounts": metas, "groups": slim, "posts": lifts,
                                             **({"podcast": {k: v for k, v in pod.items() if k != "errors"}} if pod else {})})
    return os.path.join(out, "summary.md")


# ---------- doctor and scan ----------

# yt-dlp's standalone Linux build brings its own Python: the plain `yt-dlp`
# release runs on the system python3 and current yt-dlp needs 3.10+.
YTDLP_LINUX = ('sudo curl -fL -o /usr/local/bin/yt-dlp "https://github.com/yt-dlp/yt-dlp/releases/latest/download/'
               'yt-dlp_linux$([ "$(uname -m)" = aarch64 ] && echo _aarch64)" && sudo chmod a+rx /usr/local/bin/yt-dlp')
# (label, command) lines per item; an empty label continues the step above it.
# Linux lines are for Debian and Ubuntu. whisper.cpp is in apt from Ubuntu 26.04;
# older releases build it (whisper.cpp's README steps; the symlink works because
# a CMake build-tree binary keeps an absolute RPATH to its libraries).
INSTALL = {
    "python": [("macOS", "brew install python"), ("Linux", "sudo apt install python3")],
    "yt-dlp": [("macOS", "brew install yt-dlp"), ("Linux", YTDLP_LINUX)],
    "ffmpeg": [("macOS", "brew install ffmpeg"), ("Linux", "sudo apt install ffmpeg")],
    "whisper-cli": [
        ("macOS", "brew install whisper.cpp"),
        ("Linux, Ubuntu 26.04 or newer", "sudo apt install whisper.cpp"),
        ("Linux, older releases (builds from source, a few minutes)", "sudo apt install build-essential cmake git"),
        ("", "[ -d ~/whisper.cpp ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp ~/whisper.cpp"),
        ("", "cmake -S ~/whisper.cpp -B ~/whisper.cpp/build && cmake --build ~/whisper.cpp/build -j --config Release"),
        ("", "sudo ln -sf ~/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli"),
    ],
    "deno": [("macOS", "brew install deno"), ("Linux", "curl -fsSL https://deno.land/install.sh | sh")],
}
BREW_FORMULA = {"python": "python", "yt-dlp": "yt-dlp", "ffmpeg": "ffmpeg", "whisper-cli": "whisper.cpp", "deno": "deno"}
# each line says only what that one piece turns off; the summary lines say what's left
DISABLES = {
    "python": "everything",
    "yt-dlp": "YouTube and TikTok (required for both)",
    "ffmpeg": "first-frame images and spoken hooks (Snapchat keeps Snap's transcript, photo posts their image)",
    "whisper-cli": "spoken hooks (the first 10 seconds of speech)",
    "whisper model": "spoken hooks (the first 10 seconds of speech)",
    "deno": "full YouTube support (yt-dlp works without it for now, but warns that some formats may be missing)",
    "APIFY_TOKEN": "Instagram",
}
OPTIONAL = {"ffmpeg", "whisper-cli", "whisper model", "deno", "APIFY_TOKEN"}
TOOLS = (("yt-dlp", "YTDLP", YTDLP, "--version"), ("ffmpeg", "FFMPEG", FFMPEG, "-version"),
         ("whisper-cli", "WHISPER_CLI", WHISPER, "--version"), ("deno", None, "deno", "--version"))
# where installers put tools when a minimal PATH (GUI apps, cron) leaves them out
COMMON_BINS = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"),
               os.path.expanduser("~/.deno/bin"))


def here():
    return shlex.quote(os.path.abspath(__file__))


def off_path(cmd):
    """A common install folder that holds `cmd` but isn't on PATH, else None."""
    if os.sep in cmd:
        return None
    for d in COMMON_BINS:
        if os.access(os.path.join(d, cmd), os.X_OK):
            return d
    return None


def probe():
    """{name: (status, detail)} for each dependency; status is PASS, WARN, MISSING or BROKEN.

    Local only: it runs each tool's version flag and checks whether
    APIFY_TOKEN is set, never what it is.
    """
    st = {}
    v = sys.version_info
    st["python"] = ("PASS", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}")
    for name, env, cmd, flag in TOOLS:
        opt = " (optional)" if name in OPTIONAL else ""
        path = shutil.which(cmd)
        if not path:
            d = off_path(cmd)
            if d:
                st[name] = ("MISSING", f"found in {d}, but that folder isn't on PATH{opt}")
            elif env and os.environ.get(env):
                st[name] = ("MISSING", f"{env}={cmd} doesn't point at a program{opt}")
            else:
                st[name] = ("MISSING", f"not installed{opt}")
            continue
        try:
            r = subprocess.run([path, flag], capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            st[name] = ("BROKEN", f"{path} didn't answer {flag} within 30 s")
            continue
        except OSError as e:
            st[name] = ("BROKEN", f"{path} won't start ({e.strerror})")
            continue
        text = (r.stdout + "\n" + r.stderr).strip()
        if r.returncode != 0:  # whisper-cli builds without --version still exit 0 on an unknown flag
            st[name] = ("BROKEN", f"{path} won't run (exit {r.returncode}: {(text.splitlines() or [''])[-1][:120]})")
            continue
        m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})" if name == "yt-dlp" else r"(?:version:?|deno) (\S+)", text)
        st[name] = ("PASS", f"{'.'.join(m.groups()) if m else 'version unknown'} at {path}")
        if name == "yt-dlp" and m:
            age = (TODAY - dt.date(*map(int, m.groups()))).days
            if age > YTDLP_STALE_DAYS:
                st[name] = ("WARN", st[name][1] + f", {age} days old (an old yt-dlp is the usual reason YouTube or TikTok fail)")
    problem = model_problem(WHISPER_MODEL) or pin_problem(WHISPER_MODEL)
    if problem:
        st["whisper model"] = ("MISSING" if problem == "not found" else "BROKEN", f"{problem} at {WHISPER_MODEL} (optional)")
    else:
        st["whisper model"] = ("PASS", f"{os.path.getsize(WHISPER_MODEL) / 1e6:.0f} MB at {WHISPER_MODEL}")
    raw = os.environ.get("APIFY_TOKEN", "")
    if not raw.strip():
        st["APIFY_TOKEN"] = ("MISSING", "not set (optional: Instagram only, paid per use through Apify)")
    elif re.search(r"\s", raw.strip()):
        st["APIFY_TOKEN"] = ("BROKEN", "set, but it has a space or line break inside it (value not shown)")
    else:
        st["APIFY_TOKEN"] = ("PASS", "set (value not shown)")
    return st


def fix_lines(name, status, detail):
    """(label, command) lines that fix this item; the command is always last on its line, so it pastes clean."""
    if name == "yt-dlp" and status == "WARN":
        return [("macOS", "brew upgrade yt-dlp"), ("Linux (standalone build)", "sudo yt-dlp -U"),
                ("Linux (from pip or apt)", YTDLP_LINUX)]
    env = next((e for n, e, _, _ in TOOLS if n == name), None)
    if env and os.environ.get(env) and "doesn't point at a program" in detail:
        return [(f"macOS and Linux ({env} points at a missing program; fix the path or drop the override)",
                 f"unset {env}")] + list(INSTALL[name])
    if detail.startswith("found in "):
        d = re.match(r"found in (.+?), but", detail).group(1)
        return [("macOS and Linux", f'export PATH="{d}:$PATH"')]
    if name in INSTALL:
        lines = list(INSTALL[name])
        if status == "BROKEN":
            lines[0] = ("macOS", f"brew reinstall {BREW_FORMULA[name]}")
        return lines
    if name == "whisper model":
        if os.path.basename(WHISPER_MODEL) not in MODEL_SHA256:
            return [("macOS and Linux (WHISPER_MODEL names a model --get-model doesn't fetch)", "unset WHISPER_MODEL"),
                    ("", f"python3 {here()} doctor --get-model")]
        env = f"WHISPER_MODEL={shlex.quote(WHISPER_MODEL)} " if os.environ.get("WHISPER_MODEL") else ""
        return [("macOS and Linux (downloads about 148 MB from Hugging Face)", f"{env}python3 {here()} doctor --get-model")]
    return [("macOS and Linux (token: Apify Console > Settings > API & Integrations)", "export APIFY_TOKEN=YOUR_TOKEN")]


def doctor(get_model_too):
    if get_model_too:
        get_model(WHISPER_MODEL)
    print("social-scan doctor\n", flush=True)
    st = probe()
    pad = " " * 26
    for name, (status, detail) in st.items():
        print(f"  {status:<8} {name:<14} {detail}")
        if status == "PASS":
            continue
        if status != "WARN":
            print(f"{pad}turns off: {DISABLES[name]}")
        for label, cmd in fix_lines(name, status, detail):
            print(f"{pad}{label}: {cmd}" if label else f"{pad}  {cmd}")
    ok = {k for k, (s, _) in st.items() if s in ("PASS", "WARN")}
    need = [n for n in ("yt-dlp",) if n not in ok]
    print()
    if need:
        print("YouTube and TikTok: not ready; install yt-dlp (lines above), then run doctor again.")
    elif st["yt-dlp"][0] == "WARN":
        print("YouTube and TikTok: ready, but update yt-dlp first (lines above).")
    else:
        print("YouTube and TikTok: ready.")
    if {"ffmpeg", "whisper-cli", "whisper model"} <= ok:
        print("Hooks: caption's first line, first frame and spoken hook.")
    elif "ffmpeg" in ok:
        print("Hooks: caption's first line and first frame; no spoken hook.")
    else:
        print("Hooks: caption's first line only; no first frame, no spoken hook.")
    print("Snapchat: ready (free, logged out; the newest Spotlights the profile page lists).")
    print("Podcast: ready (free: Apple and Spotify charts, Apple rating and reviews, the show's RSS feed).")
    print("Instagram: " + (f"ready (paid per use through Apify: about $0.11 per 40 reels, capped at ${IG_MAX_USD:.2f} per call)."
                           if "APIFY_TOKEN" in ok else
                           "off (optional; paid per use through Apify, about $0.11 per 40 reels; set APIFY_TOKEN to turn it on)."))
    if not need:
        print(f"\nNext (one folder per account): python3 {here()} scan --out ./scans/HANDLE --youtube @HANDLE --tiktok HANDLE")
    return 0 if not need else 1


def pin_problem(path):
    """For a file whose name has a pinned sha256: None when it matches, else what's wrong."""
    import hashlib
    want = MODEL_SHA256.get(os.path.basename(path))
    if not want:
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as e:
        return f"unreadable ({e.strerror})"
    return None if h.hexdigest() == want else "doesn't match its pinned sha256 (a cut-off or swapped download)"


def get_model(path):
    """Download the pinned whisper.cpp model to `path`. Runs only on `doctor --get-model`."""
    import hashlib
    import urllib.request
    path = os.path.abspath(path)
    if not model_problem(path) and not pin_problem(path):
        print(f"  whisper model already in place at {path} (sha256 checked)", file=sys.stderr)
        return
    name = os.path.basename(path)
    if name not in MODEL_SHA256:  # only fetch what has a pinned checksum
        sys.exit(f"--get-model only fetches {', '.join(MODEL_SHA256)}, whose sha256 it checks, and WHISPER_MODEL "
                 f"ends in {name}. Download that file yourself, or unset WHISPER_MODEL.")
    url, want = MODEL_URL + name, MODEL_SHA256[name]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    part, h, got = path + ".part", hashlib.sha256(), 0
    print(f"  downloading {url}", file=sys.stderr)
    try:  # the finally below removes the .part on every way out, Ctrl-C included
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
                total, shown = int(r.headers.get("Content-Length") or 0), -1
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if total and got * 20 // total != shown:
                        shown = got * 20 // total
                        print(f"\r  {got / 1e6:.0f} of {total / 1e6:.0f} MB", end="", file=sys.stderr, flush=True)
            print(file=sys.stderr)
            if total and got != total:
                raise ConnectionError(f"the download stopped at {got / 1e6:.0f} of {total / 1e6:.0f} MB")
        except Exception as e:  # network, TLS, HTTP or disk trouble: a hand download with the same check is safe
            q, qp = shlex.quote(path), shlex.quote(part)
            fetch = f"curl -fL -o {qp} {url} && echo '{want}  '{qp} |"
            sys.exit(f"\nModel download failed ({e}); nothing was saved. Rerun `doctor --get-model` once the network "
                     "is back, or fetch it by hand (it only moves into place if the checksum matches):\n"
                     f"  macOS: {fetch} shasum -a 256 -c && mv {qp} {q}\n"
                     f"  Linux: {fetch} sha256sum -c && mv {qp} {q}")
        if h.hexdigest() != want:
            sys.exit(f"\nThe downloaded {name} doesn't match its pinned sha256, so it was thrown away. "
                     "Don't fetch it by hand; try again later, and open an issue if it keeps happening.")
        problem = model_problem(part)
        if problem:
            sys.exit(f"\nThe downloaded {name} looks wrong ({problem}), so it was thrown away.")
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.remove(part)
    print(f"  saved {got / 1e6:.0f} MB to {path} (sha256 checked)", file=sys.stderr)


def scan(a):
    """collect, hooks, comments and report in one go. A step whose tool or
    token is missing is skipped with a one-line note instead of failing."""
    t0, notes, ran, followers_error = time.time(), [], [], None

    def note(msg):  # collected for the recap at the end, so each one prints once
        notes.append(msg)

    todo = [(p, h) for p, h in (("youtube", a.youtube), ("tiktok", a.tiktok), ("instagram", a.instagram),
                                ("snapchat", a.snapchat)) if h]
    pod_info = {}
    if not todo and not a.podcast and not load_posts(a.out)[0] and not os.path.exists(os.path.join(a.out, "podcast.json")):
        sys.exit("scan needs --youtube, --tiktok, --instagram, --snapchat or --podcast (or an --out folder that "
                 "already holds collected posts)")
    if a.podcast:
        ran.append("podcast")
        try:
            pod_info = collect_podcast(a.podcast, a.out)
            if pod_info.get("note"):
                note(pod_info["note"])
            for err in pod_info.get("errors") or []:
                note(f"podcast: couldn't read {err}")
        except Exception as e:
            note(f"podcast failed: {e}")
    prior = None if a.no_prev else (a.prev or prior_scan(a.out))
    if prior and a.snapchat:
        seed_snapchat_history(a.out, prior)
    rp = os.path.join(a.out, "raw", "instagram_reels.json")
    for plat, h in todo:
        if plat in ("youtube", "tiktok") and not shutil.which(YTDLP):
            note(f"{plat} skipped: yt-dlp isn't installed (`doctor` has the install line)")
            continue
        if plat == "instagram" and not os.environ.get("APIFY_TOKEN", "").strip():
            note("instagram skipped: APIFY_TOKEN isn't set in this shell (optional, paid per use through Apify). "
                 "Set it in your own terminal and run the scan there, or see SKILL.md step 3")
            continue
        ran.append(plat)
        try:  # one platform failing shouldn't sink the others
            if plat == "youtube":
                collect_youtube(h, a.out, a.limit, a.yt_enrich, days=a.days)
            elif plat == "tiktok":
                info = collect_tiktok(h, a.out, a.limit, quiet_followers=True, days=a.days) or {}  # the recap prints the follow-up
                followers_error = info.get("followers_error")
            elif plat == "snapchat":
                collect_snapchat(h, a.out)
                window_after(a.out, "snapchat", a.days)
            else:
                print(f"  instagram: Apify reels scrape, about $0.11 per 40 reels, capped at ${IG_MAX_USD:.2f} per call",
                      file=sys.stderr)
                try:
                    collect_instagram(h, a.out, min(a.limit, 50))
                finally:  # the reels are saved before the follower call, so trim them even if it fails
                    pi = os.path.join(a.out, "posts_instagram.json")
                    if os.path.exists(pi) and os.path.getmtime(pi) >= t0:
                        window_after(a.out, "instagram", a.days)
        except Exception as e:
            msg = f"{plat} failed: {redact(e)}"
            pi = os.path.join(a.out, "posts_instagram.json")
            if plat == "instagram" and os.path.exists(rp) and os.path.getmtime(rp) >= t0 \
                    and not (os.path.exists(pi) and os.path.getmtime(pi) >= t0):
                msg += (". The reels were saved, so this recovers them without a second paid run: "
                        f"python3 {here()} ingest-ig --out {shlex.quote(a.out)} --reels {shlex.quote(rp)}, "
                        f"then: python3 {here()} scan --out {shlex.quote(a.out)}")
            note(msg)
    if a.tiktok_followers is not None:  # keep the following count this run just read
        mp = os.path.join(a.out, "meta_tiktok.json")
        old = load(mp) or {}
        fresh = os.path.exists(mp) and os.path.getmtime(mp) >= t0
        save(mp, {"followers": a.tiktok_followers, "following": old.get("following") if fresh else None,
                  "source": "tiktok profile page (scan --tiktok-followers)", "pulled": TODAY.isoformat()})
    if todo or a.podcast:  # anything this run didn't refresh came from an earlier run: say so
        for plat in PLATFORMS:
            f = os.path.join(a.out, f"posts_{plat}.json")
            if not (os.path.exists(f) and os.path.getmtime(f) < t0):
                continue
            when = dt.datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M")
            # SKILL.md step 3 ingests Instagram from the Apify MCP into this folder just before scan
            if plat == "instagram" and plat not in dict(todo) and os.path.exists(rp) and t0 - os.path.getmtime(f) < 6 * 3600:
                note(f"instagram: the posts ingested at {when} go into the report")
            else:
                note(f"{plat}: posts_{plat}.json was already in this folder (saved {when}) and goes into the report; "
                     "if it's another account's, use a new --out folder per account")
        for plat in dict(todo):
            m = load(os.path.join(a.out, f"meta_{plat}.json"))
            if m and os.path.getmtime(os.path.join(a.out, f"meta_{plat}.json")) < t0:
                flag = " pass --tiktok-followers N to update it," if plat == "tiktok" else ""
                note(f"{plat}: the follower count ({fmt(m.get('followers'))}) is from an earlier run, pulled "
                     f"{m.get('pulled', '?')};{flag} or use a new --out folder if it's another account's")

    pi = os.path.join(a.out, "posts_instagram.json")
    if a.days and "instagram" not in dict(todo) and os.path.exists(pi) and t0 - os.path.getmtime(pi) < 6 * 3600 \
            and "instagram" not in (load(os.path.join(a.out, "window.json")) or {}):
        window_after(a.out, "instagram", a.days)  # SKILL.md step 3: reels ingested through the Apify MCP just before
        note(f"instagram: the ingested reels were trimmed to the last {a.days} days")
    pj = os.path.join(a.out, "podcast.json")
    if os.path.exists(pj) and os.path.getmtime(pj) < t0:
        old = load(pj) or {}
        what = f"podcast.json ({old.get('name') or '?'}, pulled {old.get('pulled', '?')})"
        if a.podcast:  # this run's --podcast failed before saving (the note above says why)
            note(f"podcast: this run's --podcast didn't replace it, so the earlier {what} goes into the report; "
                 "use a new --out folder if that's another show")
        else:
            note(f"podcast: {what} is from an earlier run and goes into the report; pass --podcast to refresh it, "
                 "or use a new --out folder if it's another show")
    posts, metas, _ = load_posts(a.out)
    if not posts and os.path.exists(pj):  # a podcast-only scan
        path = report(a.out, a.name or pod_info.get("name"), None, note=note, prior=prior)
        print(f"\nscan finished in {time.time() - t0:.0f} s", file=sys.stderr)
        if notes:
            print("Notes:", file=sys.stderr)
            for n in notes:
                print(f"  - {n}", file=sys.stderr)
        print(path)
        return
    if not posts:
        for n in notes:
            print(f"  - {n}", file=sys.stderr)
        failed = [n for n in notes if " failed: " in n]
        wins = load(os.path.join(a.out, "window.json")) or {}
        empty = {k: v for k, v in wins.items() if empty_window(v)}
        if a.days and empty and len(empty) == len([k for k in wins if not wins[k].get("posts")]):
            sys.exit(f"No posts in the last {a.days} days, so there's no report: "
                     + "; ".join(f"{k}'s newest is from {v.get('newest') or '?'}" for k, v in sorted(empty.items()))
                     + ". Widen --days, or leave it out to collect by post count.")
        if not ran:
            hint = "The notes above say what was skipped; `doctor` shows what's missing."
        elif failed and all("(check the handle)" in n for n in failed):
            hint = "Fix the handles above and run it again."
        else:  # advice for each platform that ran
            parts = []
            if {"youtube", "tiktok"} & set(ran):
                parts.append("YouTube/TikTok: usually a mistyped handle or a private account; if the handles are right, "
                             "update yt-dlp (`doctor` checks its age).")
            if "snapchat" in ran:
                parts.append("Snapchat: check the network, and that the account is a Public Profile with Spotlights.")
            if "podcast" in ran:
                parts.append("Podcast: check the show name, or pass its Apple Podcasts ID (the digits after 'id' in "
                             "its podcasts.apple.com link; the link works too).")
            if "instagram" in ran:
                parts.append("Instagram: 401 means APIFY_TOKEN is wrong, 402 means the Apify account is out of credit; "
                             "otherwise check the username.")
            hint = "Check the errors above. " + " ".join(parts)
        sys.exit("Nothing was collected, so there's no report. " + hint)
    if not a.no_hooks:
        if not shutil.which(YTDLP) and not any(p["platform"] in ("instagram", "snapchat") for p in posts):
            note("hooks skipped: yt-dlp isn't installed, so any earlier hooks.json is kept")
        else:
            try:
                hooks(a.out, 3, 2, 20)
            except Exception as e:
                note(f"hooks failed: {e}")
            else:  # describe what the step produced, not what the tools predicted
                hs = load(os.path.join(a.out, "hooks.json"), []) or []
                frames = sum(1 for e in hs if e.get("frame"))
                spoken = sum(1 for e in hs if e.get("spoken_status") == "ok")
                failed = [e for e in hs if not (e.get("spoken_status") or "ok").startswith(
                    ("ok", "skipped", "no speech", "photo", "no audio"))]
                skips = sorted({e["spoken_status"][len("skipped: "):] for e in hs
                                if (e.get("spoken_status") or "").startswith("skipped: ")})
                if skips or failed:
                    note(f"hooks: {len(hs)} posts, {spoken} spoken hooks, {frames} first frames"
                         + (f"; left out because {', '.join(skips)}" if skips else "")
                         + (f"; {len(failed)} clip{'' if len(failed) == 1 else 's'} failed (first: {failed[0]['spoken_status'][:100]})"
                            if failed else "") + " (`doctor` has the fixes)")
    if not a.no_comments:
        if not shutil.which(YTDLP) and not any(p["platform"] in ("instagram", "snapchat") for p in posts) \
                and not (a.tiktok_comments and any(p["platform"] == "tiktok" for p in posts)):
            note("comments skipped: yt-dlp isn't installed")
        else:
            try:
                comments(a.out, 3, 100, tiktok=a.tiktok_comments)
            except Exception as e:
                note(f"comments failed: {e}")
            else:
                cs = load(os.path.join(a.out, "comments.json"), []) or []
                tk_bad = [c for c in cs if (c.get("source") or "").startswith("tiktok comments didn't load")]
                tk_cut = [c for c in cs if "; stopped after " in (c.get("source") or "")]
                if tk_cut:
                    note(f"comments: TikTok's comment list stopped partway on {len(tk_cut)} post"
                         f"{'' if len(tk_cut) == 1 else 's'} (first: {tk_cut[0]['source'].split('; stopped after ', 1)[1][:100]}); "
                         "what was read is kept")
                if tk_bad:
                    note(f"comments: TikTok's comment list didn't answer for {len(tk_bad)} of "
                         f"{sum(1 for c in cs if c.get('platform') == 'tiktok')} posts ({tk_bad[0]['source'].split(': ', 1)[-1][:100]}); "
                         "the endpoint is unofficial, so this can happen at any time")
                bad = [c for c in cs if (c.get("source") or "").startswith("yt-dlp failed")]
                if bad:
                    note(f"comments: {len(bad)} of {len(cs)} posts didn't load (first: {bad[0]['source'][:120]})")
                if not shutil.which(YTDLP) and any(p["platform"] == "youtube" for p in posts):
                    kept = sum(1 for c in cs if c.get("platform") == "youtube")
                    note("comments: yt-dlp isn't installed, so YouTube comments are "
                         + (f"the earlier ones ({kept} posts)" if kept else "left out"))
    prev = load(os.path.join(a.out, "summary.json"), {}) or {}
    name = a.name or (handle_of(todo[0][1]) if todo else None) or prev.get("name") or pod_info.get("name")
    path = report(a.out, name, None, note=note, prior=prior)

    print(f"\nscan finished in {time.time() - t0:.0f} s", file=sys.stderr)
    if (load(os.path.join(a.out, "summary.json")) or {}).get("prior"):
        print(f"  since last scan: compared with {os.path.relpath(prior)}", file=sys.stderr)
    for plat in PLATFORMS:
        n = sum(1 for p in posts if p["platform"] == plat)
        if n:
            f = (metas.get(plat) or {}).get("followers")
            who = "subscribers" if plat == "youtube" else "followers"
            print(f"  {plat:<10} {n} posts" + (f", {fmt(f)} {who}" if f is not None else ""), file=sys.stderr)
    if any(p["platform"] == "tiktok" for p in posts) and "tiktok" not in metas:
        print("  tiktok: no follower count this run (the public profile page didn't give one"
              + (f": {followers_error}" if followers_error else "") + "). Re-run later, or read it off the profile and "
              "add it with:\n"
              f"    python3 {here()} scan --out {shlex.quote(a.out)} --tiktok-followers N --no-hooks --no-comments",
              file=sys.stderr)
    if followers_error and "tiktok" in metas:  # the recap line above didn't print: say it here
        notes.append(f"tiktok: no follower count this run ({followers_error}); the count shown is from an earlier run")
    if notes:
        print("Notes:", file=sys.stderr)
        for n in notes:
            print(f"  - {n}", file=sys.stderr)
    print(path)


# ---------- cli ----------

def positive_int(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("needs a number, 1 or more")
    return n


def main():
    # before argparse: on Python 3.6 add_subparsers(required=...) itself fails
    if sys.version_info < MIN_PY:
        sys.exit(f"social-scan needs Python {MIN_PY[0]}.{MIN_PY[1]} or newer; this is {sys.version.split()[0]} "
                 f"at {sys.executable}.\n  macOS: brew install python\n  Linux: sudo apt install python3")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="what's installed, what each missing piece turns off, and how to install it")
    d.add_argument("--get-model", action="store_true",
                   help="download the whisper model to WHISPER_MODEL (about 148 MB from Hugging Face, sha256-checked); "
                        "nothing is downloaded without this flag")

    s = sub.add_parser("scan", help="collect, hooks, comments and report in one go (one --out folder per account)")
    s.add_argument("--out", required=True, help="folder for this account's run; scan reports everything already in it")
    s.add_argument("--youtube")
    s.add_argument("--tiktok")
    s.add_argument("--instagram", help="needs APIFY_TOKEN in the environment; skipped with a note when it isn't set")
    s.add_argument("--snapchat", help="public profile, free and logged out; only the newest Spotlights the profile page lists")
    s.add_argument("--podcast", help="show name, Apple Podcasts ID or podcasts.apple.com link: chart ranks, rating, reviews, "
                   "episode cadence (free)")
    s.add_argument("--name", help="report title (default: the first handle, or the show's name for a podcast-only scan)")
    s.add_argument("--tiktok-followers", type=int, help="override the TikTok follower count scan reads off the profile page")
    s.add_argument("--limit", type=int, default=60, help="posts per tab/profile (default 60; Instagram max 50)")
    s.add_argument("--days", type=int, help=f"collect the last N days instead of a post count (up to {DAYS_CAP} posts "
                   "per tab; YouTube looks up every post in the window to date it, whatever --yt-enrich says; "
                   "Instagram and Snapchat are trimmed to the window)")
    s.add_argument("--yt-enrich", type=int, default=45,
                   help="fetch likes/comments/exact dates for the newest N YouTube uploads (default 45, 0 = off)")
    s.add_argument("--no-hooks", action="store_true", help="skip the hooks step")
    s.add_argument("--no-comments", action="store_true", help="skip the comments step")
    s.add_argument("--tiktok-comments", action="store_true",
                   help="also read comments on the top TikTok posts, through TikTok's unofficial web comment list "
                        "(logged out, unsigned; it can stop answering at any time)")
    s.add_argument("--prev", help="an earlier scan folder of this account to compare with (default: the newest "
                   "earlier dated folder beside --out)")
    s.add_argument("--no-prev", action="store_true", help="leave out the Since last scan section")

    c = sub.add_parser("collect", help="collect posts only")
    c.add_argument("--out", required=True)
    c.add_argument("--youtube")
    c.add_argument("--tiktok")
    c.add_argument("--instagram", help="needs APIFY_TOKEN in the environment; reels tab, about $0.11 per 40 reels, capped at $0.30 per Apify call (two calls)")
    c.add_argument("--snapchat", help="public profile, free and logged out; the newest Spotlights the profile page lists")
    c.add_argument("--podcast", help="show name, Apple Podcasts ID or podcasts.apple.com link: chart ranks, rating, reviews, "
                   "episode cadence (free)")
    c.add_argument("--limit", type=int, default=60, help="posts per tab/profile (default 60)")
    c.add_argument("--days", type=int, help=f"collect the last N days instead of a post count (up to {DAYS_CAP} per tab)")
    c.add_argument("--yt-enrich", type=int, default=45,
                   help="fetch likes/comments/exact dates for the newest N YouTube uploads (default 45, 0 = off)")

    i = sub.add_parser("ingest-ig", help="load Instagram JSON saved from the Apify MCP")
    i.add_argument("--out", required=True)
    src = i.add_mutually_exclusive_group(required=True)
    src.add_argument("--reels", help=f"{IG_REELS_ACTOR} dataset JSON (contiguous newest reels)")
    src.add_argument("--posts", help="older apify/instagram-scraper posts-feed JSON; readable, but flagged unreliable")
    i.add_argument("--profile")
    i.add_argument("--days", type=int, help="trim the ingested reels to the last N days (match the scan's --days)")

    m = sub.add_parser("meta", help="record a follower count by hand")
    m.add_argument("--out", required=True)
    m.add_argument("--platform", required=True, choices=["instagram", "tiktok", "youtube", "snapchat"])
    m.add_argument("--followers", type=int, required=True)
    m.add_argument("--following", type=int)
    m.add_argument("--source", default="manual")

    h = sub.add_parser("hooks", help="text, spoken and visual hooks for the top and bottom posts per group")
    h.add_argument("--out", required=True)
    h.add_argument("--top", type=int, default=3, help="top posts per group (default 3)")
    h.add_argument("--bottom", type=int, default=2, help="bottom posts per group, for contrast (default 2)")
    h.add_argument("--seconds", type=int, default=20, help="seconds of video to fetch per post (default 20)")

    k = sub.add_parser("comments", help="comment text for the top posts (YouTube, Instagram, Snapchat; TikTok with --tiktok)")
    k.add_argument("--out", required=True)
    k.add_argument("--top", type=int, default=3, help="top posts per group (default 3)")
    k.add_argument("--max", type=positive_int, default=100,
                   help="comments per post (default 100); TikTok also stops at 10 pages, about 130-170")
    k.add_argument("--tiktok-comments", "--tiktok", dest="tiktok", action="store_true",
                   help="also TikTok, through its unofficial web comment list")

    r = sub.add_parser("report", help="write summary.md, summary.json and posts.csv")
    r.add_argument("--out", required=True)
    r.add_argument("--name")
    r.add_argument("--since")
    r.add_argument("--prev", help="an earlier scan folder to compare with (default: the newest earlier dated folder)")
    r.add_argument("--no-prev", action="store_true", help="leave out the Since last scan section")

    df = sub.add_parser("diff", help="what changed between two scan folders of one account (prints markdown)")
    df.add_argument("old")
    df.add_argument("new")

    cp = sub.add_parser("compare", help="one table across accounts, one scan folder each (prints markdown)")
    cp.add_argument("dirs", nargs="+", metavar="DIR")
    cp.add_argument("--out", dest="compare_out", help="also write the table to this file")

    a = ap.parse_args()
    if a.cmd == "doctor":
        sys.exit(doctor(a.get_model))
    if a.cmd == "diff":
        for d in (a.old, a.new):
            if not os.path.isfile(os.path.join(d, "summary.json")):
                sys.exit(f"{d} has no summary.json: diff compares two folders that each hold a report")
        new = scan_state(a.new)
        new["pulled"] = new["pulled"] or TODAY.isoformat()
        old = scan_state(a.old)
        if later_than(old["pulled_at"] or old["pulled"] or "", new["pulled_at"] or new["pulled"] or ""):
            sys.exit(f"{a.new} was collected before {a.old}: diff takes the earlier folder first, then the later one")
        print("\n".join(since_last(a.old, new, new.get("name"), a.new)))
        return
    if a.cmd == "compare":
        try:
            md = compare(a.dirs)
        except ValueError as e:
            sys.exit(str(e))
        if a.compare_out:
            with open(a.compare_out, "w") as f:
                f.write(md)
        print(md, end="")
        return
    if getattr(a, "days", None) is not None and a.days < 1:
        sys.exit("--days needs a number of days, 1 or more")
    for d in (getattr(a, "prev", None),):
        if d and not os.path.isfile(os.path.join(d, "summary.json")):
            sys.exit(f"--prev {d} has no summary.json: point it at an earlier scan folder of this account")
    os.makedirs(a.out, exist_ok=True)
    if a.cmd == "scan":
        scan(a)
    elif a.cmd == "collect":
        if not (a.youtube or a.tiktok or a.instagram or a.snapchat or a.podcast):
            sys.exit("collect needs at least one of --youtube, --tiktok, --instagram, --snapchat, --podcast")
        for flag, fn in ((a.youtube, lambda: collect_youtube(a.youtube, a.out, a.limit, a.yt_enrich, days=a.days)),
                         (a.tiktok, lambda: collect_tiktok(a.tiktok, a.out, a.limit, days=a.days)),
                         (a.instagram, lambda: collect_instagram_window(a.instagram, a.out, min(a.limit, 50), a.days)),
                         (a.snapchat, lambda: (collect_snapchat(a.snapchat, a.out), window_after(a.out, "snapchat", a.days))),
                         (a.podcast, lambda: collect_podcast(a.podcast, a.out))):
            if not flag:
                continue
            try:  # one platform failing shouldn't sink the others
                fn()
            except Exception as e:
                print(f"  FAILED: {redact(e)}", file=sys.stderr)
    elif a.cmd == "ingest-ig":
        ingest_ig(a.reels or a.posts, a.profile, a.out, feed="reels" if a.reels else "posts")
        window_after(a.out, "instagram", a.days)
    elif a.cmd == "meta":  # merge, so a collector's extra fields (Snapchat's Story counts) survive
        mp = os.path.join(a.out, f"meta_{a.platform}.json")
        m = load(mp, {}) or {}
        m.update(followers=a.followers, source=a.source, pulled=TODAY.isoformat())
        if a.following is not None:
            m["following"] = a.following
        save(mp, m)
    elif a.cmd == "hooks":
        hooks(a.out, a.top, a.bottom, a.seconds)
        print("  run `report` to add the Hooks section to summary.md", file=sys.stderr)
    elif a.cmd == "comments":
        comments(a.out, a.top, a.max, tiktok=a.tiktok)
    elif a.cmd == "report":
        print(report(a.out, a.name, a.since, prior=None if a.no_prev else (a.prev or "auto")))


if __name__ == "__main__":
    main()
