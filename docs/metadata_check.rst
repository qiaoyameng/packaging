Metadata consistency checking (PEP 643)
======================================

.. currentmodule:: packaging.metadata_check

After building a wheel from a source distribution (*sdist*), the wheel's
`Core Metadata`_ should be checked against the metadata the sdist promised.
`PEP 643`_ governs which fields a build backend is allowed to generate: a field
named in the sdist's :mailheader:`Dynamic` header may change, while every
other value is a static promise. :mailheader:`Name`, :mailheader:`Version`,
and :mailheader:`Metadata-Version` can never be dynamic.

The :func:`check_metadata_consistency` function compares the two metadata
documents under those rules without invoking a build backend and without
modifying its inputs. Each input can be raw email text (``str`` or
``bytes``), a :class:`~packaging.metadata.RawMetadata` mapping, a
:class:`~packaging.metadata.Metadata` object, or a
``(RawMetadata, unparsed)`` tuple from
:func:`~packaging.metadata.parse_email`.


Usage
-----

A wheel that silently rewrites a *static* dependency is rejected:

.. doctest::

    >>> from packaging.metadata_check import check_metadata_consistency, ChangeKind
    >>> sdist = (
    ...     "Metadata-Version: 2.2\n"
    ...     "Name: example-pkg\n"
    ...     "Version: 3.1.4\n"
    ...     "Requires-Dist: requests==2.28.0\n"
    ... )
    >>> wheel = (
    ...     "Metadata-Version: 2.3\n"
    ...     "Name: example-pkg\n"
    ...     "Version: 3.1.4\n"
    ...     "Requires-Dist: requests==2.32.3\n"
    ... )
    >>> result = check_metadata_consistency(sdist, wheel)
    >>> result.ok
    False
    >>> change = result.violations[0]
    >>> change.field
    'requires-dist'
    >>> change.old
    'requests==2.28.0'
    >>> change.new
    'requests==2.32.3'
    >>> change.rule.id
    'pep643-multi-static'

The very same change is allowed when the sdist declares the field dynamic:

.. doctest::

    >>> sdist = (
    ...     "Metadata-Version: 2.2\n"
    ...     "Name: example-pkg\n"
    ...     "Version: 3.1.4\n"
    ...     "Dynamic: Requires-Dist\n"
    ... )
    >>> result = check_metadata_consistency(sdist, wheel)
    >>> result.ok
    True
    >>> result.violations
    ()
    >>> [c.rule.id for c in result.allowed if c.field == 'requires-dist']
    ['pep643-multi-dynamic']

Semantically irrelevant reformatting is not reported as a change: entry
ordering, PEP 503 name normalization, PEP 440 version/specifier
normalization, and canonical marker spelling are all understood.

.. doctest::

    >>> sdist = (
    ...     "Metadata-Version: 2.2\n"
    ...     "Name: example-pkg\n"
    ...     "Version: 3.1.4\n"
    ...     'Requires-Dist: foo_bar>=1.0; python_version >= "3.8"\n'
    ...     "Requires-Dist: bar\n"
    ... )
    >>> wheel = (
    ...     "Metadata-Version: 2.2\n"
    ...     "Name: example-pkg\n"
    ...     "Version: 3.1.4\n"
    ...     "Requires-Dist: bar\n"
    ...     'Requires-Dist: foo-bar >= 1.0.0; python_version >= \'3.8\'\n'
    ... )
    >>> check_metadata_consistency(sdist, wheel).changes
    ()

When the raw values differ but equivalence cannot be proven -- for example a
marker that might be logically equivalent but does not canonicalize to the
same form, or an unrecognized extension field -- the finding is
:attr:`ChangeKind.UNKNOWN` rather than a definitive violation.


Reference
---------

.. autofunction:: check_metadata_consistency

.. autoclass:: MetadataComparison
    :members:

.. autoclass:: FieldChange
    :members:

.. autoclass:: Rule
    :members:

.. autoclass:: ChangeKind
    :members:


Rules
'''''

Each :attr:`FieldChange.rule` has a stable identifier. The rules applied are:

``pep643-name-static`` / ``pep643-version-static``
    :mailheader:`Name` and :mailheader:`Version` cannot be dynamic (PEP 643).
``metadata-version-advanced``
    The wheel may emit a newer :mailheader:`Metadata-Version`; a downgrade is
    a violation.
``required-field-changed``
    A required field changed presence between the documents.
``pep643-scalar-static`` / ``pep643-multi-static`` /
``pep643-project-urls-static``
    A non-dynamic scalar, multi-valued, or mapping field changed.
``pep643-scalar-dynamic`` / ``pep643-multi-dynamic`` /
``pep643-project-urls-dynamic``
    The field is named in :mailheader:`Dynamic` and may be regenerated.
``pep808-dynamic-extra-value`` / ``pep808-project-urls-extra``
    Under PEP 808 (:mailheader:`Metadata-Version` 2.6), a dynamic
    multi-valued field may gain *additional* values; its static values must
    still be preserved.
``pep643-dynamic-field-list`` / ``pre-2.2-dynamic-field``
    Bookkeeping for the :mailheader:`Dynamic` header itself.
``requires-dist-equivalence-unproven``
    Two :mailheader:`Requires-Dist` entries share a name but could not be
    proven equivalent (typically a marker or direct-reference difference).
``field-value-unparseable``
    A value could not be parsed, so its semantics could not be compared.
``unknown-extension-field``
    An unrecognized (extension) field changed; its semantics and dynamic
    permission are unknown.
``pre-2.2-no-dynamic-semantics``
    The sdist uses :mailheader:`Metadata-Version` older than 2.2, which has
    no Dynamic concept; non-strict differences stay uncertain.

.. _Core Metadata: https://packaging.python.org/en/latest/specifications/core-metadata/
.. _PEP 643: https://peps.python.org/pep-0643/
