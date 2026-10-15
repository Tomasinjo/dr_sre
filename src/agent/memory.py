
"""
implements agent memory.

primary way agent uses it is with update() which returns diff and statuses
between current Report and memory.json

It does it per group which is combination of log source (top lvl key in 
query-error.json), key_field (also query-error.json) and host.

Log signature is created so the logs can be compared. The comparison
is used to track if/when the error log stopped being logged which means
it was resolved. This is only tracked to low volume error logs in 
model FullDetail and not in sampled SignatureDetail since not all logs
are present.

After PRUNE_DAYS, the resolved logs will be deleted.
"""
import argparse
import datetime as dt
import json
import os
import re

from src.victorialogs import logs
from src.agent.models import (
    SigMem,
    GroupMem,
    MemoryFile,
    Briefing,
    GroupBrief
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(_REPO_ROOT, "data")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
MIN_RESOLVE_HOURS = 12   # if error is not observed after so much hours it is deemed resolved
PRUNE_DAYS = 30        # resolved entries are forgotten after this many days
MAX_SIGNATURES = 50      # keep only so many signatures per group

_PLACEHOLDERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{6,}\b"), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def signature_of(msg: str) -> str:
    """
    makes signature from logs by removing ips, numbers, uuids with static tags
    this enables comparing logs from memory with current logs
    """
    s = msg.strip().lower()
    for rx, repl in _PLACEHOLDERS:
        s = rx.sub(repl, s)
    return re.sub(r"\s+", " ", s)[:200]


def _observe(detail: object) -> tuple[dict[str, str], bool]:
    """
    checks whether past error was resolved by comparing signatures of 
    current logs with past ones, in that case boolean will be true.
    However, boolean can only be true for FullDetail because
    for other types it cant be proven it stopped:
    - SignatureDetail contains sampled logs
    - ErrorDetail are just execution exceptions
    """
    sigs: dict[str, str] = {} # key = signature val=raw logline
    if isinstance(detail, logs.FullDetail):
        for line in detail.logs:
            sig = signature_of(line.msg)
            if len(sig) >= 3:
                sigs.setdefault(sig, line.msg[:200])
        return sigs, not detail.truncated
    if isinstance(detail, logs.SignatureDetail):
        for top in detail.top_msgs or []:
            sig = signature_of(top.msg)
            if len(sig) >= 3:
                sigs.setdefault(sig, top.msg[:200])
        return sigs, False
    return {}, False


class MemoryStore:
    """long term memory"""

    def __init__(self, path: str = MEMORY_FILE):
        self.path = path

    def load(self) -> MemoryFile:
        """read memory file"""
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return MemoryFile()
        except (OSError, ValueError) as e:
            raise RuntimeError(f"cannot read memory file {self.path}: {e}")
        try:
            return MemoryFile.model_validate(raw)
        except ValueError as e:
            raise RuntimeError(f"memory file {self.path}: {e}")

    def save(self, file: MemoryFile) -> None:
        """saves memory file"""
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(file.model_dump(exclude_defaults=True), f, indent=1)
        os.replace(tmp, self.path)


    def update(self, report: logs.Report) -> Briefing:
        """
        creates a diff between memory and current error logs.
        updates statuses of log signatures in memory so llm 
        knows what is new and what was resolved.
        """
        file = self.load()
        brief = Briefing(updated=_now(), window=report.window)
        replay = file.last_window == f"{report.start}/{report.end}"
        resolve_ok = (not replay and _window_hours(report.window) >= MIN_RESOLVE_HOURS)
        today = report.end[:10]   # window end date
        seen_gids = {g.gid for g in report.groups}

        for g in report.groups:
            mem = file.groups.get(g.gid)
            if mem is None:
                # first time seen, apply new status
                mem = GroupMem(branch=g.branch, host=g.host, key=g.key,
                               first_seen=today, last_seen=today, seen_count=0)
                file.groups[g.gid] = mem
                status = "new"
                brief.new_groups.append(g.gid)
            elif mem.resolved_on is not None:
                # stuff that was resolved but appeared again, clear resolved fields
                status = "regressed"
                brief.regressed_groups.append(g.gid)
                mem.resolved_on = None
                for sig in mem.signatures.values():
                    sig.resolved_on = None
            else:
                status = "recurring"

            if not replay: # increase counters only if data is from new report
                mem.seen_count += 1
                mem.last_seen = today
            mem.prev_count, mem.last_count = mem.last_count, g.count

            # complete means the old error is not present in current run
            sigs, complete = _observe(report.detail.get(g.gid))
            new_sigs: list[str] = [] # signature of logs that first appeared in current run
            resolved_sigs: list[str] = [] # old error was not observed in current run  
            known = 0
            for sig, sample in sigs.items():
                sm = mem.signatures.get(sig)
                if sm is None:
                    if len(mem.signatures) < MAX_SIGNATURES:
                        mem.signatures[sig] = SigMem(signature=sig, sample=sample,
                                                     first_seen=today, last_seen=today)
                        new_sigs.append(sig)
                else:
                    known += 1
                    sm.resolved_on = None
                    if not replay:
                        sm.seen_count += 1
                        sm.last_seen = today
            if complete and resolve_ok:
                for sig, sm in mem.signatures.items():
                    if sig not in sigs and sm.resolved_on is None:
                        sm.resolved_on = today
                        resolved_sigs.append(sig)

            brief.groups[g.gid] = GroupBrief(
                status=status, first_seen=mem.first_seen, seen_count=mem.seen_count,
                count=g.count, prev_count=mem.prev_count,
                volume_delta=g.count - mem.prev_count,
                known_signature_count=known,
                new_signatures=new_sigs, resolved_signatures=resolved_sigs)

        if resolve_ok:
            for gid, mem in file.groups.items():
                if gid not in seen_gids and mem.resolved_on is None:
                    mem.resolved_on = today
                    for sig in mem.signatures.values():
                        sig.resolved_on = today
                    brief.resolved_groups.append(gid)
            brief.resolved_groups.sort()

        # forget long-resolved entries (today is the current window end date)
        cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=PRUNE_DAYS)).isoformat()
        for gid, mem in list(file.groups.items()):
            if mem.resolved_on is not None and mem.resolved_on < cutoff:
                del file.groups[gid]
                brief.forgotten.append(gid)
        brief.forgotten.sort()

        if not replay:
            file.last_window = f"{report.start}/{report.end}"
            file.updated = brief.updated
            self.save(file)
        return brief


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_hours(window: str) -> int:
    """
    return int from window string e.g. "12h" returns 12
    """
    m = re.fullmatch(r"(\d+)h", window)
    return int(m.group(1)) if m else 0


def main():
    ap = argparse.ArgumentParser(description="long-term memory of recurring error groups")
    ap.add_argument("--update", action="store_true",
                    help="run a live sweep, diff it against memory, print the briefing")
    ap.add_argument("--time", default="12h", help="sweep window for --update (default 12h)")
    ap.add_argument("--dump", action="store_true", help="print the raw store JSON")
    args = ap.parse_args()

    if args.dump:
        print(json.dumps(MemoryStore().load().model_dump(), indent=1))
    elif args.update:
        print(MemoryStore().update(logs.run(time=args.time)).model_dump_json(indent=1))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
