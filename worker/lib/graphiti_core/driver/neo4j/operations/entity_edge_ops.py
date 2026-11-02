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

import logging
from typing import Any

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.operations.entity_edge_ops import EntityEdgeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.record_parsers import entity_edge_from_record
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError
from graphiti_core.local_profile import (
    LOCAL_GRAPH_PROFILE_ID,
    LOCAL_GRAPH_VECTOR_DIM,
    is_local_embedding_mode,
    prepare_entity_edge_save_data,
)
from graphiti_core.models.edges.edge_db_queries import (
    get_entity_edge_return_query,
    get_entity_edge_save_bulk_query,
    get_entity_edge_save_query,
)

logger = logging.getLogger(__name__)


class Neo4jEntityEdgeOperations(EntityEdgeOperations):
    async def save(
        self,
        executor: QueryExecutor,
        edge: EntityEdge,
        tx: Transaction | None = None,
    ) -> None:
        local_mode = is_local_embedding_mode(executor)
        edge_data: dict[str, Any] = {
            'uuid': edge.uuid,
            'source_uuid': edge.source_node_uuid,
            'target_uuid': edge.target_node_uuid,
            'name': edge.name,
            'fact': edge.fact,
            'fact_embedding': edge.fact_embedding,
            'group_id': edge.group_id,
            'episodes': edge.episodes,
            'created_at': edge.created_at,
            'expired_at': edge.expired_at,
            'valid_at': edge.valid_at,
            'invalid_at': edge.invalid_at,
        }
        edge_data.update(edge.attributes or {})
        edge_data = prepare_entity_edge_save_data(edge_data, local_mode=local_mode)

        query = get_entity_edge_save_query(GraphProvider.NEO4J, local_mode=local_mode)
        if tx is not None:
            await tx.run(query, edge_data=edge_data)
        else:
            await executor.execute_query(query, edge_data=edge_data)

        logger.debug(f'Saved Edge to Graph: {edge.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        edges: list[EntityEdge],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        local_mode = is_local_embedding_mode(executor)
        prepared: list[dict[str, Any]] = []
        for edge in edges:
            edge_data: dict[str, Any] = {
                'uuid': edge.uuid,
                'source_node_uuid': edge.source_node_uuid,
                'target_node_uuid': edge.target_node_uuid,
                'name': edge.name,
                'fact': edge.fact,
                'fact_embedding': edge.fact_embedding,
                'group_id': edge.group_id,
                'episodes': edge.episodes,
                'created_at': edge.created_at,
                'expired_at': edge.expired_at,
                'valid_at': edge.valid_at,
                'invalid_at': edge.invalid_at,
            }
            edge_data.update(edge.attributes or {})
            prepared.append(prepare_entity_edge_save_data(edge_data, local_mode=local_mode))

        query = get_entity_edge_save_bulk_query(GraphProvider.NEO4J, local_mode=local_mode)
        if tx is not None:
            await tx.run(query, entity_edges=prepared)
        else:
            await executor.execute_query(query, entity_edges=prepared)

    async def delete(
        self,
        executor: QueryExecutor,
        edge: EntityEdge,
        tx: Transaction | None = None,
    ) -> None:
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n)-[e:MENTIONS|RELATES_TO|HAS_MEMBER {uuid: $uuid}]->(m)
                FOREACH (_ IN CASE WHEN type(e) = 'RELATES_TO' THEN [1] ELSE [] END |
                    MERGE (td:EmbeddingDeletion {target_type: 'edge', target_uuid: e.uuid})
                    ON CREATE SET td.group_id = coalesce(e.group_id, ''), td.element_kind = 'edge', td.uuid = e.uuid, td.incarnation = coalesce(e.incarnation, 1), td.delete_operation_id = 'del:edge:' + e.uuid, td.deleted_at = datetime()
                    ON MATCH SET td.group_id = coalesce(e.group_id, ''), td.element_kind = 'edge', td.uuid = e.uuid, td.incarnation = coalesce(td.incarnation, 0) + 1, td.delete_operation_id = 'del:edge:' + e.uuid, td.deleted_at = datetime()
                    MERGE (fence:EmbeddingGroupFence {group_id: coalesce(e.group_id, '')})
                    ON CREATE SET fence.epoch = 1, fence.status = 'active', fence.updated_at = datetime()
                    ON MATCH SET fence.epoch = coalesce(fence.epoch, 0) + 1, fence.updated_at = datetime()
                )
                DELETE e
            """
        else:
            query = """
                MATCH (n)-[e:MENTIONS|RELATES_TO|HAS_MEMBER {uuid: $uuid}]->(m)
                DELETE e
            """
        if tx is not None:
            await tx.run(query, uuid=edge.uuid)
        else:
            await executor.execute_query(query, uuid=edge.uuid)

        logger.debug(f'Deleted Edge: {edge.uuid}')

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
    ) -> None:
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n)-[e:MENTIONS|RELATES_TO|HAS_MEMBER]->(m)
                WHERE e.uuid IN $uuids
                FOREACH (_ IN CASE WHEN type(e) = 'RELATES_TO' THEN [1] ELSE [] END |
                    MERGE (td:EmbeddingDeletion {target_type: 'edge', target_uuid: e.uuid})
                    ON CREATE SET td.group_id = coalesce(e.group_id, ''), td.element_kind = 'edge', td.uuid = e.uuid, td.incarnation = coalesce(e.incarnation, 1), td.delete_operation_id = 'del:edge:' + e.uuid, td.deleted_at = datetime()
                    ON MATCH SET td.group_id = coalesce(e.group_id, ''), td.element_kind = 'edge', td.uuid = e.uuid, td.incarnation = coalesce(td.incarnation, 0) + 1, td.delete_operation_id = 'del:edge:' + e.uuid, td.deleted_at = datetime()
                    MERGE (fence:EmbeddingGroupFence {group_id: coalesce(e.group_id, '')})
                    ON CREATE SET fence.epoch = 1, fence.status = 'active', fence.updated_at = datetime()
                    ON MATCH SET fence.epoch = coalesce(fence.epoch, 0) + 1, fence.updated_at = datetime()
                )
                DELETE e
            """
        else:
            query = """
                MATCH (n)-[e:MENTIONS|RELATES_TO|HAS_MEMBER]->(m)
                WHERE e.uuid IN $uuids
                DELETE e
            """
        if tx is not None:
            await tx.run(query, uuids=uuids)
        else:
            await executor.execute_query(query, uuids=uuids)

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> EntityEdge:
        query = """
            MATCH (n:Entity)-[e:RELATES_TO {uuid: $uuid}]->(m:Entity)
            RETURN
            """ + get_entity_edge_return_query(GraphProvider.NEO4J)
        records, _, _ = await executor.execute_query(query, uuid=uuid, routing_='r')
        edges = [entity_edge_from_record(r) for r in records]
        if len(edges) == 0:
            raise EdgeNotFoundError(uuid)
        return edges[0]

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[EntityEdge]:
        if not uuids:
            return []
        query = """
            MATCH (n:Entity)-[e:RELATES_TO]->(m:Entity)
            WHERE e.uuid IN $uuids
            RETURN
            """ + get_entity_edge_return_query(GraphProvider.NEO4J)
        records, _, _ = await executor.execute_query(query, uuids=uuids, routing_='r')
        return [entity_edge_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EntityEdge]:
        cursor_clause = 'AND e.uuid < $uuid' if uuid_cursor else ''
        limit_clause = 'LIMIT $limit' if limit is not None else ''
        query = (
            """
            MATCH (n:Entity)-[e:RELATES_TO]->(m:Entity)
            WHERE e.group_id IN $group_ids
            """
            + cursor_clause
            + """
            RETURN
            """
            + get_entity_edge_return_query(GraphProvider.NEO4J)
            + """
            ORDER BY e.uuid DESC
            """
            + limit_clause
        )
        records, _, _ = await executor.execute_query(
            query,
            group_ids=group_ids,
            uuid=uuid_cursor,
            limit=limit,
            routing_='r',
        )
        return [entity_edge_from_record(r) for r in records]

    async def get_between_nodes(
        self,
        executor: QueryExecutor,
        source_node_uuid: str,
        target_node_uuid: str,
    ) -> list[EntityEdge]:
        query = """
            MATCH (n:Entity {uuid: $source_node_uuid})-[e:RELATES_TO]->(m:Entity {uuid: $target_node_uuid})
            RETURN
            """ + get_entity_edge_return_query(GraphProvider.NEO4J)
        records, _, _ = await executor.execute_query(
            query,
            source_node_uuid=source_node_uuid,
            target_node_uuid=target_node_uuid,
            routing_='r',
        )
        return [entity_edge_from_record(r) for r in records]

    async def get_by_node_uuid(
        self,
        executor: QueryExecutor,
        node_uuid: str,
    ) -> list[EntityEdge]:
        query = """
            MATCH (n:Entity {uuid: $node_uuid})-[e:RELATES_TO]-(m:Entity)
            RETURN
            """ + get_entity_edge_return_query(GraphProvider.NEO4J)
        records, _, _ = await executor.execute_query(query, node_uuid=node_uuid, routing_='r')
        return [entity_edge_from_record(r) for r in records]

    async def load_embeddings(
        self,
        executor: QueryExecutor,
        edge: EntityEdge,
    ) -> None:
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n:Entity)-[e:RELATES_TO {uuid: $uuid}]->(m:Entity)
                RETURN CASE
                    WHEN e.fact_embedding_model2vec_v1 IS NOT NULL
                         AND size(e.fact_embedding_model2vec_v1) = $local_dim
                         AND e.fact_embedding_model2vec_v1_profile_id = $local_profile_id
                         AND coalesce(e.embedding_dirty, false) = false
                    THEN e.fact_embedding_model2vec_v1
                    ELSE NULL
                END AS fact_embedding
            """
            records, _, _ = await executor.execute_query(
                query,
                uuid=edge.uuid,
                local_dim=LOCAL_GRAPH_VECTOR_DIM,
                local_profile_id=LOCAL_GRAPH_PROFILE_ID,
                routing_='r',
            )
        else:
            query = """
                MATCH (n:Entity)-[e:RELATES_TO {uuid: $uuid}]->(m:Entity)
                RETURN e.fact_embedding AS fact_embedding
            """
            records, _, _ = await executor.execute_query(query, uuid=edge.uuid, routing_='r')
        if len(records) == 0:
            raise EdgeNotFoundError(edge.uuid)
        edge.fact_embedding = records[0]['fact_embedding']

    async def load_embeddings_bulk(
        self,
        executor: QueryExecutor,
        edges: list[EntityEdge],
        batch_size: int = 100,
    ) -> None:
        uuids = [e.uuid for e in edges]
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n:Entity)-[e:RELATES_TO]-(m:Entity)
                WHERE e.uuid IN $edge_uuids
                RETURN DISTINCT e.uuid AS uuid,
                    CASE
                        WHEN e.fact_embedding_model2vec_v1 IS NOT NULL
                             AND size(e.fact_embedding_model2vec_v1) = $local_dim
                             AND e.fact_embedding_model2vec_v1_profile_id = $local_profile_id
                             AND coalesce(e.embedding_dirty, false) = false
                        THEN e.fact_embedding_model2vec_v1
                        ELSE NULL
                    END AS fact_embedding
            """
            records, _, _ = await executor.execute_query(
                query,
                edge_uuids=uuids,
                local_dim=LOCAL_GRAPH_VECTOR_DIM,
                local_profile_id=LOCAL_GRAPH_PROFILE_ID,
                routing_='r',
            )
        else:
            query = """
                MATCH (n:Entity)-[e:RELATES_TO]-(m:Entity)
                WHERE e.uuid IN $edge_uuids
                RETURN DISTINCT e.uuid AS uuid, e.fact_embedding AS fact_embedding
            """
            records, _, _ = await executor.execute_query(query, edge_uuids=uuids, routing_='r')
        embedding_map = {r['uuid']: r['fact_embedding'] for r in records}
        for edge in edges:
            if edge.uuid in embedding_map:
                edge.fact_embedding = embedding_map[edge.uuid]
