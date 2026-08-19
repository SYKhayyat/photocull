# Code sweep: photocull

Date: 2026-08-18
Scope: full read of `photocull/` (~4,400 lines), plus the test suite and the CLI
end to end. Test suite passes: 124 passed.

Every finding below was reproduced by running the code, not inferred from
reading it. Where a claim could not be verified in this environment, it says so.

The two items the prior design review raised as defects (`lamdan/photocull-2026-08-18.md`
items 2 and 3 — the autofocus scan running once per worker, and rating rules
being validated after the run rather than before it) both appear fixed in the
current working tree. Item 5, the contact sheet's self-contained size limit, is
implemented as well.

---

## Summary

| # | Finding | Severity | File |
| --- | --- | --- | --- |
| 1 | Plain images get almost no EXIF | High | `loading.py:124` |
| 2 | XMP writer silently loses verdicts on duplicate filenames | High | `outputs/machine.py:171` |
| 3 | Config typo becomes `BrokenProcessPool` on the default path | Medium | `config.py`, `pipeline.py` |
| 4 | Reported dimensions transposed for rotated frames | Medium | `loading.py:118-123` |
| 5 | Contact sheet interpolates untrusted strings into `innerHTML` | Low | `outputs/contactsheet.py:229-237` |
| 6 | Dead third element in `_analyse_one` return | Trivial | `pipeline.py:46` |
| 7 | `DetectionContext.width` / `height` read by nobody | Trivial | `detect/base.py:31` |
| 8 | Bad `include_suffixes` raises `AttributeError`, not `ConfigError` | Trivial | `config.py` |
| 9 | `exposure.measure(percentile=...)` unreachable from config | Trivial | `metrics/exposure.py` |

---

## 1. Plain images get almost no EXIF at all

**Where:** `photocull/loading.py:124`, `PlainImageLoader.load`

`image.getexif()` returns the IFD0 tag mapping only. Every tag this tool
actually wants — `ExposureTime` (0x829A), `FNumber` (0x829D), `ISO` (0x8827),
`DateTimeOriginal` (0x9003), `FocalLength` (0x920A), `LensModel` (0xA434) —
lives in the Exif sub-IFD pointed at by tag `0x8769`, and nothing follows that
pointer on the plain-image path.

Reproduced with a JPEG written carrying a complete tag set:

```
exif keys: ['0x10f', '0x110', '0x8769']
CaptureInfo(camera='NIKON D750', lens=None, iso=None, aperture=None,
            shutter_seconds=None, focal_length=None, focal_length_35mm=None,
            timestamp=None, orientation=None)
```

Only the camera name survives, because Make and Model are the two fields that do
live in IFD0.

Three consequences:

* `CaptureInfo.reciprocal_margin` is permanently `None` for anyone shooting
  JPEG, so the handholding-rule figure never appears.
* `explain` never prints its `capture` block, since that block is gated on
  `capture.camera or capture.shutter_seconds`.
* `grouping.max_time_gap_seconds` is a silent no-op on JPEG libraries. The gap
  check in `grouping.group_by_similarity` only applies when *both* timestamps
  parse, and on this path every timestamp is `None`. A documented configuration
  knob works on raw files and quietly does nothing on JPEGs.

Raw files are unaffected: `tiffreader.TiffReader.directories()` follows
`TAG_EXIF_IFD` correctly and the full tag set arrives through `loaded.directories`.

**Fix:** merge the sub-IFD into the mapping before returning it, roughly

```python
exif_obj = image.getexif()
exif = dict(exif_obj)
exif.update(exif_obj.get_ifd(0x8769))
```

`exif.extract` already merges its two sources with `setdefault`, so the flatten
is consistent with how raw directories are handled.

**Also worth noting:** `ImageOps.exif_transpose` strips the Orientation tag from
the image it returns, so `CaptureInfo.orientation` is always `None` on this path
even though 0x0112 *is* an IFD0 tag. That is arguably correct — the rotation has
already been applied — but the field is then dead weight for plain images.

---

## 2. XMP writer silently loses verdicts on duplicate filenames

**Where:** `photocull/outputs/machine.py:171`, `XmpWriter.write`

When not writing beside the originals, sidecars are named
`directory / "xmp" / f"{report.filename}.xmp"`. Two subfolders in one shoot each
holding a `DSC_0001.NEF` — the normal result of a card-per-day workflow, and the
reason the tool is recursive by default — collide, and the second write
overwrites the first.

Reproduced with `a/DSC_0001.jpg` and `b/DSC_0001.jpg` plus one unique frame:

```
$ cat out/xmp-manifest.txt
wrote 3 sidecar(s)

$ ls out/xmp
DSC_0001.jpg.xmp
flat.jpg.xmp
```

The manifest claims three; the disk holds two. One frame's rating, label and
reasoning are gone, and nothing reports it.

This is the exact collision `ContactSheetWriter._spill_thumbnails` already
guards against with an index prefix, and it says so in a comment:

> Indexed, because two folders in one shoot may each hold a DSC_0001.NEF and a
> collision would silently show one frame twice.

**Fix:** apply the same treatment — index the sidecar names, or mirror the
source tree's relative path under `xmp/`. The manifest should also count what it
actually wrote.

**Related, lower stakes:** `ManualDetector` and `AFPointDetector` both key their
box dictionaries by basename. That is a deliberate, documented tradeoff in
`ManualDetector` ("keyed by filename rather than full path so the file survives
the folder being moved"), but it means the same duplicate-name shoot silently
applies one folder's hand-drawn box to another folder's frame. Worth at least a
line in the README.

---

## 3. A config typo becomes a `BrokenProcessPool` traceback on the default path

**Where:** `photocull/config.py` (`SubjectConfig.from_dict`) and
`photocull/pipeline.py` (`_init_worker`)

`SubjectConfig.from_dict` validates the *keys* of the `[subject]` table but not
the *values* of `zone` or `detectors`. Both are checked later, inside
`build_chain` — which, on the parallel path, first runs in the pool initialiser.

With `zone = "nowhere"` in a config file:

```
$ photocull run . -j 1
photocull: unknown zone 'nowhere'; choose from ['center', 'center-small', ...]

$ photocull run . -j 2          # the default worker count
concurrent.futures.process.BrokenProcessPool: A process in the process pool was
terminated abruptly while the future was running or pending.
```

Same for an unknown detector name in the config file. (The CLI's
`-d/--detector` flag is safe — `argparse` constrains it to `DETECTOR_NAMES`.
`rank_by` is safe too: `validate_rules` compiles it before discovery.)

This is the failure mode `_check_keys` exists to prevent, described in its own
docstring, only in the argument-*value* direction rather than the key direction.

**Fix:** either validate `zone` against `ZONES` and `detectors` against
`DETECTOR_NAMES` in `SubjectConfig.from_dict`, or build the detector chain once
in the parent before the pool is created and let it raise there. The second is
the stronger fix — it catches every future construction error in a detector, not
just the two known ones.

---

## 4. Reported dimensions are transposed for rotated frames

**Where:** `photocull/loading.py:118-123` (`PlainImageLoader`), and the same
shape at `152-154` (`RawPreviewLoader`)

`width, height = image.size` is recorded before `ImageOps.exif_transpose`, with
a comment defending the ordering against `draft()`:

> Recorded before draft(), which changes image.size as a side effect --
> reporting the drafted size as the file's dimensions would be quietly,
> confidently wrong.

That reasoning is correct for `draft()`. It just also lands before the rotate,
and the luma that everything downstream measures *is* rotated.

Reproduced with a 400x200 JPEG carrying Orientation = 6:

```
reported dims:     400 200
luma shape (h,w):  (256, 128)
```

The report and the CSV state a landscape frame; the analysis ran on a portrait
one. Nothing downstream consumes `original_width` / `original_height` (see
finding 7), so this is a wrong field rather than a wrong measurement — but it is
wrong in the CSV, the JSON, and the header line of `explain`.

**Fix:** capture the size before `draft()`, then swap it when
`exif_transpose` rotated the image (orientation values 5–8).

**Unverified, and worth checking:** the same rotation may put the autofocus box
on its side. `AFAreaXPosition` / `AFAreaYPosition` are recorded in unrotated
sensor coordinates and normalised in `afpoint._box_from_record` against
`ExifImageWidth` / `ExifImageHeight`, which are also unrotated — but the luma
that box is applied to has been through `exif_transpose`. On a portrait raw the
box would land rotated 90 degrees from where the camera actually focused. This
could not be confirmed here without a rotated raw file with AF metadata. If it
holds, it is a correctness fault in the one measurement the README singles out
as most trustworthy, and it should be promoted above everything else on this
list.

---

## 5. Contact sheet interpolates untrusted strings into `innerHTML`

**Where:** `photocull/outputs/contactsheet.py:229-237`

`ContactSheetWriter.write` neutralises the one sequence that could break out of
the `<script>` block:

```python
encoded = json.dumps(payload, default=str).replace("</", "<\\/")
```

That protects the JSON payload. But `card()` then reassembles HTML from that
payload with template literals and assigns it to `element.innerHTML`, so any
markup in a filename, an error string, a detection source or a reason is parsed
as HTML.

Reproduced by writing a sheet for a report whose filename is
`<img src=x onerror=alert(1)>.jpg` and whose reason is `<script>bad()</script>`:

```
filename injected verbatim: True
script tag in reasons: True
```

Opening the page runs it.

Stakes are low — it is a local file describing your own photographs — but
filenames arrive from a memory card, which arrived from somewhere, and the fix
is free.

**Fix:** an escape helper applied to every interpolated string, or build the
text-bearing nodes with `textContent` instead of `innerHTML`.

---

## 6. Dead third element in `_analyse_one`'s return

**Where:** `photocull/pipeline.py:46`

`_analyse_one` returns `(report, hash_bytes, hash_length)`. The consumer in
`run()` unpacks `length` and never uses it — `np.frombuffer` derives the length
from the buffer. Harmless, but it crosses the process boundary on every frame
and reads as though it matters.

---

## 7. `DetectionContext.width` and `height` are read by nobody

**Where:** `photocull/detect/base.py:31`

Both fields are populated for every frame and no detector consults either one.
`FaceDetector` takes its dimensions from `gray.shape`; `AFPointDetector` and
`ManualDetector` key off `context.path`; `SaliencyDetector` and `ZoneDetector`
work in normalised coordinates throughout.

Whether this is dead weight or a missing input depends on finding 4: if the AF
box does need un-rotating, the original orientation and dimensions are exactly
what a detector would need to do it, and this is where they would arrive.

---

## 8. Bad `include_suffixes` raises the wrong exception type

**Where:** `photocull/config.py`, `InputConfig.from_dict`

```
Config.from_dict({"input": {"include_suffixes": [5]}})
AttributeError: 'int' object has no attribute 'startswith'
```

Every other malformed value in that module produces a `ConfigError` naming the
section, the key and the offending value. This one escapes as a bare
`AttributeError`, which `main()` does not catch, so it surfaces as a traceback.

---

## 9. `exposure.measure`'s `percentile` argument is unreachable

**Where:** `photocull/metrics/exposure.py`

`measure(luma, percentile=0.5)` controls where `dynamic_range` and the tonal
range are sampled, and `analysis.py` always calls it with the default. There is
no `[exposure]` config section. Either wire it up or drop the parameter — as it
stands it advertises a tuning knob that does not exist.

---

## Missing features

Not defects; gaps worth considering.

* **`explain` never applies the rating rules.** The command exists to calibrate
  thresholds ("print every measurement for one frame, so thresholds can be
  calibrated") but cannot show what the current thresholds decided about that
  frame. `Rater.apply` is a single call away and would close the loop.
* **No way to compare two runs.** Calibration in practice means changing a
  threshold and seeing what moved. Today that is two JSON files and your own
  diff tool.
* **`keepers`, `rejects` and `xmp` are opt-in without saying so.** All three are
  registered writers, none is in the default `output.formats`
  (`json`, `csv`, `html`), and the README's output section does not mention that
  you have to ask for them. The keeper list is arguably the most useful artefact
  the tool produces and it is off by default.

---

## What holds up

Worth recording, since a defect list reads as a verdict otherwise.

* The tiled sharpness map is the right central idea and the queries built on it
  (`within`, `outside`, `sharp_fraction`, `focus_point`) are each cheap and each
  answer a question a photographer would actually ask.
* Refusing to divide by a textureless background (`min_background_acutance`) and
  reporting the ratio as undefined rather than spectacular is the correct call,
  and the comment explaining it cites the real number that motivated it.
* The rating rules keying on `subject_confidence` to exclude saliency boxes from
  subject-versus-background verdicts is a genuinely subtle piece of reasoning —
  the circularity it avoids is real, and most tools would not have noticed it.
* Failures becoming rows rather than exceptions, and the detector chain
  recording *why* it fell through, are what make the output checkable.
* `TiffReader`'s bounds — `_MAX_ENTRIES`, `_MAX_IFDS`, `_OFFSET_SENTINEL`,
  `_MAX_INLINE_BYTES` — are each defended against a specific real-world file
  rather than chosen defensively in the abstract.
