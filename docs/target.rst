Target profiles
===============

.. currentmodule:: packaging.target

When selecting wheels on one machine for installation *elsewhere* (a
packaging service, a cross-build, an offline resolver), the compatibility
tags of the running interpreter are not relevant. What is needed instead is
an explicit description of the **target** environment: its interpreter
version, ABI features, CPU architecture, operating system, and libc/system
version.

:class:`TargetProfile` provides that description. It generates an ordered
list of compatible tags entirely from the values it is given -- it never
consults :mod:`sysconfig`, :data:`sys.version_info`, or the host operating
system. If a decision cannot be made from the provided information,
:class:`InsufficientTargetInformationError` is raised rather than guessing a
platform.

Usage
-----

Construct a profile with one of the supported factory functions and compare
wheel filenames. Only filenames are inspected; nothing is downloaded.

.. doctest::

    >>> from packaging.target import TargetProfile
    >>> target = TargetProfile.linux_glibc(
    ...     python_version=(3, 13), arch="x86_64", glibc_version=(2, 35)
    ... )
    >>> result = target.evaluate_wheel(
    ...     "numpy-2.0.0-cp313-cp313-manylinux_2_28_x86_64.whl"
    ... )
    >>> result.compatible
    True
    >>> result.best_tag  # doctest: +SKIP
    <cp313-cp313-manylinux_2_28_x86_64>
    >>> bad = target.evaluate_wheel(
    ...     "numpy-2.0.0-cp313-cp313-musllinux_1_2_x86_64.whl"
    ... )
    >>> bad.compatible
    False
    >>> print(bad.explanation)  # doctest: +ELLIPSIS
    not compatible: platform 'musllinux_1_2_x86_64': target uses glibc...

The tags are generated with the same algorithm as
:func:`packaging.tags.sys_tags` (manylinux/musllinux, macOS including
``universal2``, Windows, abi3/abi3t, and pure-Python tags), but driven by
the explicit profile:

.. doctest::

    >>> free_threaded = TargetProfile.linux_glibc(
    ...     (3, 13), "x86_64", (2, 39), free_threaded=True
    ... )
    >>> free_threaded.abis
    ('cp313t',)
    >>> "cp312-abi3t-linux_x86_64" in {str(tag) for tag in free_threaded.tags}
    True

:meth:`TargetProfile.select_wheels` ranks compatible wheels by their best
matching tag, then by their build tag (per PEP 427), and keeps the overall
order stable; incompatible filenames follow in their original input order.

Reference
---------

.. autoclass:: TargetProfile
   :members:
   :class-doc-from: class

.. autodata:: PlatformOS
   :annotation:

.. autoclass:: WheelEvaluation
   :members:

.. autoclass:: InterpreterComponentMismatch
   :members:

.. autoclass:: ABIComponentMismatch
   :members:

.. autoclass:: PlatformComponentMismatch
   :members:

.. autoexception:: InvalidTargetProfile

.. autoexception:: InsufficientTargetInformationError

.. autodata:: ComponentMismatch
