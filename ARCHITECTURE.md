# How Aether works

The numbers in this codebase look arbitrary, but this covers the most important
ones before anything is changed.

## System

| File | Job |
|---|---|
| `server.py` | FastAPI app, SSE streaming, the agent loop, context management |
| `agent.py` | Tool schemas and implementations, shell classification, plan state |
| `config.py` | Model metadata, context sizes, effort tiers, system prompts |
| `ollama_client.py` | Ollama protocol, model loading, token estimation |
| `storage.py` | Chats, projects, memory, settings, canon |
| `static/app.js` | The whole UI |
| `desktop.py` | pywebview shell that hosts the server |

Everything runs locally. The server talks to Ollama on 127.0.0.1 and nothing
leaves your computer unless you turn on web search.

## The agent loop

Ollama's native tool-calling contract, same as Codex and Claude Code: a tool call
means execute it and come back, a message without one ends the turn. There is no
step limit.

Three checks catch a run that has stopped making progress and all of them let it
finish rather than ending the run.

- **Repeated call.** The model is told what the tool rejected explicitly so it
  knows what to stop repeating.
- **Narration.** A reply ending on a promise ("Let me start by creating
  index.html") is a failed action. Length cannot tell a plan from a deliverable,
  so the check reads the last 300 characters for one.
- **Early debrief.** A final answer with plan items still open gets nudged twice,
  then accepted. Refusing it outright deadlocks a model that has finished.

`todo_write` holds the plan. One item completes per call and it has to be the
active one, or a run can mark work done that never happened. If a `merge:false`
rewrite comes back having forgotten every completion, they get restored: Qwen
re-emits the whole plan mid-run with everything reset to `pending`. A rewrite that
keeps any completion is left alone.

## Context

A 27B model on a 24GB card has a hard budget so most of the design is built
around it.

| `num_ctx` | KV cache | Total resident | Notes |
|---|---|---|---|
| 32768 | ~2.9GB | ~18.7GB | Fits, too small for agentic work |
| 65536 | ~5.8GB | ~21.1GB | The agentic default |
| 131072 | ~11.7GB | ~27.4GB | Spills to system RAM |

System prompt and tool schemas cost about 6.8k tokens. At 32768 that leaves ~7k of
history, under two tool results, which is where reread loops come from. 65536
leaves ~20.7k. Chat and reasoning answer once, so they stay at 32768. `num_ctx`
only ever grows, because Ollama reloads the model whenever it changes.

### Shrinking the prompt

Messages are never deleted on disk. Aether inserts a marker message and changes
what renders after it. Two levels:

- Drop old tool calls and their results, keep the narrative. Fires around 50k and
  lands around 27k.
- Summarize everything before the marker into Objective, Done, Files, State,
  Next. The output is structured so that the model knows exactly what came before
  it and where it left off.

The latest user message is repeated verbatim after a summary, since a summary
paraphrases and the model checks its work against the literal request. Tool
results too big for the prompt go to disk and appear as head, tail, and path.

Because the prompt only grows between markers, Ollama reuses its KV cache and a
step costs only its new tokens. That is why per-step instructions ride on the
newest tool result instead of a system message: Qwen's template moves system
messages to the front, so a trailing instruction lands in the prefix and voids the
cache. Measured at 33.8s for seven tokens against 3.5s for 3,101 appended
normally.

If you profile this, `prompt_eval_count` reports the full prompt length whether or
not the cache was reused. Only `prompt_eval_s` tells you anything.

### Token estimates

Code and tool JSON tokenize about 2.5x denser than prose, so a flat 4 characters
per token underestimates. Underestimating is a significant issue, as it causes
compaction to fire late, prompt overflow, and Ollama cutting it from the front,
dropping the system prompt and the plan. The estimator adjusts for density, learns
a correction per chat, and is set to only ever guess high.

## Models

One model per job without swapping mid-run.

| Key | Model | Role |
|---|---|---|
| `general` / `reasoning` | `qwen3.8:27b` | Chat, different effort defaults |
| `agent` | `qwen3.8:27b` | Agentic, has vision, required for computer use |
| `coder` | `qwen3-coder:30b` | Agentic, faster, no vision |

## Delegation

`delegate` runs a sub-task on a fresh transcript, so its reading costs the parent
only the summary it hands back. Sub-agents cannot write files, so a delegation can
always be re-run.

Only offered on large projects. The schema costs 324 tokens on every request, and
on a small project there is no context pressure for a fresh window to relieve.

## File and shell safety

Reads and writes are limited to registered projects, Aether's folder, Documents,
Desktop, and uploads.

`edit_file` matches surrounding text instead of line numbers. Ambiguous anchors
are refused, and a failed one gets the closest real lines back so it can be fixed
in one attempt.

`write_file` will not create over an existing file without `overwrite`, and will
not replace a substantial file with much less content, which is almost always a
regeneration dropping whatever the model did not retype. Every write snapshots to
`data/backups/` first.

Writes are syntax-checked before they commit, including inline `<script>` in HTML
with line numbers mapped back to the host file. The check fails closed.

Shell commands run against a denylist, not an allowlist. An allowlist blocks
ordinary work like `cp`, `python` and `docker` far more often than it blocks
anything dangerous. `safe_auto` stops four things: destroying data, escalating
privilege, changing the machine, and running downloaded code. Commands are lexed
with `shlex` and matched on program name, including through `find -exec` and
`xargs`.

## When the agent needs you

Approvals and clarifying questions share one bar above the composer. Several stack
and page through, and a multi-part question gets tabs.

The server holds the open question keyed by chat id, and the UI polls
`/api/chats/{id}/pending` while a run streams. That polling is the recovery path:
without it, a question whose event never reached the browser leaves the run stalled
until it times out with nothing on screen. Sub-agent questions are filed under the
parent chat's id.
