# social-scan

**A Claude Code skill that audits any creator's or brand's public Instagram, TikTok, YouTube and Snapchat, plus where their podcast ranks, in about a minute.** Hand it a handle and it finds the posts that broke out, shows the hooks that made them work, and tells you how much of the account rides on a few hits. No logins, no cookies, no paid analytics seat.

<p align="center"><img src="assets/demo.gif" width="480" alt="social-scan demo: scanning MrBeast's public TikTok and YouTube from Claude Code, through the outlier chart, to the full report and install command"></p>

## What you get

You ask Claude: *"scan @duolingo's TikTok and YouTube."* About a minute later (52 seconds on this run; Instagram adds roughly 25 seconds) you get a report like this (real `scan` run with its defaults, public data, 2026-10-16):

| Group | Posts | Per week | Median views | Max | Top 3 share | Outliers 5x+ / 2-5x / 1.5-2x |
|---|---|---|---|---|---|---|
| TikTok videos | 54 | 5.6 | 870.1K | 18.00M | 36.9% | 5 / 10 / 3 |
| TikTok photos | 6 | 0.6 | 634.5K | 1.50M | 70.4% | 0 / 1 / 0 |
| YouTube Shorts | 60 | 5.8 | 999.0K | 4.90M | 14.7% | 0 / 10 / 6 |
| YouTube long-form | 60 | 0.6 (15 exact) | 96.1K | 3.30M | 39.5% | 9 / 3 / 4 |

Plus, for each platform, where its public data allows:

- **Outliers by lift.** Each post's views against that account's own median, sorted into huge (5x+), strong (2-5x) and mild (1.5-2x). Duolingo's top TikTok video ran 20.7x its median.
- **Hooks, three ways.** For the top 3 and bottom 2 posts: the caption's opening line, the first 10 seconds of speech (transcribed locally with whisper.cpp; Snapchat Spotlights use Snap's own transcript), and the frame at 0.5 seconds (a photo post's image).
- **Comment mining.** Top YouTube comments, the newest Instagram comments, Snapchat's top comments and, with `--tiktok-comments`, TikTok's, handles stripped and the creator's own comments flagged on YouTube, Snapchat and TikTok, ready to cluster into themes.
- **Snapchat Spotlights.** Views, likes, shares and top comments for the newest Spotlights the profile lists, which ones are long enough to earn (30 s+), upload bursts, missing descriptions and hashtags, and Snap's own title, keywords and content tags for the most-liked ones.
- **Podcast standing.** Where the show ranks on Apple's and Spotify's US charts for its own categories, its US Apple rating with Apple's own split of those ratings by stars, a sample of Apple reviews for comment mining, and how often and how long it publishes, read from its own RSS feed.
- **The numbers people skip.** Posting cadence, top-3 share (does the account live on a few hits?), followers per 1K views (not for Snapchat, which lists too few Spotlights and hides some views), and a flag when a gap in the data means a median can't be trusted.
- **What formats work.** For each video group, views by length, weekday and 3-hour posting block (in your time zone, or `--tz`), each row with its post count, median lift and share at 2x+. Rows under 5 posts (and weekday rows from fewer than 3 dates) are marked thin. A row is named strongest only when it leads by 1.5x and random splits of the same posts rarely match it, and when a winning day or time owes its lead to the winning length, the report says so. TikTok groups also get their share and save rates.
- **Compare accounts.** `compare` puts several accounts' scans in one table: window, cadence, median, outlier rate, top-3 share and followers per 1K per format, with `--days` to make every account cover the same dates.
- **Since last scan.** Scan the same account again a month later and the report adds what changed: followers, each group's median, cadence and 2x+ count, the posts published since, the posts still climbing, the ones that became outliers, and the podcast's chart moves. It's a table you can paste into a client check-in.
- **A brief.** Claude reads the report and writes what worked, why it likely worked, and three content tests to run next.

Everything lands in the `--out` folder you give it, one per account: `summary.md`, `summary.json`, `posts.csv`, `hooks.json`, `comments.json`, `podcast.json` and `reviews_apple.json` for a podcast, the hook frames and the raw scrape files.

## Install

In Claude Code:

```
/plugin marketplace add moally310-byte/social-scan
/plugin install social-scan@social-scan
```

Or copy the skill folder by hand:

```bash
git clone https://github.com/moally310-byte/social-scan
cp -r social-scan/skills/social-scan ~/.claude/skills/
```

### Check your machine

Point `SCAN` at the script for your install, then run `doctor` (or ask Claude: *"is social-scan set up?"*).

Manual install:

```bash
SCAN=~/.claude/skills/social-scan/scripts/social_scan.py
python3 "$SCAN" doctor
```

Plugin install (the most recently installed version):

```bash
SCAN=$(ls -dt ~/.claude/plugins/cache/social-scan/social-scan/*/skills/social-scan/scripts/social_scan.py | head -1)
python3 "$SCAN" doctor
```

`doctor` checks each piece, says what a missing one turns off, and prints the install line for macOS and Linux with the command last on the line, so it pastes clean:

```
social-scan doctor

  PASS     python         3.14.8 at /opt/homebrew/bin/python3
  PASS     yt-dlp         2026.08.19 at /opt/homebrew/bin/yt-dlp
  MISSING  ffmpeg         not installed (optional)
                          turns off: first-frame images and spoken hooks
                          macOS: brew install ffmpeg
                          Linux: sudo apt install ffmpeg
  ...

YouTube and TikTok: ready.
Hooks: caption's first line only; no first frame, no spoken hook.
Snapchat: ready (free, logged out; the newest Spotlights the profile page lists).
Podcast: ready (free: Apple and Spotify charts, Apple rating and reviews, the show's RSS feed).
Instagram: off (optional; paid per use through Apify, about $0.11 per 40 reels; set APIFY_TOKEN to turn it on).
```

It exits 0 when YouTube and TikTok can run, which takes Python 3.8+ and [`yt-dlp`](https://github.com/yt-dlp/yt-dlp). On macOS, Homebrew's yt-dlp brings its own Python; on Linux, `doctor` prints yt-dlp's standalone build, which does too (the plain release needs Python 3.10+). Everything else is optional and adds to the report:

| Piece | Adds | Without it |
|---|---|---|
| `ffmpeg` | the frame at 0.5 seconds of each top and bottom post | hooks keep the caption opening only (Spotlights keep Snap's transcript; photo posts keep their image) |
| `whisper-cli` ([whisper.cpp](https://github.com/ggml-org/whisper.cpp)) and the `base.en` model | the first 10 seconds of speech, transcribed on your machine | hooks keep the caption and the frame (Spotlights keep Snap's transcript) |
| `deno` | the JavaScript runtime yt-dlp recommends for YouTube | works for now; yt-dlp warns some YouTube formats may be missing |
| `APIFY_TOKEN` ([Apify](https://apify.com) account) | Instagram, paid per use | YouTube, TikTok and Snapchat only |

Snapchat and podcasts need nothing extra: they read public pages and feeds, free and logged out.

The whisper model (148 MB) never downloads on its own. `doctor --get-model` fetches `ggml-base.en.bin` to `~/.local/share/whisper-cpp/` (or to `WHISPER_MODEL`, if that path ends in the same file name) and checks its sha256 before saving it. On a Mac the whole set is:

```bash
brew install yt-dlp ffmpeg whisper.cpp
python3 "$SCAN" doctor --get-model
```

(Homebrew's yt-dlp pulls in deno.) Set `APIFY_TOKEN` in your own terminal; the skill never reads it from anywhere else and never prints it. Tools come from `PATH`; `YTDLP`, `FFMPEG`, `WHISPER_CLI` and `WHISPER_MODEL` point the script at others.

## Use it

Just ask Claude:

- "Scan @handle's socials before my pitch call."
- "What hooks are working for this creator on TikTok?"
- "Find their outlier reels."
- "Mine the comments on their top YouTube videos."

Or run it yourself. One command collects the posts, pulls the hooks and comments, and writes the report:

```bash
python3 "$SCAN" scan --out "./scans/duolingo/$(date +%F)" --youtube @duolingo --tiktok duolingo --name "Duolingo"
```

- `--days 30` collects the 30 calendar days ending on the collect day instead of a post count, up to 300 posts per tab. TikTok pages its listing until it passes the cutoff. YouTube looks up every post in the window to date it (Shorts have no date in the listing), about a second each, six at a time, whatever `--yt-enrich` says; a post it can't date is left out and counted, and if YouTube stops answering, the window is marked short. Instagram (paid, capped at `--limit`) and Snapchat (no paging logged out) are trimmed to the window, and the report says where a platform falls short of it. Use it whenever accounts will be compared: the same `--limit` covers 16 days on an account that posts 26 times a week and six months on one that posts twice.
- `--tz America/New_York` sets the zone for the weekday and posting-time tables (default: this computer's). Zone names need Python 3.9+; an offset like `UTC-05:00` (or `-05:00`) works on any version.
- `--yt-enrich 45` (the default) looks up the newest 45 YouTube uploads for exact dates, likes and posting times, plus the top 10 of each kind by views, so the top-posts tables show their dates. Those extra lookups count as listed everywhere sampled (view counts, cadence, rates, `--since`, every format table), so a few older hits can't tilt anything; `top_lookup` in `posts.csv` marks them. `0` turns every lookup off.
- `compare DIR DIR ...` puts one scan folder per account into a single table, each measured as of its collect day. A format one account doesn't have gets a "none" row (or "not collected" for a whole platform). Where an account's window is more than 1.5 times off the one the others share, or no two accounts share one, its median is left out; followers per 1K, which scales with the window's length, shows only for accounts scanned with the same `--days`. Outlier rate and cadence are each account's own and still compare. Short-window and all-fresh rows are marked, and scans collected days apart are flagged. `--out FILE` also writes it to a file.
- Use a new `--out` folder for each account and run, dated: `./scans/duolingo/2026-10-16`. `scan` reports everything already in the folder, and its notes say when posts or follower counts came from an earlier run.
- **Since last scan:** when `--out` is a dated folder, `scan` compares it with the newest earlier dated folder beside it that holds a report, and adds a "Since last scan" section. `--prev DIR` picks another earlier scan, and `--no-prev` leaves the section out. `diff OLD NEW` prints the same section for any two scan folders. The "then" numbers are rebuilt from the earlier folder's own posts as of the day it was collected. The section flags a group whose date window is more than 1.5 times longer in one scan (the same `--limit` covers a different range at a different cadence), names any platform collected in only one of the two scans instead of counting its posts as gone, ranks gains on rounded counts by what they are at least and marks the rough ones with ~, and counts a post as a new outlier only when its own views crossed 2x the earlier median and it's 2x+ now.
- **Snapchat history:** logged out, Snapchat lists only the newest Spotlights, so every Snapchat collect also merges what it saw into `posts_snapchat_history.json` (first and last seen, and the counts at each pull), carried forward from the newest earlier dated folder that has one. The report lists the most-viewed Spotlights that have scrolled off the profile.
- `--snapchat handle` adds Snapchat Spotlights (free, logged out) from a Public Profile. Snapchat lists only the newest Spotlights logged out (3 to 26 on accounts tested), with no paging, and hides view counts on its weakest ones: the report ranks those below every visible count, and when half or more are hidden it gives the median as "under" the lowest visible count and ranks the tables by likes.
- `--tiktok-comments` adds comments on the top 3 posts of each TikTok group (up to 100 each), read logged out through TikTok's unofficial web comment list: top-level comments with text, likes, reply count, whether the creator liked it, language and date, the creator's own comments flagged, and mentions and handles masked. Reply threads aren't read. It's off by default because the endpoint is undocumented and can stop answering; when it does, the notes say so and the rest of the scan carries on.
- `--instagram username` adds Instagram once `APIFY_TOKEN` is set.
- `--podcast "Show name"` adds a Podcast block, and works on its own too:
  - Apple's US chart rank in each of the show's categories and overall (top 200). Spotify's US rank and move in Top Podcasts and Trending (top 200) and in the show's category (top 50).
  - The Apple rating and ratings count, Apple's split of those ratings by stars, and a sample of Apple reviews, all from the US store. Apple keeps ratings per country, so a show based elsewhere can have far more at home. A show nobody has rated yet says so.
  - Episodes per week and median length over the 8 weeks before the newest episode, from the show's RSS feed. When the feed covers less than that (a new show, or a feed that keeps only its newest episodes, pinned trailers aside), the rate covers the weeks it does span; under two weeks of episodes gets no weekly rate. Items dated in the future stay out.
  - A name that matches no show exactly takes Apple's first search result, and a name several shows share takes the first of them. Either way the notes and the report give the Apple IDs: pass the right one (the digits after `id` in the show's own podcasts.apple.com link, not the publisher's; the link itself works too) to pin the show.
  - Spotify rows are matched by show name, ignoring a leading "The", so a show Spotify lists under another name reads as not in its charts.
- TikTok followers come from the public profile page; `--tiktok-followers N` overrides the count. A mistyped TikTok or Snapchat handle is named in the notes at the end ("check the handle") and that platform is skipped; the others still run.
- TikTok photo carousels get their own group (`tiktok photos`, next to `tiktok videos`): their listed length is the song's, and their first slide is the visual hook.
- A step whose tool is missing is skipped, the report still gets written, and the notes at the end say what was left out. The last line printed is the path to `summary.md`.

The single steps are still there for re-running one part, or for loading Instagram data pulled through the Apify MCP: `collect`, `ingest-ig`, `meta`, `hooks`, `comments`, `report`, `diff OLD NEW` and `compare DIR ...`. `python3 "$SCAN" <step> --help` shows each one's flags.

## Cost

YouTube, TikTok, Snapchat and podcasts are free. Instagram runs through Apify at about $0.11 for 40 reels, with a hard $0.30 cap on each Apify call built into the script (an Instagram run makes two calls: reels, then the profile).

## What it won't do

- **No logins.** Logged-out public data only. The script never passes cookies, runs yt-dlp with `--ignore-config` so a cookie setting can't sneak in from your own config, and never asks for anyone's password.
- **No private metrics.** Reach, retention, follows per post, traffic sources and ad spend only exist in the account owner's analytics. The skill ends with a checklist of what access to ask for.
- **Some blind spots.** TikTok comments come only with `--tiktok-comments`, through TikTok's unofficial web comment list, which can stop answering at any time (TikTok has required signed requests on other endpoints). Instagram samples the reels tab only (photos and carousels aren't counted). Snapchat lists only its newest Spotlights logged out and hides some view counts; saves, watch time and earnings need the account's own Insights. Podcast downloads and listens aren't public, so the Podcast block shows standing (charts, ratings, cadence), not audience size; "not in a chart" only means outside its top 200 or top 50. Apple's review feed serves some pages empty, sometimes all of them: the script reads past a stray empty page and says when pages were missing (adding the show page's reviews when the newest page is the one missing), and when none answer it uses the handful of reviews Apple shows on the show page and says so. LinkedIn, X, Threads and Facebook aren't covered yet.
- **Views aren't people.** Plays count replays, so treat views as plays, not unique viewers.

## Use it responsibly

This reads public, logged-out data only, but all four social platforms' terms restrict automated access, so how you use it is on you. The script's own requests (TikTok and Snapchat pages, and every podcast source) send a standard desktop-browser User-Agent, as yt-dlp's listings always have. Snapchat's robots.txt allows the profile and Spotlight paths it reads; TikTok's robots.txt disallows AI agents, Claude-User included, site-wide. A scan reads the TikTok profile page, and yt-dlp lists the posts through TikTok's own post-list API. The script itself opens only the photo carousels' pages (videos are told apart from the listing), plus any post the listing gives no thumbnail. The hooks step has yt-dlp fetch the first seconds of the top 3 and bottom 2 TikTok videos, which opens those video pages; `--no-hooks` skips them. With `--tiktok-comments`, the script also reads TikTok's web comment list (`/api/comment/list/`), up to 10 pages for each of the top 3 posts per TikTok group, half a second apart. Comments are other people's words: the script strips handles, so keep it that way. The hook frames in `hooks/` are the account's own footage: keep them in your report folder and don't republish them.

For a podcast, a scan makes a dozen or so single requests: Apple's Search API (or its lookup by ID), Apple's chart and review RSS feeds, the show's Apple Podcasts page, Spotify's public podcast charts and the show's own RSS feed. itunes.apple.com's robots.txt disallows the search and RSS paths for crawlers (it lists lookups only under a storefront path such as /us/lookup, not the bare /lookup the script uses), and the script reads those paths anyway, with the same browser User-Agent. The show pages on podcasts.apple.com are allowed, and podcastcharts.byspotify.com sets no rules. Reviews are saved with their rating, title, text and date, never the reviewer's name.

## Status

- `doctor` and `scan` are tested on macOS (Python 3.8, 3.9 and 3.14; yt-dlp 2026.08.19, ffmpeg 8.1, whisper.cpp 1.9.1, deno 2.9.7): full runs, runs with ffmpeg, whisper-cli or yt-dlp taken off `PATH`, a mistyped handle, a reused `--out` folder, and `doctor --get-model` (a real download, plus simulated cut-off, bad-checksum, network and Ctrl-C failures).
- The Linux install lines follow each project's own docs (yt-dlp's standalone build, Ubuntu 26.04's `whisper.cpp` package or whisper.cpp's CMake build, apt for ffmpeg) but haven't been run on a Linux machine yet. If one trips, open an issue.
- Snapchat Spotlights, TikTok photo carousels and TikTok follower counts are live-tested on public accounts, logged out (1.2.0).
- The format tables, exact posting times and top-post lookups (1.7.0) are live-tested on a creator's TikTok (3-10 minute clips at 4.2x the median, 10 of 13 at 2x+; a 6.1x Saturday that came from two dates, mostly those clips, reads as thin rather than a finding) and on Duolingo's YouTube and TikTok (15 older top posts looked up, 22 s).
- `--tiktok-comments` (1.6.0) is live-tested on a creator's six top TikTok posts: all six loaded, 395 comments in 16 s, the creator's own comments flagged.
- `--days` and `compare` (1.5.0) are live-tested on a creator's TikTok and Snapchat (110 TikToks in 30 days, 29 s) and Duolingo's YouTube and TikTok (every Short in the window dated, 21 s), compared with each other and with a scan by post count.
- Since last scan, `diff` and the Snapchat history (1.4.0) are live-tested on a creator's Snapchat plus podcast and on Duolingo's YouTube and TikTok, each against an earlier scan folder, plus offline tests for finding the earlier folder, new versus older posts, different windows, older summary files and the history merge.
- The Podcast block (1.3.0) is live-tested on four shows, from one with 7 ratings to Apple's #1, by name, Apple ID and pasted show link, on its own and alongside a Snapchat scan, plus a name with no match. Offline tests cover failed and malformed sources, the review fallback, shared, non-Latin and all-digit names, young and future-dated feeds, feeds with an inline DTD, and cut-off downloads.
- YouTube, TikTok and Instagram (through the Apify MCP) are live-tested. The direct Instagram call (`--instagram`) is tested against a mocked Apify response; if your first live run trips on something, open an issue.

## Credits

The outlier tiers, hook breakdown, comment-mining method and brief template are adapted from ScrapeCreators' [social-media-research-skills](https://github.com/ScrapeCreators/social-media-research-skills) (MIT). Their skills run on the paid ScrapeCreators API; this one rewires the methods to yt-dlp, Apify and local whisper.cpp. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT. Built by [Mo Ally](https://github.com/moally310-byte) with Claude Code.
