# Social videos

Portrait videos (1080×1920, 30 fps, H.264 MP4 plus a GIF under 8 MB) that each show one trick on a real page, then
Unfret's real result for that page with the matching finding marked, then the end card. `videos.mjs` holds each
video: the text its result must contain, what to hide or blur on the page, and the script (camera keys,
marker strokes, captions, timings).

```bash
cd scripts/social-videos && npm install           # Playwright; needs `npx playwright install chromium`
(cd ../../../unfret_extension && make build)      # the popup (dist/chrome) and Unfret Dev (dist/chrome-dev)
node capture.mjs e-no-refunds                     # a real deep check on dev.unfret.net → out/results/
node capture.mjs probe=https://…                  # a probe: any page, before it has a video
node shoot.mjs e-no-refunds                       # the page on a phone + the popup with that result → out/<video>/
FFMPEG=… node render.mjs e-no-refunds             # → out/<video>/<video>.mp4 and .gif, copied to ../media/videos
node render.mjs e-no-refunds --frames 2,8.5,15    # single frames as PNGs, to check a change
node render.mjs e-no-refunds --mascot             # the same with the animated mascot → <video>-mascot.mp4 and .gif
```

- **The real pages** aren't named in this public repo: each video's address and the brand's words to blur are in
  `pages.json` beside the finished videos (`$UNFRET_VIDEO_PAGES`; the format is in `videos.mjs`).
- **Results are real.** `capture.mjs` checks the page with Unfret Dev on dev.unfret.net, on a free pass from the
  welcome page (two a day per network) or `UNFRET_DEV_PASS`; never an admin token. It fails if the result doesn't
  contain the video's `expect` text.
- **Real shops are blurred** as the content guide asks: the brand's name, logo and byline, the platform's header,
  badges and address, ads for other brands. People (reviewers' names and photos, faces) go in `page.redact`,
  blurred past recognising. The test shop (`/test-shop`) needs none of that: its banner says it's
  made up.
- **ffmpeg**: any build with libx264, e.g. `uv run --no-project --with imageio-ffmpeg python -c "import
  imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"`.
- Everything generated stays in `out/` (git-ignored); the finished files go to `$UNFRET_VIDEOS_DIR`, by default the
  metarepo's `media/videos`.
- **The mascot** (`--mascot`): `mascot.mjs` animates the rigged drawing in `assets/unfret-mascot.svg`, one character
  with two poses: C ("Leaning in", the default) and A ("Close inspection", for close-ups), blending between them.
  A video's `script.mascot` track (format at the top of `mascot.mjs`) sets where it stands, its poses, hops, blinks
  and nods per beat; `dark: true` switches to the dark-background colours. Keep it in free space, clear of the
  captions, the marker strokes and the marked finding, and use it in a few deliberate moments, not every shot.
  `node mascot.mjs out.svg [dark]` writes pose C as a still.
- **Real pages, one trick each** (`e-`, `f-`, `g-`): English pages, US shops first (the market is the US; a
  Shopify shop shows dollars with `?currency=USD`), each one where Unfret's deep check on dev shows the finding on
  the card today. Earlier versions on Dutch and Czech pages are `ne-…` (videos in `media/videos/non-english`).
  `capture.mjs` goes on past a page it can't check. `shoot.mjs` scrolls down the page and back before the still, so
  lazy images and text that fades in on scroll are there, and skips hidden copies of a mark's text. A mark can be a
  whole element (no `text`); with `style: true` its font and background are kept, so `script.clocks` can run a live
  countdown in its place, and `script.reloads` flash the page as it reloads. `popup.expand` opens the popup's "Show
  details" when the finding is a signal listed there.
- **The test shop videos** (`ts-…`, in `media/videos/test-shop`) show `/test-shop` on dev, one trick each, all from
  one deep check of that page; kept for the record.
