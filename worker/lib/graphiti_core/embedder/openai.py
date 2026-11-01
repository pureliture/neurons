"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from collections.abc import Iterable

from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types import EmbeddingModel

from ..local_profile import is_local_embedding_enabled
from .client import EmbedderClient, EmbedderConfig

DEFAULT_EMBEDDING_MODEL = 'text-embedding-3-small'
_LOCAL_EMBEDDING_MODELS = frozenset(
    {
        'local-model2vec-256-v1',
        'minishlab/potion-multilingual-128m',
        'lbrain-memory-model2vec-256-v1',
    }
)
_LOCAL_MAX_BATCH_INPUTS = 64
_LOCAL_MAX_BATCH_UTF8_BYTES = 65536


class OpenAIEmbedderConfig(EmbedderConfig):
    embedding_model: EmbeddingModel | str = DEFAULT_EMBEDDING_MODEL
    api_key: str | None = None
    base_url: str | None = None


class OpenAIEmbedder(EmbedderClient):
    """
    OpenAI Embedder Client

    This client supports both AsyncOpenAI and AsyncAzureOpenAI clients.
    """

    def __init__(
        self,
        config: OpenAIEmbedderConfig | None = None,
        client: AsyncOpenAI | AsyncAzureOpenAI | None = None,
    ):
        if config is None:
            config = OpenAIEmbedderConfig()
        self.config = config

        if client is not None:
            self.client = client
        else:
            self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)

    def _is_local_mode(self) -> bool:
        if is_local_embedding_enabled(self):
            return True
        return str(self.config.embedding_model or '').strip().lower() in _LOCAL_EMBEDDING_MODELS

    async def _create_raw_slice(self, slice_inputs: list[str]) -> list[list[float]]:
        result = await self.client.embeddings.create(
            input=slice_inputs, model=self.config.embedding_model
        )
        ordered = sorted(result.data, key=lambda item: getattr(item, 'index', 0))
        return [list(item.embedding[: self.config.embedding_dim]) for item in ordered]

    async def _create_local_chunks(self, chunks: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        current_slice: list[str] = []
        current_bytes = 0
        for chunk in chunks:
            chunk_bytes = len(chunk.encode('utf-8'))
            if current_slice and (
                len(current_slice) >= _LOCAL_MAX_BATCH_INPUTS
                or current_bytes + chunk_bytes > _LOCAL_MAX_BATCH_UTF8_BYTES
            ):
                vectors.extend(await self._create_raw_slice(current_slice))
                current_slice = []
                current_bytes = 0
            current_slice.append(chunk)
            current_bytes += chunk_bytes
        if current_slice:
            vectors.extend(await self._create_raw_slice(current_slice))
        return vectors

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        if self._is_local_mode() and isinstance(input_data, str):
            from agent_knowledge.local_embedding.canonical import (
                aggregate_chunk_embeddings,
                normalize_canonical_text,
                split_into_canonical_chunks,
            )

            normalized = normalize_canonical_text(input_data)
            chunks = split_into_canonical_chunks(normalized or ' ')
            if len(chunks) == 1 and len(chunks[0].encode('utf-8')) <= 8000:
                result = await self.client.embeddings.create(
                    input=chunks[0], model=self.config.embedding_model
                )
                return list(result.data[0].embedding[: self.config.embedding_dim])
            chunk_vectors = await self._create_local_chunks(chunks)
            return aggregate_chunk_embeddings(
                chunk_vectors, expected_dim=self.config.embedding_dim
            )

        result = await self.client.embeddings.create(
            input=input_data, model=self.config.embedding_model
        )
        return result.data[0].embedding[: self.config.embedding_dim]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if not input_data_list:
            return []
        if self._is_local_mode():
            from agent_knowledge.local_embedding.canonical import (
                aggregate_chunk_embeddings,
                normalize_canonical_text,
                split_into_canonical_chunks,
            )

            flat_chunks: list[str] = []
            spans: list[tuple[int, int]] = []
            for text in input_data_list:
                normalized = normalize_canonical_text(str(text or ''))
                chunks = split_into_canonical_chunks(normalized or ' ')
                start = len(flat_chunks)
                flat_chunks.extend(chunks)
                spans.append((start, len(flat_chunks)))
            flat_vectors = await self._create_local_chunks(flat_chunks)
            aggregated: list[list[float]] = []
            for start, end in spans:
                sub = flat_vectors[start:end]
                if len(sub) == 1:
                    aggregated.append(list(sub[0]))
                else:
                    aggregated.append(
                        aggregate_chunk_embeddings(sub, expected_dim=self.config.embedding_dim)
                    )
            return aggregated

        result = await self.client.embeddings.create(
            input=input_data_list, model=self.config.embedding_model
        )
        return [embedding.embedding[: self.config.embedding_dim] for embedding in result.data]
