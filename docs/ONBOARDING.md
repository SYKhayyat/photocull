# Onboarding

Two paths through this page. Take the one you need.

- **Using it on your own photographs** — sections 1 to 6. About twenty minutes,
  most of it looking at a contact sheet.
- **Working on the code** — section 7 onward.

Section 3 is the one nobody should skip. Everything else in this tool is
straightforward; that section is where people form a wrong belief about what the
numbers mean and then trust it.

---

## Contents

- [1. Install](#1-install)
- [2. Your first run](#2-your-first-run)
- [3. What the numbers mean, and what they cannot be compared against](#3-what-the-numbers-mean-and-what-they-cannot-be-compared-against)
- [4. The contact sheet is the product](#4-the-contact-sheet-is-the-product)
- [5. Make it yours: config and rules](#5-make-it-yours-config-and-rules)
- [6. Fitting it into a workflow](#6-fitting-it-into-a-workflow)
- [7. The codebase](#7-the-codebase)
- [8. Making a change](#8-making-a-change)

---

## 1. Install

Python 3.11 or newer.

```sh
pip install -e .
```

That is a complete install. The core — raw decoding, sharpness, exposure, blur
characterisation, near-duplicate grouping, every report format — runs on **numpy
and Pillow alone**, deliberately.

Three things are optional, and each **degrades to a named fallback rather than
an import error**:

| Extra | Gains | Install |
| --- | --- | --- |
| `opencv-python-headless` + models | face and eye detection | `pip install -e ".[faces]"` then `photocull fetch-models` |
| `rawpy` | raw decoding from sensor data instead of the embedded preview | `pip install -e ".[raw]"` |
| `exiftool` on `PATH` | the camera's own autofocus point | your package manager |

Or all of it: `pip install -e ".[all]"`.

Then, always:

```sh
photocull doctor
```

It prints which capabilities are live here and, for each missing one, why. Run
it now so you know what your reports will and will not contain — a report that
never had face detection available will lean on saliency, and §3 explains why
that matters.

### Worth the effort: exiftool

`af-point` is the detector to want. It is not a guess at your subject at all —
the camera recorded where you told it to focus, so measuring acutance there
answers the purest form of the question.

It needs exiftool because autofocus metadata lives in undocumented maker-notes,
and reimplementing that badly would poison the one measurement you would trust
most.

## 2. Your first run

Point it at a real folder — a shoot you know, ideally one where you remember
which frames you thought you got.

```sh
photocull run ~/Pictures/shoot-2026-06 --open
```

```
photocull 0.1.0  |  835 file(s)  |  config: built-in defaults
  subject chain: manual -> af-point -> face -> saliency
  analysed 835/835 in 62.6s (13.3/s)
  subject located in 777 frame(s): {'saliency': 550, 'face+eyes': 227}
  572 group(s); 373 frame(s) have near-duplicates
```

Read that header. It tells you four things worth knowing before you look at a
single number:

1. **Which config was used.** `built-in defaults` means no `photocull.toml` was
   found.
2. **The detector chain**, in order.
3. **Which detectors actually answered, and how often.** In that run, saliency
   answered for 550 of 777 frames — which, per §3, changes how much weight the
   subject figures deserve.
4. **How many groups.** Grouping is by visual similarity, not capture time.

Three commands to know now:

```sh
photocull explain <file>          # every measurement and the verdict, one frame
photocull explain <file> --json   # the full record
photocull compare A B             # what moved between two runs' photocull.json
```

`explain` answers nearly every "why did it say that" question.

## 3. What the numbers mean, and what they cannot be compared against

### Why there is no single sharpness score

A single sharpness number for a whole photograph is a broken measurement, and
every tool built on one inherits the breakage.

The usual metric — variance of a Laplacian over the frame — responds to
**texture**, not focus. A brick wall scores enormous. A portrait against an
overcast sky scores low. Rank a library by it and you have ranked your
photographs by how much fine detail they contain. Worse, it punishes shallow
depth of field: the f/1.4 portrait with the beautiful soft background scores
below the f/16 landscape, so the tool marks down exactly the frames you worked
hardest to get.

photocull cuts the frame into tiles, measures each independently, and treats
every reported figure as a query against that map:

| Figure | What it asks |
| --- | --- |
| **peak local acutance** | Is anything in this frame critically sharp? Immune to bokeh — it only asks about the sharpest region. |
| **focus location** | Where the sharp region actually is. Frequently the whole story: the camera locked onto a shoulder instead of an eye. |
| **sharp fraction** | How much of the frame is in focus. **Description, never a score.** High is a landscape, low is a portrait. |
| **subject / background ratio** | Once a subject is located, sharpness inside it against sharpness outside. Comparable *across* photographs, because texture inflates both halves and cancels. |

### The comparisons that are safe

- **Within a near-duplicate group.** Same subject, same light, same framing —
  everything except focus held constant. This is the most reliable comparison
  the tool makes, and it drives group ranking.
- **`subject_background_ratio`** — inside one frame.
- **`subject_relative_acutance`** — the subject against the sharpest thing in its
  own frame. 1.0 means the subject *is* the sharpest thing present.

### The two that are not

**Do not trust a subject figure when the subject was located by saliency.**

The saliency detector finds a subject by looking for the region of highest local
contrast — which is very nearly what the sharpness map measures. So asking "is
the subject sharper than its background" about a saliency box asks whether
saliency worked, not whether focus landed. It is circular, and on a real
835-frame library:

| | detected faces | saliency boxes |
| --- | --- | --- |
| median `subject_background_ratio` | 3.44 | 6.92 |
| median `subject_relative_acutance` | 0.384 | **1.000** |

A median of exactly 1.000 is the circularity in one number.

Only a **manual box, autofocus metadata, or a detected face** locates a subject
independently of sharpness — and those are exactly the detectors reporting
`subject_confidence` of `high` or `medium`. The shipped rules test for that
before believing any subject figure. **If you write your own rules, test for it
too.**

**Do not compare raw `subject_acutance` across frames whose subjects were found
by different detectors.** It is the peak over whichever tiles the subject box
covers, so a saliency box spanning half the frame collects a far larger value
than a box over someone's eyes. A default rule thresholded on it once rated 307
landscapes five stars and exactly 2 portraits on the same library.

### What it will not do

It has no opinion about composition, expression, or whether anyone's eyes were
open. Those need judgement it does not have, and a tool that guesses at them
badly is worse than one that stays quiet.

**It never deletes anything.** Output is ratings, sidecars and lists. `rejects`
is a list for review, not an instruction.

## 4. The contact sheet is the product

Open `photocull.html`. Every frame appears with its **subject box drawn on** and
its **sharpest point marked**.

That is the whole design argument: a wrong subject is visible at a glance rather
than silently poisoning the numbers. You are not asked to trust a verdict — you
are shown where it looked.

Do this on your first run:

1. Sort by `subject_background_ratio`.
2. Look at the bottom of the list. Some of those are genuinely missed focus.
   Some are a subject box on the wrong thing.
3. For the second kind, **drag a box** on the frame.
4. Export. That writes `photocull-subjects.json`, which the `manual` detector
   reads on the next run — and `manual` is the highest-trust detector, so it wins
   the chain.

Re-run. The numbers for those frames now mean what you wanted them to mean.

### The sidecar

`photocull-subjects.json` keys boxes by filename, so it survives you moving or
renaming the folder. Where that is not specific enough — two subfolders of one
shoot each holding a `DSC_0001.NEF` — a key may be any trailing run of path
components, and the longest match wins:

```json
{
  "DSC_0007.NEF":            {"x": 0.41, "y": 0.22, "w": 0.18, "h": 0.24},
  "2026-06-06/DSC_0001.NEF": {"x": 0.30, "y": 0.31, "w": 0.20, "h": 0.20}
}
```

Coordinates are fractions of the frame.

### One file

The sheet inlines every thumbnail: no scripts fetched, no server, no asset
folder. It opens off a USB stick in five years.

That is linear in frame count, so above `output.self_contained_max_frames`
(default 1,500) thumbnails go to a `thumbs/` folder beside the page instead, and
the page says so in its header.

## 5. Make it yours: config and rules

```sh
photocull init          # writes a commented photocull.toml here
```

Every threshold, weight and rule lives in it. The nearest one is used by default;
`-c` names another and `--no-config` ignores them all.

Ratings are **ordered rules, and the first rule that awards stars wins**:

```toml
[[rating.rules]]
when = "is_group_best and subject_background_ratio >= 1.5"
stars = 5
label = "green"
reason = "best frame of its group, and focus landed on the subject"
```

Expressions are parsed and validated rather than `eval`'d, so a misspelt
measurement name is refused up front with a hint rather than producing a wrong
verdict.

### Two rules for writing rules

**Guard on `subject_confidence`** before believing any subject figure. §3 is not
advice; it is the reason the shipped rules are shaped the way they are.

**Prefer within-frame ratios to raw acutance.** `subject_background_ratio` is
measured inside a single frame and has no dependence on which detector answered.
`subject_acutance` does.

### Calibrating

Run against a folder where you already know the answer — frames you kept and
frames you did not. Then `photocull compare` two runs as you adjust thresholds.
That is a much faster loop than reasoning about the numbers in the abstract.

## 6. Fitting it into a workflow

Six output formats. Three are on by default:

| Format | What it is | Default |
| --- | --- | --- |
| `json` | the canonical record, everything | yes |
| `csv` | one row per frame, every measurement a column | yes |
| `html` | the contact sheet | yes |
| `keepers` | a plain list of paths worth keeping, one per line | no |
| `rejects` | the mirror image, for review | no |
| `xmp` | sidecars Lightroom, darktable, RawTherapee and Bridge read | no |

```sh
photocull run ~/shoot -f json -f html -f keepers -f xmp
```

`-f` is repeatable and **overrides** the config rather than adding to it.

`keepers.txt` is deliberately dumb, because dumb pipes well — it is the file you
hand to `xargs cp`.

Existing XMP sidecars are **never overwritten**; yours may hold develop settings
representing real work. Inside the report directory the sidecars mirror your
folder layout rather than landing flat, because two shoots can each hold a
`DSC_0001.NEF`.

### Speed

An 800-frame session takes about a minute because raw files are read through the
**full-size JPEG preview** every raw container already carries — a 30MB NEF is
opened by reading 2MB from a known byte offset rather than demosaicing sensor
data. Roughly two orders of magnitude faster.

The preview is camera-sharpened, so its absolute acutance runs a little high.
Every comparison is between frames measured the same way, so the offset cancels
— which is why **mixing loaders in one run is the thing to avoid**. The loader
used is recorded on every frame.

`prefer_raw_decode = true` routes through `rawpy` instead: more formats, and an
hour where the preview path takes a minute.

## 7. The codebase

No build step. `pip install -e ".[all]"` and you are set up.

```
photocull/
  cli.py            argument parsing and the six subcommands
  pipeline.py       the run: parallel analysis, then grouping, then rating
  loading.py        decode a file to pixels; picks a loader, records which
  tiffreader.py     the raw containers that are TIFF underneath
  containers.py     the three that are not: .rw2, .raf, .cr3
  exif.py           metadata, including shelling out to exiftool
  metrics/          sharpness, blur, exposure — the tile map lives here
  detect/           the subject chain: manual, af-point, face, saliency, simple
  grouping.py       near-duplicate grouping by visual similarity
  rating.py         the expression parser and the ordered-rules evaluator
  analysis.py       assembling a frame's record from the above
  compare.py        what moved between two runs
  outputs/          json, csv, contact sheet, xmp, keepers/rejects, naming
  config.py         loading and validating photocull.toml
  models.py         the record types
  errors.py         one exception hierarchy
```

### Four design rules the code holds to

1. **The core depends on numpy and Pillow only.** Anything heavier is an extra,
   and every extra degrades to a *named* fallback rather than an import error.
   `DetectorUnavailable` is explicitly never fatal — the chain treats it as "skip
   me" and records the reason so the report can explain the fallback rather than
   hiding it.
2. **Every figure records how it was obtained.** Which loader, which detector,
   the fallback trail. A number without its provenance cannot be compared safely,
   and §3 is why.
3. **One exception hierarchy**, so a caller can catch everything raised
   deliberately with a single `except PhotocullError` and never accidentally
   swallow a `KeyboardInterrupt` or a genuine bug.
4. **Nothing is destructive.** No deletes, no overwriting an existing XMP.

### The rating expression evaluator

`rating.py` parses expressions rather than `eval`ing them: unknown names,
unsupported operators and banned constructs are all refused at parse time, with
a hint where one is available. If you add a measurement, it must be registered
where the validator can see it or every rule that uses it will be rejected.

## 8. Making a change

```sh
pytest
ruff check .
```

**There is no CI.** Nothing runs the suite automatically, so those two commands
before you push are the entire safety net.

`filterwarnings = ["error::DeprecationWarning"]` turns deprecations into test
failures, deliberately — they get dealt with when they appear rather than when
they are removed. Fix the deprecation; do not relax the filter.

Ruff is configured for line length 100, target py311, rule sets `E`, `F`, `I`,
`UP`, `B`, `SIM`, `RUF`.

### The lesson the fixtures taught

**Synthetic containers prove arithmetic; only real files prove the camera writes
what the documentation says.**

Two faults were live while the synthetic fixtures passed:

- Canon writes eight bytes of padding before the first box inside its CR3
  preview UUID, which made **every CR3 fall back to its 160×120 thumbnail**.
  Nothing errored — the numbers just came out plausible and meaningless.
- A RAF states the frame's real dimensions only in its CFA header, so a 102MP
  GFX file was reported as a 12MP one.

Container work is verified against real camera files: Panasonic FZ200, Fujifilm
X-T4 / X-Pro3 / GFX100S II, Canon EOS R5 / R6. If you have raw files from a body
outside that set, running the suite against them is the most valuable
contribution available.

### Prior audits

Two documents in the repository are worth reading before a substantial change:

- `code-sweep-2026-08-18.md` — nine findings with severities and file
  references. Several are fixed; it still reads as an accurate map of where the
  sharp edges are.
- `lamdan/photocull-2026-08-18.md` — a design critique, arguing about whether
  the architecture is right rather than whether the code is correct.

---

## Where to go next

- [../README.md](../README.md) — the full reference: formats, detectors, output,
  configuration.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — symptom-first, starting with
  `photocull doctor`.
