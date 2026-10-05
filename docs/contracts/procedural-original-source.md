# Procedural original source storage

`repo_usage_pattern` ingress documents addressed to `index-procedural-memory`
are supported at the **raw original source storage stage** in the existing
configured CouchDB source database. A succeeded delivery means the original was
stored and read back, not that approved procedural memory was indexed.

The historical kind and target profile are preserved. The document type remains
`repo_usage_pattern`; it is not a conversation chunk, evidence bundle, steward
proposal or MemoryCard. Its deterministic ID hashes the original ingress natural
key. Content hash, original document metadata, source provider/project, original
job/key and complete wire identity hash provide provenance. The existing source
store enforces create-only identity and full-document conflicts.

Each record has `authority=non_authoritative_original`,
`processing_stage=raw_source_stored`, `recall_eligible=false` and
`projection_enabled=false`. Default session-family reads exclude these originals.
No transcript session, coverage, active-source pointer, projection state,
search registration, embedding, accepted/current record or graph outbox is
created. The approved_memory_policy and separate procedural recall dataset
remain requirements for any later, separately authorized derived memory stage.
Existing dataset purpose and recommended final role do not change.

## Bounded correction and reprocessing

`DeliveryExecutor.correct_and_execute_procedural_once` admits only a retained
procedural job with a single historical uncertain attempt, exact version and
complete immutable payload identity, and no active lease. It corrects only a bare
64-character lowercase SHA256 session hash by adding `sha256:`. Body, content
hash, natural key, job, kind, destination and all other fields remain unchanged.

Before execution, source-owned state APIs atomically preserve original job and
payload plus corrected payload and old/new identities in existing domain_records
and command_results, claim the original job, then reuse the existing executor.
There are no new tables, databases, external keys or queue messages. The original
delivery_payloads entry and historical poison remain untouched. A successful
attempt appends evidence and advances the original job while retaining the first
failure. An already recorded correction rejects retransmission; ambiguous
outcomes require exact GET-only reconciliation, never another correction/send.

Operations must bind the exact authorized job/preimage, persist an INTENT before
the single call and verify the exact raw source record and job afterward. Earlier
HTTP/NATS/ACK evidence remains historical; direct canonical reprocessing must not
be described as consuming a second NATS message. Preserve all trial/audit records
and restore code behavior without deleting newly stored evidence.
