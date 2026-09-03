# TikTok direct web research

This directory is intentionally outside `src/pinchana_tiktok`. It tests the
logged-out first-party flow without changing production extraction:

1. resolve a `vm.tiktok.com` or `vt.tiktok.com` URL with one non-following GET;
2. GET the canonical `www.tiktok.com/@user/video/ID` page;
3. parse `__UNIVERSAL_DATA_FOR_REHYDRATION__` and `itemStruct`;
4. rank `video.bitrateInfo` renditions;
5. optionally make one-byte range probes against one direct-CDN URL and the
   matching `www.tiktok.com/aweme/v1/play/` URL.

For photo HARs, the summary also detects the browser's subsequent
`/api/item/detail/` response and reports ordered images/audio without exposing
the signed request values.

The harness never writes response bodies or full signed media URLs. Its JSON
output retains host/path/query-key evidence while replacing all query values and
opaque `/video/` path values with `<redacted>`.

## Commands

Run these from the `pinchana-tiktok` repository root:

```sh
uv run python research/tiktok_web/harness.py har \
  "/path/to/capture.har" --video-id 7680845042994466066
```

```sh
uv run python research/tiktok_web/harness.py live \
  "https://www.tiktok.com/@creator/video/POST_ID" --probe
```

Compare with the existing extractor while forcing the request interval to zero:

```sh
uv run python research/tiktok_web/harness.py current \
  "https://www.tiktok.com/@creator/video/POST_ID" --interval 0
```

The `pacing` command models the independently applied global-runner and yt-dlp
request delays. It is not a network benchmark.

For photo posts, skip the empty canonical hydration and combine a non-following
short redirect with the existing anonymous player API:

```sh
uv run python research/tiktok_web/harness.py photo \
  "https://vt.tiktok.com/SHORT_CODE"
```

Use `--ranking-policy quality` (default) to prefer resolution, then bitrate and
file size. Use `--ranking-policy compatibility` to put H.264 ahead of HEVC, or
`--codec h264` when the consumer cannot decode HEVC.

Do not copy HAR files, cookies, `msToken`, signed URLs, or raw command output
into this repository. Only sanitized summaries belong in version control.
