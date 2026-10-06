"""Compatibility imports; provider-neutral normalization lives in ingestion."""
from argus.ingest.normalizer import (TOKEN_BOT_USERNAME, _ts, _upsert_participant,
    classify_author, classify_system_note, sync_merge_request, upsert_actor)
