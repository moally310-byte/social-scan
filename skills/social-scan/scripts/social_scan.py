#!/usr/bin/env python3
"""social-scan: pull public social stats and turn them into a one-page audit.

Backends
  YouTube, TikTok  -> yt-dlp, run here (logged out, no cookies)
  Instagram        -> Apify REST (`collect --instagram`, APIFY_TOKEN from the
                      environment) or Apify MCP dataset JSON via `ingest-ig`

Commands
  collect    --out DIR [--youtube H] [--tiktok H] [--instagram H] [--limit N] [--yt-enrich N]
  ingest-ig  --out DIR (--reels reels.json | --posts posts.json) [--profile profile.json]
  meta       --out DIR --platform P --followers N [--following N] [--source TEXT]
  hooks      --out DIR [--top N] [--bottom N] [--seconds S]
  comments   --out DIR [--top N] [--max N]
  report     --out DIR [--name NAME] [--since YYYY-MM-DD]

Outlier lift tiers and the hooks pass are adapted from ScrapeCreators'
social-media-research-skills @ 64ba7b4 (MIT): outlier-post-finder and
transcript-intelligence, rewired to yt-dlp, Apify and local whisper-cli.

Everything is read-only. Never add --cookies or --cookies-from-browser.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

YTDLP = os.environ.get("YTDLP", "yt-dlp")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
WHISPER = os.environ.get("WHISPER_CLI", "whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", os.path.expanduser("~/.local/share/whisper-cpp/ggml-base.en.bin"))
TODAY = dt.date.today()

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
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "yt-dlp failed")
    return json.loads(p.stdout)


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
    posts, followers = [], None
    for tab, kind in (("videos", "long"), ("shorts", "short")):
        try:
            d = ytdlp_json(f"{base}/{tab}", ["--playlist-end", str(limit),
                                              "--extractor-args", "youtubetab:approximate_date"])
        except RuntimeError as e:
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
    print(f"  youtube: {len(posts)} posts, {followers} subscribers", file=sys.stderr)


def collect_tiktok(handle, out, limit):
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
    print(f"  tiktok: {len(posts)} posts (followers not available from yt-dlp; use `meta`)", file=sys.stderr)


def apify_items(actor, payload, max_items, max_usd=IG_MAX_USD):
    """Run an Apify actor synchronously and return its dataset items.

    The token comes only from the APIFY_TOKEN environment variable, sent as a
    header. It is never printed, logged, or put in the URL. maxItems caps
    pay-per-result actors, maxTotalChargeUsd caps pay-per-event ones.
    """
    import urllib.request
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set (see SKILL.md, Instagram section)")
    url = (f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
           f"?timeout=240&maxItems={int(max_items)}&maxTotalChargeUsd={max_usd}&clean=true")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def collect_instagram(handle, out, limit):
    h = handle_of(handle)
    # the reels tab, not the posts feed: see IG_REELS_ACTOR. Photos and
    # carousels have no contiguous logged-out source, so they're left out.
    reels = apify_items(IG_REELS_ACTOR, {"username": [h], "resultsLimit": limit}, limit)
    rp, pr = os.path.join(out, "raw", "instagram_reels.json"), os.path.join(out, "raw", "instagram_profile.json")
    save(rp, reels)  # saved first, so a profile failure doesn't lose a paid scrape
    # no retries: a retried run-sync call starts (and bills) a second run
    profile = apify_items("apify/instagram-profile-scraper", {"usernames": [h]}, 1)
    save(pr, profile)
    ingest_ig(rp, pr, out, feed="reels")


def ingest_ig(posts_path, profile_path, out, feed="reels"):
    """feed: "reels" for the reels-tab scrape (contiguous), "posts" for the
    older posts-feed scrape, which is kept readable for past runs but flagged."""
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


def spoken_hook(wav, stem):
    """whisper-cli on the clip; keep confident speech segments that start inside the hook window."""
    if not os.path.exists(WHISPER_MODEL):
        return None, f"whisper model missing at {WHISPER_MODEL}"
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
        clip, wav = stem + ".mp4", stem + ".wav"
        err = fetch_clip(plat, p, seconds, clip)
        if err:
            e["spoken_status"] = err
            return e
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.5", "-i", clip, "-frames:v", "1",
                        "-vf", "scale=360:-2", stem + ".jpg"], capture_output=True, timeout=60)
        if os.path.exists(stem + ".jpg"):
            e["frame"] = os.path.relpath(stem + ".jpg", out)
        for src in (clip, p.get("audio_url")):
            if src and subprocess.run([FFMPEG, "-v", "error", "-y", "-t", str(seconds), "-i", src, "-vn",
                                       "-ar", "16000", "-ac", "1", wav], capture_output=True, timeout=90).returncode == 0:
                break
        e["spoken_hook"], e["spoken_status"] = spoken_hook(wav, stem + ".whisper") if os.path.exists(wav) \
            else (None, "no audio track")
        for f in (clip, wav):  # keep the frame and transcript as receipts, not the client's video
            if os.path.exists(f):
                os.remove(f)
        return e

    with ThreadPoolExecutor(max_workers=3) as ex:
        entries = list(ex.map(one, targets))
    save(os.path.join(out, "hooks.json"), entries)
    ok = sum(1 for e in entries if e["spoken_status"] == "ok")
    print(f"  hooks: {len(entries)} posts, {ok} spoken hooks transcribed; rerun `report` to add the Hooks section",
          file=sys.stderr)
    for e in entries:
        if e["spoken_status"] != "ok":
            print(f"    {e['platform']} {e['id']}: {e['spoken_status']}", file=sys.stderr)


def hooks_section(out, all_posts):
    entries = load(os.path.join(out, "hooks.json"))
    if not entries:
        return []
    live = {(p["platform"], p["id"]): p for p in all_posts}
    L = ["## Hooks: top posts vs bottom posts",
         "Text hook = caption or title first line, verbatim. Spoken hook = whisper-cli (base.en) on the first "
         f"{HOOK_WINDOW_MS // 1000} seconds, machine transcript: check it against the video before quoting. "
         "Frame = the picture at 0.5 seconds, the visual hook.", ""]
    for group in dict.fromkeys(e["group"] for e in entries):
        L += [f"**{group.title()}**", "| Post | Role | Lift | Text hook | Spoken hook (first 10 s) | Frame |",
              "|---|---|---|---|---|---|"]
        for e in (x for x in entries if x["group"] == group):
            p = live.get((e["platform"], e["id"]), e)
            text_hook = caption_hook(p.get("hook_text")) if p.get("hook_text") else e.get("text_hook")
            spoken = f'"{cell(e["spoken_hook"])}"' if e.get("spoken_hook") else f"_{cell(e.get('spoken_status'))}_"
            frame = f"[frame]({e['frame']})" if e.get("frame") else ""
            L.append(f"| [{fmt(p.get('views'))}]({e['url']}) | {e['role']} | {lift_txt(p)} | "
                     f"{cell(text_hook)} | {spoken} | {frame} |")
        L.append("")
    return L


def comments(out, top, max_comments):
    """Comment text for the top posts: YouTube via yt-dlp, Instagram from the posts scrape."""
    all_posts, metas, _ = load_posts(out)
    targets = [t for t in pick_targets(build_groups(all_posts, metas), top, 0) if t[0] != "tiktok"]
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


def report(out, name, since):
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
            L.append(f"| {plat} | {fmt(m.get('followers'))} | {fmt(m.get('following'))} | {m.get('source', '')} |")
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
    print(os.path.join(out, "summary.md"))


# ---------- cli ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--out", required=True)
    c.add_argument("--youtube")
    c.add_argument("--tiktok")
    c.add_argument("--instagram", help="needs APIFY_TOKEN in the environment; reels tab, about $0.11 per 40 reels, capped at $0.30 per Apify call (two calls)")
    c.add_argument("--limit", type=int, default=60, help="posts per tab/profile (default 60)")
    c.add_argument("--yt-enrich", type=int, default=45,
                   help="fetch likes/comments/exact dates for the newest N YouTube uploads (default 45, 0 = off)")

    i = sub.add_parser("ingest-ig")
    i.add_argument("--out", required=True)
    src = i.add_mutually_exclusive_group(required=True)
    src.add_argument("--reels", help=f"{IG_REELS_ACTOR} dataset JSON (contiguous newest reels)")
    src.add_argument("--posts", help="older apify/instagram-scraper posts-feed JSON; readable, but flagged unreliable")
    i.add_argument("--profile")

    m = sub.add_parser("meta")
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

    r = sub.add_parser("report")
    r.add_argument("--out", required=True)
    r.add_argument("--name")
    r.add_argument("--since")

    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.cmd == "collect":
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
                print(f"  FAILED: {e}", file=sys.stderr)
    elif a.cmd == "ingest-ig":
        ingest_ig(a.reels or a.posts, a.profile, a.out, feed="reels" if a.reels else "posts")
    elif a.cmd == "meta":
        save(os.path.join(a.out, f"meta_{a.platform}.json"),
             {"followers": a.followers, "following": a.following, "source": a.source, "pulled": TODAY.isoformat()})
    elif a.cmd == "hooks":
        hooks(a.out, a.top, a.bottom, a.seconds)
    elif a.cmd == "comments":
        comments(a.out, a.top, a.max)
    elif a.cmd == "report":
        report(a.out, a.name, a.since)


if __name__ == "__main__":
    main()
