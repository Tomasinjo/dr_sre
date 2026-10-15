"""
Returns a json for agent to analyze.
1. Summary of hits for each query per key_field
2. all logs for each of above if less than 100 samples
3. if more than 100 samples, a sample of log is returned.

"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass

import requests
import yaml
from pydantic import TypeAdapter, ValidationError

from src.victorialogs.models import (
    BranchSpec,
    ContextSpec,
    Report,
    Group,
    FullDetail,
    SignatureDetail,
    ErrorDetail,
    LogLine,
    TopMessage,
    ContextResult
)


VICTORIALOGS_URL = (os.getenv("VICTORIALOGS_URL", "")).rstrip("/")
BASE = f"{VICTORIALOGS_URL}/select/logsql/query"
TIMEOUT = int(os.getenv("VICTORIALOGS_TIMEOUT", "120"))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(_REPO_ROOT, "data")
ERROR_QUERY_FILE = os.path.join(DATA_DIR, "query-error.yaml")
CONTEXT_QUERY_FILE = os.path.join(DATA_DIR, "query-context.yaml")

_ERROR_QUERIES = TypeAdapter(dict[str, BranchSpec])
_CONTEXT_QUERIES = TypeAdapter(dict[str, ContextSpec])


def _read_yaml(path: str) -> object:
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise RuntimeError(f"cannot read query file {path}: {e}")


def _validate(adapter: TypeAdapter, raw: object, path: str):
    try:
        return adapter.validate_python(raw)
    except ValidationError as e:
        raise RuntimeError(f"query file {path}: {e}")


def load_config(path: str = ERROR_QUERY_FILE) -> dict[str, BranchSpec]:
    """Read and validate query-error.yaml"""
    return _validate(_ERROR_QUERIES, _read_yaml(path), path)


def load_context_config(path: str = CONTEXT_QUERY_FILE) -> dict[str, ContextSpec]:
    """Read and validate query-context.yaml
    optional, skip if file doesnt exist 
    """
    if not os.path.isfile(path):
        return {}
    return _validate(_CONTEXT_QUERIES, _read_yaml(path), path)


def save_config(config: dict[str, BranchSpec], path: str) -> None:
    """Writes a modified query to file"""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump({name: spec.model_dump(exclude_defaults=True)
                        for name, spec in config.items()},
                       f, sort_keys=False, width=100000)
    os.replace(tmp, path)


def add_exclusion(branch: str, exclusion: str, path: str = ERROR_QUERY_FILE) -> BranchSpec:
    """ appends exclusion to query-error.yaml """
    config = load_config(path)
    spec = config.get(branch)
    if spec is None:
        raise RuntimeError(f"unknown branch {branch!r} in {path}; "
                           f"known: {', '.join(sorted(config))}")
    if exclusion not in spec.exclusions:
        spec.exclusions.append(exclusion)
        save_config(config, path)
    return spec


@dataclass(frozen=True)
class Window:
    """ query start and end range"""
    start: str
    end: str


def parse_window(spec: str) -> Window:
    """Converts last x hours representation to logql compliant datetime ranges"""
    m = re.fullmatch(r"(\d+)h", spec)
    if not m:
        raise ValueError(f"window must be Nh, got {spec!r}")
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(hours=int(m.group(1)))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return Window(start=start.strftime(fmt), end=end.strftime(fmt))


def vql(query: str, w: Window, limit: int | None = None) -> list[dict]:
    """Run logql query, converts json log line to dict"""
    params = {"query": query, "start": w.start, "end": w.end}
    if limit:
        params["limit"] = str(limit)
    try:
        r = requests.get(BASE, params=params, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        detail = (e.response.text if e.response is not None else str(e))[:300]
        raise RuntimeError(f"VL {code} [{query[:70]}...]: {detail}")
    except requests.RequestException as e:
        raise RuntimeError(f"VL unreachable: {e}")
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def collect_groups(branches: dict[str, BranchSpec], w: Window) -> list[Group]:
    """per-group (branch, host, key_field) counts, biggest group first."""
    groups = []
    for name, spec in branches.items():
        for r in vql(spec.stats_query(), w):
            raw_key = r.get(spec.key_field)
            groups.append(Group(branch=name, host=r.get("host") or "",
                                key=raw_key or f"(no {spec.key_field})",
                                key_missing=not raw_key,
                                count=int(r.get("rows", 0)), spec=spec))
    groups.sort(key=lambda g: g.count, reverse=True)
    return groups


def full_detail(g: Group, w: Window, cap: int) -> FullDetail:
    """pull every line of a small group, capped at `cap`."""
    rows = vql(g.scoped_query(), w, limit=cap + 1)
    return FullDetail(count=g.count, truncated=len(rows) > cap,
                      logs=[LogLine(t=r.get("_time", ""), msg=str(r.get("_msg", ""))[:500])
                            for r in rows[:cap]])


def signature_detail(g: Group, w: Window, top: int, sample_limit: int) -> SignatureDetail:
    """summarize a big group instead of pulling everything."""
    out = SignatureDetail(count=g.count)
    scoped = g.scoped_query()
    try:
        rows = vql(f"{scoped} | top({top}, _msg)", w)
        out.top_msgs = [TopMessage(hits=t.get("hits", t.get("rows", "?")),
                                   msg=str(t.get("_msg", ""))[:300]) for t in rows]
    except RuntimeError as e:
        out.top_msgs_error = str(e)[:200]
    try:
        q = g.spec.filters(g.scope()) + " | stats by (_time:1h) count(*) rows"
        rows = vql(q, w)
        # VL returns each 1h bucket under plain "_time" (bucket start hour)
        out.hourly = {r.get("_time", "?"): int(r.get("rows", 0)) for r in rows}
    except RuntimeError as e:
        out.hourly_error = str(e)[:200]
    try:  # newest-first raw samples
        rows = vql(scoped, w, limit=sample_limit)
        out.samples = [LogLine(t=r.get("_time", ""), msg=str(r.get("_msg", ""))[:300])
                       for r in rows]
        out.samples_note = "newest-first; for oldest samples rerun with --oldest"
    except RuntimeError as e:
        out.samples_error = str(e)[:200]
    return out


def fetch_context(specs: dict[str, ContextSpec], w: Window) -> dict[str, ContextResult]:
    """Run every context query — correlation only, never an alert. Each entry
    degrades to an `error` note instead of failing the whole report."""
    out: dict[str, ContextResult] = {}
    for name, spec in specs.items():
        result = ContextResult(instructions=spec.instructions)
        try:
            rows = vql(spec.query, w, limit=spec.limit)
        except RuntimeError as e:
            result.error = str(e)[:200]
        else:
            result.rows = [{k: (v[:300] if isinstance(v, str) else v)
                            for k, v in r.items()
                            if not spec.fields or k in spec.fields} for r in rows]
        out[name] = result
    return out


def run(time: str = "12h", *, query_file: str = ERROR_QUERY_FILE,
        context_file: str = CONTEXT_QUERY_FILE, full_max: int = 100,
        sample_limit: int = 100, top_signatures: int = 15) -> Report:
    window = parse_window(time)
    branches = load_config(query_file)
    groups = collect_groups(branches, window)

    detail: dict[str, FullDetail | SignatureDetail | ErrorDetail] = {}
    for g in groups:
        try:
            if g.count <= full_max:
                detail[g.gid] = full_detail(g, window, full_max)
            else:
                detail[g.gid] = signature_detail(g, window, top=top_signatures,
                                                 sample_limit=sample_limit)
        except RuntimeError as e:
            detail[g.gid] = ErrorDetail(error=str(e)[:300], count=g.count)

    return Report(window=time, start=window.start, end=window.end,
                  groups=groups,
                  excluded_from_full=[g.gid for g in groups if g.count > full_max],
                  context=fetch_context(load_context_config(context_file), window),
                  detail=detail)


def die(msg: object) -> None:
    json.dump({"error": str(msg)}, sys.stdout)
    sys.exit(3)


def parse_args():
    ap = argparse.ArgumentParser(description="staged log sweep for VictoriaLogs")
    ap.add_argument("--time", default="12h")
    ap.add_argument("--query-file", default=ERROR_QUERY_FILE)
    ap.add_argument("--context-file", default=CONTEXT_QUERY_FILE)
    ap.add_argument("--full-max", type=int, default=100)
    ap.add_argument("--sample-limit", type=int, default=100)
    ap.add_argument("--top-signatures", type=int, default=15)
    ap.add_argument("--add-exclusion", nargs=2, metavar=("BRANCH", "EXCLUSION"),
                    help="append EXCLUSION to BRANCH in the query file, save, and exit")
    return ap.parse_args()


def main():
    args = parse_args()

    if args.add_exclusion:
        branch, exclusion = args.add_exclusion
        try:
            spec = add_exclusion(branch, exclusion, args.query_file)
        except RuntimeError as e:
            die(e)
        print(f"ok: [{branch}] now has {len(spec.exclusions)} exclusions")
        return

    try:
        report = run(time=args.time, query_file=args.query_file,
                     context_file=args.context_file,
                     full_max=args.full_max, sample_limit=args.sample_limit,
                     top_signatures=args.top_signatures)
    except Exception as e:
        die(e)
    print(report.model_dump_json(indent=1, exclude_none=True))


if __name__ == "__main__":
    main()
