# Modularity canon

Aether hardcodes three model tags in eleven places. This is the plan to make
any Ollama model work, without turning the codebase into a configuration
engine. It is the contract for the whole change: if an implementation step
contradicts this file, this file is wrong and gets updated first.

## The goal

1. The current pair stays the default. Nothing regresses for a 24 GB card.
2. Any model in Ollama's library can be installed and used, given capability and
   space.
3. One model is a valid setup. So is six.
4. The installer offers Default, Recommended, or type your own.
5. Models can be searched, described, installed and removed from inside Aether.

## The central idea: roles, not models

Aether does not need to know about models. Every installed model carries a set
of roles the user turns on, and each tab lists the models holding its role.

| Role | Where it appears | Requires |
|---|---|---|
| `chat` | Chat tab picker | nothing |
| `agentic` | Agentic tab picker | `tools` |

Two things follow from capability rather than from choice. Deep reasoning is
the chat role with thinking forced on, offered only by models reporting
`thinking`. Computer use needs eyes, so it routes to an agentic model reporting
`vision` and is unavailable when none is assigned.

Two utility roles the user never sees: `title` (smallest installed model) and
`compact` (follows the active chat model).

Today the four keys `general`, `reasoning`, `agent` and `coder` *are* the
models. After this change the model id is the key, and those four names become
roles resolved against whatever is assigned. The agent loop, compaction,
context math and tool contract do not move. What changes is that everything
currently keyed by one of the four names gets keyed by role instead:
`SYSTEM_PROMPTS`, `AGENTIC_KEYS`, `WARM_ORDER`, and the four named context
settings.

**Roles collapse.** One model holding both roles is the ordinary single-model
setup, not a special case.

**Roles can be empty.** No model with `tools` means no Agentic tab. No agentic
model with `vision` means no computer use. Both get hidden with the reason on
them, rather than failing at runtime with an Ollama 400.

## Assigning a model

Install does not decide for you. Every model carries two toggles, Chat and
Agentic. Solid means the model serves that tab, grey means it does not, and
either, both or neither can be on. The Chat tab's picker lists every model with
Chat solid; the Agentic tab's picker lists every model with Agentic solid. That
is what "choose either" means: assignment decides what is offered, the picker
decides what runs.

Agentic is dead rather than merely unselected when the model does not report
`tools`. A model that cannot call tools cannot run the loop, so the control is
disabled with the reason on it instead of clickable and then broken. This is
checked against `/api/show`, so it is Ollama's answer and not a list we keep.

Releasing the last Chat assignment is refused. Every model can chat, and Aether
with no chat model has nothing to talk to. Releasing the last Agentic
assignment is allowed, and hides the tab.

## Capability detection

`/api/show` already answers every question we would otherwise guess at. Verified
live against both installed models:

```
qwen3.8:27b      capabilities: ['completion', 'vision', 'tools', 'thinking']
qwen3-coder:30b  capabilities: ['completion', 'tools']
```

That is the compatibility list, maintained by Ollama, for free. No hardcoded
table of which models can see or think. `model_info` carries the rest:

| Field | Used for |
|---|---|
| `<arch>.context_length` | `context_max`, replacing the hardcoded 262144 |
| `<arch>.block_count` | KV cache size |
| `<arch>.attention.head_count_kv` | KV cache size |
| `<arch>.attention.key_length` / `value_length` | KV cache size |
| `general.parameter_count`, `general.size_label` | display, tier matching |
| `general.sampling.temp` / `top_k` / `top_p` | sampling defaults |
| `details.quantization_level` | display, fit math |

The probe is cached per model id in `data/settings/models.json` and refreshed
when the tag's `modified_at` changes. A cold probe of one model is one HTTP call
to localhost, so this never blocks a chat.

### Fit math

```
KV bytes per token = block_count * head_count_kv * (key_length + value_length)
```

At `q8_0` that is one byte per element, which is what Aether already sets. Where
a GGUF omits `key_length` and `value_length`, which is the common case, head_dim
comes from `embedding_length / head_count`.

The formula is right for ordinary attention and wrong for hybrids. The 27B is a
Mamba hybrid where only one layer in four is full attention, so the generic
number reads 8.1 GiB at 64k against 5.8 GB measured. Left uncorrected it refuses
a 64k window on the card Aether was built for, which breaks goal 1.

So the catalog carries a measured `kv_bytes_per_token` for the models we have
real numbers on, and the formula covers everything else. Profiles report which
of the two they used so the UI can say so. Unknown models therefore size
conservatively, which recommends a smaller window than needed rather than one
that will not load.

Weights come from `/api/tags` `size` for installed models, and from the registry
manifest for models that are not:

```
GET https://registry.ollama.ai/v2/library/<name>/manifests/<tag>
```

That endpoint is unauthenticated, returns exact layer sizes, and is the same one
`ollama pull` uses. It is how the wizard knows a download is 2.0 GB before
starting it, and how "type your own" validates that a tag exists at all.

## What is actually Qwen-specific today

Most of the loop is already model-agnostic. The real coupling, in the order it
will break someone:

1. **`think` is sent unconditionally.** A model without `thinking` gets a 400.
   Confirmed live: `{"error": "\"qwen2.5:0.5b\" does not support thinking"}`.
   `think: false` and omitting the key are both accepted by every model, so the
   gate only has to stop `true`. Two layers: the server reads the capability
   off the profile, and the client drops `think` and retries when Ollama
   rejects it anyway, which covers a profile that has gone stale.
2. **System prompts name the model.** "You are Aether Agentic
   (Qwen3-Coder-30B-A3B-Instruct)" is a lie on any other model. Templated.
3. **Sampling constants per model.** From the probe, with catalog overrides.
4. **`<think>` tag parsing.** Handled natively by Ollama for most models, but
   some emit tags in content and not all use `<think>`. Broaden to the known
   families.
5. **`context_max` hardcoded to 262144.** From `model_info`.
6. **Tool calls arriving as JSON in content.** Smaller models do this constantly
   instead of using the native `tool_calls` field. Without a fallback parser,
   a 7B model looks completely broken in agentic mode. This is Phase 3 and it is
   the difference between "supports small models" and "claims to".

Guards that are already generic and stay as they are: the repeated-call breaker,
the narration check, the early-debrief nudge, the `todo_write` completion
restore, and riding per-step instructions on the newest tool result. The last
one is about how chat templates hoist system messages, which is true broadly,
not a Qwen quirk.

## Tool calls a model writes into its reply

Measured on llama3.2:3b with Aether's real schemas: 2 of 16 replies put the
call in the message body instead of the `tool_calls` field, both on the deeply
nested schemas. It correlates with schema complexity, not chance.

The existing extractor already read `<tool_call>` and ` ```tool `. What it did
not read was the shape that actually occurred, a bare `{"name": ...,
"parameters": {...}}`, so those replies ended the run having done nothing.

Two rules keep the fallback from becoming a hazard. A call is only executed
when its name is a registered tool and its arguments are an object. Bare JSON
is only trusted when it reaches the end of the reply, so a call quoted
mid-sentence, which has prose after it, is left alone.

### When the JSON does not parse

The more common live failure is worse: the same model emitted `"todos":
[["activeForm": ...]]`, square brackets where objects belonged. Nothing can be
recovered from that, and inventing the arguments would run something the model
never asked for. So the loop recognises that a call was attempted, reports only
the tool name, and retries with a correction. On the run that previously ended
with zero tool calls, that turned into nineteen.

The retry has its own budget rather than sharing the stall budget, which resets
on every successful call: a model alternating good and mangled calls would
otherwise retry without bound. Verified by mutation, and removing the bound
does not merely waste steps, it hangs the loop.

## Schema trimming

All 14 schemas cost about 4.1k tokens, and the prompt guidance another 1.2k. At
the default agentic window that is affordable and nothing is trimmed, which the
tests pin. Below roughly 16k the optional tools are dropped in a fixed order
until they fit a sixth of the window, and the six tools agentic work cannot be
done without are never dropped.

The prompt is trimmed with them. `tools_system_addon` takes the actual list, so
it never names a tool that was removed, and the ask_user guidance travels with
the tool it describes.

## Effort levels

Which tiers think is a property of the tier. Whether they *can* is a property
of the model. Users could not tell the two apart, and with one model family
that was a documentation problem. With any Ollama model it is a correctness
one: the default agentic model reports no thinking capability, so the old
hints promised a reasoning pass that never ran.

So the static hints describe the budget only, and the thinking half is
appended per model: "Thinking on.", "Thinking off.", or "This model has no
thinking mode, so this level is direct." Each model also carries a one-line
summary ("Medium and above think before answering") for the picker.

`/api/health` returns the tiers inside each model entry rather than once at the
top, and `/api/context` returns them for that chat's model, because two models
in the same session can disagree about every level.

## The installer

Both installers ask the same three-way question: Default, Recommended, or type
your own. Neither carries a list of model tags any more.

`models.py` imports only the standard library, so `setup.py` can use it before
pip has run, and the Windows script shells out to `python models.py --plan` for
the same answer rather than keeping a second copy of the catalog. Download
sizes come from Ollama's registry manifest, so the disk check is exact instead
of a table of numbers that goes stale.

A typed tag is looked up before anything downloads: a name Ollama does not have
is refused and re-asked rather than failing halfway through an install.

### Recommending for a machine with no GPU

What fits and what is worth running are different questions, and RAM answers
the wrong one. 128 GB of system memory will hold a 70B, which then answers at
about a word a second. So `hardware()` reports `usable_bytes` for fit and a
separate `recommend_bytes` that caps a CPU-only machine into the 8B tier, the
largest that stays usable without a card.

Scaling verified across eight machines, from a 4 GB card up to 80 GB:

| Machine | Recommended |
|---|---|
| 4 GB card | one 4B doing both jobs |
| 8 GB card | 8B chat, 7B coder |
| 16 GB card | 14B pair |
| 24 to 64 GB card | the tuned pair, which is also Default |
| 80 GB card | adds a 70B for chat |
| no GPU, any RAM | 8B chat, 7B coder |

## Parity with the default

Goal 1 is the one that is easy to lose quietly, so it is a test rather than an
intention. `DefaultParityTests` holds every resolved value for the four
pre-role keys, captured from `config.py` at 8052512, and fails on any drift in
label, colour, sampling, effort, window or capability.

Collapsing four model keys into three roles did lose three things, all on
reasoning, all restored: its `(Reasoning)` label, its distinct colour, and its
131072 ceiling. That last one matters most. Thinking fills KV fast enough that
the model's native window was never a safe limit for it, so the ceiling is a
role property (`ROLE_CONTEXT_MAX`) rather than something inherited from the
model.

## The catalog

`catalog.json` ships in the repo. It holds the curated list: tag, label, a
one-line description, size, capability hints, a recommendation rank, and the
hardware tier it belongs to. It is the search index and it works with no
network.

Aether stays local-only by default. The catalog is offline, the probe is
localhost, and the only outbound calls are the manifest check before a pull and
the pull itself, both of which are things the user explicitly asked for by
clicking Install. Ollama's library page is linked out to rather than scraped:
`https://ollama.com/library` has no JSON search API, only HTML, and a scraper
would break silently.

Anything not in the catalog is still installable by typing its tag. The catalog
is a shortcut, never a whitelist.

## Compatibility shims

Phases 1 to 4 kept three translations so Aether stayed runnable while the UI
still spoke the four old key names. Phase 5 removed all of them: the model id
is the key from the picker to Ollama and back.

What remains is one-way and stays: a saved chat's `agent_model` becomes
`model_id` on read, and the settings file migrates once at version 3.

### What the model id cannot say

Removing the shims exposed a real bug. The send handler decided whether a turn
was agentic by asking whether its model was assigned to the agentic role. With
roles, a model can serve both, so every Chat message on the default model
became an agentic run: wrong prompt, wrong window, tool schemas attached. It
still answered, which is why only driving the real UI caught it.

The request now states its mode, because the tab is the only thing that knows.

## The model API

One listing endpoint answers everything the Models panel needs, because the
panel has to show capability, assignment and fit together: `GET /api/models`
returns the hardware probe, every installed model with its roles, what it can
serve, its window on this machine, the catalog ranked and filtered, and the
recommended tier.

Assignment is two endpoints. `/api/models/roles` sets the Chat and Agentic
toggles and refuses a role the model cannot serve, or releasing the last Chat
assignment. `/api/models/active` picks which assigned model a role runs on.

`/api/models/verify` answers "does this tag exist and will it fit" before a
download starts, which is what makes typing your own tag safe.
`/api/models/pull` streams progress and profiles the model before reporting
done, so the pickers can offer it immediately. `/api/models/delete` removes
it and clears every assignment, active pick and window override that
pointed at it.

Two details the shape of Ollama's API forces:

A pull reports bytes **per layer**, and a layer announces its total before any
of it arrives, so the newest layer's own percentage sends the bar back to zero
several times over. Progress is summed across every layer seen.

A pull's errors arrive **inside a 200 stream**, not as a status code, so the
stream is read for an `error` key rather than trusted on its status.

### Hardware

VRAM decides when a GPU is present, since a model that spills to system RAM
runs but crawls. RAM is the figure only when there is no GPU. Free disk is
measured on Ollama's model store, which is the disk a pull actually fills, not
the working directory.

## The Models panel

One side panel: what this machine is, what is installed, and what can be added.

Each installed model is a card with the two toggles, the window it gets here,
and Remove. A toggle for a role the model cannot serve is disabled with the
reason on it rather than clickable and then broken, and Ollama's `/api/show` is
what decides that.

The catalog below it is ranked by recommendation, filtered by the same search
box that accepts any Ollama tag typed exactly, and capped at six with Show
more. An entry too large for the machine says so before it is downloaded.

The picker is built from the registry, so a model appears in it the moment its
pull finishes, with no restart and no code change. Deep reasoning is a toggle
on the chat model rather than an entry of its own, shown only when that model
reports a thinking mode, and computer use is disabled when nothing installed
can see.

## Recommendation tiers

Keyed on usable memory, meaning VRAM when a GPU is present and system RAM
otherwise. It has to scale up as well as down, so a 48 GB or 80 GB card gets a
better recommendation rather than the same one as a 24 GB card.

| Usable | Chat | Agentic |
|---|---|---|
| under 6 GB | 4B class | same model, tools only |
| 6 to 12 GB | 8B class | 8B coder class |
| 12 to 20 GB | 14B class | 14B coder class |
| 20 to 32 GB | current default pair | current default pair |
| 32 to 64 GB | 27B/32B class, longer context | 30B MoE, longer context |
| 64 GB and up | 70B class or larger | 70B class or larger |

Exact tags live in `catalog.json`, not in this table, so they can be updated
without touching code. The tier is a starting point that the fit math then
validates against the actual machine.

## Assumption

One reading I committed to. Say so if it is wrong.

**"Show more" when space runs out.** Read as: the model list is capped by
default and expands on click, and there is a storage view showing what each
model costs with the ability to remove one. Both get built.

## Phases

Each phase ends with the suite green. Checked after each one before the next
starts.

- [x] Phase 0: Probe and registry
- [x] Phase 1: Roles
- [x] Phase 2: De-Qwen the runtime
- [x] Phase 3: Guards for weak models
- [x] Phase 4: Server API
- [x] Phase 5: UI
- [x] Phase 6: Wizard
- [x] Phase 7: Docs and close-out

### Phase 0: Probe and registry
`models.py`: capability and `model_info` probe, cache, KV and fit math, catalog
loader. Pure addition, nothing imports it yet. Tests for the fit math against
both known models.

### Phase 1: Roles
Settings schema for role assignments and per-model overrides. `config.MODELS`
becomes a resolved view keyed by model id, and the four legacy names become
roles. Migration for existing settings and saved chats. Empty-role handling.
Nothing else changes.

### Phase 2: De-Qwen the runtime
`think` gated on capability. System prompts templated. Sampling from the probe.
`context_max` from `model_info`. Context settings become per-model instead of
four named keys. Thinking-tag parsing broadened. A context window the user
already chose is never raised by the fit calculator; it informs new installs
only.

### Phase 3: Guards for weak models
Tool-call-in-content fallback parser. Tool schema trimming when the window is
small. Verify each existing guard against a model that is not Qwen. Degrade
gracefully when a slot cannot be filled.

### Phase 4: Server API
`/api/models` (installed, catalog, slots, fit), `/api/models/pull` streaming
progress, `/api/models/delete`, `/api/models/slots`, `/api/models/recommend`,
hardware probe.

### Phase 5: UI
Model picker driven by the registry rather than hardcoded markup. Models panel:
search, sort by recommended, descriptions, install with progress, the Chat and
Agentic toggles with their disabled state, show more, storage and removal.
Per-model context controls.

### Phase 6: Wizard
Default, Recommended, or type your own, in `setup.py` and
`scripts/install-windows.ps1`. Hardware detection, disk math from manifest
sizes, a link to Ollama's library, and validation of a typed tag before
committing to a download.

### Phase 7: Docs and close-out
README notice replaced, since it currently promises this exact feature as future
work. ARCHITECTURE model section rewritten. `aether.json` updated. Full suite.

## Where it landed

All seven phases done. 371 tests, from 164 at the start.

The four goals, against what shipped:

1. **The default does not regress.** `DefaultParityTests` holds every resolved
   value for the four pre-role keys, captured from the last release, and it
   reads zero differences. Collapsing the keys into roles did lose three things
   on reasoning, all restored.
2. **Any Ollama model works.** Verified by installing one outside the Qwen
   family: it profiled itself, appeared in both pickers and ran a real agentic
   task with no code change.
3. **One model is a valid setup, so is six.** Roles collapse onto one model, and
   an empty agentic role hides the tab with a reason rather than failing.
4. **Default, Recommended, or type your own**, in both installers, sharing one
   catalog and one plan.

Four bugs that only came out of running the thing rather than testing it:
`think: true` is a hard 400 on a model without it; a malformed tool call in the
reply body ended a run having done nothing; the malformed-call retry had no
bound and hung the loop; and the send handler inferred the tab from the model,
which turned every Chat message on the default model into an agentic run.

## Non-goals

Not doing model swapping mid-run. One model per job is a deliberate decision
from the previous round and it costs a full unload and reload on a 24 GB card.

Not doing automatic model selection per message. The user picks, Aether
recommends.

Not supporting non-Ollama backends. That is a different change, and adding
Hugging Face did not turn into one: Ollama imports GGUF from an `hf.co/` tag, so
the second library is a second *catalog*, sharing one pull, one probe and one
runtime. A second runtime — transformers, vLLM — would mean torch, a second
streaming and tool-call client, and its own VRAM lifecycle, for models that have
a GGUF within days anyway. Still not doing that.
