# Research methods: outliers, hooks, comments, competitor teardown

Read this after `report` when writing the brief, and before comparing accounts.

**Source.** Methods adapted from ScrapeCreators [`social-media-research-skills`](https://github.com/ScrapeCreators/social-media-research-skills) @ `64ba7b4dea71e130d2712ffb6c1c1024b3b7c4b2` (MIT, (c) 2026 ScrapeCreators): `outlier-post-finder`, `transcript-intelligence`, `comment-mining`, `competitor-social-research`, `creator-profile-teardown`. Their skills run on the paid ScrapeCreators API; the methods below are rewired to yt-dlp, Apify and local whisper-cli. Where a rule is ours and not theirs, it says **(ours)**.

## Outlier scoring (built into `report`)

- Baseline = the **median** views of the group (platform x format: Instagram reels, Instagram photos/carousels, TikTok videos, TikTok photos, YouTube Shorts, YouTube long-form, Snapchat Spotlight), never the mean, so one hit can't move it. Posts under 3 days old stay out of the baseline.
- `lift = views / group median`. Tiers: **huge 5x+, strong 2-5x, mild 1.5-2x** (source). Under 0.2x is a dud (ours).
- `Eng. lift = (likes + comments) / group median of the same`. Shares only exist logged out on TikTok and Snapchat, and saves only on TikTok, so both stay out to keep the number comparable across platforms (ours). Shown only when 10+ posts in the group have like counts.
- Confidence: low under 10 baseline posts (source: "do not overstate confidence from fewer than 10 posts"). Medium 10-19, high 20+ (ours: the source only says "pull at least 20").
- Lift is within one account's group. Never rank raw views across accounts of different sizes, and never pool platforms into one baseline.
- Pinned posts get a lift boost from placement; fresh posts are still climbing. Both are marked in the table.
- Per-post `lift` is in `posts.csv` and `summary.json` for any other cut. TikTok groups show their median share and save rates on a line of their own.

## What formats work (built into `report`)

- Each video group (TikTok videos, YouTube Shorts, YouTube long-form, Instagram reels) is cut three ways: length bucket, weekday and 3-hour posting block. Each row: posts, median views, median lift, share at 2x+. Settled, unpinned posts only (ours).
- Thin rows: under 5 posts, or for a weekday or time row, from fewer than 3 dates (one boosted day lifts every post on it). A thin row is never named strongest; thin rows of 3+ posts at 1.5x+, from any table of 2+ rows, are listed as **Thin leads**, anecdotes, each with the length note when the leading length explains it. A table of only thin rows, or of one row, becomes one line and isn't ranked; a group, or a kind of table, under 10 posts isn't cut (ours).
- Posting times are exact or left out: TikTok, Instagram and Snapchat give one per post; YouTube's listing gives only a midnight guess, so only uploads looked up in the newest `--yt-enrich` run count. Times are shown in `--tz`, or this computer's zone.
- Top-post lookups count as listed in every table: a Short's listing has no length, so a looked-up Short stays out of the length table, and no lookup enters the weekday or time tables. `top_lookup` in `posts.csv` and `summary.json` marks them; drop those rows before cutting either yourself.
- A table that covers only some of the group (a YouTube Short's length and every YouTube posting time exist only for uploads looked up in the newest run; long-form lengths come from the listing) measures lift against those posts' own median views, so the newest run isn't scored against older posts that have had longer to collect views (ours).
- **Strongest** needs a 1.5x median lift and a lead that random splits of the same posts give some row that isn't thin under 5% of the time, judged by how unusual each row's median is for its size (Westfall-Young min-p), so a big row's clear lead isn't drowned by noisy 5-post rows. 2,000 shuffles with a fixed seed: weekday rows are dealt whole dates and time rows a date's posts within one block (a day's posts aren't independent), and a date only trades places with dates that have as many posts, so every row keeps its count of dates and of posts; length rows are dealt single posts. A lead whose dates no other row can trade with reads "can't be tested against chance". In simulation with no real effect (1,500 groups each on seven posting schedules, with and without day-to-day swings), 4-7% of tables named a lead, and 9-15% of groups named one in any table; length assumes a post's length doesn't depend on its posting day (a creator who posts several clips of one length on the same strong days can see more). Otherwise the lead reads **Not a finding yet**, with that share. The bar is per table, so a group with three tables names a chance lead about one time in eight: a Strongest is a lead to test (ours).
- **Check the length:** a weekday or time row can win on the length of the posts in it. When the leading length is over-represented in the strongest day or time (against that table's own posts with a length), and the row's median lift drops by more than a quarter without those posts (or falls at all when fewer than 5 are left), the report gives the lift without them. No line means one of three things: no length leads, the leading length isn't over-represented in the row, or the row's lift held within a quarter without it. It never means the row still beats every other row. In research, a creator's Saturday lead (6.1x) fell to 1.1x without its 3-10 minute clips; the same row came from only two dates, so it now reads thin.
- Not cut: photo carousels (their listed length is the song's) and Snapchat (hidden view counts would tilt every row up; the Spotlight checks cover its length). Original vs named sound was tested and dropped: every named-sound post in the test sample was a carousel.
- Correlation, not cause. In the brief, a strong row is a content test with its n quoted, not a schedule rule.

## Why it worked (the brief, written by Claude)

For each top post and its bottom-post contrast, look at:
- **The three hook channels:** text hook (caption opening), spoken hook (first 10 s; Snap's own transcript for Snapchat Spotlights, whisper-cli for the rest), visual hook (`hooks/*.jpg`: the frame at 0.5 s, or a photo post's image; often carries on-screen text). Strong posts usually stack two.
- **Hook category** (ours): curiosity, contrarian, problem-aware, outcome, story, authority, comparison, mistake, demonstration, before/after, timely, identity, objection-handling. Tag top and bottom hooks; a category that repeats only in the top rows is a finding.
- Topic, format, guest or creator authority, novelty or timeliness (a release, a season), emotional trigger, controversy, and what the comments show (confusion, desire, debate).
- Say "appears to" or "suggests". Performance shows correlation; the account's own analytics would show cause.

## Hooks (`hooks` command)

- Text hook = the caption's opening sentence (verbatim). Spoken hook = Snap's own transcript for Snapchat Spotlights, else whisper-cli base.en on the first 20 s, keeping confident segments that start in the first 10 s. Visual hook = the frame at 0.5 s, or a photo post's image (a TikTok carousel's first slide, an image Spotlight).
- Machine transcripts get names and small words wrong ("I don't have" for "they don't have" in testing). Quote a spoken hook in client copy only after checking it against the video or the frame's burned-in captions.
- "No speech found" means music, animal sound, or a text-only open. The frame shows which. Whisper's default no-speech cut turned bird calls into "I'm not sure."; the script runs `-nth 0.3`.
- For a full transcript of a specific post (segment it into hook, setup, claim, evidence, payoff, CTA; pull claims, stories, frameworks, quotable lines), use a transcript tool on that post. Keep every quote tied to its URL.

## Comment mining (`comments` command, then judgment)

Sources: YouTube via yt-dlp, top-sorted, up to `--max` per post. Podcasts from Apple's US review feed (`reviews_apple.json`: newest first, 50 a page and up to 10 pages; Apple serves some pages empty, and the report names any it skipped; when page 1, the newest 50, is empty, the reviews on the US show page are added), or the handful Apple shows on the US show page when no page answers, with rating, title, text and date. Instagram from the reels scrape itself (`latestComments`, the newest ~15 per post, no extra cost). Snapchat from the profile page itself (the top ~25 per Spotlight, with likes and `by_creator`, no extra cost). TikTok with `--tiktok-comments`, from TikTok's unofficial web comment list (top-level comments only, up to `--max` per post and at most 10 pages, TikTok's own order, with `creator_liked` for comments the creator liked; reply threads aren't read, so `by_creator` there marks the creator's own top-level comments; yt-dlp returns none logged out). The endpoint can stop answering, and then the source says so. Handles are dropped on purpose; `by_creator` flags the account's own replies so they aren't read as audience.

1. Drop spam, bots, emoji-only replies and creator replies.
2. Tag each useful comment: questions, objections, complaints or pain points, praise, confusion, requests or ideas, buying intent, controversy or debate, jokes or culture signals.
3. Cluster into themes. Score each by frequency (how many) and intensity (strength of wording, likes). One loud comment is not a pattern.
4. Keep exact wording for anything reusable. Slang and misspellings are the point.
5. Turn it into actions: reply-video ideas, FAQ or content ideas, objection angles.
6. State the sample (posts, comments, newest-only for IG, which source the podcast reviews came from) and confidence. One post's comments are not market sentiment.

## Monthly check-ins (built into `scan`)

Scan the account into a new dated folder (`<project>/social-scan/<account>/<YYYY-MM-DD>/`) with the same `--days` as last time (or the same `--limit` and `--yt-enrich` if the last scan was by post count). The report's "Since last scan" section then lists follower change, each group's median, cadence, 2x+ count and top-3 share then and now, the posts published since (with lift), the posts still climbing, and the ones that became outliers. Lead the check-in with what moved and why it likely moved; a median change in a group flagged "Different windows" is partly the window, not the content, and a gain marked ~ is approximate, give or take one rounding step.

## Competitor teardown (`compare`)

1. Confirm each handle belongs to the right brand before pulling; a shared name is not a match.
2. Run `scan` for each account into its own folder (`<project>/social-scan/<account>/<YYYY-MM-DD>/`) with the same `--days` (a fixed `--limit` covers different date ranges at different cadences).
3. Run `compare DIR DIR ...`: posts per week, median per group, **outlier rate** (share of settled posts at 2x+), top 3 share, best format (the group with the most 2x+ posts) and followers per 1K views, one row per account and format. Rows flagged under "Windows differ" have no median, and followers per 1K shows only for accounts scanned with the same `--days`: compare the rest on outlier rate and cadence. Never raw views across accounts.
4. Compare hook categories and channels across the top rows of each folder's `hooks.json`, which `scan` already built. Don't re-run `hooks` later: Instagram video links expire, and a re-run replaces those rows with failures.
5. Output: snapshot table (account, platforms, audience, cadence, best format, notes) / what is working per account / cross-account patterns / gaps table (Opportunity, Evidence, Suggested test) / three content tests.

## The brief (`brief.md` beside `summary.md`)

```markdown
# <Account>: social research brief (<date>)
## Summary            sample, window, baselines, confidence per group
## Biggest outliers   post, platform, date, views, lift, tier, why it likely worked
## Repeatable patterns 2-4, each with evidence rows
## Hooks swipe file   verbatim, with channel + category + URL; top vs bottom contrast
## Comment themes     theme, bucket, frequency, intensity, exact quote
## Content tests      3, each tied to an outlier, a comment theme or a format row (quote its n)
## Caveats            public data only, sample sizes, pinned/fresh, machine transcripts
```

## Gaps (ScrapeCreators-endpoint only, not adopted)

| Their skill or source | Why not here | Reopen when |
|---|---|---|
| `ad-library-teardown` (Meta, TikTok, Google, LinkedIn ad libraries) | Paid ScrapeCreators endpoints. Meta is partly covered by the Apify `facebook-ads-scraper` add-on in SKILL.md | You need ad history beyond Meta |
| LinkedIn, Threads, X/Twitter, Facebook, Bluesky, Reddit, Rumble, Pinterest posts, comments and transcripts | No logged-out yt-dlp or vetted Apify path wired here | An account's main channel is one of these |
| `trend-discovery`, `social-listening-brief`, `audience-research` (keyword and trending search across platforms) | Needs ScrapeCreators search endpoints | You want niche trend sweeps |
| `influencer-prospecting` (creator search) | ScrapeCreators search endpoints; the Apify profile scraper covers known handles only | You need creator or guest prospecting |
| `product-demand-research` (TikTok Shop) | ScrapeCreators TikTok Shop endpoints | An account sells on TikTok Shop |
