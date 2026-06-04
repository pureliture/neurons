#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_verify = load_script("runtime-verify.py")
live_verify = load_script("live-ragflow-verify.py")


class RuntimeVerifyContractTest(unittest.TestCase):
    def test_blocked_pressure_accepts_fetch_and_nak_when_message_is_retained(self):
        evidence = {
            "stream": {"messages": 4, "lastSeq": 9},
            "consumer": {
                "ackFloor": {"stream_seq": 8},
                "numAckPending": 1,
                "numRedelivered": 1,
            },
        }

        runtime_verify.require_blocked_pressure_queue_retained(evidence, before_messages=3, before_last_seq=8)

    def test_blocked_pressure_rejects_acked_message(self):
        evidence = {
            "stream": {"messages": 4, "lastSeq": 9},
            "consumer": {"ackFloor": {"stream_seq": 9}},
        }

        with self.assertRaisesRegex(RuntimeError, "acked"):
            runtime_verify.require_blocked_pressure_queue_retained(evidence, before_messages=3, before_last_seq=8)


class LiveRagFlowVerifyContractTest(unittest.TestCase):
    def test_document_name_candidates_include_queue_content_hash_suffix(self):
        candidates = live_verify.document_name_candidates(
            "rag_ingress_live_verify_example.md",
            "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )

        self.assertEqual(
            candidates,
            [
                "rag_ingress_live_verify_example-0123456789ab.md",
                "rag_ingress_live_verify_example.md",
            ],
        )

    def test_document_search_keyword_uses_original_stem(self):
        self.assertEqual(
            live_verify.document_search_keyword(
                [
                    "rag_ingress_live_verify_example-0123456789ab.md",
                    "rag_ingress_live_verify_example.md",
                ]
            ),
            "rag_ingress_live_verify_example",
        )

    def test_authorized_retrieval_falls_back_to_unfiltered_dataset_query(self):
        calls = []

        def fake_retrieve(_base_url, _api_key, _dataset_id, _marker, *, metadata_filter=True):
            calls.append(metadata_filter)
            return [] if metadata_filter else [{"document_id": "doc_1"}]

        def fake_is_authorized(_ledger_path, document_id, *, project):
            return document_id == "doc_1" and project == "workspace-ragflow-advisor"

        chunks, authorized_chunks, mode = live_verify.retrieve_authorized_chunks(
            "http://ragflow",
            "token",
            "dataset",
            "marker",
            "ledger.sqlite",
            retrieve_fn=fake_retrieve,
            is_authorized_fn=fake_is_authorized,
        )

        self.assertEqual(calls, [True, False])
        self.assertEqual(chunks, [{"document_id": "doc_1"}])
        self.assertEqual(authorized_chunks, [{"document_id": "doc_1"}])
        self.assertEqual(mode, "dataset_unfiltered_fallback")


if __name__ == "__main__":
    unittest.main()
