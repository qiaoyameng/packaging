"""Check wheel Core Metadata against the static promises of an sdist.

The comparison follows PEP 643 ("Metadata for Package Source Distributions"):
fields that are not declared ``Dynamic`` in the *sdist* metadata must keep the
meaning they had there; declared dynamic fields may change freely.

Only :mod:`packaging` is used to parse the two metadata documents. No build
backend is invoked and the inputs are never modified.
"""

from .core import (
    Change,
    ChangeKind,
    MetadataDocument,
    compare_metadata,
    load_metadata,
)

__all__ = [
    "Change",
    "ChangeKind",
    "MetadataDocument",
    "compare_metadata",
    "load_metadata",
]
