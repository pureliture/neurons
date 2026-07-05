#!/usr/bin/env python3
import importlib.util
import json
import subprocess
import tempfile
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
live_verify = load_script("live-index-verify.py")


class PostcheckContractTest(unittest.TestCase):
    def _run_postcheck_offline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_path = Path(tmpdir) / "postcheck.json"
            output = subprocess.check_output(
                [
                    "bash",
                    str(Path(__file__).resolve().parent / "postcheck.sh"),
                    "--offline",
                    "--evidence",
                    str(evidence_path),
                ],
                text=True,
            )
            self.assertEqual(output.strip(), evidence_path.read_text(encoding="utf-8").strip())
            return json.loads(output)

    def test_postcheck_offline_marks_api_shape_only_without_full_e2e(self):
        evidence = self._run_postcheck_offline()

        self.assertEqual(evidence["mode"], "offline")
        runtime = evidence["runtime"]
        self.assertEqual(runtime["verificationLevel"], "api_shape_only")
        self.assertFalse(runtime["fullE2EVerified"])
        self.assertFalse(runtime["verified"])


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

    def test_runtime_verify_evidence_names_queue_smoke_not_full_e2e(self):
        evidence = runtime_verify.build_runtime_verification_evidence(
            health={"component": "ingress-api", "status": "ok"},
            status={"target": {"pressure": "CLOSED"}},
            postcheck_json={"runtime": {"verified": False, "verificationLevel": "api_shape_only"}},
            enqueue={"accepted": True},
            rejected={"accepted": False},
            stream={"config": {"name": "RAG_INGRESS_QUEUE", "subjects": ["rag.ingress.>"]}, "state": {}},
            consumer={"name": "rag_target_delivery_worker", "stream_name": "RAG_INGRESS_QUEUE"},
            expected_pressure="CLOSED",
        )

        self.assertTrue(evidence["runtime"]["verified"])
        self.assertEqual(evidence["runtime"]["verificationLevel"], "api_queue_smoke")
        self.assertFalse(evidence["runtime"]["fullE2EVerified"])
        self.assertEqual(evidence["postcheck"]["verificationLevel"], "api_shape_only")


class LiveRetiredIndexBridgeVerifyContractTest(unittest.TestCase):
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

        def fake_retrieve(_base_url, _api_key, _dataset_id, _marker, *, project, metadata_filter=True):
            self.assertEqual(project, "neurons")
            calls.append(metadata_filter)
            return [] if metadata_filter else [{"document_id": "doc_1"}]

        def fake_is_authorized(_ledger_path, document_id, *, project):
            return document_id == "doc_1" and project == "neurons"

        chunks, authorized_chunks, mode = live_verify.retrieve_authorized_chunks(
            "http://retired_index_bridge",
            "token",
            "dataset",
            "marker",
            "ledger.sqlite",
            project="neurons",
            retrieve_fn=fake_retrieve,
            is_authorized_fn=fake_is_authorized,
        )

        self.assertEqual(calls, [True, False])
        self.assertEqual(chunks, [{"document_id": "doc_1"}])
        self.assertEqual(authorized_chunks, [{"document_id": "doc_1"}])
        self.assertEqual(mode, "dataset_unfiltered_fallback")


if __name__ == "__main__":
    unittest.main()
