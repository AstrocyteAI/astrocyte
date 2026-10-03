"""Unit tests for ``parse_pgvector`` — no database required.

Regression for the pgvector-python 0.5 crash: registered ``vector`` columns
load as ``pgvector.Vector``, which is not iterable, so the old
``list(row["embedding"])`` in ``list_vectors`` / ``list_recent_vectors``
raised ``TypeError: 'Vector' object is not iterable``. Under 0.4 they load
as numpy arrays; unregistered connections return the text form.
"""

from __future__ import annotations

import pytest
from pgvector import Vector

from astrocyte_postgres._vectors import parse_pgvector


def _assert_floats(result: list[float] | None, expected: list[float]) -> None:
    assert result == pytest.approx(expected)
    assert type(result) is list
    assert all(type(x) is float for x in result)


def test_pgvector_vector_object():
    # pgvector 0.5+ registered path. Construct a real Vector so the test
    # tracks whatever the installed pgvector-python actually returns.
    _assert_floats(parse_pgvector(Vector([0.1, 0.2, 0.3])), [0.1, 0.2, 0.3])


def test_numpy_array():
    # pgvector 0.4 registered path. numpy is optional from pgvector 0.5 on.
    np = pytest.importorskip("numpy")
    _assert_floats(parse_pgvector(np.array([0.1, 0.2, 0.3], dtype=np.float32)), [0.1, 0.2, 0.3])


def test_text_form():
    # Unregistered connection: psycopg returns the vector's text literal.
    # ``list()`` on this would silently yield characters.
    _assert_floats(parse_pgvector("[0.1,0.2,0.3]"), [0.1, 0.2, 0.3])
    assert parse_pgvector("[]") == []


def test_plain_sequences():
    _assert_floats(parse_pgvector([1, 2, 3]), [1.0, 2.0, 3.0])
    _assert_floats(parse_pgvector((1.5, 2.5)), [1.5, 2.5])


def test_none_passes_through():
    assert parse_pgvector(None) is None


def test_unknown_shape_fails_loudly():
    with pytest.raises(TypeError, match="unsupported"):
        parse_pgvector(object())
