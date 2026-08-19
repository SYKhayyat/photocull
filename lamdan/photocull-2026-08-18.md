# Lamdan review: photocull

Date: 2026-08-18
Scope: full sweep, all 36 tracked files / 4,812 lines. Nothing excluded.
Method: design committed before reading implementations; every region read;
git history used only to rank findings, never to select them.

---

## Summary

photocull is the right software, built in largely the right shape, on a core
measurement I tried to design better and could not. The thesis — that a single
scalar sharpness figure is a broken measurement, and the fix is a tiled acutance
map that every reported figure queries — is correct, well argued, and correctly
implemented.

The findings below are concentrated in one place: the layer that turns
measurements into verdicts. That layer makes absolute-threshold judgements on
numbers the project's own documentation spends two sections explaining are not
absolutely comparable, and the most consequential outputs (keepers.txt,
rejects.txt, XMP star ratings) key off it.

Ranked by wrongness multiplied by the cost of leaving it in place.

---

## 1. Star ratings and group ranking are two separate concerns, and conflating them puts the least trustworthy number in charge of the most consequential output

**Lens 2 (architecture). Verdict: rewrite.**

The project makes two kinds of comparison, and it documents the difference
better than most projects document anything:

- **Within a near-duplicate group** — same subject, same light, same framing.
  The README calls this "the most reliable comparison the tool makes."
- **Absolute thresholds across a library** — the README's own section "What the
  numbers can and cannot be compared against" exists to explain why these are
  dangerous, and records that a threshold rule on `subject_acutance` rated 307
  landscapes five stars and 2 portraits on a real library.

The shipped defaults route the consequential outputs through the second kind.
`DEFAULT_RULES` in `photocull/config.py:309` is a ladder of absolute thresholds;
`KeeperListWriter` (`photocull/outputs/machine.py:171`) selects on
`rating >= 4`; `RejectListWriter` (`:190`) selects on `rating <= 2`; `XmpWriter`
writes `xmp:Rating` into the files the photographer will actually cull in.
Group rank — the reliable signal — reaches that pipeline only through a single
rule, `group_size > 1 and is_group_best`, which sits fifth in a first-match
ladder and is unreachable for any frame that is not part of a multi-frame group.

The concrete consequence, from reading the rule order:

- Rule 3, `highlight_clipped > 0.05` → 2 stars, fires before the group-best rule.
  A backlit portrait with a blown rim-light and perfect eye focus is rated 2 and
  written to `rejects.txt`.
- This contradicts a principle the codebase states explicitly.
  `photocull/metrics/exposure.py` says of clipping: "Reported, never scored.
  ... that is the photographer's call, not this tool's." The shipped defaults
  score it, and the score is decisive.
- A unique frame (`group_size == 1`) with excellent focus but a
  `subject_background_ratio` of 2.8 lands at 3 stars, so it appears in neither
  keepers nor rejects. The tool's headline promise is finding the keepers; a
  frame that is a keeper by every measurement it took can silently fail to be
  listed as one.

**Steelman.** This is right if the star rating is understood as a rough triage
signal for a human who will open the contact sheet anyway, and if keepers.txt is
a convenience rather than a decision. It is also right if photographers really
do want an absolute technical bar — some do, particularly for client delivery
where a blown highlight genuinely disqualifies a frame regardless of focus.

Neither defence survives the packaging. `keepers.txt` is documented as the file
you pipe to `xargs cp`; that is a decision, not a hint. And the absolute-bar
argument requires the thresholds to be calibrated, which the project's own
history says they were not, and which the README says cannot be done in a way
that holds across content.

**The change.** Separate the two concerns rather than expanding the rule set.

1. Make the shipped `keepers`/`rejects` defaults key on group rank plus
   `subject_background_ratio` — both within-frame or within-group comparisons —
   and leave absolute-threshold rules for users who opt into them.
2. Move rule 3 (highlight clipping) below the group-best rule, or drop the star
   penalty and surface clipping as a label only. A measurement the code
   describes as not-a-judgement should not be able to reject a frame on its own.
3. Give ungrouped frames a path to keeper status that does not depend on an
   absolute acutance threshold.

**The cost.** Small and local. `DEFAULT_RULES` is a literal; the writers already
read `rating`. Roughly a dozen lines plus a note in the README. No call sites
outside `config.py` change. Existing user configs are unaffected, because a
config that supplies `rating.rules` replaces the defaults wholesale.

---

## 2. The autofocus detector runs a full-tree exiftool scan once per worker process

**Lens 2/3. Verdict: rewrite.**

`AFPointDetector` (`photocull/detect/afpoint.py:50`) is the best idea in the
package: it does not guess at the subject, it reads where the camera was told to
focus. Its docstring correctly identifies the performance trap — "Spawning one
process per file would cost more than the entire rest of the analysis" — and
batches the whole tree into one exiftool invocation.

That batching is then defeated by the process pool. `pipeline._init_worker`
(`photocull/pipeline.py:34`) constructs an `Analyzer` per worker process, which
builds its own detector chain, which builds its own `AFPointDetector`. The scan
is lazy, triggered from `available()` on the worker's first frame, so each of
the (default: up to 8) workers independently runs
`exiftool -j -n -q -r <whole tree>` and independently parses the result.

The per-file cost was solved and a per-worker cost was introduced in its place.
On the README's own 835-frame example this is eight recursive metadata scans of
835 raw files, running concurrently and contending for the same disk, each under
a 300-second timeout. This fires on every default run on a machine with exiftool
installed, because `af-point` is second in the default detector chain.

**Steelman.** Lazy per-worker loading keeps `AFPointDetector` self-contained —
it needs no cooperation from the pipeline, and the same object works in the
single-worker path, in `explain`, and in `doctor` with no special casing. That
is real design value, and it is why the code looks reasonable in isolation.

It does not survive contact with the pool. The detector's own docstring
establishes that this metadata fetch is expensive enough to warrant a global
batch; a global batch executed N times is not a global batch.

**The change.** Hoist the scan into the parent. Run the exiftool pass once in
`pipeline.run()` before the pool is created, and pass the resulting
filename-to-box mapping through `_init_worker`'s `initargs` — it is a small,
picklable dict. `AFPointDetector` grows an optional pre-loaded mapping and keeps
its current lazy path for the single-process callers.

**The cost.** Roughly 15 lines across `afpoint.py` and `pipeline.py`. The
mapping is a few hundred bytes per frame, so passing it to workers is cheap.
Incremental and independently testable.

---

## 3. Rating rules are validated after the entire run, which is the exact failure the module exists to prevent

**Lens 3. Verdict: rewrite. Cheapest fix here by a wide margin.**

`photocull/rating.py` opens by explaining why it implements an AST interpreter
rather than calling `eval`: so that a whitelist "can say `subject_sharpnes` is
not a known measurement, did you mean `subject_acutance`? at load time, where
`eval` would raise `NameError` on photograph 400 of 800." The README repeats the
promise: "validated when the config loads ... before the run starts, not a crash
on frame 400."

The expressions are compiled in `pipeline.run()`, at `photocull/pipeline.py:70`
(`rank_by`) and `:143` (the rule set) — both after every file has been analysed.

Measured directly: a config with `subject_acutence` (one transposed letter, the
exact typo the docstring cites) against a six-file folder raised the error after
2.3 seconds of completed analysis. The error message itself is excellent and
suggests the correction. It simply arrives after all the work. Scaled to the
README's own example run, that is 62.6 seconds discarded.

**Steelman.** Compiling in `run()` is where the measurement names are known:
`Rater` is constructed with `sorted(reports[0].flat_metrics())`, and that
namespace is derived from a real report rather than duplicated as a constant.
That is a genuinely good instinct — it guarantees the names a user may write are
exactly the names the CSV will contain, which is a property worth protecting.

The property is worth protecting and does not require waiting. The name set
depends only on the *shape* of `PhotoReport`, not on any file's contents.

**The change.** Build the known-name set from a zero-valued `PhotoReport`
sentinel (or a `flat_metric_names()` classmethod that `flat_metrics` is
implemented in terms of), and compile `config.rating.rules` and
`config.rating.rank_by` in `command_run` immediately after `_load_config`,
before `discover`. Keep the construction in `run()` as well; it becomes a
no-cost re-validation.

**The cost.** Around five lines, plus one test asserting a bad rule raises
before any file is read. No behaviour change for valid configs.

---

## 4. Three of the twelve advertised raw formats cannot be opened at all

**Lens 3 / correctness. Verdict: rewrite for two of them, delete the third's claim.**

`RAW_SUFFIXES` (`photocull/loading.py:51`) advertises twelve extensions.
`RawPreviewLoader` reaches all of them through `TiffReader`, which requires a
TIFF header at byte 0 and a magic number in `(42, 0x4F52, 0x5352)`
(`photocull/tiffreader.py:363`). Three listed formats are not that. Verified
against real container headers:

| Format | Bodies affected | Container | Result |
| --- | --- | --- | --- |
| `.cr3` | every Canon EOS R series | ISO BMFF (`ftyp`/`crx`) | `not a TIFF container: byte order mark b'\x00\x00'` |
| `.raf` | every Fujifilm X and GFX | `FUJIFILMCCD-RAW` header, TIFF at an internal offset | `not a TIFF container: byte order mark b'FU'` |
| `.rw2` | every Panasonic Lumix | TIFF byte order, magic 85 | `unexpected TIFF magic 85` |

Each becomes an `UnreadableImage`, which `Analyzer.analyse` converts into a
failed-report row. So the behaviour is not a crash — it is a run where every
frame silently reports "not a readable raw container". A photographer pointing
this at a Lumix or Fuji shoot gets a full, well-formatted report of nothing.

**Steelman.** The failure is honest and non-fatal: the row carries the reason,
`doctor` reports what is available, and nothing is deleted or corrupted. The
extension list may also be forward-looking, on the theory that a `rawpy`
installation covers these via `RawDecodeLoader`. That last defence is real but
narrow — `prefer_raw_decode` is off by default, so the default path fails.

**The change.**

- **RW2**: add 85 to the accepted magic set. One line.
- **RAF**: the file header carries the offset of an embedded TIFF/JPEG block;
  `TiffReader` already accepts a `base` offset parameter, which is precisely the
  hook needed. Modest, maybe 20 lines of header parsing.
- **CR3**: a real ISO BMFF box walker. Either implement it deliberately as its
  own reader, or remove `.cr3` from `RAW_SUFFIXES` and say in the README that
  Canon R-series requires `rawpy`. Claiming support that does not exist is worse
  than a documented gap.

**The cost.** RW2 is trivial. RAF is contained within `tiffreader.py`. CR3 is a
real decision, and removing the claim is a legitimate resolution.

---

## 5. The self-contained contact sheet stops being openable well before the library does

**Lens 2. Verdict: wrong-but-keep at current scale; rewrite before it grows.**

`ContactSheetWriter` inlines every thumbnail as a base64 data URI in a single
HTML file. The reasoning is sound and is the best part of the tool's philosophy:
no server, no asset folder, "it opens off a USB stick in five years."

Measured at the shipped default `thumbnail_edge = 320`, a realistic photographic
thumbnail encodes to about 22 KB of data URI. That gives:

- 835 frames (the README's own example): **~19 MB** in one HTML file
- 5,000 frames (one wedding, or a month of shooting): **~113 MB**

`loading="lazy"` is set on the `<img>` tags, which defers decode but does
nothing for file size — the browser still downloads, parses and holds the whole
payload, and the JSON blob is embedded in a `<script>` block that must be parsed
in full before the first card renders.

**Steelman.** Nineteen megabytes is a large file and a perfectly openable one,
and the archival property is genuinely valuable — a report folder with a
thousand loose JPEGs is exactly the fragile artifact single-file HTML exists to
avoid. At the scale the tool currently targets, this is the right trade.

**The change.** Keep the single-file default, and add a threshold rather than a
flag the user has to know about: above roughly 1,500 frames, spill thumbnails to
a sibling `thumbs/` directory and reference them relatively. The page stays
portable as a folder, which is still one thing to copy. Alternatively, a
`self_contained` config key defaulting to `true` — but a silent, documented
threshold serves the user who does not read config files, which is the same user
this design is protecting.

**The cost.** Contained entirely within `contactsheet.py` and one new config
key. Not urgent; it becomes urgent the first time someone points this at a
wedding.

---

## 6. Every module that touches a file is untested

**Lens 2 (structural). Verdict: rewrite the coverage, not the code.**

42 tests, all passing in about a second. They cover `metrics/` and
`rating.py`/`config.py` thoroughly and thoughtfully — the texture-confound test
at `tests/test_metrics.py:67` and the box-size-independence test at `:102` are
testing the actual thesis of the project, which is rarer than it should be.

Untested, entirely: `loading.py`, `tiffreader.py`, `exif.py`, `pipeline.py`,
`assets.py`, `cli.py`, all four `outputs/` modules, and all six detectors in
`detect/`. That is roughly 2,000 of 4,812 lines, and the split is not random:
the tested modules take NumPy arrays and dictionaries, and the untested ones
take file paths.

This is not a scolding — it is the explanation for finding #4. `tiffreader.py`
is 263 lines of binary header parsing with zero tests and a single touching
commit, and it is the module that turns out to be broken for three of the
formats it advertises. Those are one fact, not two.

**Steelman.** Testing file I/O needs fixtures, and raw fixtures are large and
awkward to commit — a single NEF is 25 MB, and a repository that ships a
gigabyte of camera files to test a header parser has made a different mistake.

That justifies not committing real raws. It does not justify zero tests, because
the thing that broke is a *header*, and headers are sixteen bytes that can be
constructed in a test file. The check that found finding #4 was fifteen lines of
Python building three byte strings by hand.

**The change.** Start with synthetic container headers for each entry in
`RAW_SUFFIXES` — assert each one either parses or fails with a specific,
intended message. That single test file would have caught #4 at authoring time.
Then `pipeline.run()` over a temporary directory of generated JPEGs (the
harness used to verify #3 above is most of it), and one round-trip test per
writer.

**The cost.** A day, and it retires the largest unknown in the project.

---

## What holds

**Lens 1 — this is the right software to have built.** Stated with the work
behind it, not as a courtesy.

I considered two competing artifacts and both lose:

- **A Lightroom/darktable plugin instead of a standalone CLI.** This is where
  culling actually happens, and the `XmpWriter` is already a partial admission
  of it. It loses on the thesis: the measurement requires a fixed working
  resolution and batch access to embedded raw previews across a whole folder.
  Host plugin APIs hand you one image at a time, already through the host's
  colour and sharpening pipeline, which destroys exactly the cross-frame
  comparability that the entire argument rests on. Standalone measurement with
  an XMP handoff into the culling tool is the correct decomposition.
- **A single script rather than a package.** Tempting at 4,800 lines. It loses
  because six detectors with four different optional-dependency profiles
  genuinely need the availability-and-fallback protocol that `detect/base.py`
  defines, and `doctor` is load-bearing rather than ceremony — a tool that
  silently degrades needs a command that says what it degraded to.

Applying the deletion test to each layer: `detect/`, `outputs/` and `loading.py`
each have three or more implementations behind their interface and would grow
conditionals if inlined, so they earn their indirection. `metrics/` is three
independent passes over the same array and is correctly split. `models.py` is
the shared vocabulary that keeps the other three from depending on each other.
None of them are framework ceremony.

**The part I tried to improve and could not.** `SharpnessMap` in `models.py:100`
— one expensive pass over the pixels, and every downstream figure (peak, focus
location, sharp fraction, subject, background, ratio) is a cheap array query
against it rather than another pass. I would have written three passes and
noticed the redundancy later, if at all. `subject_background_ratio` as the
cross-frame-comparable figure, with texture inflating both halves and
cancelling, is genuinely the right insight and I could not construct a better
one.

Two details worth naming specifically, both from the second commit, both
evidently the result of running the tool against a real library and believing
the data over the design:

- `min_background_acutance` (`metrics/sharpness.py:138`) — refusing to produce a
  ratio against a textureless background, with the comment recording the actual
  observed failure (a night shot scoring 134 and sorting above every real
  photograph).
- `subject_relative_acutance` (`:149`) — removing the box-size dependence that
  made raw `subject_acutance` incomparable across detectors.

Both are small. Both are the mark of someone who ran the thing and let it prove
them wrong, which is the hardest habit on this list to acquire.

**Where the sketch was wrong.** Before reading any implementation I predicted
`tiffreader.py` would be 263 lines reimplementing what Pillow already provides.
That was wrong, and worth recording. Pillow will not give you the byte offset
and length of the largest embedded preview inside a NEF or CR2, and reading
those bytes directly is what makes the whole "two orders of magnitude faster"
performance claim true. The module earns its existence. Its problem is finding
#4 — incomplete container coverage — not redundancy.

---

## Coverage

All 36 tracked files read in full. No region was sampled, skimmed, or inferred
from filenames. Nothing was excluded: the repository has no vendored
dependencies, generated code, or fixtures.

Findings are distributed across five of the seven regions (config/rating,
detect, pipeline, loading, outputs, tests), which is some evidence against
anchoring — though findings #1 and #3 both land on the config-and-rating layer.
That concentration looks like a real property of the codebase rather than an
artifact of reading order: it is the only layer that converts measurement into
judgement, and it is where the project's own first-to-second-commit correction
was already concentrated.

Git history was consulted only after the sweep, to rank. It contributed one
thing: `tiffreader.py` has a single touching commit and no tests, matching the
"quiet fossil" pattern — a module nobody has revisited, in which finding #4 was
sitting untouched.
