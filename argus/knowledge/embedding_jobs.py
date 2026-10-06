"""Durable vector repair, independent of chat-model worker lanes.

Audit changes and their jobs commit together. Workers embed outside a database
transaction and install the result only if the learning still has that wording.
The bounded repair pass also recovers older NULL vectors and exhausted jobs.
"""
import asyncio
import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast, exists, or_, select, update

from argus.domain.models import EMBEDDING_DIM, Job, Learning
from argus.jobs.queue import enqueue, run_worker_forever
from argus.knowledge.embeddings import EmbeddingError, embed_text, embedding_fingerprint
from argus.providers.settings import settings_for_repository

logger = logging.getLogger("argus.embedding_jobs")
REPAIR_LIMIT = 100
REPAIR_INTERVAL_S = 3600
REPAIR_BACKLOG_DELAY_S = 1
EMBEDDING_TIMEOUT_S = 120


def content_fingerprint(topic, hint_text):
    content = json.dumps([topic, hint_text], ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def enqueue_embedding(session, learning):
    fingerprint = content_fingerprint(learning.topic, learning.hint_text)
    return await enqueue(session, "embed_learning", {
        "learning_id": str(learning.id), "content_fingerprint": fingerprint},
        dedup_key=f"embed_learning:{learning.id}:{fingerprint}")


async def run_embedding_job(sf, settings, payload):
    learning_id = uuid.UUID(payload["learning_id"])
    async with sf() as s:
        learning = await s.get(Learning, learning_id)
        if learning is None:
            return
        settings = await settings_for_repository(s, settings, learning.repo_id)
        row = (await s.execute(select(Learning.topic, Learning.hint_text).where(
            Learning.id == learning_id, Learning.status == "active",
            Learning.embedding.is_(None)))).first()
    if row is None or content_fingerprint(*row) != payload["content_fingerprint"]:
        return  # Archived, superseded, or already repaired.
    topic, hint_text = row
    async with asyncio.timeout(EMBEDDING_TIMEOUT_S):
        vector = await embed_text(f"{topic} :: {hint_text}", settings, is_query=False)
    if (vector is None or len(vector) != EMBEDDING_DIM
            or not all(math.isfinite(v) for v in vector)):
        raise EmbeddingError("embedding repair returned an invalid vector")
    async with sf() as s:
        # A rewrite that commits while the model is answering wins this race.
        await s.execute(update(Learning).where(
            Learning.id == learning_id, Learning.status == "active",
            Learning.embedding.is_(None), Learning.topic == topic,
            Learning.hint_text == hint_text).values(embedding=vector,
                embedding_fingerprint=embedding_fingerprint(settings)))
        await s.commit()


async def recover_missing_embeddings(session, *, now=None):
    now = now or datetime.now(timezone.utc)
    recent_or_alive = exists().where(
        Job.kind == "embed_learning",
        Job.payload["learning_id"].astext == cast(Learning.id, String),
        or_(Job.status.in_(("queued", "running")),
            (Job.status == "failed") & (Job.created_at > now - timedelta(
                seconds=REPAIR_INTERVAL_S))))
    rows = (await session.execute(select(Learning).where(
        Learning.status == "active", Learning.embedding.is_(None),
        ~recent_or_alive).order_by(Learning.id).limit(REPAIR_LIMIT)
        .with_for_update(skip_locked=True))).scalars().all()
    for learning in rows:
        await enqueue_embedding(session, learning)
    return len(rows)


async def run_embedding_worker(sf, base_settings, stop, *, worker_id):
    from argus.settings_store import load_effective_settings

    async def handler(payload):
        async with sf() as s:
            settings = await load_effective_settings(s, base=base_settings)
        await run_embedding_job(sf, settings, payload)

    async def repair():
        while not stop.is_set():
            count = 0
            try:
                async with sf() as s:
                    count = await recover_missing_embeddings(s)
                    await s.commit()
                if count:
                    logger.info("enqueued %d missing learning embeddings", count)
            except Exception:
                logger.exception("embedding repair sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=(
                    REPAIR_BACKLOG_DELAY_S if count >= REPAIR_LIMIT else REPAIR_INTERVAL_S))
            except TimeoutError:
                pass

    task = asyncio.create_task(repair())
    try:
        await run_worker_forever(sf, {"embed_learning": handler}, stop, worker_id=worker_id)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
