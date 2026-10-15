from typing import Literal
from pydantic import BaseModel, Field

class SigMem(BaseModel):
    """recurring message signature inside a group."""
    signature: str
    sample: str = ""            # one raw example, first occurrence
    first_seen: str             # date string
    last_seen: str
    seen_count: int = 1         # cycles in which it appeared
    resolved_on: str | None = None


class GroupMem(BaseModel):
    """One (branch, host, key) bucket across all sweeps so far"""
    branch: str
    host: str
    key: str
    first_seen: str
    last_seen: str
    seen_count: int = 1         # cycles in which the group appeared
    last_count: int = 0         # log volume in the latest cycle
    prev_count: int = 0         # volume one cycle earlier (for deltas)
    signatures: dict[str, SigMem] = Field(default_factory=dict)
    resolved_on: str | None = None


class MemoryFile(BaseModel):
    version: Literal[1] = 1
    updated: str = ""
    last_window: str = ""       # "<start>/<end>" of the last applied update
    groups: dict[str, GroupMem] = Field(default_factory=dict)


class Briefing(BaseModel):
    """main out - diff between report and errors from previous runs"""
    updated: str
    window: str
    groups: dict[str, "GroupBrief"] = Field(default_factory=dict)
    new_groups: list[str] = Field(default_factory=list) # list of gids from Report.groups()
    regressed_groups: list[str] = Field(default_factory=list)   # resolved, now back
    resolved_groups: list[str] = Field(default_factory=list)   # seen before, absent now
    forgotten: list[str] = Field(default_factory=list)       # pruned this cycle


class GroupBrief(BaseModel):
    status: Literal["new", "recurring", "regressed"]
    first_seen: str
    seen_count: int
    count: int                  # this cycle's volume
    prev_count: int
    volume_delta: int
    known_signature_count: int
    new_signatures: list[str] = Field(default_factory=list)
    resolved_signatures: list[str] = Field(default_factory=list)
