#!/usr/bin/env python3
"""social-scan: pull public social stats and turn them into a one-page audit.

Backends
  YouTube, TikTok  -> yt-dlp, run here (logged out, no cookies)
  Instagram        -> Apify REST (`scan --instagram` or `collect --instagram`,
                      APIFY_TOKEN from the environment) or Apify MCP dataset
                      JSON via `ingest-ig`

Start here
  doctor     [--get-model]   what's installed, what each missing piece turns off,
                             and the install line for macOS and Linux
  scan       --out DIR [--youtube H] [--tiktok H] [--instagram H] [--name N]
             [--tiktok-followers N] [--limit N] [--yt-enrich N] [--no-hooks] [--no-comments]
                             collect, hooks, comments and report in one go;
                             a step whose tool is missing is skipped with a note.
                             Use one --out folder per account: scan reports
                             everything already in it.

Single steps
  collect    --out DIR [--youtube H] [--tiktok H] [--instagram H] [--limit N] [--yt-enrich N]
  ingest-ig  --out DIR (--reels reels.json | --posts posts.json) [--profile profile.json]
  meta       --out DIR --platform P --followers N [--following N] [--source TEXT]
  hooks      --out DIR [--top N] [--bottom N] [--seconds S]
  comments   --out DIR [--top N] [--max N]
  report     --out DIR [--name NAME] [--since YYYY-MM-DD]

Outlier lift tiers and the hooks pass are adapted from ScrapeCreators'
social-media-research-skills @ 64ba7b4 (MIT): outlier-post-finder and
transcript-intelligence, rewired to yt-dlp, Apify and local whisper-cli.

Tools come from PATH; YTDLP, FFMPEG, WHISPER_CLI and WHISPER_MODEL override them.
Everything is read-only. Never add --cookies or --cookies-from-browser.
"""
import argparse
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

def collect_youtube(handle, out, limit, enrich):
    base = f"https://www.youtube.com/@{handle_of(handle)}"
    posts, followers, errs = [], None, []
    for tab, kind in (("videos", "long"), ("shorts", "short")):
        try:
            d = ytdlp_json(f"{base}/{tab}", ["--playlist-end", str(limit),
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
    if enrich and posts:
        def full(p):
            try:
                return p, ytdlp_json(f"https://www.youtube.com/watch?v={p['id']}", flat=False, timeout=90)
            except Exception:
                return p, None
        # a third of the budget to the newest long-form, the rest to the newest
        # Shorts, then backfill unused slots from whichever kind has more
        all_l = [p for p in posts if p["kind"] == "long"]
        all_s = [p for p in posts if p["kind"] == "short"]
        longs = all_l[: max(enrich // 3, 1)]
        shorts = all_s[: enrich - len(longs)]
        longs = all_l[: enrich - len(shorts)]
        targets = longs + shorts
        with ThreadPoolExecutor(max_workers=6) as ex:
            for p, d in ex.map(full, targets):
                if not d:
                    continue
                p["date"], p["date_approx"] = to_date(d), False
                p["likes"] = num(d.get("like_count"))
                p["comments"] = num(d.get("comment_count"))
                p["duration_s"] = d.get("duration") or p["duration_s"]
                v = num(d.get("view_count"))
                p["views"] = v if v is not None else p["views"]
        print(f"  youtube: enriched {len(targets)} of {len(posts)}", file=sys.stderr)

    save(os.path.join(out, "posts_youtube.json"), posts)
    if followers is not None:
        save(os.path.join(out, "meta_youtube.json"),
             {"followers": followers, "following": None, "source": "yt-dlp channel_follower_count",
              "pulled": TODAY.isoformat()})
    print(f"  youtube: {len(posts)} posts, "
          + (f"{followers:,} subscribers" if followers is not None else "subscriber count not shown"), file=sys.stderr)


def collect_tiktok(handle, out, limit, quiet_followers=False):
    d = ytdlp_json(f"https://www.tiktok.com/@{handle_of(handle)}", ["--playlist-end", str(limit)])
    save(os.path.join(out, "raw", "tiktok.json"), d)
    posts = []
    for e in d.get("entries") or []:
        posts.append({
            "platform": "tiktok", "kind": "video", "id": e.get("id"), "url": e.get("url"),
            "date": to_date(e), "date_approx": False, "duration_s": e.get("duration"),
            "views": num(e.get("view_count")), "likes": num(e.get("like_count")),
            "comments": num(e.get("comment_count")), "shares": num(e.get("repost_count")),
            "saves": num(e.get("save_count")), "pinned": False,
            "caption": first_line(e.get("description") or e.get("title")),
            "hook_text": first_line(e.get("description") or e.get("title"), 300),
        })
    if not posts:
        print("  tiktok: 0 posts (private, empty, or blocked)", file=sys.stderr)
    save(os.path.join(out, "posts_tiktok.json"), posts)
    print(f"  tiktok: {len(posts)} posts" + ("" if quiet_followers else
          f" (followers aren't visible logged out; add them with: python3 {here()} meta --out {shlex.quote(out)} "
          "--platform tiktok --followers N)"), file=sys.stderr)


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
    s = {"group": name, "posts": len(window), "with_views": sum(1 for p in window if p.get("views") is not None)}
    if dates:
        # cadence runs to today, so a quiet stretch after the last post counts
        span = (TODAY - dt.date.fromisoformat(dates[0])).days
        s.update(first=dates[0], last=dates[-1],
                 per_week=round(len(dates) / (span / 7), 1) if span >= 14 else "n/a")
        s["gap"] = largest_gap(dates)
    # baseline excludes posts under 3 days old, which are still climbing
    in_window = [p for p in viewed if not p.get("pinned")] or viewed
    settled = [p for p in in_window if not is_fresh(p)]
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
        s["duds"] = [p for p in settled if med > 0 and p["views"] < 0.2 * med]
        # top 5 by views, extended to 10 rows only when more than 5 posts reach 1.5x
        s["top"] = s["outliers"][:10] if len(s["outliers"]) > 5 else ranked[:5]
        s["bottom"] = sorted(settled, key=lambda p: p["views"])[:5]
        top3 = sum(sorted((p["views"] for p in in_window), reverse=True)[:3])
        s["top3_share"] = top3 / s["total_views"] if s["total_views"] else None
    s["rate_n"] = sum(1 for p in window if p.get("likes") is not None and p.get("views"))
    s["like_rate"] = median_rate(window, "likes")
    s["comment_rate"] = median_rate(window, "comments")
    s["share_rate"] = median_rate(window, "shares")
    s["fresh"] = sum(1 for p in window if is_fresh(p))
    s["pinned"] = sum(1 for p in posts if p.get("pinned"))
    return s


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

    Shares and saves stay out: only TikTok exposes them logged out, so adding
    them would make the same number mean different things per platform.
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
JUNK = re.compile(r"thanks? (you )?for watching|subscribe|subtitles? by|amara\.org|^\W*$|^\s*[\[(].*[\])]\s*$", re.I)


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


def fetch_clip(plat, p, seconds, path):
    """First `seconds` of the post's video, logged out. Returns an error string or None."""
    if plat == "instagram":
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
    return ("video link expired or blocked: " if plat == "instagram" else "") + err[:160]


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
        print(f"  hooks: {speech_off(detail=True)}, so {keeps}, no spoken hooks (`doctor` has the fix)", file=sys.stderr)
    if no_clips and any(t[0] != "instagram" for t in targets):
        print("  hooks: yt-dlp not installed, so YouTube and TikTok keep caption openings only", file=sys.stderr)

    def one(t):
        plat, group, role, p = t
        stem = os.path.join(hdir, f"{plat}_{p['id']}")
        e = {"platform": plat, "group": group, "role": role, "id": p["id"], "url": p["url"],
             "views": p.get("views"), "lift": p.get("lift"), "tier": p.get("tier"),
             "text_hook": caption_hook(p.get("hook_text") or p.get("caption")), "spoken_hook": None,
             "spoken_status": None, "frame": None}
        if p.get("kind") in ("image", "carousel"):
            e["spoken_status"] = "photo or carousel: text hook only"
            return e
        if no_frames:
            e["spoken_status"] = f"skipped: {no_speech}"
            return e
        if no_clips and plat != "instagram":
            e["spoken_status"] = "skipped: yt-dlp not installed"
            return e
        clip, wav = stem + ".mp4", stem + ".wav"
        try:
            err = fetch_clip(plat, p, seconds, clip)
            if err:
                e["spoken_status"] = err
                return e
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.5", "-i", clip, "-frames:v", "1",
                            "-vf", "scale=360:-2", stem + ".jpg"], capture_output=True, timeout=60)
            if os.path.exists(stem + ".jpg"):
                e["frame"] = os.path.relpath(stem + ".jpg", out)
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
    save(os.path.join(out, "hooks.json"), entries)
    ok = sum(1 for e in entries if e["spoken_status"] == "ok")
    print(f"  hooks: {len(entries)} posts, {ok} spoken hooks transcribed -> hooks.json", file=sys.stderr)
    for e in entries:
        if e["spoken_status"] != "ok" and not (e["spoken_status"] or "").startswith("skipped: "):
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
    if spoken:
        intro += (f" Spoken hook = whisper-cli (base.en) on the first {secs} seconds, machine transcript: "
                  "check it against the video before quoting.")
    if frames:
        intro += " Frame = the picture at 0.5 seconds, the visual hook."
    missing = [w for w, on in (("spoken hooks", spoken), ("first frames", frames)) if not on]
    if missing:
        intro += f" {' and '.join(missing).capitalize()} weren't pulled on this run."
    head = ["Post", "Role", "Lift", "Text hook"] + ([f"Spoken hook (first {secs} s)"] if spoken else []) \
        + (["Frame"] if frames else [])
    L = ["## Hooks: top posts vs bottom posts", intro, ""]
    for group in dict.fromkeys(e["group"] for e in entries):
        L += [f"**{group.title()}**", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for e in (x for x in entries if x["group"] == group):
            p = live[(e["platform"], e["id"])]
            text_hook = caption_hook(p.get("hook_text")) if p.get("hook_text") else e.get("text_hook")
            row = [f"[{fmt(p.get('views'))}]({e['url']})", e["role"], lift_txt(p), cell(text_hook)]
            if spoken:
                row.append(f'"{cell(e["spoken_hook"])}"' if e.get("spoken_hook") else f"_{cell(e.get('spoken_status'))}_")
            if frames:
                row.append(f"[frame]({e['frame']})" if e.get("frame") else "")
            L.append("| " + " | ".join(row) + " |")
        L.append("")
    return L


def comments(out, top, max_comments):
    """Comment text for the top posts: YouTube via yt-dlp, Instagram from the posts scrape."""
    all_posts, metas, _ = load_posts(out)
    targets = [t for t in pick_targets(build_groups(all_posts, metas), top, 0) if t[0] != "tiktok"]
    if not shutil.which(YTDLP) and any(t[0] == "youtube" for t in targets):
        print("  comments: yt-dlp not installed, so YouTube comments are left out", file=sys.stderr)
        targets = [t for t in targets if t[0] != "youtube"]
    inline = load(os.path.join(out, "comments_instagram_inline.json"), {})
    result = []
    for plat, group, _, p in targets:
        e = {"platform": plat, "group": group, "id": p["id"], "url": p["url"], "lift": p.get("lift"),
             "tier": p.get("tier"), "comment_count": p.get("comments"), "source": None, "comments": []}
        if plat == "youtube":
            try:
                d = ytdlp_json(p["url"], ["--write-comments", "--extractor-args",
                                          f"youtube:max_comments={max_comments},all,0,0;comment_sort=top"],
                               flat=False, timeout=180)
            except Exception as err:
                e["source"] = f"yt-dlp failed: {err}"
            else:
                e["source"] = "yt-dlp, top-sorted"
                # handles dropped on purpose; uploader replies flagged so they aren't read as audience
                e["comments"] = [{"text": c.get("text"), "likes": num(c.get("like_count")),
                                  "date": to_date(c), "reply": c.get("parent") != "root",
                                  "by_creator": bool(c.get("author_is_uploader"))}
                                 for c in d.get("comments") or [] if c.get("text")]
        else:
            e["source"] = "instagram posts scrape (newest comments only)"
            e["comments"] = inline.get(p["id"], [])
        result.append(e)
    save(os.path.join(out, "comments.json"), result)
    n = sum(len(e["comments"]) for e in result)
    print(f"  comments: {n} comments across {len(result)} posts -> comments.json "
          "(TikTok comments don't load logged out; see references/research-methods.md)", file=sys.stderr)


def load_posts(out, since=None):
    all_posts, metas = [], {}
    for plat in ("youtube", "tiktok", "instagram"):
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
    for plat in ("instagram", "tiktok", "youtube"):
        ps = [p for p in all_posts if p["platform"] == plat]
        if not ps:
            continue
        # followers per 1K views is per platform (all its posts in the window),
        # shown on the platform's first row only
        fp1k = followers_per_1k(ps, metas.get(plat))
        if plat == "youtube":
            subs = [(label, [p for p in ps if p["kind"] == kind])
                    for kind, label in (("short", "youtube shorts"), ("long", "youtube long-form"))]
        elif plat == "instagram":
            subs = [("instagram reels", [p for p in ps if p["kind"] == "reel"]),
                    ("instagram photos/carousels", [p for p in ps if p["kind"] != "reel"])]
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
            first = False
            groups.append((plat, g))
    return groups


def flags(p):
    f = [x for x, on in (("pinned", p.get("pinned")), ("fresh", is_fresh(p))) if on]
    return f" ({', '.join(f)})" if f else ""


def lift_txt(p):
    if p.get("lift") is None:
        return "n/a"
    return f"{p['lift']:.1f}x" if p["lift"] >= 0.95 else f"{p['lift']:.2f}x"


def report(out, name, since, note=None):
    all_posts, metas, undated_dropped = load_posts(out, since)
    if not all_posts:
        sys.exit("No posts found. Run collect / ingest-ig first.")
    groups = build_groups(all_posts, metas)

    with open(os.path.join(out, "posts.csv"), "w", newline="") as f:
        cols = ["platform", "group", "kind", "date", "date_approx", "views", "lift", "tier", "eng_lift",
                "likes", "comments", "shares", "saves", "duration_s", "pinned", "caption", "url", "id"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in sorted(all_posts, key=lambda p: (p["platform"], p.get("date") or ""), reverse=True):
            w.writerow(p)

    L = [f"# Social scan: {name or os.path.basename(os.path.abspath(out))}",
         f"Pulled {TODAY.isoformat()} from public, logged-out data. {len(all_posts)} posts"
         + (f" since {since}." if since else "."), ""]

    L += ["## Accounts", "| Platform | Followers | Following | Source |", "|---|---|---|---|"]
    for plat in ("instagram", "tiktok", "youtube"):
        m = metas.get(plat)
        if m:
            src = m.get("source", "") + (f", pulled {m['pulled']}" if m.get("pulled") else "")
            L.append(f"| {plat} | {fmt(m.get('followers'))} | {fmt(m.get('following'))} | {src} |")
    L.append("")

    L += ["## Scorecard",
          "| Group | Posts | Per week | Median views | Max | Top 3 share | Outliers 5x+ / 2-5x / 1.5-2x | Confidence | Like rate (n) | Comment rate | Followers per 1K views (proxy) |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, s in groups:
        rn = f" ({s['rate_n']})" if s["like_rate"] is not None else ""
        t = s.get("tiers")
        tiers = f"{t['huge']} / {t['strong']} / {t['mild']}" if t else "n/a"
        L.append(f"| {s['group']} | {s['posts']} | {s.get('per_week', 'n/a')} | {fmt(s.get('median'))} | "
                 f"{fmt(s.get('max'))} | {pct(s.get('top3_share'))} | {tiers} | {s['confidence']} ({s['baseline_n']}) | "
                 f"{pct(s['like_rate'])}{rn} | {pct(s['comment_rate'])} | {s.get('followers_per_1k') or 'n/a'} |")
    if undated_dropped:
        L.append(f"\n{undated_dropped} undated posts left out by --since (un-enriched YouTube Shorts).")
    L.append("")

    for _, s in groups:
        if not s.get("top"):
            continue
        t = s["tiers"]
        L += [f"## {s['group'].title()}",
              f"{s.get('first', '?')} to {s.get('last', '?')}. Baseline: median {fmt(s.get('median'))} views "
              f"across {s['baseline_n']} posts, confidence {s['confidence']}. Outliers: {t['huge']} at 5x+, "
              f"{t['strong']} at 2-5x, {t['mild']} at 1.5-2x; {len(s['duds'])} under 0.2x."]
        if s["fresh"]:
            L.append(f"{s['fresh']} posts are under 3 days old and still collecting views; they show a lift but stay out of the tier counts.")
        if s["pinned"]:
            L.append(f"{s['pinned']} pinned posts are listed for reference but sit outside the window: they stay out of "
                     "the dates, cadence, median and tier counts. Pinning keeps them in front of visitors, which explains part of their lift.")
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
        L += ["", "**Top posts** (lift = views / this group's median)",
              "| Date | Views | Lift | Tier | Eng. lift (likes+comments) | Likes | Opening line |",
              "|---|---|---|---|---|---|---|"]
        for p in s["top"]:
            el = f"{p['eng_lift']:.1f}x" if p.get("eng_lift") is not None else "n/a"
            L.append(f"| {p.get('date') or '?'}{flags(p)} | [{fmt(p['views'])}]({p['url']}) | {lift_txt(p)} | "
                     f"{p.get('tier') or ''} | {el} | {fmt(p.get('likes'))} | {p['caption']} |")
        L += ["", "**Bottom 5**", "| Date | Views | Lift | Likes | Opening line |", "|---|---|---|---|---|"]
        for p in s["bottom"]:
            L.append(f"| {p.get('date') or '?'}{flags(p)} | [{fmt(p['views'])}]({p['url']}) | {lift_txt(p)} | "
                     f"{fmt(p.get('likes'))} | {p['caption']} |")
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
          "- Median views leave out posts under 3 days old. Like rate (n) shows how many posts had like counts.",
          "- YouTube dates from flat listings are approximate unless enriched; Shorts without enrichment have no date.",
          "- Posts under 3 days old are still climbing. Don't compare them with older posts.",
          "- Instagram comes from the reels tab, the only feed that returns the newest posts in order when logged out. "
          "Photos and carousels aren't sampled. Pinned posts are shown but kept out of every window figure "
          "(reports before 2026-10-15 counted them in the median).",
          "- Reach, follows per post, retention and ad spend need the client's own analytics access."]

    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    slim = [{k: v for k, v in s.items() if k not in ("top", "bottom", "outliers", "duds")} for _, s in groups]
    lifts = [{k: p.get(k) for k in ("platform", "group", "id", "url", "date", "views", "lift", "tier", "eng_lift", "pinned")}
             for p in all_posts if p.get("lift") is not None]
    save(os.path.join(out, "summary.json"), {"pulled": TODAY.isoformat(), "name": name, "accounts": metas,
                                             "groups": slim, "posts": lifts})
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
    "ffmpeg": "first-frame images and spoken hooks",
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
    t0, notes, ran = time.time(), [], []

    def note(msg):  # collected for the recap at the end, so each one prints once
        notes.append(msg)

    todo = [(p, h) for p, h in (("youtube", a.youtube), ("tiktok", a.tiktok), ("instagram", a.instagram)) if h]
    if not todo and not load_posts(a.out)[0]:
        sys.exit("scan needs --youtube, --tiktok or --instagram (or an --out folder that already holds collected posts)")
    rp = os.path.join(a.out, "raw", "instagram_reels.json")
    for plat, h in todo:
        if plat != "instagram" and not shutil.which(YTDLP):
            note(f"{plat} skipped: yt-dlp isn't installed (`doctor` has the install line)")
            continue
        if plat == "instagram" and not os.environ.get("APIFY_TOKEN", "").strip():
            note("instagram skipped: APIFY_TOKEN isn't set in this shell (optional, paid per use through Apify). "
                 "Set it in your own terminal and run the scan there, or see SKILL.md step 3")
            continue
        ran.append(plat)
        try:  # one platform failing shouldn't sink the others
            if plat == "youtube":
                collect_youtube(h, a.out, a.limit, a.yt_enrich)
            elif plat == "tiktok":
                collect_tiktok(h, a.out, a.limit, quiet_followers=True)  # the recap prints the follow-up
            else:
                print(f"  instagram: Apify reels scrape, about $0.11 per 40 reels, capped at ${IG_MAX_USD:.2f} per call",
                      file=sys.stderr)
                collect_instagram(h, a.out, min(a.limit, 50))
        except Exception as e:
            msg = f"{plat} failed: {redact(e)}"
            pi = os.path.join(a.out, "posts_instagram.json")
            if plat == "instagram" and os.path.exists(rp) and os.path.getmtime(rp) >= t0 \
                    and not (os.path.exists(pi) and os.path.getmtime(pi) >= t0):
                msg += (". The reels were saved, so this recovers them without a second paid run: "
                        f"python3 {here()} ingest-ig --out {shlex.quote(a.out)} --reels {shlex.quote(rp)}, "
                        f"then: python3 {here()} scan --out {shlex.quote(a.out)}")
            note(msg)
    if a.tiktok_followers is not None:
        save(os.path.join(a.out, "meta_tiktok.json"), {"followers": a.tiktok_followers, "following": None,
                                                       "source": "tiktok profile page (scan --tiktok-followers)",
                                                       "pulled": TODAY.isoformat()})
    if todo:  # anything this run didn't refresh came from an earlier run: say so
        for plat in ("youtube", "tiktok", "instagram"):
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

    posts, metas, _ = load_posts(a.out)
    if not posts:
        for n in notes:
            print(f"  - {n}", file=sys.stderr)
        if not ran:
            hint = "The notes above say what was skipped; `doctor` shows what's missing."
        elif [p for p in ran if p != "instagram"]:
            hint = ("Check the errors above: usually a mistyped handle or a private account. If the handles are right, "
                    "update yt-dlp (`doctor` checks its age).")
        else:
            hint = ("Check the Instagram error above: 401 means APIFY_TOKEN is wrong, 402 means the Apify account is out "
                    "of credit; otherwise check the username.")
        sys.exit("Nothing was collected, so there's no report. " + hint)
    if not a.no_hooks:
        if not shutil.which(YTDLP) and not any(p["platform"] == "instagram" for p in posts):
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
        if not shutil.which(YTDLP) and not any(p["platform"] == "instagram" for p in posts):
            note("comments skipped: yt-dlp isn't installed")
        else:
            try:
                comments(a.out, 3, 100)
            except Exception as e:
                note(f"comments failed: {e}")
            else:
                cs = load(os.path.join(a.out, "comments.json"), []) or []
                bad = [c for c in cs if (c.get("source") or "").startswith("yt-dlp failed")]
                if bad:
                    note(f"comments: {len(bad)} of {len(cs)} posts didn't load (first: {bad[0]['source'][:120]})")
                if not shutil.which(YTDLP) and any(p["platform"] == "youtube" for p in posts):
                    note("comments: YouTube left out because yt-dlp isn't installed")
    prev = load(os.path.join(a.out, "summary.json"), {}) or {}
    name = a.name or (handle_of(todo[0][1]) if todo else None) or prev.get("name")
    path = report(a.out, name, None, note=note)

    print(f"\nscan finished in {time.time() - t0:.0f} s", file=sys.stderr)
    for plat in ("youtube", "tiktok", "instagram"):
        n = sum(1 for p in posts if p["platform"] == plat)
        if n:
            f = (metas.get(plat) or {}).get("followers")
            who = "subscribers" if plat == "youtube" else "followers"
            print(f"  {plat:<10} {n} posts" + (f", {fmt(f)} {who}" if f is not None else ""), file=sys.stderr)
    if any(p["platform"] == "tiktok" for p in posts) and "tiktok" not in metas:
        print("  tiktok followers aren't visible logged out. Read them off the profile page, then add them with:\n"
              f"    python3 {here()} scan --out {shlex.quote(a.out)} --tiktok-followers N --no-hooks --no-comments",
              file=sys.stderr)
    if notes:
        print("Notes:", file=sys.stderr)
        for n in notes:
            print(f"  - {n}", file=sys.stderr)
    print(path)


# ---------- cli ----------

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
    s.add_argument("--name", help="report title (default: the first handle)")
    s.add_argument("--tiktok-followers", type=int, help="follower count read off the TikTok profile (yt-dlp can't see it)")
    s.add_argument("--limit", type=int, default=60, help="posts per tab/profile (default 60; Instagram max 50)")
    s.add_argument("--yt-enrich", type=int, default=45,
                   help="fetch likes/comments/exact dates for the newest N YouTube uploads (default 45, 0 = off)")
    s.add_argument("--no-hooks", action="store_true", help="skip the hooks step")
    s.add_argument("--no-comments", action="store_true", help="skip the comments step")

    c = sub.add_parser("collect", help="collect posts only")
    c.add_argument("--out", required=True)
    c.add_argument("--youtube")
    c.add_argument("--tiktok")
    c.add_argument("--instagram", help="needs APIFY_TOKEN in the environment; reels tab, about $0.11 per 40 reels, capped at $0.30 per Apify call (two calls)")
    c.add_argument("--limit", type=int, default=60, help="posts per tab/profile (default 60)")
    c.add_argument("--yt-enrich", type=int, default=45,
                   help="fetch likes/comments/exact dates for the newest N YouTube uploads (default 45, 0 = off)")

    i = sub.add_parser("ingest-ig", help="load Instagram JSON saved from the Apify MCP")
    i.add_argument("--out", required=True)
    src = i.add_mutually_exclusive_group(required=True)
    src.add_argument("--reels", help=f"{IG_REELS_ACTOR} dataset JSON (contiguous newest reels)")
    src.add_argument("--posts", help="older apify/instagram-scraper posts-feed JSON; readable, but flagged unreliable")
    i.add_argument("--profile")

    m = sub.add_parser("meta", help="record a follower count by hand")
    m.add_argument("--out", required=True)
    m.add_argument("--platform", required=True, choices=["instagram", "tiktok", "youtube"])
    m.add_argument("--followers", type=int, required=True)
    m.add_argument("--following", type=int)
    m.add_argument("--source", default="manual")

    h = sub.add_parser("hooks", help="text, spoken and visual hooks for the top and bottom posts per group")
    h.add_argument("--out", required=True)
    h.add_argument("--top", type=int, default=3, help="top posts per group (default 3)")
    h.add_argument("--bottom", type=int, default=2, help="bottom posts per group, for contrast (default 2)")
    h.add_argument("--seconds", type=int, default=20, help="seconds of video to fetch per post (default 20)")

    k = sub.add_parser("comments", help="comment text for the top posts (YouTube, Instagram)")
    k.add_argument("--out", required=True)
    k.add_argument("--top", type=int, default=3, help="top posts per group (default 3)")
    k.add_argument("--max", type=int, default=100, help="YouTube comments per post (default 100)")

    r = sub.add_parser("report", help="write summary.md, summary.json and posts.csv")
    r.add_argument("--out", required=True)
    r.add_argument("--name")
    r.add_argument("--since")

    a = ap.parse_args()
    if a.cmd == "doctor":
        sys.exit(doctor(a.get_model))
    os.makedirs(a.out, exist_ok=True)
    if a.cmd == "scan":
        scan(a)
    elif a.cmd == "collect":
        if not (a.youtube or a.tiktok or a.instagram):
            sys.exit("collect needs at least one of --youtube, --tiktok, --instagram")
        for flag, fn in ((a.youtube, lambda: collect_youtube(a.youtube, a.out, a.limit, a.yt_enrich)),
                         (a.tiktok, lambda: collect_tiktok(a.tiktok, a.out, a.limit)),
                         (a.instagram, lambda: collect_instagram(a.instagram, a.out, min(a.limit, 50)))):
            if not flag:
                continue
            try:  # one platform failing shouldn't sink the others
                fn()
            except Exception as e:
                print(f"  FAILED: {redact(e)}", file=sys.stderr)
    elif a.cmd == "ingest-ig":
        ingest_ig(a.reels or a.posts, a.profile, a.out, feed="reels" if a.reels else "posts")
    elif a.cmd == "meta":
        save(os.path.join(a.out, f"meta_{a.platform}.json"),
             {"followers": a.followers, "following": a.following, "source": a.source, "pulled": TODAY.isoformat()})
    elif a.cmd == "hooks":
        hooks(a.out, a.top, a.bottom, a.seconds)
        print("  run `report` to add the Hooks section to summary.md", file=sys.stderr)
    elif a.cmd == "comments":
        comments(a.out, a.top, a.max)
    elif a.cmd == "report":
        print(report(a.out, a.name, a.since))


if __name__ == "__main__":
    main()
