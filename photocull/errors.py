"""Exception hierarchy.

One base class so callers can catch everything this package raises deliberately
with a single ``except PhotocullError``, and never accidentally swallow a
``KeyboardInterrupt`` or a genuine bug.
"""

from __future__ import annotations


class PhotocullError(Exception):
    """Base for every error this package raises deliberately."""


class UnreadableImage(PhotocullError):
    """A file could not be decoded by any registered loader."""


class ConfigError(PhotocullError):
    """The configuration file is malformed or refers to something unknown."""


class ExpressionError(ConfigError):
    """A user-supplied rating expression is invalid or uses a banned construct."""


class DetectorUnavailable(PhotocullError):
    """A detector cannot run here (missing optional dependency or missing data).

    This is never fatal. The detector chain treats it as "skip me" and moves on
    to the next candidate, recording the reason so the report can explain the
    fallback rather than hiding it.
    """
