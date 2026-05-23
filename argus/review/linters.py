import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("argus.linters")
MAX_ITEMS = 40


async def run_ruff(workspace: Path, paths: list[str]) -> list[dict]:
    if not paths or shutil.which("ruff") is None:
        return []

    def _sync() -> list[dict]:
        try:
            r = subprocess.run(["ruff", "check", "--output-format", "json", *paths],
                               cwd=workspace, capture_output=True, text=True,
                               timeout=120)
            raw = json.loads(r.stdout or "[]")
            return [{"path": i.get("filename", ""),
                     "line": (i.get("location") or {}).get("row", 0),
                     "code": i.get("code", ""),
                     "message": i.get("message", "")} for i in raw]
        except Exception as e:
            logger.warning("ruff run failed: %s", e)
            return []

    return await asyncio.to_thread(_sync)


def linter_block(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["## Already flagged by tooling — do NOT repeat these"]
    for i in items[:MAX_ITEMS]:
        lines.append(f"- {i['path']}:{i['line']} {i['code']} {i['message']}")
    return "\n".join(lines)
