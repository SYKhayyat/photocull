"""The commented config file ``photocull init`` writes.

Kept as a literal rather than generated from the dataclasses on purpose: the
comments are the documentation, and generated files lose them.
"""

from __future__ import annotations

DEFAULT_CONFIG_TOML = '''# photocull configuration.
#
# Every threshold this tool obeys lives here. The defaults are defensible, not
# authoritative -- what counts as "sharp" depends on your lens, your body and
# what you shoot, so calibrate with `photocull explain` on a frame you already
# know is good, then set the numbers to match.

[input]
# Long edge, in pixels, that everything is measured at. Acutance is scale
# dependent, so a fixed working size is what makes two different cameras
# comparable at all. Larger is more sensitive to fine detail and slower.
working_edge = 1024

# Long edge of the thumbnails embedded in the contact sheet. Larger thumbnails
# make a wrong subject box easier to spot and make the HTML file bigger.
thumbnail_edge = 320

recursive = true

# Decode raw sensor data with rawpy instead of reading the embedded JPEG
# preview. Far slower, and only worth it when comparing bodies whose in-camera
# sharpening differs -- the preview is camera-sharpened, so its acutance runs
# high, but the offset is identical across frames from one body and cancels.
prefer_raw_decode = false

# Restrict to specific extensions, e.g. [".nef", ".jpg"]. Empty means everything
# a loader recognises.
include_suffixes = []

[sharpness]
# Tiles along the long edge. More tiles localise focus more precisely and make
# each tile's measurement noisier.
grid_long_edge = 24

# A tile counts toward "in focus" if it reaches this fraction of the frame's
# peak acutance. This drives sharp_fraction, which describes depth of field.
sharp_fraction_threshold = 0.5

# Acutance at or above which a region is considered critically sharp. THE most
# useful number to calibrate: run `photocull explain` on a frame you know nailed
# focus and set this just below its peak local value.
sharp_acutance = 40.0

[subject]
# Detectors in preference order. The first that finds a subject wins, and the
# report always records which one answered.
#
#   manual    boxes you drew, from the sidecar below -- always trust these first
#   af-point  where the camera was told to focus (needs exiftool installed)
#   face      OpenCV face detection, narrowed to the eyes when they resolve
#   saliency  spectral residual; works on anything, weakest of the four
#   zone      a fixed region; an assumption, not a measurement
#   none      no subject; whole-frame figures only, which stay meaningful
detectors = ["manual", "af-point", "face", "saliency"]

# Used only by the "zone" detector: center, center-small, center-wide,
# upper-third, lower-third, left-third, right-third.
zone = "center"

# Prefer the eye region over the whole face when eyes are found. Usually right:
# a face box includes hair and collar, which can be crisp while the eyes are not.
prefer_eyes = true

# Where the contact sheet's exported boxes are read from.
sidecar = "photocull-subjects.json"

# Minimum confidence for a face to be believed. The detector will happily find
# faces in foliage and clouds at lower settings, and a false face produces a
# tiny box whose acutance is meaningless -- worse than having no subject at all,
# because it looks like an answer. Lower it if real faces are being missed.
face_score = 0.9

# Minimum face width, as a fraction of the frame. A face smaller than this is
# too few pixels to judge focus from even when the detection is correct.
face_min_size = 0.05

[grouping]
# Gather near-identical frames so they can be ranked against each other.
enabled = true

# Difference-hash size. 8 gives a 64-bit fingerprint, which is plenty.
hash_size = 8

# Maximum Hamming distance still considered the same photograph. Lower is
# stricter. Around 10 groups genuine re-takes; above ~16 starts merging
# different photographs that share a composition.
max_distance = 10

# Optionally refuse to group visually similar frames taken far apart in time.
# 0 disables the constraint. Useful for repeated setups like a studio backdrop.
max_time_gap_seconds = 0

[rating]
# How frames are ordered inside a group. Any expression over the measurements.
rank_by = "subject_or_max_acutance"

# Rules are tried in order and the first match wins, so put the specific ones
# first. Available names are exactly the CSV columns -- run `photocull explain
# --json` to see them all for a real frame.
#
# A missing measurement (no subject found, say) compares false rather than
# raising, so rules stay readable without None guards everywhere.

[[rating.rules]]
when = "max_local_acutance < 12"
stars = 1
label = "red"
reason = "nothing in the frame is sharp"

[[rating.rules]]
when = "subject_found and subject_background_ratio is not None and subject_background_ratio < 0.9"
stars = 2
label = "yellow"
reason = "background is sharper than the subject - focus missed"

[[rating.rules]]
when = "highlight_clipped > 0.05"
stars = 2
label = "yellow"
reason = "highlights clipped beyond recovery"

# Note what this rule does NOT use: raw subject_acutance. That number is the
# peak over whichever tiles the subject box covers, so a saliency box spanning
# half the frame collects a much larger value than a box over someone's eyes --
# the two are not comparable, and a threshold on it quietly rates every portrait
# below every landscape. subject_background_ratio is measured within one frame
# and does not have that problem.
[[rating.rules]]
when = "subject_found and subject_background_ratio >= 3 and max_local_acutance >= 25 and highlight_clipped <= 0.02"
stars = 5
label = "green"
reason = "subject clearly sharper than its background, highlights intact"

[[rating.rules]]
when = "group_size > 1 and is_group_best"
stars = 4
label = "green"
reason = "best frame of its group"

[[rating.rules]]
when = "max_local_acutance >= 25"
stars = 3
reason = "acceptably sharp somewhere"

[[rating.rules]]
when = "True"
stars = 2
reason = "no rule matched strongly"

[output]
# json, csv, html, keepers, rejects, xmp
formats = ["json", "csv", "html"]

directory = "photocull-report"

# Include the full per-tile acutance grid in the JSON. Large, occasionally
# invaluable for debugging why a frame scored the way it did.
include_tile_map = false

# Write XMP sidecars beside the originals rather than into the report folder.
# Existing sidecars are never overwritten -- yours may hold real develop work.
write_xmp_next_to_originals = false

open_html = false
'''
