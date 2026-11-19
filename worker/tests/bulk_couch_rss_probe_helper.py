"""Peak RSS of the real CouchDB HTTP read path in the stream runner.

Public-safe test helper. It runs only from the synthetic isolated-CouchDB RSS
test and uses environment-defined local endpoints/credentials; it contains no
real credential, private path, or captured fixture body.
"""

from __future__ import annotations

import base64
import json
import os
import resource
import sys
import tempfile
import traceback

from agent_knowledge.couchdb_source import source_revision as sr
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.llm_brain_core import bulk_stream_runner as runner
from agent_knowledge.llm_brain_core.bulk_full_input import FullInputBuilder
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
)
from agent_knowledge.llm_brain_core.bulk_source_validation import plan_validated_source
from agent_knowledge.llm_brain_core.bulk_streaming import iter_planned_chunks
from agent_knowledge.couchdb_source.document_model import build_session_id_hash

FullInputBuilder.__init__ = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError("full builder used in stream mode")
)
BRANCH = []
_read_pointer = sr._read_currentness_pointer


def _instrumented_read_pointer(**kwargs):
    result = _read_pointer(**kwargs)
    BRANCH.append(bool(result[1] is not None))
    return result


sr._read_currentness_pointer = _instrumented_read_pointer

AUTHORITY_SEAM = os.environ.get("BULK_RSS_AUTHORITY_SEAM", "")
if AUTHORITY_SEAM == "fake_empty":
    runner._pg_authority_by_hash = lambda *args, **kwargs: {}
elif not os.environ.get("TRIAL_PG_DSN"):
    raise RuntimeError("authority DSN unavailable; set fake seam explicitly")

RESULT_FLAGS = {"texts": 0, "first": False, "middle": False,
                "last": False, "evidence": 0}
project = sys.argv[1]
SID = build_session_id_hash("synthetic-provider", project + "-session")
runner._select_sessions_bounded = lambda *args, **kwargs: type("Selection", (), {
    "sessions": [{"session_id_hash": SID, "project": project}]
})()
auth = "Basic " + base64.b64encode(
    f"admin:{os.environ['TRIAL_COUCH_PASSWORD']}".encode()
).decode()
store = CouchDBHttpSourceStore(
    base_url=os.environ["TRIAL_COUCH_URL"], db=os.environ["TRIAL_COUCH_DB"],
    auth_header=auth,
)
COUNTS = {"store_get": 0, "get_many": 0, "iter_by_session": 0, "currentness": 0}
_get = store.get
_iter = store.iter_by_session
_get_many = store.get_many
_currentness = runner.session_source_revision_from_couchdb_source


def _counted_get(doc_id):
    COUNTS["store_get"] += 1
    return _get(doc_id)


def _counted_iter(**kwargs):
    COUNTS["iter_by_session"] += 1
    yield from _iter(**kwargs)


def _counted_get_many(*args, **kwargs):
    COUNTS["get_many"] += 1
    return _get_many(*args, **kwargs)


def _counted_currentness(**kwargs):
    COUNTS["currentness"] += 1
    return _currentness(**kwargs)


store.get = _counted_get
store.iter_by_session = _counted_iter
runner.session_source_revision_from_couchdb_source = _counted_currentness
store.get_many = _counted_get_many


class State:
    def is_complete(self, key):
        return False

    def is_session_complete(self, **kwargs):
        return False

    def register_plan(self, **kwargs):
        return None

    def claim(self, *, key, owner):
        return 1

    def complete(self, **kwargs):
        return None


class Extractor:
    model = "free-model"
    provider = "synthetic-provider"
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def extract(self, batch):
        RESULT_FLAGS["texts"] += len(batch)
        for item in batch:
            text = item.text
            if "[FIRST]" in text:
                RESULT_FLAGS["first"] = True
            if "[MIDDLE]" in text:
                RESULT_FLAGS["middle"] = True
            if "[LAST]" in text:
                RESULT_FLAGS["last"] = True
            if item.episode.payload.get("input_lane") == "evidence":
                RESULT_FLAGS["evidence"] += 1
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name="Synthetic", type="Concept", summary="s"),),
            )
            for item in batch
        ))


class Writer:
    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            chunk_receipts={item.session_key: "receipt:" + item.session_key for item in inputs},
        )


try:
    with tempfile.TemporaryDirectory() as tmp:
        report = runner.run_streamed_full_input(
            ledger_path=os.path.join(tmp, "ledger.db"),
            source_store=store,
            extractor=Extractor(),
            writer=Writer(),
            full_input_state=State(),
            policy_version="couch-rss",
            project=project,
            limit=1,
            batch_size=4,
        )
except Exception:
    traceback.print_exc()
    raise

expected_plan = plan_validated_source(
    store, session_id_hash=SID,
    expected_source_hash=runner.session_source_revision_from_couchdb_source(
        session_id_hash=SID, source_store=store
    )
)
EXPECTED_HASHES = sorted(c.canonical_hash for c in iter_planned_chunks(store, expected_plan))
print(json.dumps({
    "branch_pointer_present": BRANCH,
    "resolver_branch": "active" if BRANCH and any(BRANCH) else "legacy",
    "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    "authority_seam": AUTHORITY_SEAM or "isolated_pg",
    "report": report,
    "model_flags": RESULT_FLAGS,
    "counts": COUNTS,
    "expected_hashes": EXPECTED_HASHES,
}, sort_keys=True))
