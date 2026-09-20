"""Regression: PostgreSQL float32->halfvec may zero a Python-nonzero half."""
import struct

import pytest

from test_pg_qdrant_import import lane, run, counts, state
from agent_knowledge.postgres_store.pgvector_store import _parse_vector, _vector_literal


@pytest.mark.parametrize('dry_run', [True, False])
def test_pg_normalized_zero_rejected_before_insert_or_receipt(lane, dry_run):
    vector = [2.98023225e-8] * 3072
    assert struct.unpack('e', struct.pack('e', vector[0]))[0] != 0
    with lane['sql'].transaction() as conn:
        value = conn.execute('SELECT %s::halfvec AS embedding',
                             (_vector_literal(vector),)).fetchone()['embedding']
        parsed = _parse_vector(value)
        assert parsed is not None and not any(parsed)
    lane['point']['vector'] = vector
    with pytest.raises(ValueError, match='^Qdrant import rejected: [a-z_]+$'):
        run(lane, dry_run=dry_run)
    assert counts(lane) == (0, 0)
    assert state(lane) is None


@pytest.mark.parametrize('dry_run', [True, False])
def test_pg_normalized_nonzero_subnormal_remains_valid(lane, dry_run):
    lane['point']['vector'] = [5.960464477539063e-8] * 3072
    result = run(lane, dry_run=dry_run)
    assert result['status'] == ('validated' if dry_run else 'projected')
    assert counts(lane) == ((0, 0) if dry_run else (1, 0))
