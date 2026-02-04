"""Thread detection service for Sieve - Algorithmic story thread clustering.

Detects threads (ongoing story clusters) by combining embedding similarity
with entity overlap. No LLM calls — purely algorithmic using existing
embeddings and extracted entities.

Algorithm:
1. Get all articles with embeddings AND entities from last 30 days
2. Build inverted entity index: entity_name_lower -> set(article_ids)
3. For each article:
   a) Embedding similarity: KNN top-5 via sqlite-vec
   b) Entity overlap: articles sharing 2+ entities via inverted index
   c) Union the results -> that article's "related" set
4. Build undirected graph from all article->related edges
5. Find connected components (BFS)
6. Filter: keep components with >= CLUSTER_THRESHOLD articles
7. For each qualifying cluster:
   - Check overlap with existing threads (>50% shared articles -> extend)
   - Otherwise create new thread
   - Name from most frequent entity across cluster articles
   - Store primary_entities as top-5 most frequent
"""

import json
import logging
from collections import Counter, defaultdict, deque

from db import (
    add_articles_to_thread,
    create_thread,
    get_all_thread_article_ids,
    get_articles_with_entities_in_range,
    get_threads,
    search_by_embedding_with_date,
    update_thread,
)

logger = logging.getLogger(__name__)

CLUSTER_THRESHOLD = 5       # Minimum articles to form a thread
EMBEDDING_TOP_K = 5         # Number of KNN neighbors per article
DATE_RANGE_DAYS = 30        # Look back window for articles
ENTITY_OVERLAP_MIN = 2      # Minimum shared entities to link articles
THREAD_OVERLAP_RATIO = 0.5  # Ratio of shared articles to merge into existing thread


def _build_entity_index(articles):
    """Build inverted index from entity name -> set of article IDs.

    Args:
        articles: List of article dicts with 'id' and 'entities' (JSON string)

    Returns:
        Dict mapping lowercase entity name to set of article IDs
    """
    index = defaultdict(set)

    for article in articles:
        entities_raw = article.get("entities")
        if not entities_raw:
            continue

        try:
            entities_dict = json.loads(entities_raw) if isinstance(entities_raw, str) else entities_raw
        except (json.JSONDecodeError, TypeError):
            continue

        article_id = article["id"]
        for category_entities in entities_dict.values():
            if not isinstance(category_entities, list):
                continue
            for entity_name in category_entities:
                if isinstance(entity_name, str) and entity_name.strip():
                    index[entity_name.strip().lower()].add(article_id)

    return dict(index)


def _find_entity_neighbors(article, entity_index, min_overlap=ENTITY_OVERLAP_MIN):
    """Find articles sharing at least min_overlap entities with this article.

    Args:
        article: Article dict with 'id' and 'entities'
        entity_index: Inverted index from _build_entity_index()
        min_overlap: Minimum number of shared entities

    Returns:
        Set of related article IDs (excluding the article itself)
    """
    entities_raw = article.get("entities")
    if not entities_raw:
        return set()

    try:
        entities_dict = json.loads(entities_raw) if isinstance(entities_raw, str) else entities_raw
    except (json.JSONDecodeError, TypeError):
        return set()

    article_id = article["id"]

    # Gather all entity names for this article
    article_entities = set()
    for category_entities in entities_dict.values():
        if not isinstance(category_entities, list):
            continue
        for name in category_entities:
            if isinstance(name, str) and name.strip():
                article_entities.add(name.strip().lower())

    # Count how many entities each other article shares
    neighbor_counts = Counter()
    for entity_name in article_entities:
        for other_id in entity_index.get(entity_name, set()):
            if other_id != article_id:
                neighbor_counts[other_id] += 1

    return {aid for aid, count in neighbor_counts.items() if count >= min_overlap}


def _find_embedding_neighbors(article):
    """Find similar articles via KNN embedding search.

    Args:
        article: Article dict with 'id' and 'embedding' (binary blob)

    Returns:
        Set of related article IDs (excluding the article itself)
    """
    embedding_blob = article.get("embedding")
    if not embedding_blob:
        return set()

    try:
        results = search_by_embedding_with_date(
            embedding_blob,
            limit=EMBEDDING_TOP_K,
            days=DATE_RANGE_DAYS,
            exclude_id=article["id"],
        )
        return {r["id"] for r in results}
    except Exception as e:
        logger.warning(f"Embedding search failed for article {article['id']}: {e}")
        return set()


def _find_connected_components(graph):
    """Find connected components in an undirected graph using BFS.

    Args:
        graph: Dict mapping node_id -> set of neighbor node_ids

    Returns:
        List of sets, each set being a connected component
    """
    visited = set()
    components = []

    for node in graph:
        if node in visited:
            continue

        # BFS from this node
        component = set()
        queue = deque([node])

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            component.add(current)

            for neighbor in graph.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)

        if component:
            components.append(component)

    return components


def _name_thread_from_entities(article_ids, articles_by_id):
    """Generate a thread name from the most frequent entity across articles.

    Args:
        article_ids: Set of article IDs in the cluster
        articles_by_id: Dict mapping article ID to article dict

    Returns:
        Tuple of (thread_name, top_5_entities_list)
    """
    entity_counts = Counter()

    for aid in article_ids:
        article = articles_by_id.get(aid)
        if not article:
            continue

        entities_raw = article.get("entities")
        if not entities_raw:
            continue

        try:
            entities_dict = json.loads(entities_raw) if isinstance(entities_raw, str) else entities_raw
        except (json.JSONDecodeError, TypeError):
            continue

        for category_entities in entities_dict.values():
            if not isinstance(category_entities, list):
                continue
            for name in category_entities:
                if isinstance(name, str) and name.strip():
                    entity_counts[name.strip()] += 1

    if not entity_counts:
        return "Unnamed Thread", []

    top_entities = [name for name, _ in entity_counts.most_common(5)]
    thread_name = top_entities[0]

    return thread_name, top_entities


def detect_threads(on_progress=None):
    """
    Detect story threads by clustering articles via embedding similarity + entity overlap.

    Algorithmic — no LLM calls. Uses existing embeddings and extracted entities.

    Returns:
        dict with threads_created, threads_updated, articles_linked
    """
    result = {
        "threads_created": 0,
        "threads_updated": 0,
        "articles_linked": 0,
    }

    # Step 1: Get articles with both embeddings and entities
    articles = get_articles_with_entities_in_range(days=DATE_RANGE_DAYS)
    total = len(articles)

    if total < CLUSTER_THRESHOLD:
        logger.info(f"Only {total} articles with entities+embeddings — need at least {CLUSTER_THRESHOLD} for threads")
        return result

    logger.info(f"Thread detection: analyzing {total} articles from last {DATE_RANGE_DAYS} days")

    articles_by_id = {a["id"]: a for a in articles}
    article_ids = set(articles_by_id.keys())

    # Step 2: Build inverted entity index
    entity_index = _build_entity_index(articles)
    logger.info(f"Entity index built: {len(entity_index)} unique entities")

    # Step 3: Build relationship graph
    graph = defaultdict(set)

    for i, article in enumerate(articles):
        aid = article["id"]

        # a) Embedding neighbors
        emb_neighbors = _find_embedding_neighbors(article)
        # Only include neighbors that are in our working set
        emb_neighbors &= article_ids

        # b) Entity overlap neighbors
        ent_neighbors = _find_entity_neighbors(article, entity_index)
        ent_neighbors &= article_ids

        # c) Union
        all_neighbors = emb_neighbors | ent_neighbors

        for neighbor_id in all_neighbors:
            graph[aid].add(neighbor_id)
            graph[neighbor_id].add(aid)

        if on_progress and (i + 1) % 50 == 0:
            try:
                on_progress(i + 1, total)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    logger.info(f"Relationship graph built: {len(graph)} nodes with edges")

    # Step 4: Find connected components
    components = _find_connected_components(graph)
    logger.info(f"Found {len(components)} connected components")

    # Step 5: Filter by threshold
    qualifying = [c for c in components if len(c) >= CLUSTER_THRESHOLD]
    logger.info(f"{len(qualifying)} components meet threshold of {CLUSTER_THRESHOLD}+ articles")

    if not qualifying:
        return result

    # Step 6: Load existing thread associations for overlap detection
    existing_thread_articles = get_all_thread_article_ids()
    existing_threads = {t["id"]: t for t in get_threads(limit=500)}

    # Step 7: Process each qualifying cluster
    for cluster in qualifying:
        # Check overlap with existing threads
        best_thread_id = None
        best_overlap = 0

        for thread_id, thread_article_ids in existing_thread_articles.items():
            overlap = len(cluster & thread_article_ids)
            overlap_ratio = overlap / len(cluster)

            if overlap_ratio > THREAD_OVERLAP_RATIO and overlap > best_overlap:
                best_thread_id = thread_id
                best_overlap = overlap

        thread_name, top_entities = _name_thread_from_entities(cluster, articles_by_id)
        primary_entities_json = json.dumps(top_entities)

        if best_thread_id:
            # Extend existing thread
            add_articles_to_thread(best_thread_id, list(cluster))
            update_thread(
                best_thread_id,
                name=thread_name,
                primary_entities=primary_entities_json,
            )
            result["threads_updated"] += 1
            result["articles_linked"] += len(cluster)
            logger.info(f"Updated thread {best_thread_id} ({thread_name}): {len(cluster)} articles")
        else:
            # Create new thread
            thread_id = create_thread(thread_name, primary_entities_json)
            add_articles_to_thread(thread_id, list(cluster))
            result["threads_created"] += 1
            result["articles_linked"] += len(cluster)
            logger.info(f"Created thread {thread_id} ({thread_name}): {len(cluster)} articles")

    logger.info(
        f"Thread detection complete: {result['threads_created']} created, "
        f"{result['threads_updated']} updated, {result['articles_linked']} articles linked"
    )

    return result
