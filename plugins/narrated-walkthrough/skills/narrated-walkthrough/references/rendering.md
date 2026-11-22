# Rendering, timing and the browser surface

## The clock model

```
scene.start[n] = Σ scene.duration[0..n-1]
scene.duration[n] = measured length of audio[n]
total = Σ all durations
```

Length comes from the WAV header (`data` bytes ÷ `byte_rate`), so the builder needs no
external audio tool and behaves identically on any platform. `afinfo` is only a
fallback for a container a header parse cannot describe.

The page never computes time from the audio element. It computes time from a
wall clock anchored at the last seek:

```js
wallBase + (performance.now() - wallStart) / 1000
```

The audio element is *slaved* to that value. On every frame, `syncAudio(t)` finds
the scene that owns `t`, loads its clip if it changed, and re-seeks when the
element drifts more than 0.45 s. Because each clip is exactly as long as its
slot, this converges rather than fights.

Why not simply trust `audio.currentTime`? Because a single element cannot hold
eight clips, and swapping `src` mid-playback resets position asynchronously. The
timeline is the source of truth; the element follows it.

### Seeking correctly

Seeking must go through `loadedmetadata`. Setting `currentTime` before metadata
exists silently clamps to zero, which is how a deck ends up stuck on scene one.
The builder's `pending` flag holds the target until the new clip reports ready,
then applies it, re-anchors the wall clock, and resumes playback.

### Scene advance

Two independent mechanisms, deliberately redundant:

1. the clock crosses `scene.start` on the next frame;
2. the clip fires `ended` and seeks to the next scene's start.

If a clip is a few milliseconds short or long, (1) wins and nothing stutters.

## Playback policy

- Audio requires a user gesture. Keep the Play button visible and never claim
  playback started without observing `playing === true`.
- The **Use system voice** fallback exists for missing or unplayable clips. It is
  a degraded path: it re-speaks a scene from its start and cannot seek within a
  scene. Say so rather than pretending it is equivalent.
- Pause must re-anchor the wall clock, or resuming jumps forward by the paused
  duration.

## Captions

Update from the cue list every frame, not inside the scene-change branch — that
is the classic one-scene lag. Each cue carries `{t, end, text}` computed by
evenly splitting a scene's measured duration. De-duplicate by text before
touching the DOM.

## The ask panel

Rendered once from `DATA.disposition`. Each option is a button that copies its
phrase so the listener can paste it into the chat; the panel also states that
free-form answers and no answer are both fine. Copy failures are swallowed
silently — the panel is an affordance, not a dependency.

## Serving

`--serve` starts `python3 -m http.server` on `127.0.0.1` from the plan directory
and stops any previous instance on that port first. Serve one deck per port.
`--stop` shuts it down. Prefer the harness's own browser surface for viewing; it
gives you a script hook (`window.state()`, `window.seek(t)`) that a detached
server alone cannot.

## Offline and self-containment

- The deck is one HTML file plus `audio/`.
- Mermaid is a local vendored asset named by `mermaid_source`. There is no CDN
  default: the builder fails closed without a path, and `--cdn` opts into a
  version-pinned remote build. A deck whose default artifact makes a network
  request cannot be verified as offline.
- Diagram rendering happens inline in the DOM, so no headless rasterisation step
  is required and no document content leaves the machine.

## Renderer traps

| Symptom | Correction |
|---|---|
| Blank diagrams | render inline; if rasterising, set `htmlLabels: false` |
| Double-scaled or clipped slides | do not wrap the deck in a fixed-height container |
| Diagram wider than the panel | split the scene; do not shrink the font |
| Stale diagram after an edit | scene ids are the cache key — change the id when the diagram changes |
| Partial page during rebuild | write the file atomically (temp + rename) |

## Browser verification

Standards for browser and Playwright verification belong to
`playwright-report-retrieval`; the checks below are the minimum for this deck and
link there rather than defining a second ladder.

```js
// structure
return { slides: document.querySelectorAll('.slide').length,
         svgs: document.querySelectorAll('.diagram svg').length,
         cues: DATA.cues.length, total: DATA.total };

// playback
window.seek(70.2); return window.state();       // scene, caption, t
document.getElementById('play').click();
return window.state();                          // t advances over time

// invitation
return { askShown: window.state().askShown,
         options: [...document.querySelectorAll('#ask button')].map(b => b.textContent) };
```

A validating plan is not a playing deck. Run all three.
