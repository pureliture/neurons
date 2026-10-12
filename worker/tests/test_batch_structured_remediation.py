"""Capable providers receive the batch schema; validation is still authoritative."""
import json
import re

from agent_knowledge.session_memory.batch_miner import BatchedEnvelopeMiner
from agent_knowledge.session_memory import extraction_llm


def source():
    return {"project":"test","knowledge_id":"synthetic","content":"The owner decided to retain private access.",
            "content_hash":"sha256:"+"0"*64}


class StructuredCompletion:
    def __init__(self): self.calls=0; self.schema=None
    def __call__(self,messages): raise AssertionError("structured provider fell back to unstructured")
    def complete_batch(self,messages,schema):
        self.calls+=1; self.schema=schema
        key=re.findall(r"SOURCE_KEY: ([^\s]+)",messages[-1]["content"])[0]
        return json.dumps([{"source_key":key,"candidate":None}])


def test_batch_uses_explicit_provider_schema_capability_once():
    completion=StructuredCompletion()
    candidates,report=BatchedEnvelopeMiner(completion_fn=completion).mine_batches([source()],refresh_watermark="test")
    assert candidates==[] and report.model_calls==completion.calls==1
    assert completion.schema["type"]=="array"
    assert completion.schema["minItems"]==completion.schema["maxItems"]==1
    assert completion.schema["items"]["required"]==["source_key","candidate"]
    assert completion.schema["items"]["additionalProperties"] is False


def test_local_native_adapter_submits_schema_without_repairing_response():
    posted=[]
    raw='[{"source_key":"unknown","candidate":null}]'
    def post(url,headers,body):
        posted.append(json.loads(body))
        return json.dumps({"message":{"content":raw}})
    completion=extraction_llm.build_ollama_completion_fn("http://synthetic.invalid","synthetic",post_fn=post)
    schema={"type":"array"}
    assert completion.complete_batch([],schema)==raw
    assert len(posted)==1 and posted[0]["format"]==schema and posted[0]["stream"] is False
    assert posted[0]["model"]=="synthetic"


def test_local_native_adapter_failure_is_redacted_and_has_no_retry():
    calls=[]
    def post(*args,**kwargs):calls.append(1);raise OSError("Bearer syntheticprivatekey")
    completion=extraction_llm.build_ollama_completion_fn("http://synthetic.invalid","synthetic",post_fn=post)
    try:completion.complete_batch([],{"type":"array"})
    except extraction_llm.ExtractionTransportError as exc:assert "syntheticprivatekey" not in str(exc)
    else:raise AssertionError("provider failure was swallowed")
    assert len(calls)==1
