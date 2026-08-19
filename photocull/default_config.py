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

# Background acutance below which subject_background_ratio is reported as
# undefined rather than computed. A featureless background -- night sky, studio
# backdrop, blown overcast -- gives the division nothing to work with, and the
# resulting enormous ratio ranks empty frames above real photographs.
min_background_acutance = 2.0

[exposure]
# Where the tonal range is read from. dynamic_range is the gap between the
# percentile-th and (100 - percentile)th brightness, so 0.5 ignores the extreme
# half-percent at each end -- enough that one hot pixel or a single specular
# highlight cannot define the range of a whole photograph. Raise it to ignore
# more; 0 measures true min to max.
#
# Clipping is not affected by this and never will be: highlight_clipped and
# shadow_clipped count pixels at the ends of the range, because that is what
# clipping means. Both are reported, neither is scored.
percentile = 0.5

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

# Rules are tried in order and the first rule that awards stars wins, so put the
# specific ones first. Available names are exactly the CSV columns -- run
# `photocull explain --json` to see them all for a real frame.
#
# A missing measurement (no subject found, say) compares false rather than
# raising, so rules stay readable without None guards everywhere.
#
# A rule with no `stars` is an annotation: it attaches its label and reason and
# lets the ladder continue. Use that for things you want flagged but not judged.
#
# The defaults deliberately drive the 4- and 5-star verdicts -- the ones that
# decide keepers.txt, rejects.txt and your XMP star ratings -- off *relative*
# comparisons: rank within a near-duplicate group, and subject versus background
# inside one frame. Absolute thresholds across a whole library do not hold; see
# "What the numbers can and cannot be compared against" in the README.

# Reported, not scored. Clipped highlights are the photographer's call, and a
# rule set that rejected a backlit portrait over a blown rim light would be
# overruling the person holding the camera. A rule with no `stars` is an
# annotation: it flags and steps aside.
[[rating.rules]]
when = "highlight_clipped > 0.05"
label = "yellow"
reason = "highlights clipped beyond recovery - your call whether that matters"

# The one absolute threshold that survives: "nothing anywhere in this frame is
# sharp" does not need calibrating against content to be true.
[[rating.rules]]
when = "max_local_acutance < 12"
stars = 1
label = "red"
reason = "nothing in the frame is sharp"

# Every rule below that mentions the subject also asks for subject_confidence to
# be high or medium, and that guard is load-bearing.
#
# The saliency detector finds your subject by looking for the region of highest
# local contrast, which is very nearly what the sharpness map measures. So
# "is the subject sharper than its background" asked about a saliency box tells
# you saliency worked, not that focus landed -- it is circular. On a real
# 835-frame library the median ratio was 3.44 for detected faces and 6.92 for
# saliency boxes. Only manual boxes, autofocus metadata and detected faces
# locate a subject independently of sharpness, and only those report high or
# medium confidence. Frames without one fall through to group rank below, which
# is independent of all of it.
#
# Note also what these rules do NOT use: raw subject_acutance. It is the peak
# over whichever tiles the subject box covers, so a box spanning half the frame
# collects a much larger value than a box over someone's eyes, and a threshold
# on it quietly rates every portrait below every landscape.
[[rating.rules]]
when = 'subject_confidence in ["high", "medium"] and subject_background_ratio < 0.9'
stars = 2
label = "yellow"
reason = "background is sharper than the subject - focus missed"

[[rating.rules]]
when = 'is_group_best and subject_confidence in ["high", "medium"] and subject_background_ratio >= 1.5'
stars = 5
label = "green"
reason = "best frame of its group, and focus landed on the subject"

[[rating.rules]]
when = 'subject_confidence in ["high", "medium"] and subject_background_ratio >= 3'
stars = 5
label = "green"
reason = "subject clearly sharper than its background"

[[rating.rules]]
when = "is_group_best"
stars = 4
label = "green"
reason = "best frame of its group"

# The keeper path for a frame with no near-duplicates to beat. Without it a
# unique, well-focused photograph could only reach keepers.txt by clearing an
# absolute acutance bar, which is the comparison that does not hold.
[[rating.rules]]
when = 'subject_confidence in ["high", "medium"] and subject_background_ratio >= 1.5'
stars = 4
label = "green"
reason = "focus landed on the subject rather than behind it"

# Lost to a near-duplicate. Not a reject -- just not the one to work on.
[[rating.rules]]
when = "group_size > 1 and not is_group_best"
stars = 3
reason = "a near-duplicate beat this frame"

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

# The contact sheet inlines every thumbnail, so it opens off a USB stick in five
# years with no server and no asset folder. That is linear in frame count: at
# ~22 KB apiece, a 5,000-frame wedding is a 113 MB single file. Above this many
# frames the thumbnails go to a thumbs/ folder beside the page instead, which is
# still one directory to copy. Raise it if you would rather have the one file.
self_contained_max_frames = 1500

open_html = false
'''
