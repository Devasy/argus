"""The golden set definition: which MRs to re-review, and what is known true.

Keyed by (project_path, mr_iid) throughout, because mr_iid is not unique --
MR 12 exists in six repositories with unrelated titles.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml

VERDICTS = ("human_right", "bot_right", "bot_right_trivial")

DEFAULT_PATH = Path(__file__).with_name("golden_set.yaml")


@dataclass(frozen=True)
class GoldenMR:
    project_path: str
    mr_iid: int
    title: str
    scored: bool
    note: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.project_path, self.mr_iid)


@dataclass(frozen=True)
class Item:
    id: str
    project_path: str
    mr_iid: int
    file: str
    line: int
    verdict: str
    claim: str
    why: str

    @property
    def key(self) -> tuple[str, int]:
        return (self.project_path, self.mr_iid)


@dataclass(frozen=True)
class GoldenSet:
    version: int
    merge_requests: list[GoldenMR]
    items: list[Item]

    def items_for(self, project_path: str, mr_iid: int) -> list[Item]:
        return [i for i in self.items
                if i.project_path == project_path and i.mr_iid == mr_iid]


def load(path: Path | None = None) -> GoldenSet:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text())
    mrs = [GoldenMR(project_path=m["project_path"], mr_iid=int(m["mr_iid"]),
                    title=m.get("title", ""), scored=bool(m.get("scored")),
                    note=m.get("note"))
           for m in raw.get("merge_requests") or []]
    items = [Item(id=i["id"], project_path=i["project_path"],
                  mr_iid=int(i["mr_iid"]), file=i["file"], line=int(i["line"]),
                  verdict=i["verdict"], claim=i["claim"], why=i.get("why", ""))
             for i in raw.get("items") or []]
    return GoldenSet(version=int(raw["version"]), merge_requests=mrs,
                     items=items)


def select(gs: GoldenSet, scored_only: bool) -> list[GoldenMR]:
    """The behavioural-only MRs cost roughly ten times the scored ones in
    serial worker time and cannot be judged for correctness, so narrowing to
    the scored set is usually what you want for a baseline."""
    return [m for m in gs.merge_requests if m.scored or not scored_only]
