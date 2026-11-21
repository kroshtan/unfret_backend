# Social videos

Portrait videos (1080×1920, 30 fps, H.264 MP4 plus a GIF under 8 MB) that each show one trick on a real page, then
Unfret's real result for that page with the matching finding marked, then the end card. `videos.mjs` holds each
video: the text its result must contain, what to hide or blur on the page, and the script (camera keys,
marker strokes, captions, timings).

```bash
cd scripts/social-videos && npm install           # Playwright; needs `npx playwright install chromium`
(cd ../../../unfret_extension && make build)      # the popup (dist/chrome) and Unfret Dev (dist/chrome-dev)
node capture.mjs c-piece-count                    # a real deep check on dev.unfret.net → out/results/
node shoot.mjs c-piece-count                      # the page on a phone + the popup with that result → out/<video>/
FFMPEG=… node render.mjs c-piece-count            # → out/<video>/<video>.mp4 and .gif, copied to ../media/videos
node render.mjs c-piece-count --frames 2,8.5,15   # single frames as PNGs, to check a change
```

- **The real pages** aren't named in this public repo: each video's address and the brand's words to blur are in
  `pages.json` beside the finished videos (`$UNFRET_VIDEO_PAGES`; the format is in `videos.mjs`).
- **Results are real.** `capture.mjs` checks the page with Unfret Dev on dev.unfret.net, on a free pass from the
  welcome page (two a day per network) or `UNFRET_DEV_PASS`; never an admin token. It fails if the result doesn't
  contain the video's `expect` text.
- **Real shops are blurred** as the content guide asks: the brand's name, logo and byline, the platform's header,
  badges and address, ads for other brands. The test shop (`/test-shop`) needs none of that: its banner says it's
  made up.
- **ffmpeg**: any build with libx264, e.g. `uv run --no-project --with imageio-ffmpeg python -c "import
  imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"`.
- Everything generated stays in `out/` (git-ignored); the finished files go to `$UNFRET_VIDEOS_DIR`, by default the
  metarepo's `media/videos`.
