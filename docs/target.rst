Target profiles
===============

.. currentmodule:: packaging.target

The functions in :mod:`packaging.tags` describe the tags supported by the
*running* interpreter.  A packaging service that filters wheels for a variety
of targets on a different machine instead needs an explicit, offline
description of each target.  :mod:`packaging.target` provides that
description: construct a :class:`TargetProfile` with the interpreter version,
ABI features, platform architecture and libc/system version, then evaluate or
rank wheel filenames.

Only CPython is supported in this version, covering Linux manylinux and
musllinux targets as well as Windows and macOS.  Every value needed to
generate tags must be supplied explicitly -- the host machine is never
inspected to fill in a missing interpreter, ABI, architecture or libc
version.

Usage
-----

.. doctest::

    >>> from packaging.target import ManylinuxProfile, TargetProfile, evaluate_wheel
    >>> profile = TargetProfile(
    ...     python_version=(3, 13),
    ...     platform=ManylinuxProfile(arch="x86_64", glibc=(2, 28)),
    ... )
    >>> result = evaluate_wheel(
    ...     profile, "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"
    ... )
    >>> result  # doctest: +ELLIPSIS
    WheelMatch(...)
    >>> result.best_tag
    <cp313-cp313-manylinux_2_28_x86_64 ...>

If a required value is not provided, no platform is guessed:

.. doctest::

    >>> result = evaluate_wheel(TargetProfile(), "foo-1.0-py3-none-any.whl")
    >>> result  # doctest: +ELLIPSIS
    InsufficientInformation(...)
    >>> result.missing  # doctest: +SKIP
    ('CPython interpreter version', 'target platform (OS and architecture)')

Ranking several candidates returns the best match first, with the wheel
build tag (PEP 427) and then the input position used as stable tie-breakers:

.. doctest::

    >>> from packaging.target import rank_wheels
    >>> candidates = [
    ...     "foo-1.0-cp313-abi3-manylinux_2_17_x86_64.whl",
    ...     "foo-1.0-1-cp313-cp313-manylinux_2_28_x86_64.whl",
    ...     "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
    ... ]
    >>> ranking = rank_wheels(profile, candidates)
    >>> [r.match.filename for r in ranking if r.available]  # doctest: +NORMALIZE_WHITESPACE
    ['foo-1.0-1-cp313-cp313-manylinux_2_28_x86_64.whl',
     'foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl',
     'foo-1.0-cp313-abi3-manylinux_2_17_x86_64.whl']

Reference
---------

.. autoclass:: TargetProfile()
   :members: tags, is_complete, missing_information

.. autoclass:: PlatformProfile()
   :members:

.. autoclass:: ManylinuxProfile()
   :members:

.. autoclass:: MusllinuxProfile()
.. autoclass:: WindowsProfile()
.. autoclass:: MacOSProfile()

.. autoclass:: WheelMatch()
   :members:

.. autoclass:: WheelRejection()
   :members:

.. autoclass:: Mismatch()
   :members:

.. autoclass:: InsufficientInformation()
   :members:

.. autoclass:: RankedWheel()
   :members:

.. autoexception:: IncompleteProfileError

.. autofunction:: evaluate_wheel

.. autofunction:: rank_wheels
