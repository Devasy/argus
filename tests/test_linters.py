import shutil

import pytest

from argus.review.linters import linter_block, run_ruff


def test_linter_block_formats_and_caps():
    items = [{"path": f"m{i}.py", "line": i, "code": "F401",
              "message": "unused import"} for i in range(50)]
    block = linter_block(items)
    assert "do NOT repeat" in block and block.count("F401") == 40
    assert linter_block([]) == ""


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
async def test_run_ruff_real(tmp_path):
    (tmp_path / "bad.py").write_text("import os\n")
    items = await run_ruff(tmp_path, ["bad.py"])
    assert any(i["code"] == "F401" for i in items)


async def test_run_ruff_no_paths(tmp_path):
    assert await run_ruff(tmp_path, []) == []
