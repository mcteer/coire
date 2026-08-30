"""Test doubles shipped with the package.

They live in the package rather than under `tests/` because the Linux integration image runs
the fake engine as a subprocess of a real agent (research R9): it has to be importable from an
installed wheel, not only from a source checkout.
"""
