---
name: social-scan
description: Pull PUBLIC Instagram, TikTok and YouTube numbers for any account (per-post views, likes, comments, dates, follower counts) and turn them into a one-page audit with medians, outlier lift tiers, top and bottom posts with their caption, spoken and first-frame hooks, top-post comments, cadence and followers per 1K views. Use for "scan their socials," "pull their Instagram/TikTok/YouTube numbers," "audit this creator's or brand's social," "how are their reels doing," "find their outlier or viral posts," "what hooks are working for them," "mine their comments," "social audit before my pitch call," "compare these accounts," or prep for a social-media client. Logged-out data only, via yt-dlp, Apify and local whisper. NOT native analytics (reach, retention and ad spend need the account owner's access).
---

# social-scan

`doctor` once per machine, then one `scan` per account: one report.

Script: `scripts/social_scan.py` in this skill's folder (written `SCAN` below; use its full path). Python 3.8+, standard library only. `doctor` says what else is installed and what each missing piece turns off: `yt-dlp` for YouTube and TikTok; `ffmpeg`, `whisper-cli` (whisper.cpp) and a model file (default `~/.local/share/whisper-cpp/ggml-base.en.bin`, override with `WHISPER_MODEL`) for hooks; an Apify account for Instagram. Outlier tiers, hooks, comment mining and the brief are adapted from ScrapeCreators' social-media-research-skills (MIT); method, attribution and gaps in `references/research-methods.md`.

## Rules

- **Logged-out data only.** Never pass `--cookies` or `--cookies-from-browser`, never log in to anyone's account to scrape, never use Instaloader with a login. (Platform terms and account-ban risk.)
- **The Apify token comes only from the `APIFY_TOKEN` environment variable** the user sets in their own terminal. Never read it out of a keychain or config file yourself, never print it, never put it in a URL.
- **Cap every Apify run** (`--limit`, plus `maxItems` for pay-per-result actors or `maxTotalChargeUsd` for pay-per-event ones; the script sends both, at $0.30 per Apify call). An Instagram scan costs about $0.11 per 40 reels and can't pass $0.60 (two calls); YouTube and TikTok are free. The optional add-ons below bill separately.
- **One folder per account and run: `<project>/social-scan/<account>/<YYYY-MM-DD>/`.** `scan` reports everything already in its `--out` folder. Keep the raw files; they're the receipts.

## Steps

1. **Check the machine** (once): `python3 SCAN doctor`. Exit 0 means YouTube and TikTok can run; everything marked optional only adds to the report. For a MISSING or BROKEN tool, show the user the install line it prints and run it only on their OK. The whisper model (148 MB) downloads only with `doctor --get-model`, and only when the user says yes. Never run the APIFY_TOKEN line, never ask for the token, and never run `--instagram` yourself even if doctor shows the token as set: the direct Instagram path is paid, so the user runs it in their own terminal (step 3).
2. **Get handles.** YouTube `@handle`, TikTok handle, Instagram username. The account's website footer usually links all three. yt-dlp can't see TikTok followers: open the profile in a browser, read "Followers", and pass it to `scan` as `--tiktok-followers`.
3. **Instagram, if it's in scope** (reels tab, newest N in order; about $0.11 for 40). Do it before step 4. Pick whichever path works:
   - **Direct (preferred, scriptable).** Ask the user to run the whole scan in their own terminal, so their token never passes through the chat:
     ```bash
     APIFY_TOKEN=YOUR_TOKEN python3 SCAN scan --out <DIR> --instagram username --youtube @Handle --tiktok handle --tiktok-followers N --name "Account Name"
     ```
     Ask them to paste back the Notes it ends with, tell them what was skipped, then go to step 5.
   - **Apify MCP.** Call `apify/instagram-reel-scraper` with `{"username":["<user>"],"resultsLimit":40}` (the direct path pulls 50) and `callOptions.maxTotalChargeUsd: 0.30` (it bills per event, so `maxItems` doesn't cap it), and `apify/instagram-profile-scraper` with `{"usernames":["<user>"]}`. Use `waitSecs: 0`, then poll `get-actor-run`. Save each `get-dataset-items` result as a JSON array in `<DIR>/raw/`, then:
     ```bash
     python3 SCAN ingest-ig --out <DIR> --reels <DIR>/raw/instagram_reels.json --profile <DIR>/raw/instagram_profile.json
     ```
     Step 4 picks the ingested posts up from `<DIR>`. If an MCP call stalls, stop retrying and switch to the direct path. A large `get-dataset-items` result (about 430 KB for 40 reels) may come back as a saved tool-result file: extract `items` from that file with Python into `<DIR>/raw/instagram_reels.json`. Never retype the JSON by hand; the signed video links must stay byte-exact. The Apify dataset API refuses token-free reads.
   - **Fallback.** The logged-out Instagram page shows the follower count only. Record it with `meta --platform instagram`.
   - **Why the reels tab, not the posts feed.** Logged out, the posts feed (`apify/instagram-scraper` with `resultsType: posts`, and `apify/instagram-post-scraper`) returns the grid's first 12 (pinned plus newest), then a scattered set of older posts. On one test account it skipped 29 live posts across ten weeks and inflated the reel median about fifteenfold. `apify/instagram-reel-scraper` returned the newest reels with nothing missing. Photos and carousels aren't sampled. `ingest-ig --posts` still reads old posts-feed files and marks them **Unreliable sample**. If a report says **Check the profile**, a gap of 7+ days broke a regular cadence: confirm on the profile before quoting that group's median. On each new account, check once that the newest 9 non-pinned reels match the logged-out grid (the `/reel/` links on the profile page). If the grid is mostly photos or carousels, say in the brief that the reels median doesn't represent the account.
4. **Scan** (about a minute; YouTube and TikTok are free):
   ```bash
   python3 SCAN scan --out <DIR> --youtube @Handle --tiktok handle --tiktok-followers 12400 --name "Account Name"
   ```
   It collects (`--limit 60` posts per tab; `--yt-enrich 45` fetches likes, comments and exact dates for the newest 45 uploads, since Shorts have no dates without it), then runs hooks, comments and report on everything in `<DIR>`, Instagram posts from step 3 included. Run it straight after an Instagram ingest: the video links expire within hours. A step whose tool or token is missing is skipped and the report is still written; the Notes at the end say what was skipped or failed, and when posts or a follower count came from an earlier run. Tell the user both. After a step-3 ingest, the note that the ingested Instagram posts go into the report is expected; it isn't an earlier run. The last line on stdout is the path to `summary.md`. Without `--tiktok-followers`, it prints the one command that adds the count afterwards.
   - **Hooks:** top 3 and bottom 2 posts per group: the caption opening, the first 10 seconds of speech (whisper-cli, approximate) and the frame at 0.5 s, into `hooks/` and `hooks.json`. Without whisper they keep the caption and frame; without ffmpeg (or, for YouTube and TikTok, yt-dlp), the caption only. The report leaves out columns nothing filled, and leaves out a `hooks.json` built from an earlier collect.
   - **Comments:** YouTube top comments via yt-dlp; Instagram's newest ~15 per post come with the reels scrape. TikTok comments don't load logged out. Writes `comments.json`, handles dropped.
   - **Report:** `summary.md` (accounts; scorecard with outlier tiers and confidence; per group the top posts with lift, tier and engagement lift, the bottom 5; the Hooks section), `summary.json` (with per-post lift) and `posts.csv`.
5. **Write the brief.** Read the report and its caveats, look at the hook frames, then write `brief.md` beside it with the template and the why-it-worked, comment-mining and hook-category methods in `references/research-methods.md`. Check any spoken hook against the video before it goes in client copy. To compare accounts, run each into its own folder and follow the competitor-teardown section there.

**Single steps,** for re-running one part: `collect`, `ingest-ig`, `meta`, `hooks [--top 3 --bottom 2 --seconds 20]`, `comments [--top 3 --max 100]`, `report [--name N] [--since YYYY-MM-DD]`. `scan --no-hooks` or `--no-comments` leaves a step out; `scan --out <DIR>` with no handles re-runs hooks, comments and report on what's there. For a date window, run `scan`, then `report --out <DIR> --name "Account Name" --since YYYY-MM-DD` (hook rows outside the window drop out; `comments.json` still covers the whole scan).

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
- Un-enriched YouTube uploads (past the newest `--yt-enrich`) only carry a "3 months ago"-style date, so their dates bunch onto a few days and can be months late. The report marks them (approx) and leaves them out of per week and the gap check; `date_approx` in `posts.csv` and `summary.json` says which. Raise `--yt-enrich` to date more.
- A following count far above the follower count suggests follow-for-follow, which inflates followers.
- **Top 3 share** = the three biggest posts' share of all views. A high share means the account lives on a few hits, which is a finding in itself.

## Status

- Live-tested: `doctor` (also with ffmpeg, whisper-cli or yt-dlp off `PATH`, and `--get-model`, including simulated failed downloads), `scan` on YouTube and TikTok (also with tools off `PATH`, a mistyped handle and a reused folder), `meta`, `hooks`, `comments`, `report`, and Instagram through the Apify MCP plus `ingest-ig`.
- The direct `--instagram` call is tested against a mocked Apify response (actor, input, caps, files), not yet against a live run. The Linux install lines in `doctor` follow each project's docs but haven't been run on Linux.
- Not built yet: a `compare` command for the competitor teardown, TikTok comments, same-age cohort comparisons, caption length vs views, a weekday table with sample sizes, matching the same clip across platforms.

## Access checklist (native analytics)

Public data can't show reach, follows per post, retention, traffic sources or ad spend. Ask the account owner for:
1. **Instagram:** Partner access in Meta Business Suite with Insights. Never take a password.
2. **Ads:** Analyst access in Ads Manager (the only way to see boosts and spend).
3. **TikTok:** Business Center access or a CSV export.
4. **YouTube:** Studio Viewer, or Viewer (Limited) to hide revenue.
5. **Podcast host dashboard** (downloads per episode) and link-in-bio click data.
