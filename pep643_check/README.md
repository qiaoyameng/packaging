# PEP 643 Core Metadata comparison

`pep643_check` compares the static Core Metadata promised by an sdist with the
Core Metadata emitted by a wheel. It never invokes a build backend and never
mutates either input.

## Rules

- `Name`, `Version`, and other non-`Dynamic` fields are static promises.
  Differences are violations unless the sdist declares the email header in
  `Dynamic` under PEP 643 (`Metadata-Version: 2.2` or newer).
- `Name`, `Version`, and `Metadata-Version` are violations even if a malformed
  sdist lists them as dynamic.
- Multi-valued fields use their own semantics:
  - `Requires-Dist` is compared as a multiset. Ordering, distribution-name
    normalization, extras normalization, harmless specifier/marker formatting
    and provably equivalent marker rewrites do not count as changes.
  - Markers that parse but cannot be proven equivalent produce `uncertain`
    rather than a false clean result.
  - `Classifier` ordering, `Provides-Extra` casing, `Keywords` spacing, and
    `Project-URL` label/URL presentation are normalized before comparison.
- A missing optional field is equivalent to an explicit empty value.
- Metadata older than 2.2 has no `Dynamic` semantics.
- Newly added unknown fields are `uncertain`; private `X-*` extension headers
  are ignored.

## API

```python
from pep643_check import compare_metadata

report = compare_metadata(sdist_metadata, wheel_metadata)
for change in report.changes:
    print(change.kind, change.field, change.rule, change.old, change.new)
```

Each input may be email text/bytes (`PKG-INFO` or `METADATA`), a
`packaging.metadata.RawMetadata`-like mapping, or a parsed
`packaging.metadata.Metadata`.

## CLI

```bash
python -m pep643_check path/to/PKG-INFO path/to/METADATA
python -m pep643_check --json path/to/PKG-INFO path/to/METADATA
```

Exit statuses are `0` (clean), `1` (proven static-metadata violation), and `2`
(no violation, but at least one difference could not be proven equivalent).

The `examples/` directory contains the two controls used for development:

- `*static_tampered*`: an undeclared static `Requires-Dist` change.
- `*legal_dynamic*`: valid `Requires-Dist` and `Classifier` changes.
