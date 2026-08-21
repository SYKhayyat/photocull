# Troubleshooting

**Run this first.** It answers more questions than the rest of this page put
together:

```
photocull doctor
```

It reports which optional capabilities are live on this machine and, for each
missing one, *why* it is missing. Give it a path and it also reports what it
found there:

```
photocull doctor ~/Pictures/shoot-2026-06
```

The second most useful command is `explain`, which prints every measurement and
the verdict for a single frame:

```
photocull explain ~/Pictures/shoot/DSC_0007.NEF
photocull explain ~/Pictures/shoot/DSC_0007.NEF --json
```

Most "why did it rate this that way" questions are one `explain` away.

---

## Contents

- [Install and dependencies](#install-and-dependencies)
- [Files that will not open](#files-that-will-not-open)
- [Subject detection](#subject-detection)
- [Numbers that look wrong](#numbers-that-look-wrong)
- [Ratings](#ratings)
- [Configuration](#configuration)
- [Reports and output](#reports-and-output)
- [Speed and memory](#speed-and-memory)
- [Development](#development)

---

## Install and dependencies

### `photocull: command not found`

The console script is installed by `pip install -e .`. If the install
succeeded and the command is missing, your `pip` and your `python` are different
environments, or the scripts directory is not on `PATH`.

```sh
python -m photocull --version     # works regardless of PATH
```

`python -m photocull` is a complete substitute for the `photocull` command
everywhere on this page.

### Python version

3.11 or newer. Check with `python --version`. `.python-version` in the
repository names the version used for development.

### An optional feature is not working

**Nothing here is fatal.** Every optional dependency degrades to a named
fallback rather than an import error — that is a design rule, not an accident.
`photocull doctor` tells you which fell back and why.

| Missing | Consequence |
| --- | --- |
| `opencv-python-headless` | no face or eye detection; the chain falls through to saliency |
| the face model files | same, even with opencv installed — run `photocull fetch-models` |
| `rawpy` | raw files are read through their embedded preview only; `prefer_raw_decode` cannot work |
| `exiftool` on `PATH` | no `af-point` detector, which is the *most* trustworthy one |

```sh
pip install -e ".[all]"      # opencv, rawpy and pytest
photocull fetch-models       # the face model, one time
```

### `photocull fetch-models` fails

It downloads model files once. Failures are almost always network or proxy.
`--force` re-downloads even if files are present, which is the fix if a previous
download was truncated.

If your environment cannot reach the internet, face detection is simply
unavailable here. Everything else works, and the reports will name `saliency` as
the detector that answered — see
[the saliency warning](#the-numbers-look-too-good-and-the-detector-was-saliency).

### opencv is installed and faces still are not detected

Two separate requirements: the library **and** the model files. `photocull
fetch-models` gets the second. `photocull doctor` distinguishes them.

### exiftool is installed but `af-point` never fires

Check it is on the `PATH` the process actually sees:

```sh
exiftool -ver
```

Then check the files. Autofocus point lives in undocumented maker-notes, so it
is camera-dependent — some bodies do not record it, and it is absent from
plain JPEGs exported from an editor. The detector chain records why it skipped,
and the report shows the fallback trail.

This is worth chasing, because `af-point` is not a guess at your subject at all:
the camera recorded where you told it to focus. Reimplementing maker-note
parsing badly would poison the one measurement you would trust most, which is
why exiftool is shelled out to rather than replaced.

## Files that will not open

### A raw file is skipped or reported unreadable

Try the other decoder:

```toml
prefer_raw_decode = true
```

This routes through `rawpy`, which handles considerably more formats than the
built-in container readers do. It is much slower — see
[Speed](#a-run-is-far-slower-than-the-readme-suggests) — and it changes the
measurement scale, so do not mix it with preview-loaded frames inside one run.

If `rawpy` is not installed, install it: `pip install -e ".[raw]"`.

### The numbers for a CR3 look plausible but meaningless

The symptom to watch for is a frame whose measurements are internally
consistent and simply wrong — nothing errors.

photocull reads the 1620×1080 `PRVW` image from a CR3, which is comfortably
above the 1024px working resolution. There was a real fault here once: Canon
writes eight bytes of padding before the first box inside its preview UUID, and
before that was handled every CR3 fell back to its **160×120 thumbnail**. That
is fixed, but it is the shape of failure to expect from container parsing —
plausible numbers from the wrong image.

`photocull explain <file> --json` reports the loader and the dimensions it
actually read. If the dimensions look like a thumbnail, that is your answer.

### A Fujifilm RAF reports the wrong resolution

Also fixed, and also worth knowing about: a RAF states the frame's real
dimensions only in its CFA header, and reading them from elsewhere reported a
102MP GFX file as a 12MP one.

Check with `explain --json`. Real camera files from a body not in the tested set
— FZ200, X-T4, X-Pro3, GFX100S II, EOS R5, EOS R6 — are the most likely place
for a new instance of this.

Synthetic fixtures prove arithmetic; only real files prove the camera writes what
the documentation says. A report with a real file attached is genuinely useful.

### A plain JPEG has almost no EXIF

Known, and it limits what can be reported for non-raw files — the raw containers
carry richer metadata and are parsed more thoroughly. It does not affect the
sharpness measurements, which are made from pixels.

### Which formats are supported?

Plain: JPEG, PNG, TIFF, WebP, BMP.

Raw: `.nef` `.nrw` `.cr2` `.cr3` `.arw` `.srf` `.sr2` `.dng` `.raf` `.rw2`
`.orf` `.pef`.

Most are TIFF containers wearing a different extension. Three are not — `.rw2`
(TIFF with magic 85, preview under Panasonic's private tag `0x002E`), `.raf`
(`FUJIFILMCCD-RAW` header pointing at a whole JPEG), and `.cr3` (ISO base media,
box-walked to `PRVW`).

## Subject detection

### No subject was found in a frame

Not an error. Whole-frame figures are still meaningful and still reported; only
the subject-relative ones are absent. The report records `none` as the detector.

### The subject box is on the wrong thing

Open the contact sheet. Every frame has its subject box drawn on and its
sharpest point marked, precisely so a wrong subject is visible at a glance
rather than silently poisoning the numbers.

Then drag a new box on the frame and export. That writes
`photocull-subjects.json`, which the `manual` detector reads on the next run —
and `manual` is the highest-trust detector, so it wins the chain.

### My manual subject box is being ignored

The sidecar keys boxes **by filename**, so the file survives you moving or
renaming the folder. Two ways that goes wrong:

1. **Two files with the same name in different subfolders.** A key may be any
   trailing run of path components, and the longest match wins:
   ```json
   {
     "DSC_0007.NEF":            {"x": 0.41, "y": 0.22, "w": 0.18, "h": 0.24},
     "2026-06-06/DSC_0001.NEF": {"x": 0.30, "y": 0.31, "w": 0.20, "h": 0.20}
   }
   ```
2. **The sidecar is not where the run looks for it.** It belongs beside the
   images, named `photocull-subjects.json`.

Coordinates are fractions of the frame, not pixels.

### The box is rotated wrong on a portrait frame

Autofocus coordinates are recorded in **sensor orientation**, so on a portrait
frame the box is turned to match the rotated image before anything is measured
through it. Boxes you draw yourself are already in viewing orientation and are
used as drawn.

If a manual box appears rotated, you may have hand-written it in sensor
coordinates. Draw it in the contact sheet instead.

### Why did a weaker detector answer?

The chain runs first-to-find-a-subject-wins, in order: `manual`, `af-point`,
`face`, `saliency`, `zone`, `none`. The fallback trail is recorded on the frame,
so the report never leaves you guessing. `explain --json` shows it.

A `DetectorUnavailable` — missing optional dependency or missing model data — is
never fatal; the chain treats it as "skip me" and records the reason.

## Numbers that look wrong

### An out-of-focus sweater measures sharper than an in-focus eye

Correct behaviour, and the single most important thing to understand about this
tool.

**Acutance is only comparable between frames of similar content.** It responds
to texture, and no amount of tiling removes that. A sweater has more fine detail
than skin.

Comparisons that are safe:

- **Within a near-duplicate group** — same subject, same light, same framing, so
  everything except focus is held constant. This is the most reliable comparison
  the tool makes and it is what drives group ranking.
- **`subject_background_ratio`** — subject against background inside one frame.
  Texture inflates both halves and largely cancels.
- **`subject_relative_acutance`** — subject against the sharpest thing in its own
  frame. 1.0 means the subject *is* the sharpest thing present.

### The numbers look too good, and the detector was saliency

This is the trap, and it is not a small effect.

The saliency detector finds a subject by looking for the region of **highest
local contrast** — which is very nearly what the sharpness map measures. So
asking "is the subject sharper than its background" about a saliency box asks
whether saliency worked, not whether focus landed. It is circular.

On a real 835-frame library:

| | detected faces | saliency boxes |
| --- | --- | --- |
| median `subject_background_ratio` | 3.44 | 6.92 |
| median `subject_relative_acutance` | 0.384 | **1.000** |

A median of exactly 1.000 means the saliency box was, more often than not,
placed on the sharpest thing in the frame by construction.

Only a **manual box, autofocus metadata, or a detected face** locates a subject
independently of sharpness — and those are exactly the detectors that report
`subject_confidence` of `high` or `medium`. The shipped rules test for that
before believing any subject figure.

**If you write your own rules, test for it too.**

### `sharp_fraction` is low and I expected a high score

`sharp_fraction` is reported as **description, never as a score**. High is a
landscape; low is a portrait. It is not better or worse, and nothing should
threshold on it as though it were.

### A single sharpness number would be easier

And broken. The usual metric — variance of a Laplacian over the whole frame —
responds to texture rather than focus. A brick wall scores enormous, a portrait
against an overcast sky scores low, and shallow depth of field is punished: the
f/1.4 portrait with the beautiful soft background scores below the f/16
landscape. That ranks your library by how much fine detail it contains, which is
not a thing anyone wanted to know.

photocull deliberately does not produce that scalar.

### Two runs disagree and I changed nothing

```sh
photocull compare before/photocull.json after/photocull.json
```

That says what moved. The usual causes:

- **A different loader.** Mixing preview-loaded raws with `prefer_raw_decode`
  conversions compares two different scales. The loader is recorded on every
  frame for this reason.
- **A different detector answered**, because an optional dependency appeared or
  disappeared. `doctor` on both machines will show it.
- **The config changed** — including a `photocull.toml` found in a parent
  directory that was not there before.

### Preview-loaded raws read slightly sharp

Expected. The embedded preview is camera-sharpened, so its absolute acutance runs
a little high. Every comparison is between frames measured the same way, so the
offset cancels — which is exactly why mixing loaders in one run is the thing to
avoid.

## Ratings

### Everything is rated five stars, or nothing is

Look at what your rules key on.

A real instance: a default rule thresholded on raw `subject_acutance` rated
**307 landscapes five stars and exactly 2 portraits** on one library. The cause
is that `subject_acutance` is the peak over whichever tiles the subject box
covers, so a saliency box spanning half the frame collects a far larger value
than a box over someone's eyes — and it is therefore not comparable across
frames whose subjects were found by different detectors.

The shipped rules use `subject_background_ratio` instead, which is measured
inside a single frame and has no such dependence, guarded by a test on where the
subject box came from.

### A frame is rated in a way I do not understand

```sh
photocull explain <file>
```

Ratings are **ordered rules and the first rule that awards stars wins**. `explain`
shows the measurements and the verdict, so you can see which rule matched.

```toml
[[rating.rules]]
when = "is_group_best and subject_background_ratio >= 1.5"
stars = 5
label = "green"
reason = "best frame of its group, and focus landed on the subject"
```

Reordering the rules changes the outcome. That is the usual fix.

### `is_group_best` is not what I expected

Grouping is by **visual similarity, not capture time**. Timestamp clustering
only finds bursts, and three considered frames of the same composition over two
minutes are the same decision to make and invisible to a time-based grouper.

`--no-group` skips grouping entirely, which also makes `is_group_best`
unavailable to your rules.

### It rated a badly composed photograph highly

It has no opinion about composition, expression, or whether anyone's eyes were
open. Those need judgement it does not have, and a tool that guesses at them
badly is worse than one that stays quiet. It measures technical execution and
says so.

### Did it delete anything?

**Never.** Output is ratings, sidecars and lists. `rejects` is a list for review,
not an instruction.

## Configuration

### A rule expression is rejected

`ExpressionError` names the rule and the reason. Expressions are parsed and
validated rather than `eval`'d, so unknown measurement names, unsupported
operators and banned constructs are all refused up front — with a hint where one
is available.

A misspelt measurement name is the most common cause. `photocull explain <file>
--json` lists every name that exists.

### My config file is not being read

The nearest `photocull.toml` is used by default. Two flags settle it:

```sh
photocull run PATH -c /explicit/path/photocull.toml
photocull run PATH --no-config          # built-in defaults, ignore all files
```

The run header prints which config was used — `config: built-in defaults` means
none was found or `--no-config` was passed.

```sh
photocull init                # write a commented default config here
photocull init --force        # overwrite an existing one
```

### A config typo used to produce an unreadable traceback

It did — a misspelt zone came back as a `BrokenProcessPool` traceback from the
worker pool, saying nothing about the config file. Config is now validated
before workers are started, so errors arrive as `ConfigError` with the offending
key named.

If you still see a `BrokenProcessPool` from a config problem, that is a bug
worth reporting with the config attached.

## Reports and output

### The contact sheet is enormous

It inlines every thumbnail so it is one self-contained file that opens off a USB
stick in five years with no server and no asset folder. That is linear in frame
count — at roughly 22 KB apiece, five thousand frames would be a 113 MB single
file.

Above `output.self_contained_max_frames` (default 1,500) the thumbnails go to a
`thumbs/` folder beside the page instead, and the page says so in its header.
Still one directory to copy, still no server.

Lower the threshold if you want the split sooner.

### I asked for keepers/rejects/XMP and got none

Those three are **opt-in**. `json`, `csv` and `html` are on by default.

```sh
photocull run ~/shoot -f json -f html -f keepers -f xmp
```

`-f` is repeatable and **overrides** the config, rather than adding to it. Or
set `output.formats` in the config file.

### XMP sidecars were not written for some frames

Existing XMP sidecars are **never overwritten** — yours may hold develop
settings representing real work. That is deliberate and there is no flag to
force it; move or delete the sidecar if you want it regenerated.

Inside the report directory the sidecars mirror your folder layout rather than
landing in one flat folder, because two shoots in one library can each hold a
`DSC_0001.NEF` and a flat folder would answer that by keeping one verdict and
discarding the other.

### `--open` did nothing

It asks the OS to open the contact sheet. On a headless machine, over SSH, or in
a container there is nothing to open it with. Open
`<report-dir>/photocull.html` yourself.

### Where did the reports go?

Next to the analysed folder unless you said otherwise:

```sh
photocull run ~/shoot -o ~/reports/shoot
```

### `keepers.txt` seems too simple

Deliberately. Dumb pipes well — it is the file you hand to `xargs cp`.

## Speed and memory

### A run is far slower than the README suggests

The headline figure — an 800-frame session in about a minute — depends on raw
files being read through the **embedded full-size JPEG preview** that every raw
container already carries. A 30MB NEF is opened by reading 2MB from a known byte
offset rather than demosaicing sensor data, which is roughly two orders of
magnitude faster.

If you set `prefer_raw_decode = true`, you gave that up. That is the trade:
`rawpy` handles more formats and takes an hour where the preview path takes a
minute.

Other causes:

- **A slow or network filesystem.** Every frame is read once.
- **Too few workers.** Default is CPU count capped at 8.
  ```sh
  photocull run PATH -j 8
  ```
- **Grouping on a very large set.** `--no-group` skips it, at the cost of
  `is_group_best`.

### It used too much memory, or a worker died

Reduce the worker count:

```sh
photocull run PATH -j 2
```

Each worker decodes an image independently, so peak memory scales with worker
count times frame size — and raw previews from a 100MP body are not small.

### Can I run it on one file?

Yes. `run` takes a file or a folder, and `explain` takes a single image.

## Development

```sh
pip install -e ".[all]"
pytest
```

### Tests fail on a `DeprecationWarning`

Intentional. `filterwarnings = ["error::DeprecationWarning"]` in
`pyproject.toml` turns them into failures, so a deprecation is dealt with when
it appears rather than when it is removed.

Fix the deprecation. Do not relax the filter.

### Lint

```sh
ruff check .
```

Configured in `pyproject.toml`: line length 100, target py311, rule sets
`E`, `F`, `I`, `UP`, `B`, `SIM`, `RUF`.

### There is no CI

Correct — nothing runs the suite automatically. Run `pytest` and `ruff check .`
before you push; nothing else will.

### A test needs real camera files

Some behaviour can only be verified against them. The synthetic fixtures passed
while two real faults were live — the CR3 padding byte and the RAF dimension
header — so a fixture that proves arithmetic is not a fixture that proves the
camera writes what the documentation says.

If you have raw files from a body outside the tested set, running the suite
against them is the most valuable contribution available.

---

## Reporting something not on this page

Include:

```sh
photocull --version
photocull doctor
photocull explain <the file> --json
```

plus the camera body and format if it is a decoding problem, and the relevant
part of your `photocull.toml` if it is a rating one.
