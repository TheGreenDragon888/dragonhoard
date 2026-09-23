"""
tests/conftest.py

Puts every test's temporary database on tmpfs when the host has one.

Each database-backed test builds its own SQLite file in a
tempfile.TemporaryDirectory(), and SQLite fsyncs twice per commit. On this
host's root disk - a virtual rotational disk shared with the other services -
those fsyncs are the whole cost of the suite: the same 631 tests took 9 min 5 s
there and 14.7 s on /dev/shm on 2026-09-17. Nothing in the suite asserts
anything about durability, so nothing is lost by taking the disk out of it.

Pytest imports this before collecting the test modules, and
TemporaryDirectory() consults tempfile.tempdir on every call, so setting it
here covers every test without any test knowing. Only pytest reads a conftest;
`python -m unittest discover` still uses the default temp directory.

A run that finishes leaves nothing behind - every test's teardown deletes its
directory. A run killed mid-test leaves that test's directory (a few hundred
KB) in /dev/shm until a reboot clears tmpfs; later runs never reuse or remove
it. Accepted as harmless.

An explicit TMPDIR is honoured over this, so the directory can still be chosen
from outside when there is a reason to.
"""
import os
import tempfile

_TMPFS = "/dev/shm"

if "TMPDIR" not in os.environ and os.path.isdir(_TMPFS) and os.access(_TMPFS, os.W_OK):
    tempfile.tempdir = _TMPFS
