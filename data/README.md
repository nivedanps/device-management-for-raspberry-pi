# Monitoring data (local only)

Everything the pilot stores lives in this folder. Nothing is synced anywhere.

- `fleet.db` — the database the dashboard reads (one SQLite file; no database server).
- `heartbeats/<device>/<YYYY-MM-DD>.jsonl` — a plain-text copy of every heartbeat, one JSON
  object per line, one file per box per day (UTC date). Open it with any text editor.
- `agent-buffer.db` — heartbeats the agent could not deliver yet (only while the server is down).

Detail older than `retention_days` (30 by default) is deleted automatically, both from
`fleet.db` and as whole day files here. No school data is ever written to this folder.
