import json
import os
from typing import Literal

from langchain_core.tools import tool

from src.victorialogs import logs
from src.agent import memory

Branch = Literal[tuple(logs.load_config(logs.ERROR_QUERY_FILE))]


@tool
def run_log_extraction(time: str = "12h") -> str:
    """
    Retrieves error logs from victorialogs. It also includes results 
    of context queries in query-context.yaml which can help with
    finding a root cause. 

    It also includes diff from memory.json which help with tracking 
    which errors are new and which are resolved.
    """
    try:
        report = logs.run(time=time)
    except Exception as e:
        return f"Error executing log extraction: {e}"
    try:
        brief = memory.MemoryStore().update(report)
        memory_block = json.loads(brief.model_dump_json(exclude_none=True))
    except Exception as e:
        memory_block = {"error": f"memory unavailable this run: {e}"}
    return json.dumps({
        "report": json.loads(report.model_dump_json(exclude_none=True)),
        "memory": memory_block,
    })


@tool
def silence_error_in_query(branch: Branch, exclusion: str) -> str:
    """Appends an exclusion to a branch in query-error.yaml so it doesnt appear in future execution.

    Has to be compliant with VictoriaLogs LogsQL
    
    Example exclusion for containers: and -(_msg:"HTTP error 403" and container_name:"xxxx-app")
    Example exclusion for journald: and -(_msg:"cannot get freq")
    """
    try:
        spec = logs.add_exclusion(branch, exclusion)
        return f"ok: [{branch}] now has {len(spec.exclusions)} exclusions"
    except Exception as e:
        return f"Error updating query file: {e}"
