"""The batch prompt must specify one unambiguous outer response shape."""
import json
import re

from agent_knowledge.session_memory.batch_miner import BatchedEnvelopeMiner


def test_batch_prompt_has_no_conflicting_flat_candidate_instruction():
    seen = []
    def completion(messages):
        seen.extend(messages)
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
        return json.dumps([{"source_key": key, "candidate": None} for key in keys])
    chunk = {"content": "No durable decision", "project": "test", "knowledge_id": "synthetic",
             "content_hash": "sha256:" + "0" * 64}
    BatchedEnvelopeMiner(completion_fn=completion).mine_batches([chunk], refresh_watermark="test")
    system = seen[0]["content"]
    assert "each candidate is an object with keys" not in system
    assert "EXAMPLE:" in system
    example = json.loads(system.split("EXAMPLE:", 1)[1].strip())
    assert set(example[0]) == {"source_key", "candidate"}
    assert set(example[0]["candidate"]) >= {"card_type", "title", "statement", "typed_payload"}
