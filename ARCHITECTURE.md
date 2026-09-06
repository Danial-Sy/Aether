# How Aether works

The numbers in this codebase look arbitrary, but this covers the most important
ones before anything is changed.

## System

| File | Job |
|---|---|
| `server.py` | FastAPI app, SSE streaming, the agent loop, context management |
| `agent.py` | Tool schemas and implementations, shell classification, plan state |
| `config.py` | Role tables, effort tiers, system prompts, defaults |
| `models.py` | Model probing, the registry, fit math, hardware |
| `library.py` | Browsing Ollama's library and Hugging Face's GGUF repositories |
| `catalog.json` | Curated model list, descriptions, hardware tiers |
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

The window belongs to the job, not the model: the same model needs more history
agentically than it does in chat. Settings hold one window per role, a model can
override it, and both are clamped to what the model and the card can serve.

The numbers below are the default 27B on a 24GB card, which is the budget the
rest of the design was built around.

| `num_ctx` | KV cache | Total resident | Notes |
|---|---|---|---|
| 32768 | ~2.9GB | ~18.7GB | Fits, too small for agentic work |
| 65536 | ~5.8GB | ~21.1GB | The agentic default |
| 131072 | ~11.7GB | ~27.4GB | Spills to system RAM |

System prompt and tool schemas cost about 6.8k tokens. At 32768 that leaves ~7k of
history, under two tool results, which is where reread loops come from. 65536
leaves ~20.7k. Chat and reasoning answer once, so they stay at 32768. `num_ctx`
only ever grows, because Ollama reloads the model whenever it changes.

All 14 tool schemas cost about 4.1k tokens and their prompt guidance another
1.2k. Nothing is trimmed at the agentic default. Below roughly 16k the optional
tools are dropped in a fixed order, and the prompt is trimmed with them so it
never names a tool that is not there.

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

Any model Ollama can run. Aether does not know about models, it knows about
three jobs, and every installed model carries the roles the user turned on.

| Role | Where it appears | Requires |
|---|---|---|
| `chat` | Chat tab picker | nothing |
| `reasoning` | The chat model with thinking on | a thinking mode |
| `agentic` | Agentic tab picker | tool calling |

One model per job, without swapping mid-run. One model can hold every role,
which is what a single-model install looks like. The defaults are Qwen3.8 27B
for chat and computer use and Qwen3-Coder 30B for agentic work, because the
mixture of experts emits tool calls far faster than a dense model its size.

### Two libraries, one install

Models are browsed from a full screen with two sources behind one scroll.

| | Ollama | Hugging Face |
|---|---|---|
| Size | ~240 curated models | tens of thousands of GGUF repositories |
| Fetched | the library page, parsed and cached for a day | queried live, paged by cursor |
| Paged by | offset, over the local copy | Hugging Face's own opaque cursor |
| A row is | a family, with its parameter sizes | a repository, with its quantizations |

Hugging Face is a *source*, not a backend. Ollama imports GGUF directly from a
tag beginning `hf.co/`, so a Hugging Face model is pulled, probed, assigned and
run by exactly the code that handles an Ollama one. Nothing downstream of the
pull knows where a model came from.

Two rules the Ollama tag grammar does not share. Hugging Face tags keep their
capitals, because Ollama stores the repository's own spelling and folding the
case stops an installed model matching itself. And they never get `:latest`,
because the part after the colon is a quantization: inventing one gets `model
not found` from the pull.

### Saying what is wrong before the download

Ollama's library is curated and Hugging Face's is not, so every row carries its
own warnings, read from the GGUF header Hugging Face returns inline: a chat
template with no tool support means chat but no Agentic, a missing template
means replies may arrive with raw markers in them, a gated repository needs an
account, a short window is called out, and a vision model warns that its
projector is left behind by the import, so it lands text-only. All of it is a
guess that `/api/show` overwrites the moment the model is actually here.

Choosing a version is one dialog for both sources, because an Ollama tag and a
Hugging Face quantization are the same question: 25 rows with real sizes, the
native window, and which of them this machine can hold. The largest one that
fits leads the list with a star, since quantization trades accuracy for room
and a list of twenty-five is a question nobody wants to be asked.

A card names the versions it already has rather than saying only that one is
installed, and stays installable, because a model you have is one you might
want another size of.

### The same model under two names

Nothing links an Ollama tag to a Hugging Face repository, so the same weights
can be downloaded twice from two places. Architecture and parameter count are
read from the same GGUF header by both libraries, so they identify the model
rather than the copy, and a row carrying an installed model's signature says
so. Where the architecture is not known yet — an Ollama row is a family, not a
file — the model's name stands in, bounded so that it has to be the whole name
and not the start of another: `qwen3.8` is the model in `Qwen3.8-27B-GGUF`, but
`qwen3` is neither that nor `qwen3-coder`, both of which merely begin the same
way and one of which is also a 30B mixture of experts.

The claim is deliberately weaker than "this is installed". An abliterated or
uncensored fine-tune matches its base model on architecture and size while
being a different file, so the warning states what is known and leaves the
judgement to the reader.

### Capabilities come from Ollama

`/api/show` reports what a model can do, so there is no compatibility table to
maintain:

```
qwen3.8:27b      capabilities: ['completion', 'vision', 'tools', 'thinking']
qwen3-coder:30b  capabilities: ['completion', 'tools']
```

That is load-bearing. `think: true` on a model without a thinking mode is a hard
400 from Ollama, so the capability gates the request, and the client drops the
key and retries if a stale profile gets it wrong. Tool calling gates the agentic
role: a model without it cannot be assigned there, and the toggle is disabled
with the reason on it. Vision gates computer use.

`model_info` supplies the rest: the native context length, and the block and
head counts that size the KV cache.

### What fits

```
KV bytes per token = block_count * head_count_kv * (key_length + value_length)
```

At `q8_0` that is a byte per element. The formula is right for ordinary
attention and wrong for hybrids, so the catalog carries a measured figure for
models with real numbers behind them and the formula covers the rest. Profiles
say which they used. Qwen3.8 27B reads 8.1 GiB at 64k computed against 5.8 GB
measured, and using the computed figure would refuse a 64k window on the card
Aether was built for.

### Effort levels

Which tiers think is a property of the tier. Whether they can is a property of
the model. The static hints describe the budget only, and each model appends its
own half: "Thinking on.", "Thinking off.", or "This model has no thinking mode."
The default agentic model reports no thinking capability, so without this the
slider promised a reasoning pass that never ran.

### Tool calls in the reply body

Weaker models write the call into the message instead of the `tool_calls` field.
Measured on llama3.2:3b with the real schemas, 2 of 16 replies did, both on the
deeply nested ones. A call is lifted out only when it names a registered tool
and its arguments are an object, and bare JSON is trusted only when it reaches
the end of the reply, so a call quoted mid-sentence is never executed.

When the JSON does not parse at all, which is the more common failure, nothing
is recoverable and guessing the arguments would run something the model never
asked for. The loop reports that a call was attempted and retries with a
correction, on a budget of its own so a model alternating good and mangled calls
cannot retry without bound.

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
