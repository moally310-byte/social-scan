# social-scan

**A Claude Code skill that audits any creator's or brand's public Instagram, TikTok and YouTube in about a minute.** Hand it a handle and it finds the posts that broke out, shows the hooks that made them work, and tells you how much of the account rides on a few hits. No logins, no cookies, no paid analytics seat.

## What you get

You ask Claude: *"scan @duolingo's TikTok and YouTube."* About 50 seconds later (YouTube and TikTok; Instagram adds roughly 25 seconds) you get a report like this (real run, public data, 2026-10-15):

| Group | Posts | Per week | Median views | Max | Top 3 share | Outliers 5x+ / 2-5x / 1.5-2x |
|---|---|---|---|---|---|---|
| TikTok | 40 | 6.4 | 596.9K | 5.70M | 30.6% | 2 / 11 / 1 |
| YouTube Shorts | 40 | 7.0 | 976.5K | 2.90M | 19.9% | 0 / 5 / 3 |
| YouTube long-form | 40 | 0.8 | 104.3K | 3.30M | 41.8% | 9 / 1 / 3 |

Plus, for every platform:

- **Outliers by lift.** Each post's views against that account's own median, sorted into huge (5x+), strong (2-5x) and mild (1.5-2x). Duolingo's top TikTok ran 9.6x its median.
- **Hooks, three ways.** For the top 3 and bottom 2 posts: the caption's opening line, the first 10 seconds of speech (transcribed locally with whisper.cpp), and the frame at 0.5 seconds.
- **Comment mining.** Top YouTube comments and the newest Instagram comments, handles stripped, ready to cluster into themes.
- **The numbers people skip.** Posting cadence, top-3 share (does the account live on a few hits?), followers per 1K views, and a flag when a gap in the data means a median can't be trusted.
- **A brief.** Claude reads the report and writes what worked, why it likely worked, and three content tests to run next.

Everything lands in a dated folder: `summary.md`, `summary.json`, `posts.csv`, the hook frames and the raw scrape files.

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

### Requirements

- Python 3 (standard library only)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) for YouTube and TikTok
- For hooks: `ffmpeg`, `whisper-cli` from [whisper.cpp](https://github.com/ggml-org/whisper.cpp), and the `base.en` model (about 148 MB)

```bash
brew install yt-dlp ffmpeg whisper-cpp
mkdir -p ~/.local/share/whisper-cpp
curl -L -o ~/.local/share/whisper-cpp/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

- For Instagram: an [Apify](https://apify.com) account. Set `APIFY_TOKEN` in your own terminal; the skill never reads it from anywhere else.

## Use it

Just ask Claude:

- "Scan @handle's socials before my pitch call."
- "What hooks are working for this creator on TikTok?"
- "Find their outlier reels."
- "Mine the comments on their top YouTube videos."

Or run the script yourself. With the manual install it lives at `~/.claude/skills/social-scan/scripts/`; with the plugin install, under `~/.claude/plugins/cache/social-scan/social-scan/<version>/skills/social-scan/scripts/`.

```bash
SCAN=~/.claude/skills/social-scan/scripts/social_scan.py
python3 $SCAN collect  --out ./scan --youtube @duolingo --tiktok duolingo
python3 $SCAN meta     --out ./scan --platform tiktok --followers 18300000 --following 317
python3 $SCAN hooks    --out ./scan
python3 $SCAN comments --out ./scan
python3 $SCAN report   --out ./scan --name "Duolingo"
```

Instagram: `APIFY_TOKEN=... python3 $SCAN collect --out ./scan --instagram username --limit 40`

## Cost

YouTube and TikTok are free. Instagram runs through Apify at about $0.11 for 40 reels, with a hard $0.30 cap on each Apify call built into the script (an Instagram run makes two calls: reels, then the profile).

## What it won't do

- **No logins.** Logged-out public data only. The script never passes cookies, runs yt-dlp with `--ignore-config` so a cookie setting can't sneak in from your own config, and never asks for anyone's password.
- **No private metrics.** Reach, retention, follows per post, traffic sources and ad spend only exist in the account owner's analytics. The skill ends with a checklist of what access to ask for.
- **Some blind spots.** TikTok comments don't load logged out. Instagram samples the reels tab only (photos and carousels aren't counted). LinkedIn, X, Threads and Facebook aren't covered yet.
- **Views aren't people.** Plays count replays, so treat views as plays, not unique viewers.

## Use it responsibly

This reads public, logged-out data only, but all three platforms' terms restrict automated access, so how you use it is on you. Comments are other people's words: the script strips handles, so keep it that way. The hook frames in `hooks/` are the account's own footage: keep them in your report folder and don't republish them.

## Status

Live-tested on YouTube, TikTok and Instagram through the Apify MCP. The direct Instagram call (`collect --instagram`) is tested against a mocked Apify response; if your first live run trips on something, open an issue.

## Credits

The outlier tiers, hook breakdown, comment-mining method and brief template are adapted from ScrapeCreators' [social-media-research-skills](https://github.com/ScrapeCreators/social-media-research-skills) (MIT). Their skills run on the paid ScrapeCreators API; this one rewires the methods to yt-dlp, Apify and local whisper.cpp. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT. Built by [Mo Ally](https://github.com/moally310-byte) with Claude Code.
