from __future__ import annotations

import hashlib
import json
import uuid
from urllib.parse import quote, urlencode


class RetiredIndexBridgeApiError(RuntimeError):
    pass


def envelope_code(result: dict) -> int:
    """RetiredIndexBridge Memory 응답 envelope의 논리 code.

    `result` = `_send_memory` 반환 shape: `{status_code, text, json}`. `json` 은
    dict(`{"code": N, ...}`) / str(`"All add to task."`) / None 중 하나다.
    json 이 dict이고 code 가 None 이 아니면 int(code), 그 외엔 0. status_code 는 보지 않는다.
    """
    body = result.get("json")
    if isinstance(body, dict):
        code = body.get("code")
        return 0 if code is None else int(code)
    return 0


def envelope_failed(result: dict) -> bool:
    """RetiredIndexBridge는 논리 에러를 HTTP 200 + envelope code!=0 으로 신호한다.
    status_code!=200 이거나 envelope_code!=0 이면 실패."""
    if result.get("status_code") != 200:
        return True
    return envelope_code(result) != 0


class RetiredIndexBridgeHttpClient:
    def __init__(self, *, base_url: str, bearer_token: str, transport=None, request_timeout_seconds: float = 30):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.transport = transport or _urllib_transport
        self.request_timeout_seconds = max(float(request_timeout_seconds), 1.0)

    def upload_document(self, dataset_id: str, content: str, *, filename: str = "agent-knowledge.md") -> dict:
        boundary = "agentknowledge-" + uuid.uuid4().hex
        safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'.encode(),
                b"Content-Type: text/markdown\r\n\r\n",
                content.encode("utf-8"),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        data = self._request(
            "POST",
            f"/api/v1/datasets/{quote(dataset_id)}/documents",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        document = data[0] if isinstance(data, list) else data
        if not isinstance(document, dict):
            raise RetiredIndexBridgeApiError("upload response missing document")
        document_id = document.get("id") or document.get("document_id")
        if not document_id:
            raise RetiredIndexBridgeApiError("upload response missing document id")
        return {"document_id": document_id, "run": document.get("run", "UNSTART")}

    def upload_file(self, dataset_id: str, file_bytes: bytes, *, filename: str, content_type: str = "application/octet-stream") -> dict:
        """Upload a raw file (PDF/PPTX/DOCX/...) for RetiredIndexBridge native parsing.

        Unlike ``upload_document`` (which uploads UTF-8 text), this forwards the
        original file bytes unchanged so RetiredIndexBridge parses the real document. RetiredIndexBridge
        dispatches its parser by the filename extension, so ``filename`` must keep
        the source extension."""
        boundary = "agentknowledge-" + uuid.uuid4().hex
        safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        data = self._request(
            "POST",
            f"/api/v1/datasets/{quote(dataset_id)}/documents",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        document = data[0] if isinstance(data, list) else data
        if not isinstance(document, dict):
            raise RetiredIndexBridgeApiError("upload response missing document")
        document_id = document.get("id") or document.get("document_id")
        if not document_id:
            raise RetiredIndexBridgeApiError("upload response missing document id")
        return {"document_id": document_id, "run": document.get("run", "UNSTART")}

    def update_metadata(self, dataset_id: str, document_id: str, metadata: dict) -> None:
        request = build_metadata_update_request(dataset_id, document_id, metadata)
        self._request(request["method"], request["path"], json_body=request["json"])

    def request_parse(self, dataset_id: str, document_ids: list[str]) -> None:
        request = build_parse_request(dataset_id, document_ids)
        self._request(request["method"], request["path"], json_body=request["json"])

    def get_document_status(self, dataset_id: str, document_id: str) -> dict:
        query = urlencode({"page": 1, "page_size": 100, "keywords": document_id})
        data = self._request("GET", f"/api/v1/datasets/{quote(dataset_id)}/documents?{query}")
        status = _extract_document_status(data, document_id)
        if status is not None:
            return status
        query = urlencode({"page": 1, "page_size": 100})
        data = self._request("GET", f"/api/v1/datasets/{quote(dataset_id)}/documents?{query}")
        status = _extract_document_status(data, document_id)
        if status is not None:
            return status
        raise RetiredIndexBridgeApiError("document status not found")

    def list_documents(self, dataset_id: str, *, page: int = 1, page_size: int = 100, keywords: str = "") -> list[dict]:
        query_fields = {"page": max(int(page), 1), "page_size": max(int(page_size), 1)}
        if keywords:
            query_fields["keywords"] = keywords
        query = urlencode(query_fields)
        data = self._request("GET", f"/api/v1/datasets/{quote(dataset_id)}/documents?{query}")
        docs = data.get("docs", data) if isinstance(data, dict) else data
        if not isinstance(docs, list):
            raise RetiredIndexBridgeApiError("document list response missing docs")
        return [doc for doc in docs if isinstance(doc, dict)]

    def get_document_meta(self, dataset_id: str, document_id: str) -> dict | None:
        """Read-only fetch of a single document (incl. ``meta_fields``) by id.

        Backs the G3 RetiredIndexBridge-direct recall read-model: RetiredIndexBridge ``/api/v1/retrieval``
        does not return per-chunk ``meta_fields``, so the governance/structural
        envelope is fetched from the indexed document by exact id. Returns the
        matching document dict, or ``None`` when not found (no fuzzy fallback, so a
        mismatched id never enriches the wrong document).
        """
        if not dataset_id or not document_id:
            return None
        query = urlencode({"page": 1, "page_size": 5, "id": document_id})
        data = self._request("GET", f"/api/v1/datasets/{quote(dataset_id)}/documents?{query}")
        docs = data.get("docs", data) if isinstance(data, dict) else data
        if not isinstance(docs, list):
            return None
        for doc in docs:
            if isinstance(doc, dict) and str(doc.get("id") or doc.get("document_id") or "") == document_id:
                return doc
        return None

    def list_document_chunks(self, dataset_id: str, document_id: str, *, page_size: int = 100, max_pages: int = 100) -> list[str]:
        """Read-only complete content of a document via its parsed chunks."""
        contents: list[str] = []
        page = 1
        size = max(int(page_size), 1)
        while page <= max_pages:
            query = urlencode({"page": page, "page_size": size})
            data = self._request(
                "GET",
                f"/api/v1/datasets/{quote(dataset_id)}/documents/{quote(document_id)}/chunks?{query}",
            )
            chunks = None
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, dict):
                    chunks = inner.get("chunks")
                if chunks is None:
                    chunks = data.get("chunks")
            if not isinstance(chunks, list) or not chunks:
                break
            for chunk in chunks:
                if isinstance(chunk, dict):
                    contents.append(str(chunk.get("content") or chunk.get("content_with_weight") or ""))
            if len(chunks) < size:
                break
            page += 1
        return contents

    def list_datasets(
        self,
        *,
        dataset_id: str = "",
        name: str = "",
        include_parsing_status: bool = False,
    ) -> list[dict]:
        query_fields = {}
        if dataset_id:
            query_fields["id"] = dataset_id
        if name:
            query_fields["name"] = name
        if include_parsing_status:
            query_fields["include_parsing_status"] = "true"
        query = urlencode(query_fields)
        path = "/api/v1/datasets" + (f"?{query}" if query else "")
        data = self._request("GET", path)
        return _extract_dataset_list(data)

    def create_dataset(self, *, name: str, **fields) -> dict:
        if not name:
            raise ValueError("dataset name is required")
        payload = {"name": name}
        payload.update({key: value for key, value in fields.items() if value not in {None, ""}})
        data = self._request("POST", "/api/v1/datasets", json_body=payload)
        if not isinstance(data, dict):
            raise RetiredIndexBridgeApiError("dataset create response missing data")
        return data

    def update_dataset(self, dataset_id: str, **fields) -> None:
        if not dataset_id:
            raise ValueError("dataset_id is required")
        payload = {key: value for key, value in fields.items() if value is not None and value != ""}
        self._request("PUT", f"/api/v1/datasets/{quote(dataset_id)}", json_body=payload)

    def retrieve(
        self,
        question: str,
        dataset_ids: list[str],
        filters: dict | None = None,
        limit: int = 10,
        rerank_id: str = "",
        document_ids: list[str] | None = None,
        similarity_threshold: float = 0.2,
    ) -> list[dict]:
        request = build_retrieval_request(
            question,
            dataset_ids,
            filters=filters,
            limit=limit,
            rerank_id=rerank_id,
            document_ids=document_ids,
            similarity_threshold=similarity_threshold,
        )
        data = self._request(request["method"], request["path"], json_body=request["json"])
        chunks = data.get("chunks", data) if isinstance(data, dict) else data
        if not isinstance(chunks, list):
            raise RetiredIndexBridgeApiError("retrieval response missing chunks")
        return [chunk for chunk in chunks if isinstance(chunk, dict)]

    def list_transcript_memory_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
        dataset_name: str = "transcript-memory",
        query: str = "conversation chunk",
        limit: int = 200,
    ) -> list[dict]:
        """Read-only transcript-memory chunk enumeration for session-memory builds."""
        dataset_ids = [
            str(dataset.get("id") or "")
            for dataset in self.list_datasets(name=dataset_name)
            if dataset.get("id")
        ]
        return transcript_memory_records_from_retired_index_bridge(
            self,
            dataset_ids,
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
            query=query,
            limit=limit,
        )

    def list_session_memory_chunks(
        self,
        *,
        project: str | None = None,
        provider: str = "",
        dataset_name: str = "session-memory",
        limit: int = 200,
    ) -> list[dict]:
        """Read-only session-memory (durable SoT) enumeration for autopilot mining."""
        dataset_ids = [
            str(dataset.get("id") or "")
            for dataset in self.list_datasets(name=dataset_name)
            if dataset.get("id")
        ]
        return session_memory_records_from_retired_index_bridge(
            self, dataset_ids, project=project, provider=provider, limit=limit
        )

    def chat_completion(self, messages: list[dict], *, llm_id: str = "", stream: bool = False) -> str:
        request = build_chat_completion_request(messages, llm_id=llm_id, stream=stream)
        data = self._request(request["method"], request["path"], json_body=request["json"])
        if isinstance(data, dict):
            return str(data.get("answer", ""))
        return ""

    def disable_document(self, dataset_id: str, document_id: str) -> None:
        self._request("PATCH", f"/api/v1/datasets/{quote(dataset_id)}/documents/{quote(document_id)}", json_body={"enabled": 0})

    def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        if not document_ids:
            return
        self._request("DELETE", f"/api/v1/datasets/{quote(dataset_id)}/documents", json_body={"ids": document_ids})

    def _send_memory(self, request: dict):
        body = None
        if request.get("json") is not None:
            body = json.dumps(request["json"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.bearer_token}", "Content-Type": "application/json"}
        url = self.base_url + request["path"]
        if self.transport is _urllib_transport:
            response = self.transport(request["method"], url, headers, body or b"", timeout_seconds=self.request_timeout_seconds)
        else:
            response = self.transport(request["method"], url, headers, body or b"")
        text = response.body.decode("utf-8", errors="replace") if response.body else ""
        try:
            parsed = json.loads(text) if text else None
        except (json.JSONDecodeError, ValueError):
            parsed = None
        return {"status_code": response.status_code, "text": text, "json": parsed}

    def create_memory(self, *, name: str, memory_type: list[str], embd_id: str, llm_id: str):
        return self._send_memory(build_create_memory_request(name=name, memory_type=memory_type, embd_id=embd_id, llm_id=llm_id))

    def add_message(self, *, memory_id: list[str], agent_id: str, session_id: str, user_input: str, agent_response: str, user_id: str = ""):
        return self._send_memory(build_add_message_request(memory_id=memory_id, agent_id=agent_id, session_id=session_id, user_input=user_input, agent_response=agent_response, user_id=user_id))

    def search_messages(self, *, query: str, memory_id: str, similarity_threshold: float = 0.2, keywords_similarity_weight: float = 0.7, top_n: int = 10):
        return self._send_memory(build_search_messages_request(query=query, memory_id=memory_id, similarity_threshold=similarity_threshold, keywords_similarity_weight=keywords_similarity_weight, top_n=top_n))

    def list_memories(self, *, memory_type: list[str] | None = None, keywords: str = "", page: int = 1, page_size: int = 50):
        return self._send_memory(build_list_memories_request(memory_type=memory_type, keywords=keywords, page=page, page_size=page_size))

    def delete_memory(self, memory_id: str):
        return self._send_memory(build_delete_memory_request(memory_id))

    def update_message_status(self, *, memory_id: str, message_id: str, status: bool):
        return self._send_memory(build_update_message_status_request(memory_id=memory_id, message_id=message_id, status=status))

    def disable_message(self, *, memory_id: str, message_id: str):
        return self.update_message_status(memory_id=memory_id, message_id=message_id, status=False)

    def _request(self, method: str, path: str, *, json_body: dict | None = None, body: bytes | None = None, content_type: str = "application/json"):
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": content_type,
        }
        if self.transport is _urllib_transport:
            response = self.transport(
                method,
                self.base_url + path,
                headers,
                body or b"",
                timeout_seconds=self.request_timeout_seconds,
            )
        else:
            response = self.transport(method, self.base_url + path, headers, body or b"")
        if response.status_code >= 400:
            raise RetiredIndexBridgeApiError(f"HTTP {response.status_code}")
        try:
            payload = json.loads(response.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetiredIndexBridgeApiError("invalid JSON response") from exc
        if payload.get("code", 0) != 0:
            raise RetiredIndexBridgeApiError(str(payload.get("message", "RetiredIndexBridge API error")))
        return payload.get("data", {})


def _extract_document_status(data: dict | list, document_id: str) -> dict | None:
    docs = data.get("docs", data) if isinstance(data, dict) else data
    if not isinstance(docs, list):
        raise RetiredIndexBridgeApiError("document list response missing docs")
    for doc in docs:
        if isinstance(doc, dict) and doc.get("id") == document_id:
            return {"run": doc.get("run", ""), "progress": doc.get("progress", 0)}
    return None


def _extract_dataset_list(data: dict | list) -> list[dict]:
    datasets = data
    if isinstance(data, dict):
        datasets = data.get("datasets", data.get("data", data.get("items", [])))
    if not isinstance(datasets, list):
        raise RetiredIndexBridgeApiError("dataset list response missing datasets")
    return [dataset for dataset in datasets if isinstance(dataset, dict)]


def transcript_memory_records_from_retired_index_bridge(
    retired_index_bridge,
    dataset_ids,
    *,
    project: str | None = None,
    provider: str | None = None,
    session_id_hash: str | None = None,
    query: str = "conversation chunk",
    limit: int = 200,
) -> list[dict]:
    """Enumerate transcript-memory chunks for a session purely from RetiredIndexBridge.

    ``list_documents`` supplies candidate document ids and ``meta_fields``;
    ``list_document_chunks`` supplies full body text. The Mac ledger is never
    consulted.
    """
    ids = [str(dataset_id) for dataset_id in (dataset_ids or []) if dataset_id]
    if not ids:
        return []

    fragment = (session_id_hash or "").split(":")[-1][:12]
    records: list[dict] = []
    for dataset_id in ids:
        try:
            docs = retired_index_bridge.list_documents(
                dataset_id,
                keywords=fragment,
                page_size=max(int(limit), 1),
            )
        except Exception:  # noqa: BLE001 - read-SoT must fail closed
            docs = []
        matched: list[tuple[str, dict]] = []
        for doc in docs or []:
            if not isinstance(doc, dict):
                continue
            document_id = str(doc.get("id") or doc.get("document_id") or "")
            if not document_id:
                continue
            meta = doc.get("meta_fields") or doc.get("metadata") or {}
            if not isinstance(meta, dict) or not meta:
                meta = _transcript_memory_meta_for_document(retired_index_bridge, [dataset_id], document_id) or {}
            if not meta:
                continue
            meta_type = meta.get("result_type") or meta.get("type") or meta.get("kind")
            if meta_type != "conversation_chunk":
                continue
            if session_id_hash and str(meta.get("session_id_hash") or "") != session_id_hash:
                continue
            if provider and str(meta.get("provider") or "") != provider:
                continue
            if project and str(meta.get("project") or "") != project:
                continue
            matched.append((document_id, dict(meta)))
        if not matched:
            continue
        contents = _transcript_memory_content_by_document(
            retired_index_bridge, [dataset_id], [document_id for document_id, _ in matched], query, limit
        )
        for document_id, meta in matched:
            content = contents.get(document_id, "")
            valid_hash = _ensure_sha256_content_hash(meta.get("content_hash"), content)
            meta["content_hash"] = valid_hash
            records.append({"metadata": meta, "content": content, "content_hash": valid_hash})
    return _drop_turn_window_subsumed_records(records)


def session_memory_records_from_retired_index_bridge(
    retired_index_bridge,
    dataset_ids,
    *,
    project: str | None = None,
    provider: str = "",
    limit: int = 200,
) -> list[dict]:
    """Read durable session-memory docs (the lossless SoT) as mineable records.

    Unlike transcript-memory (transient conversation_chunk docs with rich meta_fields),
    session-memory docs (``ak-session-memory-<provider>-<project>-<hash>``) carry no
    meta_fields — provider/project live in the document name — so we filter by name and
    fetch body via list_document_chunks. Output shape matches the transcript reader so the
    envelope miner consumes it unchanged.
    """
    ids = [str(dataset_id) for dataset_id in (dataset_ids or []) if dataset_id]
    page_size = max(int(limit), 1)
    records: list[dict] = []
    for dataset_id in ids:
        page = 1
        while len(records) < limit:
            try:
                docs = retired_index_bridge.list_documents(dataset_id, page=page, page_size=page_size)
            except Exception:  # noqa: BLE001 - read-SoT must fail closed
                docs = []
            if not docs:
                break
            for doc in docs:
                if len(records) >= limit:
                    break
                if not isinstance(doc, dict):
                    continue
                document_id = str(doc.get("id") or doc.get("document_id") or "")
                name = str(doc.get("name") or "")
                if not document_id or not name:
                    continue
                if provider and f"session-memory-{provider}-" not in name:
                    continue
                if project and f"-{project}-" not in name:
                    continue
                try:
                    parts = retired_index_bridge.list_document_chunks(dataset_id, document_id)
                except Exception:  # noqa: BLE001
                    parts = []
                content = "".join(parts) if isinstance(parts, list) else str(parts or "")
                if not content:
                    continue
                content_hash = _ensure_sha256_content_hash(doc.get("content_hash"), content)
                records.append(
                    {
                        "metadata": {
                            "project": project or "",
                            "provider": provider,
                            "knowledge_id": document_id,
                            "result_type": "session_memory",
                        },
                        "content": content,
                        "content_hash": content_hash,
                    }
                )
            if len(docs) < page_size:
                break
            page += 1
    return records


def _drop_turn_window_subsumed_records(records: list[dict]) -> list[dict]:
    def window(record: dict) -> tuple[int, int]:
        meta = record.get("metadata") or {}
        try:
            return (int(meta.get("turn_start_index") or 0), int(meta.get("turn_end_index") or 0))
        except (TypeError, ValueError):
            return (0, 0)

    deduped: list[dict] = []
    seen: set[tuple] = set()
    for record in records:
        key = (window(record), str((record.get("metadata") or {}).get("content_hash") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)

    kept: list[dict] = []
    for index, record in enumerate(deduped):
        start, end = window(record)
        contained = False
        for other_index, other in enumerate(deduped):
            if other_index == index:
                continue
            other_start, other_end = window(other)
            if (other_start, other_end) == (start, end):
                continue
            if other_start <= start and other_end >= end:
                contained = True
                break
        if not contained:
            kept.append(record)
    return kept


def _transcript_memory_content_by_document(retired_index_bridge, dataset_ids, document_ids, query, limit) -> dict[str, str]:
    by_document: dict[str, str] = {}
    if hasattr(retired_index_bridge, "list_document_chunks"):
        from concurrent.futures import ThreadPoolExecutor

        def _fetch(task):
            dataset_id, document_id = task
            try:
                parts = retired_index_bridge.list_document_chunks(dataset_id, document_id)
            except Exception:  # noqa: BLE001 - read-SoT must fail closed
                parts = []
            return document_id, ("".join(parts) if parts else "")

        # RetiredIndexBridge has no batch chunks endpoint, so the per-document fan-out is the
        # dominant build wall-clock. Reads are stateless; run them concurrently and
        # merge first-non-empty-per-id (preserves the prior sequential semantics).
        tasks = [(dataset_id, document_id) for dataset_id in dataset_ids for document_id in document_ids]
        if tasks:
            with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as executor:
                for document_id, content in executor.map(_fetch, tasks):
                    if content and not by_document.get(document_id):
                        by_document[document_id] = content
        return by_document

    grouped: dict[str, list[str]] = {}
    try:
        hits = retired_index_bridge.retrieve(query, dataset_ids, document_ids=list(document_ids), limit=max(len(document_ids) * 4, int(limit)))
    except Exception:  # noqa: BLE001 - read-SoT must fail closed
        hits = []
    for hit in hits or []:
        if isinstance(hit, dict):
            document_id = str(hit.get("document_id") or hit.get("doc_id") or "")
            if document_id:
                grouped.setdefault(document_id, []).append(str(hit.get("content") or ""))
    return {document_id: "".join(parts) for document_id, parts in grouped.items()}


def _ensure_sha256_content_hash(value: str, content: str) -> str:
    text = str(value or "")
    if text.startswith("sha256:") and len(text) > len("sha256:"):
        return text
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _transcript_memory_meta_for_document(retired_index_bridge, dataset_ids, document_id: str) -> dict | None:
    for dataset_id in dataset_ids:
        try:
            doc = retired_index_bridge.get_document_meta(dataset_id, document_id)
        except Exception:  # noqa: BLE001 - read-SoT must fail closed
            doc = None
        if isinstance(doc, dict) and doc:
            meta = doc.get("meta_fields") or doc.get("metadata") or {}
            if isinstance(meta, dict) and meta:
                return dict(meta)
    return None


def build_metadata_update_request(dataset_id: str, document_id: str, metadata: dict) -> dict:
    return {
        "method": "PATCH",
        "path": f"/api/v1/datasets/{dataset_id}/documents/{document_id}",
        "json": {"meta_fields": metadata},
    }


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes, *, timeout_seconds: float = 30):
    from urllib import request
    from urllib.error import HTTPError, URLError

    from .transport_contract import ProxyResponse

    req = request.Request(url, data=body if body else None, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return ProxyResponse(
                status_code=response.status,
                body=response.read(),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except HTTPError as exc:
        return ProxyResponse(
            status_code=exc.code,
            body=exc.read(),
            headers={key.lower(): value for key, value in exc.headers.items()},
        )
    except (URLError, TimeoutError) as exc:
        raise RetiredIndexBridgeApiError(f"connection failed: {str(exc)}") from exc


def build_chat_completion_request(messages: list[dict], *, llm_id: str = "", stream: bool = False) -> dict:
    payload = {"messages": messages, "stream": stream}
    if llm_id:
        payload["llm_id"] = llm_id
    return {
        "method": "POST",
        "path": "/api/v1/chat/completions",
        "json": payload,
    }


def build_parse_request(dataset_id: str, document_ids: list[str]) -> dict:
    return {
        "method": "POST",
        "path": f"/api/v1/datasets/{dataset_id}/documents/parse",
        "json": {"document_ids": document_ids},
    }


def build_retrieval_request(
    question: str,
    dataset_ids: list[str],
    *,
    filters: dict | None = None,
    limit: int = 10,
    rerank_id: str = "",
    document_ids: list[str] | None = None,
    similarity_threshold: float = 0.2,
) -> dict:
    conditions = []
    for key, value in (filters or {}).items():
        conditions.append({"name": key, "comparison_operator": "=", "value": value})
    top_k = 32 if rerank_id else 1024
    payload = {
        "question": question,
        "dataset_ids": dataset_ids,
        "page_size": max(int(limit), 1),
        "similarity_threshold": float(similarity_threshold),
        "vector_similarity_weight": 0.3,
        "top_k": top_k,
        "keyword": False,
    }
    if conditions:
        payload["metadata_condition"] = {"logic": "and", "conditions": conditions}
    if document_ids:
        payload["document_ids"] = document_ids
    if rerank_id:
        payload["rerank_id"] = rerank_id
    return {
        "method": "POST",
        "path": "/api/v1/retrieval",
        "json": payload,
    }


def build_create_memory_request(*, name: str, memory_type: list[str], embd_id: str, llm_id: str) -> dict:
    return {
        "method": "POST",
        "path": "/api/v1/memories",
        "json": {"name": name, "memory_type": list(memory_type), "embd_id": embd_id, "llm_id": llm_id},
    }


def build_add_message_request(*, memory_id: list[str], agent_id: str, session_id: str, user_input: str, agent_response: str, user_id: str = "") -> dict:
    payload = {
        "memory_id": list(memory_id),
        "agent_id": agent_id,
        "session_id": session_id,
        "user_input": user_input,
        "agent_response": agent_response,
    }
    if user_id:
        payload["user_id"] = user_id
    return {"method": "POST", "path": "/api/v1/messages", "json": payload}


def build_search_messages_request(*, query: str, memory_id: str, similarity_threshold: float = 0.2, keywords_similarity_weight: float = 0.7, top_n: int = 10) -> dict:
    qs = urlencode({
        "query": query,
        "memory_id": memory_id,
        "similarity_threshold": similarity_threshold,
        "keywords_similarity_weight": keywords_similarity_weight,
        "top_n": max(int(top_n), 1),
    })
    return {"method": "GET", "path": f"/api/v1/messages/search?{qs}", "json": None}


def build_delete_memory_request(memory_id: str) -> dict:
    return {"method": "DELETE", "path": f"/api/v1/memories/{quote(str(memory_id))}", "json": None}


def build_update_message_status_request(*, memory_id: str, message_id: str, status: bool) -> dict:
    # 라이브 검증된 endpoint: PUT /api/v1/messages/{memory_id}:{message_id} {"status": bool}.
    # 복합 path 의 콜론 구분자는 보존하고 양 컴포넌트만 safe="" 로 quote(슬래시/공백 등 인코딩).
    composite = f"{quote(str(memory_id), safe='')}:{quote(str(message_id), safe='')}"
    return {"method": "PUT", "path": f"/api/v1/messages/{composite}", "json": {"status": status}}


def build_list_memories_request(*, memory_type: list[str] | None = None, keywords: str = "", page: int = 1, page_size: int = 50) -> dict:
    fields = {"page": max(int(page), 1), "page_size": max(int(page_size), 1)}
    if memory_type:
        fields["memory_type"] = ",".join(memory_type)
    if keywords:
        fields["keywords"] = keywords
    return {"method": "GET", "path": f"/api/v1/memories?{urlencode(fields)}", "json": None}
