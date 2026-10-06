"""Vertex API-key transport reuses Gemini's REST protocol and tool signatures."""
import re

VERTEX_API_BASE = "https://aiplatform.googleapis.com/v1/publishers/google"


def validate_vertex(config):
    base = (config.api_base or VERTEX_API_BASE).rstrip("/")
    if not re.fullmatch(r"https://aiplatform\.googleapis\.com/v1/(?:projects/[a-z0-9-]+/locations/global/)?publishers/google", base):
        raise ValueError("Vertex endpoint must use Google's global publishers/google API")
    if not config.model.startswith("vertex_ai/gemini-"):
        raise ValueError("Vertex endpoint requires a vertex_ai/gemini-* model")
    if config.free_only or config.fallback is not None:
        raise ValueError("Vertex uses explicit credit-backed billing without fallback")
    return base


def transport_kwargs(config):
    return {"model": "gemini/" + config.model.split("/", 1)[1],
            "api_base": validate_vertex(config), "max_tokens": 16384}
