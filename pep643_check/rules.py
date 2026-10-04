"""Field classification and rule labels for the PEP 643 comparison."""

from __future__ import annotations

from enum import Enum


# Lower-case email header name -> canonical underscore key used in reports.
# Kept local on purpose: this checker does not reach into private mapping
# tables of packaging.metadata.
EMAIL_FIELDS: dict[str, str] = {
    "metadata-version": "metadata_version",
    "name": "name",
    "version": "version",
    "dynamic": "dynamic",
    "platform": "platforms",
    "supported-platform": "supported_platforms",
    "summary": "summary",
    "description": "description",
    "description-content-type": "description_content_type",
    "keywords": "keywords",
    "home-page": "home_page",
    "download-url": "download_url",
    "author": "author",
    "author-email": "author_email",
    "maintainer": "maintainer",
    "maintainer-email": "maintainer_email",
    "license": "license",
    "license-expression": "license_expression",
    "license-file": "license_files",
    "classifier": "classifiers",
    "requires-dist": "requires_dist",
    "requires-python": "requires_python",
    "requires-external": "requires_external",
    "project-url": "project_urls",
    "provides-extra": "provides_extra",
    "provides-dist": "provides_dist",
    "obsoletes-dist": "obsoletes_dist",
    "requires": "requires",
    "provides": "provides",
    "obsoletes": "obsoletes",
    "import-name": "import_names",
    "import-namespace": "import_namespaces",
}

# PEP 643: Dynamic may never be used for the three identity fields.
NEVER_DYNAMIC = frozenset({"name", "version", "metadata_version"})

# Fields whose meaning is order sensitive. Project-URL labels are keys, so the
# dict itself makes label ordering irrelevant.
SCALAR_FIELDS = frozenset(
    {
        "metadata_version",
        "name",
        "version",
        "summary",
        "description",
        "description_content_type",
        "home_page",
        "download_url",
        "author",
        "author_email",
        "maintainer",
        "maintainer_email",
        "license",
        "license_expression",
        "requires_python",
    }
)

# Multisets: repeated entries can be meaningful, shuffling cannot.
MULTISET_FIELDS = frozenset(
    {
        "platforms",
        "supported_platforms",
        "requires_external",
        "provides_dist",
        "obsoletes_dist",
        "requires",
        "provides",
        "obsoletes",
        "import_names",
        "import_namespaces",
        "license_files",
    }
)

# Sets of individual, independently meaningful values.
SET_FIELDS = frozenset({"classifiers", "provides_extra"})


class FieldKind(Enum):
    """How a particular field is interpreted when it is not dynamic."""

    SCALAR = "scalar"
    NAME = "name"
    VERSION = "version"
    REQUIRES_PYTHON = "requires-python"
    REQUIRES_DIST = "requires-dist"
    KEYWORDS = "keywords"
    PROJECT_URLS = "project-urls"
    DYNAMIC = "dynamic"
    MULTISET = "multiset"
    SET = "set"
    UNKNOWN = "unknown"


def field_kind(field: str) -> FieldKind:
    """Return the semantic kind of a canonical core metadata field."""

    if field == "name":
        return FieldKind.NAME
    if field == "version":
        return FieldKind.VERSION
    if field == "requires_python":
        return FieldKind.REQUIRES_PYTHON
    if field == "requires_dist":
        return FieldKind.REQUIRES_DIST
    if field == "keywords":
        return FieldKind.KEYWORDS
    if field == "project_urls":
        return FieldKind.PROJECT_URLS
    if field == "dynamic":
        return FieldKind.DYNAMIC
    if field in SET_FIELDS:
        return FieldKind.SET
    if field in MULTISET_FIELDS:
        return FieldKind.MULTISET
    if field in SCALAR_FIELDS:
        return FieldKind.SCALAR
    return FieldKind.UNKNOWN
