# requirements-merger

Merge multiple plugins' `Requires-Dist` lists over a **finite target
environment matrix** and report the final constraint on every distribution in
every target.

The merger does **not** resolve package versions and never contacts an index.
It only combines the constraints already asserted and states whether they are
provably compatible, provably conflicting, or undecidable from static
metadata.

## Requirements

Python with `packaging >= 26.1` (uses `SpecifierSet.__and__` and
`SpecifierSet.is_unsatisfiable()`; set relations such as
`is_disjoint()`/`is_subset()` are also available when needed). In this
checkout use the bundled virtualenv:

```sh
PYTHONPATH=src .venv/bin/python -m requirements_merger examples/demo.json
```

Exit status is `1` if any target has a proven conflict, otherwise `0`.

## Input

```json
{
  "plugins": [
    {"name": "build-core", "requires_dist": ["flask>=2.0"]},
    {"name": "build-extra", "requires_dist": ["flask<2 ; sys_platform == 'win32'"]}
  ],
  "targets": [
    {"name": "win",  "extras": ["gui"],
     "environment": {"sys_platform": "win32", "python_full_version": "3.12.4"}},
    {"name": "linux","extras": [],
     "environment": {"sys_platform": "linux", "python_version": "3.11"}}
  ]
}
```

- `environment` holds PEP 508 marker variables; unset ones default to the
  running interpreter. Giving only `python_full_version` derives
  `python_version` (and vice versa). An explicit `null` removes a variable.
- `extras` are the project extras selected for that target; names are
  normalized per PEP 685.

## Algorithm

1. Parse every `Requires-Dist` line with `packaging.requirements.Requirement`;
   malformed lines are collected under `malformed_requirements` instead of
   aborting the run.
2. Group by PEP 503 normalized distribution name (`canonicalize_name`).
3. For each target, evaluate each marker in the `metadata` context: once with
   no extra selected and once per selected extra (core-metadata semantics),
   also exposing the full selected set as `extras`.
4. Within each surviving group combine constraints.

## Verdicts per package

- `satisfied` — constraints are mutually compatible. `merged` gives the
  intersected `SpecifierSet`, the single direct URL (if any), and the union of
  requested extras.
- `conflict` — incompatibility is proven. `minimal_conflict` is an
  **inclusion-minimal** proof: removing any single listed requirement makes
  the conflict no longer provable. Every report item carries the original
  requirement text and the originating plugin (`requirements` lists the full
  active set).
- `unknown` — neither compatibility nor incompatibility can be proven. The
  original requirements are retained verbatim and no simplified constraint is
  fabricated. This covers:
  - `===` arbitrary equality (literal-string matching, outside PEP 440
    ranges),
  - a direct URL coexisting with an index version constraint,
  - markers that raise during evaluation.

### Direct URLs

`name @ url` pins a concrete index-external source:

- the **same** URL repeated merges like a duplicate;
- two **different** URLs are a proven conflict and never treated as the same
  source;
- one URL plus a plain version specifier is `unknown` (static metadata cannot
  show the archive honors the range);
- one URL plus an unconstrained requirement is `satisfied`.

## Result grouping

`groups` merges targets with byte-identical per-package results (e.g. two
Windows targets differing only in Python version); `targets` keeps the
ungrouped view. A constraint active only on one target never appears in
another target's group — see the Windows-only `flask<2` case in
`examples/demo.json`.
