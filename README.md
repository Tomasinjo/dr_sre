# Dr. Sre
Agent that periodically fetch error logs from VictoriaLogs and reports back basic analysis via Telegram.
User can require error query tuning for benign errors.

### Requirements
- Have a working victorialogs instance with logs in it. Examples are for podman container app logs and journald logs shipped with vector. 
- Ollama with at least one model, I use Qwen 3.8 27b. Make sure to extend context from default to for example 256k to support potentially large amount of logs.
- Telgram bot


### Query files
Find examples in `data/`. 


`query-error.yaml`: base error queries. The agent will add exclusions here when instructed.

```yml
docker:  # anything, identifier.
  base: (_msg:i("failed") or _msg:i("fail") or _msg:i("error") or _msg:i("err") or _msg:i("cannot")) and source_type:"docker_logs" # base error query
  key_field: container_name  # field that represents single service/daemon. Used for constructing summarization query.
  fields:  # the fields to be returned by query, depends on how you parse the data in log shipper / victorialogs
  - _time
  - _msg
  - container_name
  exclusions: []  # Agent tool will add victorialogs logsql compliant exclusions here. Items get merged with base query. I recommend initial manual tuning of "benign" error logs to avoid spamming the context.
```



`query-context.yaml`: optional. Queries that serve as context for agent analysis. My example has query for recent bash command history. This helps agent to determine whether the observed error is related to user activity.



### Test / prepare
Rename `.env.example` to `.env` and add your settings.
Rename `query-error.yaml.example` to `query-error.yaml`, add your queries and initial exclusions
Optionaly rename `query-context.yaml.example` to `query-context.yaml`, add your context queries and instructions.

Test `run_log_extraction`, instructions in tools section.

`uv run --env-file .env main.py`

Go to configured Telegram chat and enter `/report` to see what a scheduled report will look like.

### Run
Place `docker-compose.yml` to desired directory and run `docker-compose up -d`

The report is sent according to setting in .env. You can reply via Telegram, typically to obtain more information or to instruct it to silence certain error. In that case, the agent will call tool `silence_error_in_query`.

### Tools
`run_log_extraction`: main tool that runs `src/victorialogs/logs.py`. Agent is instructed to always execute it. Program `logs.py` will compose queries from `query-error.yaml` and execute it along with context queries in `query-context.yaml`. It returns:
1. error log count by key_field (e.g. container name or systemd service)
2. all error logs for each of the above, if less than 100 lines
3. sampled error logs for each of group from 1. if more than 100 lines to avoid spamming the context.

Test it:
`uv run --env-file .env python -m src.victorialogs.logs --time 12h`

`silence_error_in_query`: Called by agent when instructed to silence/tune specific error log.

### Memory
Short term: Full conversation context is limited to 30k tokens.
Long term: The memory is managed in code rather by agent for more deterministic results. It keeps track of past error logs and their statuses. This helps agent to track which errors are new and which were resolved.


Example of memory.json. You dont't need this file, it will be created after first run.

Top level key is combination of query name (top lvl key in `query-error.yaml`), host and service identifier (key `key_field`).  

`signatures` include log history and its state. Logs are converted to signature (timestamps, numbers, ips, uuids replaced with `<n>`) so they can be compared to current run. The program will use the memory to make a diff with current errors and tag them with status (new, recurring, regressed) and comparison of volume.

```json
  "docker:zenki:ollama": {
   "branch": "docker",
   "host": "zenki",
   "key": "ollama",
   "first_seen": "2026-10-03",
   "last_seen": "2026-10-04",
   "seen_count": 4,
   "last_count": 9,
   "prev_count": 10,
   "signatures": {
    "common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to <n>, abort": {
     "signature": "common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to <n>, abort",
     "sample": "common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 999, abort",
     "first_seen": "2026-10-03",
     "last_seen": "2026-10-04",
     "seen_count": 4
    },
    "common_params_fit_impl: cannot meet free memory target of <n> mib, need to reduce device memory by <n> mib": {
     "signature": "common_params_fit_impl: cannot meet free memory target of <n> mib, need to reduce device memory by <n> mib",
     "sample": "common_params_fit_impl: cannot meet free memory target of 2160 MiB, need to reduce device memory by 2456 MiB",
     "first_seen": "2026-10-03",
     "last_seen": "2026-10-04",
     "seen_count": 4
    },
    "time=<n>-<n>-<n>t<n>:<n>:<n>z level=warn source=llama_server.go:<n> msg=\"llama-server startup failed with projector gpu offload; retrying with projector cpu offload\" model=/root/.ollama/models/blobs/s": {
     "signature": "time=<n>-<n>-<n>t<n>:<n>:<n>z level=warn source=llama_server.go:<n> msg=\"llama-server startup failed with projector gpu offload; retrying with projector cpu offload\" model=/root/.ollama/models/blobs/s",
     "sample": "time=2026-10-02T22:27:38.505Z level=WARN source=llama_server.go:1098 msg=\"llama-server startup failed with projector GPU offload; retrying with projector CPU offload\" model=/root/.ollama/models/blobs/",
     "first_seen": "2026-10-03",
     "last_seen": "2026-10-04",
     "seen_count": 4
    },
   }
  }
```
