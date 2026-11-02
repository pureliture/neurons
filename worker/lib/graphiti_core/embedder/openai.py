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

    async def _embed_local_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` under ``canonical_embedding_v1`` through one shared path.

        Every local call shape (``str``, ``[str]``, batches) goes through the same
        normalize -> split -> plan -> aggregate pipeline, so item/size limits of the
        service are respected and multi-chunk vectors use UTF-8 byte-length weights.
        """
        from agent_knowledge.local_embedding.canonical import (
            aggregate_chunk_embeddings,
            normalize_canonical_text,
            plan_request_batches_v1,
            split_into_canonical_chunks,
        )

        flat_chunks: list[str] = []
        spans: list[tuple[int, int]] = []
        for text in texts:
            normalized = normalize_canonical_text(str(text or ''))
            chunks = split_into_canonical_chunks(normalized or ' ')
            start = len(flat_chunks)
            flat_chunks.extend(chunks)
            spans.append((start, len(flat_chunks)))

        batches = plan_request_batches_v1(
            flat_chunks,
            model=str(self.config.embedding_model),
            encoding_format=None,
            dimensions=None,
        )
        flat_vectors: list[list[float]] = []
        for batch in batches:
            flat_vectors.extend(await self._create_raw_slice(batch))
        if len(flat_vectors) != len(flat_chunks):
            raise ValueError('local embedding response count does not match request chunks')

        byte_lengths = [len(chunk.encode('utf-8')) for chunk in flat_chunks]
        return [
            aggregate_chunk_embeddings(
                flat_vectors[start:end],
                byte_lengths[start:end],
                expected_dim=self.config.embedding_dim,
            )
            for start, end in spans
        ]

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        if self._is_local_mode():
            if isinstance(input_data, str):
                return (await self._embed_local_texts([input_data]))[0]
            if (
                isinstance(input_data, (list, tuple))
                and input_data
                and all(isinstance(item, str) for item in input_data)
            ):
                return (await self._embed_local_texts(list(input_data)))[0]

        result = await self.client.embeddings.create(
            input=input_data, model=self.config.embedding_model
        )
        return result.data[0].embedding[: self.config.embedding_dim]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if not input_data_list:
            return []
        if self._is_local_mode():
            return await self._embed_local_texts(list(input_data_list))

        result = await self.client.embeddings.create(
            input=input_data_list, model=self.config.embedding_model
        )
        return [embedding.embedding[: self.config.embedding_dim] for embedding in result.data]
