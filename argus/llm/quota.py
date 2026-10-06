"""Shared, durable Gemini budgets for the local free-tier pilot."""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from sqlalchemy import text

from argus.db import get_engine, session_factory
from argus.domain.models import RuntimeSetting


class QuotaDeferred(RuntimeError):
    def __init__(self, retry_at, message="Free model quota exhausted; job deferred"):
        super().__init__(message)
        self.retry_at = retry_at


def quota_key(reference, model):
    return "gemini-quota:" + hashlib.sha256(f"{reference}:{model}".encode()).hexdigest()


@asynccontextmanager
async def quota_session(database_url):
    engine = get_engine(database_url)
    try:
        async with session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


async def reserve(database_url, reference, model, tokens, limits):
    rpm, tpm, rpd = limits
    if min(limits) < 1:
        raise ValueError("Gemini free-tier budgets must be positive")
    if tokens > tpm:
        raise ValueError("request exceeds ARGUS_GEMINI_FREE_TPM; reduce chunks/context or adjust the local budget")
    key = quota_key(reference, model)
    while True:
        now = datetime.now(timezone.utc)
        minute = int(now.timestamp()) // 60
        pacific = now.astimezone(ZoneInfo("America/Los_Angeles"))
        day = pacific.date().isoformat()
        wait = 0
        async with quota_session(database_url) as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
            row = await session.get(RuntimeSetting, key)
            state = dict(row.value) if row else {}
            until = datetime.fromisoformat(state["paused_until"]) if state.get("paused_until") else now
            if until > now:
                raise QuotaDeferred(until)
            if state.get("day") != day:
                state.update(day=day, daily=0)
            if state.get("minute") != minute:
                state.update(minute=minute, requests=0, tokens=0)
            if state.get("daily", 0) >= rpd:
                midnight = (pacific + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                raise QuotaDeferred(midnight.astimezone(timezone.utc), "local Gemini daily budget reached; job deferred")
            if state.get("requests", 0) >= rpm or state.get("tokens", 0) + tokens > tpm:
                wait = (minute + 1) * 60 - now.timestamp() + .1
            else:
                state.update(requests=state.get("requests", 0) + 1,
                             tokens=state.get("tokens", 0) + tokens,
                             daily=state.get("daily", 0) + 1)
                if row:
                    row.value = state
                else:
                    session.add(RuntimeSetting(key=key, value=state))
                await session.commit()
        if not wait:
            return
        await asyncio.sleep(min(wait, 60))


async def pause(database_url, reference, model, seconds=60):
    key = quota_key(reference, model)
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))
    async with quota_session(database_url) as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
        row = await session.get(RuntimeSetting, key)
        state = dict(row.value) if row else {}
        existing = datetime.fromisoformat(state["paused_until"]) if state.get("paused_until") else retry_at
        retry_at = max(existing, retry_at)
        state["paused_until"] = retry_at.isoformat()
        if row:
            row.value = state
        else:
            session.add(RuntimeSetting(key=key, value=state))
        await session.commit()
    return QuotaDeferred(retry_at)
