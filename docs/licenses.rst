Licenses
=========

.. currentmodule:: packaging.licenses


Helper for canonicalizing SPDX
`License-Expression metadata <https://peps.python.org/pep-0639/#term-license-expression>`__
as `defined in PEP 639 <https://peps.python.org/pep-0639/#spdx>`__, and for
evaluating a normalized License-Expression against an explicit caller-supplied
license policy.

.. versionadded:: 24.2

The :func:`evaluate_license_expression` function only applies the policy it is
given; it draws no legal conclusions of its own. Licenses and
``license WITH exception`` combinations that the policy does not name are
reported as unknown rather than silently allowed.

Reference
---------

.. automodule:: packaging.licenses
   :members:
