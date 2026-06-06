import json
import logging
import uuid
from dataclasses import dataclass

logger = logging.getLogger("argus.jobs")


@dataclass(frozen=True)
class WorkerSpec:
    """One worker loop; endpoint (an llm_endpoints.name) of None keeps each job's own default config."""
    id: str
    kinds: tuple[str, ...]
    endpoint: str | None = None


def review_handler(sf, run_review, endpoint: str | None):
    """Wrap run_review so a worker with an endpoint re-routes every review the caller didn't pin."""
    async def _review(payload: dict):
        if endpoint and not payload.get("pinned"):
            await route_review_to_endpoint(sf, uuid.UUID(payload["review_id"]), endpoint)
        return await run_review(payload)
    return _review


async def route_review_to_endpoint(sf, review_id, endpoint_name: str) -> bool:
    """Swap the review's enqueue-time (is_default) llm_config for this worker's endpoint; False if absent."""
    from argus.domain.models import Review
    from argus.llm.config import resolve_llm_config_by_name
    async with sf() as session:
        cfg = await resolve_llm_config_by_name(session, endpoint_name)
        if cfg is None:
            logger.warning("worker endpoint %r not found in llm_endpoints; review %s keeps "
                           "its enqueue-time config", endpoint_name, review_id)
            return False
        review = await session.get(Review, review_id)
        if review is None:
            return False
        review.llm_config = cfg.model_dump()
        await session.commit()
    return True


async def distill_llm_config(session, endpoint_name: str | None):
    """The LLM a distill job runs on: the worker's endpoint if set and present, else is_default."""
    from argus.llm.config import resolve_llm_config, resolve_llm_config_by_name
    if endpoint_name:
        cfg = await resolve_llm_config_by_name(session, endpoint_name)
        if cfg is not None:
            return cfg
        logger.warning("worker endpoint %r not found in llm_endpoints; distill uses the "
                       "default endpoint", endpoint_name)
    return await resolve_llm_config(session, None, None)


def default_specs(known_kinds: list[str]) -> list[WorkerSpec]:
    return [WorkerSpec(id="w1", kinds=tuple(known_kinds))]


def parse_worker_specs(raw: str, known_kinds: list[str]) -> list[WorkerSpec]:
    """Parse ARGUS_WORKER_SPECS; malformed input logs and falls back to one worker so a typo can't stop reviews."""
    if not raw or not raw.strip():
        return default_specs(known_kinds)
    try:
        specs = _parse(json.loads(raw), known_kinds)
    except (ValueError, TypeError) as e:
        logger.error("invalid ARGUS_WORKER_SPECS (%s); falling back to a single worker", e)
        return default_specs(known_kinds)
    uncovered = set(known_kinds) - {k for s in specs for k in s.kinds}
    if uncovered:
        logger.warning("no worker claims job kind(s) %s; those jobs will stay queued",
                       sorted(uncovered))
    return specs


def _parse(data, known_kinds: list[str]) -> list[WorkerSpec]:
    if not isinstance(data, list) or not data:
        raise ValueError("expected a non-empty JSON list")
    specs, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"worker entry {item!r} is not an object")
        wid = item.get("id")
        if not isinstance(wid, str) or not wid:
            raise ValueError(f"worker entry {item!r} needs a non-empty string id")
        if wid in seen:
            raise ValueError(f"duplicate worker id {wid!r}")
        seen.add(wid)
        kinds = item.get("kinds")
        if not isinstance(kinds, list) or not kinds:
            raise ValueError(f"worker {wid!r} needs a non-empty kinds list")
        unknown = [k for k in kinds if k not in known_kinds]
        if unknown:
            raise ValueError(f"worker {wid!r} has unknown kind(s) {unknown}")
        endpoint = item.get("endpoint")
        if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
            raise ValueError(f"worker {wid!r} endpoint must be a non-empty string")
        specs.append(WorkerSpec(id=wid, kinds=tuple(dict.fromkeys(kinds)), endpoint=endpoint))
    return specs
