# Findings: direct TikTok web hydration

Date: 2026-09-03. Branch: `research/direct-web-hydration`. Production code was
not changed.

## Decision

The one-page SSR path is ready for a guarded video-only canary, not an immediate
default flip. A canonical public video page returned the full `itemStruct` and
all `bitrateInfo` renditions in one request. A confirmed public photo post did
not expose `webapp.video-detail` in canonical-page hydration, so photo posts
must retain the player API path.

## Measured result

For the primary video, five canonical metadata runs produced:

| Flow | Requests | Median | Range |
| --- | ---: | ---: | ---: |
| Direct canonical SSR | 1 | 617.943 ms | 585.134–686.944 ms |
| Current player API + web, interval 0 | 2 | 1229.269 ms | 1088.528–1265.558 ms |

That is a 49.73% median reduction for metadata extraction on this egress. The
exact sanitized samples are in `results/2026-09-03-sanitized.json`.

The current player response exposed only 576×1024 H.264 for the sample. The
canonical page additionally exposed 720×1280 and 1080×1920 HEVC. All four fresh
renditions were temporarily downloaded and inspected; every file had AAC audio
and the declared video codec/dimensions.

## Media routing

- Fresh v16 and v19 direct URLs returned `206 video/mp4` without a redirect.
- The matching `www.tiktok.com/aweme/v1/play/` URL redirected to TikTok's `/404`
  HTML route, so it is not a working fallback on this path/egress.
- Rewriting that path to each of the three production playback hosts worked and
  redirected to a TikTok CDN, but was slower than using direct CDN URLs.

This agrees with current yt-dlp behavior: it parses `bitrateInfo` but filters
media formats hosted at `www.tiktok.com` ([source](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py)).

## Photo follow-up: carousel and single image

Two additional Chromium HARs confirm a different first-party browser flow for
photo posts:

```text
short 301 → canonical /photo/ page → JS startup → signed /api/item/detail/
```

The canonical documents completed in 369 and 474 ms but contained only app,
AB-test, business-context and translation hydration scopes. The usable
`itemStruct.imagePost` arrived from `/api/item/detail/` 1.669 and 1.702 seconds
after the respective canonical requests began. End-to-end from the short-request
start to item-detail completion was about 2.05 and 2.07 seconds.

The detail payload preserved the expected order and contained:

- carousel: three 864×1536 JPEG images, each with p16 and p19 mirrors, plus a
  60-second audio track;
- single image: one 1080×1439 JPEG with p16 and p19 mirrors, plus a 19-second
  audio track.

Both mirror pairs returned `206 image/jpeg` with identical total sizes. The
request had no cookies, but it was not a simple reusable endpoint: replaying the
captured signed query succeeded with its captured Chrome 152 user agent, while
removing the user agent or substituting Chrome 136 produced HTTP 200 with an
empty body. This is consistent with a UA-sensitive web signature; naive query
ablation cannot isolate optional parameters because changing the query also
invalidates the signature.

The anonymous player API returned the same image counts, order, dimensions and
audio in one request: 429 ms for the carousel and 446 ms for the single image.
An optimized `Location`-only short resolver followed by player API took median
891 and 887 ms over three runs, versus 1149 and 1148 ms for single current
interval-0 runs. Therefore `/api/item/detail/` is useful as browser evidence,
not as the recommended Pinchana photo surface.

## Pacing

The default `2.0` seconds is applied twice at different layers:

- as yt-dlp's per-request extraction sleep ([api.py](https://github.com/Pinchana/pinchana-tiktok/blob/0ee4b29034177f5a959db7cedbcfcffbaff16761/src/pinchana_tiktok/api.py));
- as the global upstream job-start interval ([main.py](https://github.com/Pinchana/pinchana-tiktok/blob/0ee4b29034177f5a959db7cedbcfcffbaff16761/src/pinchana_tiktok/main.py)).

For a canonical current flow with two metadata requests, yt-dlp adds two seconds.
For a short current flow with three logical requests on the reused session, it
adds four seconds, and the runner may add up to another two seconds between
short resolution and extraction. The observed interval-2 short benchmark took
6909.689 ms before that possible runner delay.

## Proposed canary chain

1. `/video/ID`: one canonical GET → validate matching `itemStruct` → rank and
   preserve every direct CDN mirror.
2. Missing/challenged/mismatched hydration: current player API → current yt-dlp
   webpage fallback.
3. `/photo/ID`: player API remains primary. For short photo links, stop redirect
   resolution at the first canonical `Location` and skip canonical HTML.
4. Direct CDN mirrors: v16 → v19 → metadata refresh → existing rewritten
   playback hosts only as the final recovery path.
5. Split job pacing from internal request pacing; measure block rate with
   internal pacing at zero before changing the production default.

Keep the canary disabled by default until it passes a multi-day, multi-egress
matrix. Recent yt-dlp reports show that hydration can still be unavailable for
some users or regions ([example](https://github.com/yt-dlp/yt-dlp/issues/17393)),
and TikTok documents oEmbed—not this SSR extraction contract—as its supported
developer surface ([TikTok documentation](https://developers.tiktok.com/docs/en/embed-videos)).
