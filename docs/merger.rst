Requirement Merger
==================

.. currentmodule:: packaging.merger

The merger combines ``Requires-Dist`` requirements contributed by several
plugins over a finite matrix of target environments, and reports the final
constraint each target imposes on every package. It works purely at the
constraint level: it never resolves actual package versions and never
contacts a package index.

.. versionadded:: 26.4

Usage
-----

.. doctest::

    >>> from packaging.merger import Target, format_report, merge_requirements
    >>> plugins = {
    ...     "plugin-a": ["foo>=2.0; sys_platform == 'win32'", "foo[bar]"],
    ...     "plugin-b": ["foo<3.0"],
    ... }
    >>> targets = [
    ...     Target("windows", {"sys_platform": "win32"}),
    ...     Target("linux", {"sys_platform": "linux"}),
    ... ]
    >>> report = merge_requirements(plugins, targets)
    >>> print(format_report(report))
    selected extras: (none)
    targets: windows
      foo[bar]: <3.0,>=2.0 [ok]
    targets: linux
      foo[bar]: <3.0 [ok]

Each requirement's marker is evaluated against every target environment and
every selected extra, so a constraint only affects the targets where its
marker holds. Targets whose merged results are identical are grouped
together in the report.

Conflicts
---------

When the merged specifier set cannot be satisfied, the conflict is reported
as a deletion-minimal core of the original requirements with their sources:
removing any single entry makes the conflict unprovable. Because markers are
evaluated per target, a conflict that only exists on one platform does not
pollute the others:

.. doctest::

    >>> plugins = {
    ...     "plugin-a": ["foo>=2.0; sys_platform == 'win32'"],
    ...     "plugin-b": ["foo<2.0; sys_platform == 'win32'"],
    ... }
    >>> report = merge_requirements(plugins, targets)
    >>> print(format_report(report))
    selected extras: (none)
    targets: windows
      foo: <2.0,>=2.0 (not simplified) [conflict]
        conflict (version): merged specifier <2.0,>=2.0 is unsatisfiable
          foo>=2.0; sys_platform == 'win32'  (from plugin-a)
          foo<2.0; sys_platform == 'win32'  (from plugin-b)
    targets: linux

Direct URLs
-----------

Direct URL requirements follow explicit compatibility rules:

* requirements pointing at the *same* URL string are the same source and
  merge normally;
* requirements pointing at *different* URLs are never assumed to be the same
  source and are reported as a conflict;
* a direct URL combined with version specifiers cannot be verified without
  fetching the URL, so both constraints are kept and the package is reported
  with an ``unknown`` status.

Arbitrary equality
------------------

``===`` (arbitrary equality) specifiers cannot be simplified through the
range algebra, so the original constraints are kept as-is and the simplified
form is reported as unknown. Unsatisfiability is still detected through
:meth:`SpecifierSet.is_unsatisfiable() <packaging.specifiers.SpecifierSet.is_unsatisfiable>`.

Reference
---------

.. automodule:: packaging.merger
    :members:
