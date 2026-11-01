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
from graphiti_core.driver.operations.entity_node_ops import EntityNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.record_parsers import entity_node_from_record
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.local_profile import (
    LOCAL_GRAPH_PROFILE_ID,
    LOCAL_GRAPH_VECTOR_DIM,
    is_local_embedding_mode,
    prepare_entity_node_save_data,
)
from graphiti_core.models.nodes.node_db_queries import (
    get_entity_node_return_query,
    get_entity_node_save_bulk_query,
    get_entity_node_save_query,
)
from graphiti_core.nodes import EntityNode

logger = logging.getLogger(__name__)


class Neo4jEntityNodeOperations(EntityNodeOperations):
    async def save(
        self,
        executor: QueryExecutor,
        node: EntityNode,
        tx: Transaction | None = None,
    ) -> None:
        local_mode = is_local_embedding_mode(executor)
        entity_data: dict[str, Any] = {
            'uuid': node.uuid,
            'name': node.name,
            'name_embedding': node.name_embedding,
            'group_id': node.group_id,
            'summary': node.summary,
            'created_at': node.created_at,
        }
        entity_data.update(node.attributes or {})
        entity_data = prepare_entity_node_save_data(entity_data, local_mode=local_mode)
        labels = ':'.join(list(set(node.labels + ['Entity'])))

        query = get_entity_node_save_query(GraphProvider.NEO4J, labels, local_mode=local_mode)

        if tx is not None:
            await tx.run(query, entity_data=entity_data)
        else:
            await executor.execute_query(query, entity_data=entity_data)

        logger.debug(f'Saved Node to Graph: {node.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[EntityNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        local_mode = is_local_embedding_mode(executor)
        prepared: list[dict[str, Any]] = []
        for node in nodes:
            entity_data: dict[str, Any] = {
                'uuid': node.uuid,
                'name': node.name,
                'group_id': node.group_id,
                'summary': node.summary,
                'created_at': node.created_at,
                'name_embedding': node.name_embedding,
                'labels': list(set(node.labels + ['Entity'])),
            }
            entity_data.update(node.attributes or {})
            prepared.append(prepare_entity_node_save_data(entity_data, local_mode=local_mode))

        query = get_entity_node_save_bulk_query(
            GraphProvider.NEO4J,
            prepared,
            local_mode=local_mode,
        )

        if tx is not None:
            await tx.run(query, nodes=prepared)
        else:
            await executor.execute_query(query, nodes=prepared)

    async def delete(
        self,
        executor: QueryExecutor,
        node: EntityNode,
        tx: Transaction | None = None,
    ) -> None:
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n {uuid: $uuid})
                WHERE n:Entity OR n:Episodic OR n:Community
                OPTIONAL MATCH (n)-[r]-()
                WITH n, collect(r.uuid) AS edge_uuids,
                     [rel IN collect(r) WHERE rel IS NOT NULL AND type(rel) = 'RELATES_TO' | {uuid: rel.uuid, group_id: coalesce(rel.group_id, n.group_id, ''), inc: coalesce(rel.incarnation, 1)}] AS rel_metas
                FOREACH (rm IN [x IN rel_metas WHERE x.uuid IS NOT NULL] |
                    MERGE (td:EmbeddingDeletion {group_id: rm.group_id, element_kind: 'edge', uuid: rm.uuid})
                    ON CREATE SET td.incarnation = rm.inc, td.delete_operation_id = 'del:edge:' + rm.uuid, td.deleted_at = datetime()
                    ON MATCH SET td.incarnation = coalesce(td.incarnation, 0) + 1, td.delete_operation_id = 'del:edge:' + rm.uuid, td.deleted_at = datetime()
                )
                FOREACH (_ IN CASE WHEN n:Entity THEN [1] ELSE [] END |
                    MERGE (nd:EmbeddingDeletion {group_id: coalesce(n.group_id, ''), element_kind: 'node', uuid: n.uuid})
                    ON CREATE SET nd.incarnation = coalesce(n.incarnation, 1), nd.delete_operation_id = 'del:node:' + n.uuid, nd.deleted_at = datetime()
                    ON MATCH SET nd.incarnation = coalesce(nd.incarnation, 0) + 1, nd.delete_operation_id = 'del:node:' + n.uuid, nd.deleted_at = datetime()
                    MERGE (fence:EmbeddingGroupFence {group_id: coalesce(n.group_id, '')})
                    ON CREATE SET fence.epoch = 1, fence.status = 'active', fence.updated_at = datetime()
                    ON MATCH SET fence.epoch = coalesce(fence.epoch, 0) + 1, fence.updated_at = datetime()
                )
                DETACH DELETE n
                RETURN edge_uuids
            """
        else:
            query = """
                MATCH (n {uuid: $uuid})
                WHERE n:Entity OR n:Episodic OR n:Community
                OPTIONAL MATCH (n)-[r]-()
                WITH collect(r.uuid) AS edge_uuids, n
                DETACH DELETE n
                RETURN edge_uuids
            """
        if tx is not None:
            await tx.run(query, uuid=node.uuid)
        else:
            await executor.execute_query(query, uuid=node.uuid)

        logger.debug(f'Deleted Node: {node.uuid}')

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n:Entity {group_id: $group_id})
                CALL (n) {
                    MERGE (nd:EmbeddingDeletion {group_id: coalesce(n.group_id, ''), element_kind: 'node', uuid: n.uuid})
                    ON CREATE SET nd.incarnation = coalesce(n.incarnation, 1), nd.delete_operation_id = 'del:node:' + n.uuid, nd.deleted_at = datetime()
                    ON MATCH SET nd.incarnation = coalesce(nd.incarnation, 0) + 1, nd.delete_operation_id = 'del:node:' + n.uuid, nd.deleted_at = datetime()
                    MERGE (fence:EmbeddingGroupFence {group_id: coalesce(n.group_id, '')})
                    ON CREATE SET fence.epoch = 1, fence.status = 'active', fence.updated_at = datetime()
                    ON MATCH SET fence.epoch = coalesce(fence.epoch, 0) + 1, fence.updated_at = datetime()
                    DETACH DELETE n
                } IN TRANSACTIONS OF $batch_size ROWS
            """
        else:
            query = """
                MATCH (n:Entity {group_id: $group_id})
                CALL (n) {
                    DETACH DELETE n
                } IN TRANSACTIONS OF $batch_size ROWS
            """
        if tx is not None:
            await tx.run(query, group_id=group_id, batch_size=batch_size)
        else:
            await executor.execute_query(query, group_id=group_id, batch_size=batch_size)

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n:Entity)
                WHERE n.uuid IN $uuids
                CALL (n) {
                    MERGE (nd:EmbeddingDeletion {group_id: coalesce(n.group_id, ''), element_kind: 'node', uuid: n.uuid})
                    ON CREATE SET nd.incarnation = coalesce(n.incarnation, 1), nd.delete_operation_id = 'del:node:' + n.uuid, nd.deleted_at = datetime()
                    ON MATCH SET nd.incarnation = coalesce(nd.incarnation, 0) + 1, nd.delete_operation_id = 'del:node:' + n.uuid, nd.deleted_at = datetime()
                    MERGE (fence:EmbeddingGroupFence {group_id: coalesce(n.group_id, '')})
                    ON CREATE SET fence.epoch = 1, fence.status = 'active', fence.updated_at = datetime()
                    ON MATCH SET fence.epoch = coalesce(fence.epoch, 0) + 1, fence.updated_at = datetime()
                    DETACH DELETE n
                } IN TRANSACTIONS OF $batch_size ROWS
            """
        else:
            query = """
                MATCH (n:Entity)
                WHERE n.uuid IN $uuids
                CALL (n) {
                    DETACH DELETE n
                } IN TRANSACTIONS OF $batch_size ROWS
            """
        if tx is not None:
            await tx.run(query, uuids=uuids, batch_size=batch_size)
        else:
            await executor.execute_query(query, uuids=uuids, batch_size=batch_size)

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> EntityNode:
        query = """
            MATCH (n:Entity {uuid: $uuid})
            RETURN
            """ + get_entity_node_return_query(GraphProvider.NEO4J)
        records, _, _ = await executor.execute_query(query, uuid=uuid, routing_='r')
        nodes = [entity_node_from_record(r) for r in records]
        if len(nodes) == 0:
            raise NodeNotFoundError(uuid)
        return nodes[0]

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[EntityNode]:
        query = """
            MATCH (n:Entity)
            WHERE n.uuid IN $uuids
            RETURN
            """ + get_entity_node_return_query(GraphProvider.NEO4J)
        records, _, _ = await executor.execute_query(query, uuids=uuids, routing_='r')
        return [entity_node_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EntityNode]:
        cursor_clause = 'AND n.uuid < $uuid' if uuid_cursor else ''
        limit_clause = 'LIMIT $limit' if limit is not None else ''
        query = (
            """
            MATCH (n:Entity)
            WHERE n.group_id IN $group_ids
            """
            + cursor_clause
            + """
            RETURN
            """
            + get_entity_node_return_query(GraphProvider.NEO4J)
            + """
            ORDER BY n.uuid DESC
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
        return [entity_node_from_record(r) for r in records]

    async def load_embeddings(
        self,
        executor: QueryExecutor,
        node: EntityNode,
    ) -> None:
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n:Entity {uuid: $uuid})
                RETURN CASE
                    WHEN n.name_embedding_model2vec_v1 IS NOT NULL
                         AND size(n.name_embedding_model2vec_v1) = $local_dim
                         AND n.name_embedding_model2vec_v1_profile_id = $local_profile_id
                         AND coalesce(n.embedding_dirty, false) = false
                    THEN n.name_embedding_model2vec_v1
                    ELSE NULL
                END AS name_embedding
            """
            records, _, _ = await executor.execute_query(
                query,
                uuid=node.uuid,
                local_dim=LOCAL_GRAPH_VECTOR_DIM,
                local_profile_id=LOCAL_GRAPH_PROFILE_ID,
                routing_='r',
            )
        else:
            query = """
                MATCH (n:Entity {uuid: $uuid})
                RETURN n.name_embedding AS name_embedding
            """
            records, _, _ = await executor.execute_query(query, uuid=node.uuid, routing_='r')
        if len(records) == 0:
            raise NodeNotFoundError(node.uuid)
        node.name_embedding = records[0]['name_embedding']

    async def load_embeddings_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[EntityNode],
        batch_size: int = 100,
    ) -> None:
        uuids = [n.uuid for n in nodes]
        if is_local_embedding_mode(executor):
            query = """
                MATCH (n:Entity)
                WHERE n.uuid IN $uuids
                RETURN DISTINCT n.uuid AS uuid,
                    CASE
                        WHEN n.name_embedding_model2vec_v1 IS NOT NULL
                             AND size(n.name_embedding_model2vec_v1) = $local_dim
                             AND n.name_embedding_model2vec_v1_profile_id = $local_profile_id
                             AND coalesce(n.embedding_dirty, false) = false
                        THEN n.name_embedding_model2vec_v1
                        ELSE NULL
                    END AS name_embedding
            """
            records, _, _ = await executor.execute_query(
                query,
                uuids=uuids,
                local_dim=LOCAL_GRAPH_VECTOR_DIM,
                local_profile_id=LOCAL_GRAPH_PROFILE_ID,
                routing_='r',
            )
        else:
            query = """
                MATCH (n:Entity)
                WHERE n.uuid IN $uuids
                RETURN DISTINCT n.uuid AS uuid, n.name_embedding AS name_embedding
            """
            records, _, _ = await executor.execute_query(query, uuids=uuids, routing_='r')
        embedding_map = {r['uuid']: r['name_embedding'] for r in records}
        for node in nodes:
            if node.uuid in embedding_map:
                node.name_embedding = embedding_map[node.uuid]
