"""photocull -- measure where focus actually landed, and show the work.

The package is arranged so that each concern has exactly one home:

``models``      immutable value objects, shared by everything
``loading``     files to normalised arrays, one loader per container type
``metrics``     pure measurement passes over those arrays
``detect``      subject-location strategies and the fallback chain
``grouping``    near-duplicate clustering by perceptual hash
``rating``      user-written rules, evaluated safely
``pipeline``    orchestration: discovery, parallelism, grouping, rating
``outputs``     report writers, one per format
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
