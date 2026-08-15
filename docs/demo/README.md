# Product demonstration

`ratereplay-demo.webm` is a 1280 by 720 recording of the real static public walkthrough.
It visits all six stages from the landing page through the aggregate-only redacted report.
The capture test fails if it observes a non-GET request, an authenticated endpoint, a cross-origin request, a cookie, or mutable browser storage.

The video is intentionally silent.
Every public claim remains visible in the product interface and is bound to the immutable simulated artifact manifest recorded in `ratereplay-demo.json`.
The metadata also locks the video hash, duration, dimensions, codec, capture source commit, and assertion results.

Install the locked browser toolchain with `make bootstrap`, install `ffprobe`, and regenerate the demonstration with:

```sh
make demo-video
```

Independently re-probe the committed artifact and verify its metadata lock with:

```sh
make demo-video-check
```

Video saving follows [Playwright's documented browser-context lifecycle](https://playwright.dev/docs/videos).
The page and context close before the capture is copied to its stable repository path.
