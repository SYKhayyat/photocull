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

And two that are not safe.

**A subject-sharpness comparison, when the subject was located by saliency.**
The saliency detector finds your subject by looking for the region of highest
local contrast — which is very nearly what the sharpness map measures. So asking
"is the subject sharper than its background" about a saliency box asks whether
saliency worked, not whether focus landed. It is circular, and it is not a small
effect: on a real 835-frame library the median `subject_background_ratio` was
3.44 for detected faces and 6.92 for saliency boxes, and
`subject_relative_acutance` was 1.000 at the *median* for saliency against 0.384
for faces. Only a manual box, autofocus metadata, or a detected face locates a
subject independently of sharpness — and those are exactly the detectors that
report `subject_confidence` of `high` or `medium`. The shipped rules test for
that before believing any subject figure. If you write your own, test for it too.

**Raw `subject_acutance` across frames whose subjects were found by different
detectors.** It is the peak over whichever tiles the subject box covers, and a
saliency box spanning half the frame collects a far larger value than a box over
someone's eyes. A default rule thresholded on it rated 307 landscapes five stars
and exactly 2 portraits, on the same library. The shipped rules now use
`subject_background_ratio`, which is measured inside a single frame and has no
such dependence — guarded, as above, by where the subject box came from.

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

Autofocus coordinates are recorded in sensor orientation, so on a portrait frame
the box is turned to match the rotated image before anything is measured through
it. Boxes you draw yourself are already in viewing orientation and are used as
drawn.

### Naming a frame in the subject sidecar

`photocull-subjects.json` keys boxes by filename, so the file survives you
moving or renaming the folder. Where that is not specific enough — two subfolders
of one shoot each holding a `DSC_0001.NEF` — a key may instead be any trailing
run of path components, and the longest match wins:

```json
{
  "DSC_0007.NEF":            {"x": 0.41, "y": 0.22, "w": 0.18, "h": 0.24},
  "2026-06-06/DSC_0001.NEF": {"x": 0.30, "y": 0.31, "w": 0.20, "h": 0.20}
}
```

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

Ratings are ordered rules, and the first rule that awards stars wins:

```toml
[[rating.rules]]
when = "is_group_best and subject_background_ratio >= 1.5"
stars = 5
label = "green"
reason = "best frame of its group, and focus landed on the subject"
```

Rules are expressions over the measurements — the same names you see as CSV
columns. They are validated when the config loads, so a typo is
`unknown measurement 'subject_acutence'; did you mean 'subject_acutance'?`
before a single file is opened, not a crash on frame 400. A missing measurement
compares false rather than raising, so rules stay readable without null guards.

First-match rules rather than a weighted score, deliberately: a weighted score
cannot explain itself, and every verdict here carries the reason it was reached.

### Rules that flag rather than judge

A rule with no `stars` is an **annotation**: it attaches its reason, and its
label if the deciding rule does not supply one, then lets the ladder carry on.

```toml
[[rating.rules]]
when = "highlight_clipped > 0.05"
label = "yellow"
reason = "highlights clipped beyond recovery - your call whether that matters"
```

This exists because reporting something and judging it are different jobs. A
backlit portrait with a blown rim light and perfect focus on the eyes is a
keeper; a rule set that quietly wrote it to `rejects.txt` would be overruling
the person holding the camera.

### What the defaults key on, and why

`keepers.txt`, `rejects.txt` and the XMP star ratings are the outputs you
actually cull against, so the shipped rules drive them off the comparisons that
hold — rank within a near-duplicate group, and subject against background inside
one frame. See "What the numbers can and cannot be compared against" above: an
absolute acutance threshold across a whole library is exactly the comparison
that does not work, and it is not what decides your keepers.

Every rule that mentions the subject also requires `subject_confidence` to be
`high` or `medium`, for the circularity reason set out above. A frame whose
subject was only guessed at by saliency falls through to group rank instead,
which is independent of the sharpness map entirely.

The one absolute rule that survives is `max_local_acutance < 12` — "nothing
anywhere in this frame is sharp" needs no calibration against content to be true.

A frame with no near-duplicates still has a route to keeper status, through
`subject_background_ratio`, so a unique photograph that is a keeper by every
measurement taken cannot silently fail to be listed as one.

### Calibrating

`sharp_acutance` is the number worth setting yourself. Run `photocull explain` on
a frame you know nailed focus and set it just below that frame's peak local
value. The shipped default of 40 was calibrated against a library of 24MP raws,
where peak local acutance runs about 6 at the 10th percentile and 130 at the top.

`explain` prints the verdict as well as the measurements, so you can see what the
current rules made of that frame — with the caveat that rules asking about a
frame's standing among its near-duplicates cannot fire for a single frame.

The other half of calibrating is seeing what a change moved. Keep the first run's
JSON, change a threshold, run again, and diff the two:

```
photocull run ~/shoot -o before
photocull run ~/shoot -o after -c looser.toml
photocull compare before/photocull.json after/photocull.json
```

It reports how many frames crossed the keeper and reject lines, in which
direction, and which frames moved furthest and why — rather than a text diff of
two JSON files in which every measurement shifted slightly.

## Output formats

| Format | What it is | On by default |
| --- | --- | --- |
| `json` | the canonical record, everything | yes |
| `csv` | one row per frame, every measurement a column | yes |
| `html` | the contact sheet | yes |
| `keepers` | a plain list of paths worth keeping, one per line | no |
| `rejects` | the mirror image, for review rather than deletion | no |
| `xmp` | sidecars Lightroom, darktable, RawTherapee and Bridge read | no |

The last three are opt-in. Ask for them with `-f`, which is repeatable and
overrides the config:

```
photocull run ~/shoot -f json -f html -f keepers -f xmp
```

or set `output.formats` in your config file. `keepers.txt` is deliberately dumb,
because dumb pipes well — it is the file you hand to `xargs cp`.

Existing XMP sidecars are never overwritten — yours may hold develop settings
representing real work. Inside the report directory the sidecars mirror your
folder layout rather than landing in one flat folder, because two shoots in one
library can each hold a `DSC_0001.NEF` and a flat folder would answer that by
keeping one verdict and discarding the other.

The contact sheet inlines every thumbnail, so it is one file that opens off a
USB stick in five years with no server and no asset folder. That is linear in
frame count: at roughly 22 KB apiece, five thousand frames would be a 113 MB
single file. Above `output.self_contained_max_frames` (default 1,500) the
thumbnails go to a `thumbs/` folder beside the page instead, and the page says
so in its header. Still one directory to copy, still no server.

## Formats

Plain images: JPEG, PNG, TIFF, WebP, BMP.

Raw: `.nef` `.nrw` `.cr2` `.cr3` `.arw` `.srf` `.sr2` `.dng` `.raf` `.rw2`
`.orf` `.pef`.

Most of those are TIFF containers wearing a different extension, and are read
directly. Three are not, and each needed its own handling:

| Format | Bodies | Container | How it is read |
| --- | --- | --- | --- |
| `.rw2` | Panasonic Lumix | TIFF, but magic 85 | preview under Panasonic's private tag `0x002E` |
| `.raf` | Fujifilm X, GFX | `FUJIFILMCCD-RAW` header | header points at a whole JPEG; EXIF from its APP1 segment |
| `.cr3` | Canon EOS R | ISO base media (like an MP4) | box walk to `PRVW` for the image and `CMT1`/`CMT2` for metadata |

Note for CR3: the 1620×1080 `PRVW` image is used, not the full-size JPEG in
`mdat`. It is comfortably above the 1024px working resolution everything is
measured at, so following the movie sample tables to fetch a larger image that
gets downsampled anyway would buy nothing.

All three are verified against real camera files as well as synthetic ones —
Panasonic FZ200, Fujifilm X-T4/X-Pro3/GFX100S II, Canon EOS R5/R6. That
distinction earned its keep: the synthetic fixtures passed while two real faults
were live. Canon writes eight bytes of padding before the first box inside its
preview UUID, which made every CR3 fall back to its 160×120 thumbnail — nothing
errored, the numbers just came out plausible and meaningless. And a RAF states
the frame's real dimensions only in its CFA header, so a 102MP GFX file was
being reported as a 12MP one. Synthetic containers prove arithmetic; only real
files prove the camera writes what the documentation says.

If a raw will not open, `prefer_raw_decode = true` routes through `rawpy`
instead, which handles considerably more than this does.

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
photocull explain FILE      every measurement, and the verdict, for one frame
photocull compare A B       what moved between two runs' photocull.json
photocull doctor            what this machine can actually do
photocull init              write a commented config file
photocull fetch-models      download the optional face model, once
```

## Licence

MIT.
