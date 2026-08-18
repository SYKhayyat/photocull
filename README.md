# photocull

Measures where focus actually landed in your photographs, and shows its work.

It does not tell you which pictures are good. It tells you which ones are
technically sound, why it thinks so, and exactly where it looked — so checking a
verdict takes a second instead of requiring you to trust it.

```
photocull run ~/Pictures/shoot-2026-06 --open
```

```
photocull 0.1.0  |  835 file(s)  |  config: built-in defaults
  subject chain: manual -> af-point -> face -> saliency
  analysed 835/835 in 62.6s (13.3/s)
  subject located in 777 frame(s): {'saliency': 550, 'face+eyes': 227}
  572 group(s); 373 frame(s) have near-duplicates
```

## Why not just measure sharpness

Because a single sharpness number for a whole photograph is a broken
measurement, and every tool built on one inherits the breakage.

The usual metric — the variance of a Laplacian over the frame — responds to
**texture**, not focus. A brick wall scores enormous. A portrait against an
overcast sky scores low. Rank a library by it and you have ranked your
photographs by how much fine detail they contain, which is not a thing anyone
wanted to know. Worse, it punishes shallow depth of field: the f/1.4 portrait
with the beautiful soft background scores below the f/16 landscape, so the tool
marks down exactly the frames you worked hardest to get.

photocull stops producing that scalar. It cuts the frame into tiles, measures
each independently, and treats every reported figure as a query against that
map:

- **peak local acutance** — is anything in this frame critically sharp? Immune
  to bokeh, because it only asks about the sharpest region.
- **focus location** — where the sharp region actually is. Frequently the whole
  story: you find out the camera locked onto a shoulder instead of an eye.
- **sharp fraction** — how much of the frame is in focus. Reported as
  description, never as a score. High is a landscape, low is a portrait.
- **subject / background ratio** — once a subject is located, sharpness inside
  it against sharpness outside it. This one is comparable *across* photographs:
  texture inflates both halves equally and cancels, leaving a number that means
  the same thing in every frame — did focus land where it should have.

A tack-sharp face against a soft background scores high. A sharp background with
a soft face — the shot you thought you got — scores low, and scores low loudly.

## What the numbers can and cannot be compared against

This matters more than any feature, so it is stated plainly rather than buried.

**Acutance is only comparable between frames of similar content.** It responds to
texture, and no amount of tiling removes that — a sweater has more fine detail
than skin, so a perfectly focused eye can measure lower than an out-of-focus
cardigan in the same photograph. Comparisons that are safe:

- **Within a near-duplicate group.** Same subject, same light, same framing, so
  everything except focus is held constant. This is the most reliable comparison
  the tool makes, and it is what drives group ranking.
- **Subject against background, inside one frame** — `subject_background_ratio`.
  Texture inflates both halves, so it largely cancels.
- **Subject against the sharpest thing in its own frame** —
  `subject_relative_acutance`. 1.0 means the subject *is* the sharpest thing
  present.

And one that is not safe, which shipped broken in the first draft: **raw
`subject_acutance` across frames whose subjects were found by different
detectors.** It is the peak over whichever tiles the subject box covers, and a
saliency box spanning half the frame collects a far larger value than a box over
someone's eyes. A default rule thresholded on it rated 307 landscapes five stars
and exactly 2 portraits, on the same library. The shipped rules now use
`subject_background_ratio`, which is measured inside a single frame and has no
such dependence.

## What it will not do

It has no opinion about composition, expression, or whether anyone's eyes were
open. Those need judgement it does not have, and a tool that guesses at them
badly is worse than one that stays quiet. It measures technical execution and
says so.

It also never deletes anything. Output is ratings, sidecars and lists.

## Install

Python 3.11+.

```
pip install -e .
```

Nothing else is required. The core — raw decoding, sharpness, exposure, blur
characterisation, near-duplicate grouping, every report format — runs on numpy
and Pillow alone.

Optional, and each one degrades to a named fallback rather than an error:

| Extra | Gains |
| --- | --- |
| `opencv-python` + `photocull fetch-models` | face and eye detection |
| `rawpy` | raw decoding from sensor data instead of the embedded preview |
| `exiftool` on PATH | the camera's own autofocus point |

`photocull doctor` prints exactly which of these are live here, and why each
missing one is missing.

## Subject detection

The subject box is what makes the ratio meaningful, so photocull tries several
ways to find one and always records which answered:

| Detector | How it decides | Trust |
| --- | --- | --- |
| `manual` | a box you drew yourself | highest |
| `af-point` | where the camera was told to focus, from maker-notes | highest |
| `face` | face detection, narrowed to the eyes via landmarks | high |
| `saliency` | spectral residual — statistically unusual regions | low |
| `zone` | a fixed region of the frame; an assumption | low |
| `none` | no subject; whole-frame figures only, still meaningful | — |

`af-point` deserves the attention. It is not a guess at your subject at all: the
camera recorded where you told it to focus, so measuring acutance there answers
the purest form of the question. It needs exiftool, because autofocus metadata
lives in undocumented maker-notes and reimplementing that badly would poison the
one measurement you would trust most.

Detectors run as a chain — first to find a subject wins — and the fallback trail
is recorded on the frame, so a report never leaves you wondering why a weaker
detector answered.

## The contact sheet

The HTML report is the point. Every frame appears with its subject box drawn on
and its sharpest point marked, so a wrong subject is visible at a glance rather
than silently poisoning the numbers. Sort by any measurement, filter to problems
or to group-bests, and drag a box on any frame to overrule the automatic
subject — export writes the sidecar the `manual` detector reads on the next run.

One self-contained file. Thumbnails inlined, no scripts fetched, no server. It
opens off a USB stick in five years.

## Near-duplicate grouping

Frames are grouped by **visual similarity**, not capture time. Timestamp
clustering only finds bursts, and plenty of people do not shoot bursts — three
considered frames of the same composition over two minutes are the same decision
to make and invisible to a time-based grouper. Within each group the frames are
ranked, and `is_group_best` is available to your rules.

## Configuration

Every threshold, weight and rule lives in `photocull.toml`. `photocull init`
writes a commented copy.

Ratings are ordered rules, first match wins:

```toml
[[rating.rules]]
when = "subject_found and subject_acutance >= 40 and highlight_clipped <= 0.02"
stars = 5
label = "green"
reason = "subject critically sharp, highlights intact"
```

Rules are expressions over the measurements — the same names you see as CSV
columns. They are validated when the config loads, so a typo is
`unknown measurement 'subject_acutence'; did you mean 'subject_acutance'?`
before the run starts, not a crash on frame 400. A missing measurement compares
false rather than raising, so rules stay readable without null guards.

First-match rules rather than a weighted score, deliberately: a weighted score
cannot explain itself, and every verdict here carries the reason it was reached.

### Calibrating

`sharp_acutance` is the number worth setting yourself. Run `photocull explain` on
a frame you know nailed focus and set it just below that frame's peak local
value. The shipped default of 40 was calibrated against a library of 24MP raws,
where peak local acutance runs about 6 at the 10th percentile and 130 at the top.

## Output formats

`json` (canonical, everything), `csv`, `html`, `keepers` and `rejects` (plain
path lists that pipe), and `xmp` sidecars carrying star ratings and colour
labels that Lightroom, darktable, RawTherapee and Bridge read natively.

Existing XMP sidecars are never overwritten — yours may hold develop settings
representing real work.

## Performance

Raw files are read through the full-size JPEG preview every raw container
already carries: a 30MB NEF is opened by reading 2MB from a known byte offset
rather than demosaicing sensor data. That is roughly two orders of magnitude
faster and is what makes an 800-frame session take a minute rather than an hour.

The preview is camera-sharpened, so its absolute acutance runs a little high.
Every comparison is between frames measured the same way, so the offset cancels
— and the loader used is recorded on every report, because mixing preview-loaded
raws with `prefer_raw_decode` conversions in one run would compare two different
scales.

## Commands

```
photocull run PATH          analyse a folder, write reports
photocull explain FILE      every measurement for one frame
photocull doctor            what this machine can actually do
photocull init              write a commented config file
photocull fetch-models      download the optional face model, once
```

## Licence

MIT.
