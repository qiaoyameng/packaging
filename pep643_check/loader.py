"""Load raw or enriched Core Metadata into a uniform MetadataDocument.

Neither input is mutated.  Both input flavors are converted to email-style
metadata and parsed through one path, so comparisons never compare an
unvalidated raw value against an enriched object under different rules.
"""

from __future__ import annotations

from typing import Any

from packaging import metadata as packaging_metadata

from .model import MetadataDocument
from .rules import EMAIL_FIELDS

_DYNAMIC_MIN_VERSION = (2, 2)
_CORE_FIELDS = frozenset(EMAIL_FIELDS.values())


def _version_tuple(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def _fold_newlines(value: str) -> str:
    """Turn embedded line boundaries into RFC 5322 continuation lines."""

    return value.replace("\r\n", "\n").replace("\n", "\n ")


def _emit(lines: list[str], email_name: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _emit(lines, email_name, item)
    elif isinstance(value, dict):
        for label, url in value.items():
            lines.append(f"{email_name}: {_fold_newlines(f'{label}, {url}')}")
    else:
        lines.append(f"{email_name}: {_fold_newlines(str(value))}")


def _mapping_to_email(data: dict[str, Any]) -> str:
    lines: list[str] = []
    description: Any = None
    for key, value in data.items():
        field = EMAIL_FIELDS.get(str(key).lower(), str(key))
        if field not in _CORE_FIELDS:
            _emit(lines, str(key), value)
            continue
        if field == "description":
            description = value
            continue
        email_name = next(name for name, raw_name in EMAIL_FIELDS.items() if raw_name == field)
        _emit(lines, email_name, value)

    text = "\n".join(lines)
    if description is None:
        return text
    body = description if isinstance(description, str) else str(description)
    return f"{text}\n\n{body}" if text else body


def _extract_dynamic(email_text: bytes | str) -> set[str]:
    """Split Dynamic tokens using an RFC 5322 parser.

    Manual comma splitting cannot tell a comma from continuation indentation
    (``Dynamic: Name: x`` is one malformed token, not a field called ``name``).
    """

    import email.parser
    import email.policy

    if isinstance(email_text, bytes):
        message = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(
            email_text
        )
    else:
        message = email.parser.Parser(policy=email.policy.compat32).parsestr(
            email_text
        )
    tokens: set[str] = set()
    for value in message.get_all("dynamic", []) or []:
        for token in str(value).split(","):
            token = token.strip().lower()
            if token:
                tokens.add(token)
    return tokens


def _enrich(
    raw: packaging_metadata.RawMetadata,
) -> tuple[dict[str, object], dict[str, str | list[str]]]:
    enriched = packaging_metadata.Metadata.from_raw(raw.copy(), validate=False)
    parsed: dict[str, object] = {}
    invalid: dict[str, str | list[str]] = {}
    for field in list(raw):
        try:
            parsed[field] = getattr(enriched, field)
        except packaging_metadata.InvalidMetadata:
            value = raw[field]
            if isinstance(value, (str, list)):
                invalid[field] = value
    return parsed, invalid


def load_document(source: Any) -> MetadataDocument:
    """Load email text/bytes, a RawMetadata mapping, or parsed Metadata."""

    if isinstance(source, MetadataDocument):
        return source
    if isinstance(source, (bytes, str)):
        email_text = source
    elif isinstance(source, packaging_metadata.Metadata):
        email_text = source.as_rfc822().as_string()
    elif isinstance(source, dict):
        email_text = _mapping_to_email(source)
    else:
        raise TypeError(f"unsupported metadata input: {type(source).__name__!s}")

    raw, unparsed = packaging_metadata.parse_email(email_text)
    parsed, invalid_known = _enrich(raw)

    unknown: dict[str, list[str]] = {}
    invalid: dict[str, str | list[str]] = {}
    for header, values in unparsed.items():
        field = EMAIL_FIELDS.get(header, header)
        (invalid if header in EMAIL_FIELDS else unknown)[field] = values
    invalid.update(invalid_known)

    metadata_version = raw.get("metadata_version")
    if not isinstance(metadata_version, str):
        metadata_version = None
    version_tuple = _version_tuple(metadata_version)
    return MetadataDocument(
        raw=dict(raw),
        parsed=parsed,
        unknown=unknown,
        invalid=invalid,
        metadata_version=metadata_version,
        dynamic=frozenset(_extract_dynamic(email_text)),
        has_dynamic_semantics=(
            version_tuple is not None and version_tuple >= _DYNAMIC_MIN_VERSION
        ),
    )
