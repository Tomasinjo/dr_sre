# agent.py
import os

from langchain.agents import create_agent
from langchain_ollama import ChatOllama

from src.agent.tools import run_log_extraction, silence_error_in_query

tools = [run_log_extraction, silence_error_in_query]
llm = ChatOllama(
    model=os.getenv("OLLAMA_MODEL"),
    base_url=os.getenv("OLLAMA_BASE_URL"),
)

SYSTEM_PROMPT = """You are site reliability engineering agent.
Your tasks are:
1. Analyze raw error logs, identifying root causes, file paths, line numbers, and impact.
2. If directed to silence an error, use the `silence_error_in_query`.
3. The `run_log_extraction` result carries a `memory` block computed by diffing
   this sweep against every previous one, it is your long-term memory. Let it
   focus the report:
   - "new" groups and `new_signatures` are the headline: analyze those first,
     in depth. A new signature inside a long-known noisy group is still news.
   - "recurring" groups are known issues: one line each with their history
     (first_seen, seen_count) and a call-out only for large `volume_delta`.
   - "regressed" means a group that had resolved is back — flag prominently.
   - `resolved_groups` disappeared since the last sweep: note as good news,
     and mention when a temporary exclusion could now be removed.
Always output concise, actionable summaries for developers.
This report will be sent via telegram so do not use markdown
"""

agent = create_agent(llm, tools, system_prompt=SYSTEM_PROMPT)
