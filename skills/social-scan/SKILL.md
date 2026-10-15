---
name: social-scan
description: Pull PUBLIC Instagram, TikTok and YouTube numbers for any account (per-post views, likes, comments, dates, follower counts) and turn them into a one-page audit with medians, outlier lift tiers, top and bottom posts with their caption, spoken and first-frame hooks, top-post comments, cadence and followers per 1K views. Use for "scan their socials," "pull their Instagram/TikTok/YouTube numbers," "audit this creator's or brand's social," "how are their reels doing," "find their outlier or viral posts," "what hooks are working for them," "mine their comments," "social audit before my pitch call," "compare these accounts," or prep for a social-media client. Logged-out data only, via yt-dlp, Apify and local whisper. NOT native analytics (reach, retention and ad spend need the account owner's access).
---

# social-scan

One command per platform, one report.

Script: `scripts/social_scan.py` in this skill's folder (written `SCAN` below; use its full path). Python 3, standard library only. Needs `yt-dlp`. The `hooks` step also needs `ffmpeg`, `whisper-cli` (whisper.cpp) and a model file, by default `~/.local/share/whisper-cpp/ggml-base.en.bin` (override with `WHISPER_MODEL`). Instagram needs an Apify account. Outlier tiers, hooks, comment mining and the brief are adapted from ScrapeCreators' social-media-research-skills (MIT); method, attribution and gaps in `references/research-methods.md`.

## Rules

- **Logged-out data only.** Never pass `--cookies` or `--cookies-from-browser`, never log in to anyone's account to scrape, never use Instaloader with a login. (Platform terms and account-ban risk.)
- **The Apify token comes only from the `APIFY_TOKEN` environment variable** the user sets in their own terminal. Never read it out of a keychain or config file yourself, never print it, never put it in a URL.
- **Cap every Apify run** (`--limit`, plus `maxItems` for pay-per-result actors or `maxTotalChargeUsd` for pay-per-event ones; the script sends both, at $0.30 per Apify call; an Instagram run makes two calls). A normal audit costs about $1 to $3; YouTube and TikTok are free.
- **Save each run to `<project>/social-scan/<YYYY-MM-DD>/`.** Keep the raw files; they're the receipts.

## Steps

1. **Get handles.** YouTube `@handle`, TikTok handle, Instagram username. The account's website footer usually links all three.
2. **YouTube and TikTok** (free, about 25 seconds):
   ```bash
   python3 SCAN collect --out <DIR> --youtube @Handle --tiktok handle
   ```
   `--limit 60` posts per tab by default. `--yt-enrich 45` fetches likes, comments and exact dates for the newest 45 uploads (Shorts have no dates without it).
3. **TikTok followers.** yt-dlp can't see them. Open the profile in a browser, read "Followers", then record it:
   ```bash
   python3 SCAN meta --out <DIR> --platform tiktok --followers 12400 --following 310 --source "tiktok profile page"
   ```
4. **Instagram** (reels tab, newest N in order; about $0.11 for 40). Pick whichever path works:
   - **Direct (preferred, scriptable).** Ask the user to run it in their own terminal, so their token never passes through the chat:
     ```bash
     APIFY_TOKEN=<their token> python3 SCAN collect --out <DIR> --instagram username --limit 40
     ```
   - **Apify MCP.** Call `apify/instagram-reel-scraper` with `{"username":["<user>"],"resultsLimit":40}` and `callOptions.maxTotalChargeUsd: 0.30` (it bills per event, so `maxItems` doesn't cap it), and `apify/instagram-profile-scraper` with `{"usernames":["<user>"]}`. Use `waitSecs: 0`, then poll `get-actor-run`. Save each `get-dataset-items` result as a JSON array, then:
     ```bash
     python3 SCAN ingest-ig --out <DIR> --reels raw/instagram_reels.json --profile raw/instagram_profile.json
     ```
     If an MCP call stalls, stop retrying and switch to the direct path. A large `get-dataset-items` result (about 430 KB for 40 reels) may come back as a saved tool-result file: extract `items` from that file with Python into `raw/instagram_reels.json`. Never retype the JSON by hand; the signed video links must stay byte-exact. The Apify dataset API refuses token-free reads.
   - **Fallback.** The logged-out Instagram page shows the follower count only. Record it with `meta --platform instagram`.
   - **Why the reels tab, not the posts feed.** Logged out, the posts feed (`apify/instagram-scraper` with `resultsType: posts`, and `apify/instagram-post-scraper`) returns the grid's first 12 (pinned plus newest), then a scattered set of older posts. On one test account it skipped 29 live posts across ten weeks and inflated the reel median about fifteenfold. `apify/instagram-reel-scraper` returned the newest reels with nothing missing. Photos and carousels aren't sampled. `ingest-ig --posts` still reads old posts-feed files and marks them **Unreliable sample**. If a report says **Check the profile**, a gap of 7+ days broke a regular cadence: confirm on the profile before quoting that group's median. On each new account, check once that the newest 9 non-pinned reels match the logged-out grid (the `/reel/` links on the profile page). If the grid is mostly photos or carousels, say in the brief that the reels median doesn't represent the account.
5. **Hooks** (about 45 seconds, free). Run it straight after collection: Instagram video links expire within hours.
   ```bash
   python3 SCAN hooks --out <DIR>
   ```
   Top 3 and bottom 2 posts per group: caption opening, the first 10 seconds of speech (whisper-cli, approximate), and the frame at 0.5 s into `hooks/`. Writes `hooks.json`.
6. **Comments** (about 10 seconds, free): `python3 SCAN comments --out <DIR>`. YouTube top comments via yt-dlp; Instagram's newest ~15 per post come with the reels scrape. TikTok comments don't load logged out. Writes `comments.json`, handles dropped.
7. **Report:**
   ```bash
   python3 SCAN report --out <DIR> --name "Account Name" [--since YYYY-MM-DD]
   ```
   Writes `summary.md` (accounts; scorecard with outlier tiers and confidence; per group the top posts with lift, tier and engagement lift, the bottom 5; the Hooks section when `hooks.json` exists), `summary.json` (with per-post lift) and `posts.csv`.
8. **Write the brief.** Read the report and its caveats, look at the hook frames, then write `brief.md` beside it with the template and the why-it-worked, comment-mining and hook-category methods in `references/research-methods.md`. Check any spoken hook against the video before it goes in client copy. To compare accounts, run each into its own folder and follow the competitor-teardown section there.

## Optional add-ons

- **More comments than `comments` pulls:** `apify/instagram-comment-scraper` (the free plan returns only about 15 newest per post). TikTok comments need an Apify actor (untested).
- **Ad check:** Meta Ad Library via `apify/facebook-ads-scraper`. Search by **Page ID**, not name; in testing a name search found 1 of 3 real ads. Only active ads and EU-delivered ads stay visible, so "none found" is weak evidence. Google Ads Transparency Center for YouTube ads.
- **Guest and partner reach:** `apify/instagram-profile-scraper` with a list of usernames gives follower counts and verified badges in one run.

## Reading the numbers

- Views are logged-out plays. Instagram plays include replays. None are unique viewers.
- **Followers per 1K views** = today's followers ÷ (views on that platform's posts in the window ÷ 1,000). It's a proxy for how well reach turns into follows. Compare platforms and runs over time; don't treat it as follows-from-posts.
- Posts under 3 days old are still climbing; the report leaves them out of the bottom 5, the baseline and the tier counts.
- **Lift** = a post's views ÷ its own group's median (reels with reels, Shorts with Shorts). Tiers: huge 5x+, strong 2-5x, mild 1.5-2x. Confidence: low under 10 posts, medium 10-19, high 20+. Never compare lift or raw views across accounts without the teardown method.
- Pinned posts sit in the Instagram sample whatever their age (the reels tab has its own pins, separate from the grid's). The report lists them but keeps them out of the dates, cadence, median, tier counts and followers per 1K.
- A following count far above the follower count suggests follow-for-follow, which inflates followers.
- **Top 3 share** = the three biggest posts' share of all views. A high share means the account lives on a few hits, which is a finding in itself.

## Status

- Live-tested: YouTube and TikTok collection, `meta`, `hooks`, `comments`, `report`, and Instagram through the Apify MCP plus `ingest-ig`.
- The direct `collect --instagram` call is tested against a mocked Apify response (actor, input, caps, files), not yet against a live run.
- Not built yet: a `compare` command for the competitor teardown, TikTok comments, same-age cohort comparisons, caption length vs views, a weekday table with sample sizes, matching the same clip across platforms.

## Access checklist (native analytics)

Public data can't show reach, follows per post, retention, traffic sources or ad spend. Ask the account owner for:
1. **Instagram:** Partner access in Meta Business Suite with Insights. Never take a password.
2. **Ads:** Analyst access in Ads Manager (the only way to see boosts and spend).
3. **TikTok:** Business Center access or a CSV export.
4. **YouTube:** Studio Viewer, or Viewer (Limited) to hide revenue.
5. **Podcast host dashboard** (downloads per episode) and link-in-bio click data.
