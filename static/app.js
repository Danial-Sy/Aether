// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Danial Syed
// Prompt dock. Everything that parks a run on the user, shell approvals and
// ask_user questions alike, lands in one bar above the composer.
// One render path, and pollPendingPrompt() re-reads the open prompt from the
// server so a dropped SSE frame self-heals instead of stranding the run.

const promptQueue = [];        // [{ id, kind, chatId, event, answers, page }]
let promptActive = 0;          // which queued prompt is on screen
let _pendingPoll = null;

function promptId(ev) {
  const base = `${ev.type}:${ev.chat_id || ""}`;
  if (ev.type === "shell_approval") return `${base}:${ev.command || ""}`;
  return `${base}:${(ev.questions || []).map((q) => q.id || q.question).join("|")}`;
}

function queuePrompt(ev) {
  const id = promptId(ev);
  if (promptQueue.some((p) => p.id === id)) return;   // re-poll of a known prompt
  promptQueue.push({
    id,
    kind: ev.type,
    chatId: ev.chat_id || state.chatId,
    event: ev,
    answers: {},
    page: 0,
  });
  promptActive = promptQueue.length - 1;
  renderPromptDock();
}

/** Drop one prompt (answered here, or resolved server-side by a timeout). */
function dropPrompt(match) {
  const i = promptQueue.findIndex(match);
  if (i < 0) return;
  promptQueue.splice(i, 1);
  if (promptActive >= promptQueue.length) promptActive = Math.max(0, promptQueue.length - 1);
  renderPromptDock();
}

async function answerPrompt(prompt, payload) {
  dropPrompt((p) => p.id === prompt.id);
  try {
    await fetch(`/api/chats/${prompt.chatId}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (_) {
    setStatus("Could not send your answer. The run may still be waiting.");
  }
}

function renderPromptDock() {
  const host = $("#approvalDock");
  if (!host) return;
  if (!promptQueue.length) {
    host.hidden = true;
    host.innerHTML = "";
    document.title = "Aether";
    return;
  }
  const prompt = promptQueue[Math.min(promptActive, promptQueue.length - 1)];
  host.hidden = false;
  document.title = "● Needs you · Aether";
  host.innerHTML = "";
  host.appendChild(
    prompt.kind === "shell_approval" ? shellApprovalBody(prompt) : askUserBody(prompt)
  );
  // Multiple parked prompts stack; the strip toggles between them.
  if (promptQueue.length > 1) host.prepend(promptPager(prompt));
  const first = host.querySelector("button:not(:disabled), input");
  if (first) first.focus({ preventScroll: true });
}

function promptPager(prompt) {
  const bar = document.createElement("div");
  bar.className = "prompt-pager";
  bar.innerHTML = `
    <button type="button" class="prompt-nav prev" title="Previous">‹</button>
    <span class="prompt-count">${promptActive + 1} of ${promptQueue.length} waiting</span>
    <button type="button" class="prompt-nav next" title="Next">›</button>`;
  bar.querySelector(".prev").onclick = () => {
    promptActive = (promptActive - 1 + promptQueue.length) % promptQueue.length;
    renderPromptDock();
  };
  bar.querySelector(".next").onclick = () => {
    promptActive = (promptActive + 1) % promptQueue.length;
    renderPromptDock();
  };
  return bar;
}

function shellApprovalBody(prompt) {
  const ev = prompt.event;
  const binary = ev.binary || "";
  const wrap = document.createElement("div");
  wrap.className = "prompt-body";
  wrap.innerHTML = `
    <div class="approval-why">Needs approval: ${escapeHtml(ev.why || "unrecognised command")}</div>
    <pre class="approval-cmd"></pre>
    <div class="approval-actions">
      <button type="button" class="approval-run">Run</button>
      <button type="button" class="approval-deny">Deny</button>
      <label class="approval-remember">
        <input type="checkbox" class="approval-always" />
        <span>Always allow${binary ? ` <code>${escapeHtml(binary)}</code>` : " this command"}</span>
      </label>
    </div>`;
  wrap.querySelector(".approval-cmd").textContent = ev.command || "";
  const send = (approved) => {
    const always = wrap.querySelector(".approval-always")?.checked;
    answerPrompt(prompt, {
      answers: { shell_approval: approved ? "Run command" : "Deny" },
      cancelled: false,
      remember: approved && always ? (binary ? "binary" : "command") : null,
    });
  };
  wrap.querySelector(".approval-run").onclick = () => send(true);
  wrap.querySelector(".approval-deny").onclick = () => send(false);
  return wrap;
}

function askUserBody(prompt) {
  const qs = prompt.event.questions || [];
  const wrap = document.createElement("div");
  wrap.className = "prompt-body";
  const answered = (q) => {
    const v = prompt.answers[q.id];
    return Array.isArray(v) ? v.length > 0 : !!(v && String(v).trim());
  };

  // One question at a time with a tab strip, so a four-part ask stays readable
  // in a bar this size instead of pushing the composer off screen.
  const tabs = document.createElement("div");
  tabs.className = "ask-tabs";
  if (qs.length > 1) {
    qs.forEach((q, i) => {
      const t = document.createElement("button");
      t.type = "button";
      t.className = `ask-tab${i === prompt.page ? " active" : ""}${answered(q) ? " done" : ""}`;
      t.textContent = q.header || `Q${i + 1}`;
      t.onclick = () => { prompt.page = i; renderPromptDock(); };
      tabs.appendChild(t);
    });
  }

  const q = qs[Math.min(prompt.page, Math.max(0, qs.length - 1))] || {};
  const block = document.createElement("div");
  block.className = "ask-q";
  block.innerHTML = `<div class="ask-qtext">${escapeHtml(q.question || "Aether needs a decision")}</div>`;

  const opts = document.createElement("div");
  opts.className = "ask-opts";
  (q.options || []).forEach((o) => {
    const b = document.createElement("button");
    b.type = "button";
    const chosen = q.multi_select
      ? (prompt.answers[q.id] || []).includes(o.label)
      : prompt.answers[q.id] === o.label;
    b.className = `ask-opt${chosen ? " active" : ""}`;
    b.innerHTML =
      `<span class="ask-opt-label">${escapeHtml(o.label)}</span>` +
      (o.description ? `<span class="ask-opt-desc">${escapeHtml(o.description)}</span>` : "");
    b.onclick = () => {
      if (q.multi_select) {
        const cur = new Set(prompt.answers[q.id] || []);
        cur.has(o.label) ? cur.delete(o.label) : cur.add(o.label);
        prompt.answers[q.id] = [...cur];
      } else {
        prompt.answers[q.id] = o.label;
      }
      renderPromptDock();
    };
    opts.appendChild(b);
  });
  block.appendChild(opts);

  // Always allow a typed answer. It is the only input when the model asked
  // something open-ended with no options.
  const other = document.createElement("input");
  other.type = "text";
  other.className = "ask-other";
  other.placeholder = (q.options || []).length ? "Or type your own…" : "Type your answer…";
  const cur = prompt.answers[q.id];
  if (cur && !(q.options || []).some((o) => o.label === cur)) {
    other.value = Array.isArray(cur) ? cur.join(", ") : cur;
  }
  other.oninput = () => {
    const v = other.value.trim();
    if (v) prompt.answers[q.id] = q.multi_select ? [v] : v;
    else delete prompt.answers[q.id];
    const submit = wrap.querySelector(".ask-submit");
    if (submit) submit.disabled = !qs.every(answered);
  };
  block.appendChild(other);

  const foot = document.createElement("div");
  foot.className = "ask-foot";
  foot.innerHTML = `
    <button type="button" class="ask-submit"${qs.every(answered) ? "" : " disabled"}>Send answers</button>
    <button type="button" class="ask-skip">Skip, you decide</button>`;
  foot.querySelector(".ask-submit").onclick = () =>
    answerPrompt(prompt, { answers: prompt.answers, cancelled: false });
  foot.querySelector(".ask-skip").onclick = () =>
    answerPrompt(prompt, { answers: {}, cancelled: true });

  if (qs.length > 1) wrap.appendChild(tabs);
  wrap.appendChild(block);
  wrap.appendChild(foot);
  return wrap;
}

/** Safety net: read the open prompt straight from the server while a run streams. */
async function pollPendingPrompt() {
  if (!state.chatId) return;
  try {
    const r = await api(`/api/chats/${state.chatId}/pending`);
    if (r && r.pending) queuePrompt(r.pending);
    else dropPrompt((p) => p.chatId === state.chatId);
  } catch (_) {}
}

function startPendingPoll() {
  if (_pendingPoll) return;
  _pendingPoll = setInterval(pollPendingPrompt, 2500);
}

function stopPendingPoll() {
  if (!_pendingPoll) return;
  clearInterval(_pendingPoll);
  _pendingPoll = null;
  // One last read: a prompt raised in the final moments of a run still needs an
  // answer, and the stream has already closed by the time we get here.
  pollPendingPrompt();
}

function currentEffort() {
  if (state.effort && state.effortLevels.includes(state.effort)) return state.effort;
  const def = state.modelDefaults[currentModelKey()];
  return state.effortLevels.includes(def) ? def : "high";
}

const state = {
  mode: "chat", // chat | agentic
  modelKey: "general",
  warmedKey: null, // last model key confirmed resident in VRAM
  think: false, // off by default for snappy chat; toggle in model menu
  // Reasoning effort. null = follow the model's own default (see /api/health),
  // so the reasoning model lands on "medium" instead of over-thinking on high.
  effort: null,
  effortLevels: ["low", "medium", "high"],
  effortMeta: {},
  modelDefaults: {},
  computerUse: false,
  // Which model runs the job: "coder" is faster, "agent" has vision and is
  // required for computer use. One model per run.
  agentModel: "coder",
  abortController: null,
  chatId: null,
  chats: [],
  projects: [],
  webSearch: false,
  streaming: false,
  attachments: [],
  settings: {},
  agentTodos: [],
  agentTodoCollapsed: false,
  fileLibrary: [],
};


const CTX_STEPS = {
  general: [32768, 65536, 131072, 262144],
  reasoning: [16384, 32768, 65536, 131072],
  agent: [32768, 65536, 131072, 262144],
  coder: [65536, 131072, 262144],
};
const CTX_SETTING = {
  general: "chat_context",
  reasoning: "reasoning_context",
  agent: "agent_context",
  coder: "coder_context",
};

function fmtCtx(n) {
  return `${Math.round(n / 1024)}K`;
}

function confirmAction({ title = "Are you sure?", body = "", ok = "Delete" } = {}) {
  return new Promise((resolve) => {
    const dlg = $("#confirmDialog");
    $("#confirmTitle").textContent = title;
    $("#confirmBody").textContent = body;
    $("#confirmOk").textContent = ok;
    const onClose = () => {
      dlg.removeEventListener("close", onClose);
      resolve(dlg.returnValue === "ok");
    };
    dlg.addEventListener("close", onClose);
    dlg.showModal();
  });
}

function promptEdit({ title = "Edit", label = "Value", value = "", multiline = true } = {}) {
  return new Promise((resolve) => {
    const dlg = $("#promptDialog");
    const card = dlg.querySelector(".dialog-card");
    $("#promptTitle").textContent = title;
    $("#promptLabel").textContent = label;
    const input = $("#promptInput");
    input.value = value || "";
    input.rows = multiline ? 6 : 1;
    input.style.minHeight = multiline ? "140px" : "44px";
    input.style.resize = multiline ? "vertical" : "none";
    if (card) card.classList.toggle("dialog-wide", !!multiline);
    const onClose = () => {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue === "ok") resolve(input.value.trim());
      else resolve(null);
    };
    dlg.addEventListener("close", onClose);
    dlg.showModal();
    setTimeout(() => {
      input.focus();
      const len = input.value.length;
      try { input.setSelectionRange(len, len); } catch (_) {}
    }, 30);
  });
}

function beginInlineUserEdit(el, m) {
  if (!el || el.classList.contains("editing") || state.streaming) return;
  const bubble = el.querySelector(".bubble");
  if (!bubble) return;
  el.classList.add("editing");
  const original = m.content || "";
  bubble.innerHTML = `<div class="msg-edit">
      <textarea class="msg-edit-input" rows="3"></textarea>
      <div class="msg-edit-actions">
        <button type="button" class="btn-ghost msg-edit-cancel">Cancel</button>
        <button type="button" class="btn-primary msg-edit-save">Save & resubmit</button>
      </div>
    </div>`;
  const ta = bubble.querySelector(".msg-edit-input");
  const saveBtn = bubble.querySelector(".msg-edit-save");
  const cancelBtn = bubble.querySelector(".msg-edit-cancel");
  ta.value = original;
  const autosize = () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(220, Math.max(56, ta.scrollHeight)) + "px";
  };
  ta.addEventListener("input", autosize);
  autosize();
  setTimeout(() => {
    ta.focus();
    const len = ta.value.length;
    try { ta.setSelectionRange(len, len); } catch (_) {}
  }, 20);

  const restore = async () => {
    const chat = await api(`/api/chats/${state.chatId}`);
    renderMessages(chat.messages || []);
    await refreshContext();
  };

  cancelBtn.onclick = () => restore();
  saveBtn.onclick = async () => {
    const text = ta.value.trim();
    if (!text) return;
    saveBtn.disabled = true;
    try {
      await resubmitEditedMessage(m.id, text);
    } catch (e) {
      saveBtn.disabled = false;
      setStatus(e.message || "Could not resubmit");
    }
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      restore();
    }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      saveBtn.click();
    }
  });
}

async function resubmitEditedMessage(mid, text) {
  if (state.streaming) return;
  state.streaming = true;
  state.abortController = new AbortController();
  setSendMode("stop");
  startPendingPoll();
  setStatus("Resubmitting…");

  // Refresh UI to truncated history first (optimistic: reload after request starts)
  let streamEl = null;
  let unlocked = false;
  try {
    const res = await fetch(`/api/chats/${state.chatId}/messages/${mid}/resubmit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: state.abortController.signal,
      body: JSON.stringify({
        content: text,
        model_key: currentModelKey(),
        agent_model: isAgenticMode() ? state.agentModel : null,
        think: !!(modelSupportsThink() && state.think),
        effort: currentEffort(),
      }),
    });
    if (!res.ok) throw new Error(await res.text());

    // Show truncated chat + new streaming bubble
    const chat = await api(`/api/chats/${state.chatId}`);
    renderMessages(chat.messages || []);
    streamEl = appendMessage({ role: "assistant", content: "", thinking: "", model: "…" }, { streaming: true });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        let ev;
        try {
          ev = JSON.parse(line.slice(5).trim());
        } catch (_) {
          continue;   // one unreadable frame must not end the stream
        }
        streamEl = handleSSE(ev, streamEl) || streamEl;
        if (ev.type === "done" || ev.type === "stopped" || ev.type === "error") {
          if (!unlocked) {
            unlocked = true;
            finishStreamingUI();
          }
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") setStatus("Stopped");
    else {
      setStatus(e.message);
      if (streamEl) updateStreamingEl(streamEl, { content: `⚠️ ${e.message}`, thinking: "" });
    }
  } finally {
    if (!unlocked) finishStreamingUI();
    await loadChats();
    await refreshContext();
    setTimeout(() => { loadChats().catch(() => {}); }, 2500);
  }
}

let _ctxMeta = { key: "general", used: 0, limit: 131072 };

function closeCtxDropdown() {
  const wrap = $("#ctxWrap");
  const dd = $("#ctxDropdown");
  if (!wrap || !dd) return;
  wrap.classList.remove("open");
  dd.hidden = true;
}

function openCtxDropdown() {
  const wrap = $("#ctxWrap");
  const dd = $("#ctxDropdown");
  if (!wrap || !dd) return;
  // Always follow the currently selected model (not a stale last-API key)
  const key = currentModelKey();
  _ctxMeta.key = key;
  const steps = CTX_STEPS[key] || CTX_STEPS.general;
  const cur = Number(state.settings[CTX_SETTING[key]] || steps[Math.min(2, steps.length - 1)]);
  let idx = steps.indexOf(cur);
  if (idx < 0) {
    idx = steps.reduce((best, v, i) => (Math.abs(v - cur) < Math.abs(steps[best] - cur) ? i : best), 0);
  }
  const slider = $("#ctxSlider");
  slider.min = 0;
  slider.max = String(steps.length - 1);
  slider.value = String(idx);
  const used = Number(_ctxMeta.used || 0);
  const limit = Number(steps[idx] || _ctxMeta.limit || 0);
  const usedLabel = used < 1024 ? `${used} tok` : fmtCtx(used);
  $("#ctxDdModel").textContent = `${MODEL_META[key]?.short || key} · ${usedLabel} / ${fmtCtx(limit)}`;
  $("#ctxDdVal").textContent = fmtCtx(steps[idx]);
  syncCompactBtn();
  dd.hidden = false;
  wrap.classList.add("open");
  dd.style.animation = "none";
  void dd.offsetWidth;
  dd.style.animation = "";
}

/** The manual compaction control only makes sense on a chat with history. */
function syncCompactBtn() {
  const btn = $("#btnCompactNow");
  const hint = $("#ctxCompactHint");
  if (!btn) return;
  let why = "";
  if (!state.chatId) why = "Open a conversation to compact it.";
  else if (state.streaming) why = "Finish or stop the current reply first.";
  btn.disabled = !!why;
  if (hint) {
    hint.textContent =
      why || "Summarizes the older turns and frees the window. Nothing is deleted.";
  }
}

/** Compact on demand, rather than waiting for the auto-compact threshold. */
async function compactNow() {
  if (!state.chatId || state.streaming) return;
  closeCtxDropdown();
  showCompact(true);
  setStatus("Compacting conversation…");
  try {
    const res = await fetch(`/api/chats/${state.chatId}/compact`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let freed = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        let ev;
        try {
          ev = JSON.parse(line.slice(5).trim());
        } catch (_) {
          continue;
        }
        if (ev.type !== "compacted") continue;
        if (Array.isArray(ev.messages)) {
          renderMessages([
            {
              role: "assistant",
              content:
                "Conversation compacted to free context. Earlier details stay available for this chat.",
              model: "aether",
              compact_notice: true,
            },
            ...ev.messages,
          ]);
        }
        if (ev.context) {
          freed = ev.context.pct;
          setContextMeter(ev.context.pct, ev.context);
        }
      }
    }
    setStatus(freed === null ? "Nothing to compact yet" : `Compacted, ${Math.round(freed)}% used`);
    await refreshContext();
  } catch (e) {
    setStatus(e.message || "Compaction failed");
  } finally {
    showCompact(false);
    syncCompactBtn();
  }
}

async function applyCtxSlider() {
  const key = currentModelKey();
  _ctxMeta.key = key;
  const steps = CTX_STEPS[key] || CTX_STEPS.general;
  const idx = Number($("#ctxSlider").value);
  const val = steps[idx];
  $("#ctxDdVal").textContent = fmtCtx(val);
  const sk = CTX_SETTING[key];
  state.settings[sk] = val;
  // sync settings panel selects if present
  const map = { chat_context: "setChatCtx", reasoning_context: "setReasonCtx", coder_context: "setCoderCtx", agent_context: "setAgentCtx" };
  const el = map[sk] && $(`#${map[sk]}`);
  if (el) el.value = String(val);
  await api("/api/settings", { method: "PATCH", body: JSON.stringify({ data: { [sk]: val } }) });
  await refreshContext();
}



const TAGLINES = {
  morning: [
    "Early bird gets the worm!",
    "Rise and compile.",
    "Morning. Let’s make the bugs nervous.",
    "Early bird gets the stack trace.",
    "Sun’s up. So are the unread emails.",
    "Good morning. Shall we invent something mildly reckless?",
    "GOOD MORNINGGGGG!!!",
  ],
  afternoon: [
    "What can we tackle together?",
    "You so tuff.",
    "The void is listening, pitch it an idea.",
    "Productivity hour, allegedly.",
    "What's on your mind?.",
    "What’s the quest, traveler?",
    "Stop procrastinating and LOCK IN.",
    "I’m lowkey shadow the way I CHAOS CONTROL.",
  ],
  evening: [
    "Good evening govna.",
    "Golden hour, golden commits.",
    "Wind down… or wind up a new project.",
    "Dinner can wait but this banger idea can’t.",
    "Twilight debugging hits different.",
    "¡Buenas tardes!",
  ],
  late: [
    "It’s late. So are the BEST ideas.",
    "3am club checking in.",
    "Sleep is merely a suggestion.",
    "Double... no TRIPLE check your work.",
    "The servers never sleep. Neither do we, apparently.",
    "Late night prompting is better anyways.",
    "Insomnia, but make it productive.",
  ],
};

function daypart() {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return "morning";
  if (h >= 12 && h < 17) return "afternoon";
  if (h >= 17 && h < 22) return "evening";
  return "late";
}


const STATUS_WORDS = {
  morning: ["Brewing", "Rising", "Warming", "Gathering", "Focusing"],
  afternoon: ["Composing", "Charting", "Weaving", "Shaping", "Aligning"],
  evening: ["Softening", "Tuning", "Glowing", "Settling", "Crafting"],
  late: ["Orbiting", "Humming", "Drifting", "Sparking", "Crystallizing"],
};

function pickStatusWord() {
  const bucket = STATUS_WORDS[daypart()] || STATUS_WORDS.afternoon;
  return `${bucket[Math.floor(Math.random() * bucket.length)]}...`;
}

function pickTagline() {
  const bucket = TAGLINES[daypart()] || TAGLINES.afternoon;
  return bucket[Math.floor(Math.random() * bucket.length)];
}

function refreshTagline() {
  const el = $("#emptyTagline");
  if (el) el.textContent = pickTagline();
}

function modelSupportsThink(key = currentModelKey()) {
  // Qwen3.8 hybrid exposes an optional thinking pass.
  return key === "general" || key === "reasoning" || key === "agent";
}

function syncThinkToggle() {
  const btn = $("#btnThinkToggle");
  const sep = $("#thinkSep");
  const key = currentModelKey();
  const showToggle = modelSupportsThink(key);
  const effortAllowsThinking = state.effortMeta[currentEffort()]?.think !== false;
  const effectiveThinking = !!state.think && effortAllowsThinking;
  if (btn) btn.hidden = !showToggle;
  if (sep) sep.hidden = !showToggle;
  if (btn) {
    btn.classList.toggle("active", effectiveThinking);
    const check = $("#thinkCheck");
    if (check) check.hidden = !effectiveThinking;
    const desc = $("#thinkToggleDesc");
    if (desc) {
      desc.textContent = !effortAllowsThinking
        ? "Off, disabled by this effort level"
        : state.think
          ? "On, deeper reasoning"
          : "Off, faster replies";
    }
  }
  // --- reasoning effort slider ---
  const row = $("#effortRow");
  const slider = $("#effortSlider");
  if (row && slider) {
    const levels = state.effortLevels;
    const active = currentEffort();
    const idx = Math.max(0, levels.indexOf(active));
    // Level count is server-driven, so the slider has to resize with it.
    if (String(slider.max) !== String(levels.length - 1)) {
      slider.max = String(Math.max(0, levels.length - 1));
    }
    if (String(slider.value) !== String(idx)) slider.value = String(idx);
    const ticks = $("#effortTicks");
    if (ticks && ticks.childElementCount !== levels.length) {
      ticks.innerHTML = "";
      levels.forEach((key, i) => {
        const t = document.createElement("span");
        t.className = "effort-tick";
        t.textContent = state.effortMeta[key]?.label || key;
        t.onclick = () => {
          slider.value = String(i);
          slider.dispatchEvent(new Event("input", { bubbles: true }));
        };
        ticks.appendChild(t);
      });
    }
    [...(ticks?.children || [])].forEach((t, i) => t.classList.toggle("active", i === idx));
    const desc = $("#effortDesc");
    if (desc) {
      const meta = state.effortMeta[active] || {};
      const label = meta.label || active.charAt(0).toUpperCase() + active.slice(1);
      const hint = meta.hint ? `, ${meta.hint}` : "";
      desc.textContent =
        state.effort === null ? `${label}, model default` : `${label}${hint}`;
    }
  }

  const chip = $("#thinkChip");
  if (chip) {
    // Chip tracks the Thinking toggle for any model that supports it.
    const showChip = showToggle && effectiveThinking;
    chip.hidden = !showChip;
  }
}


const MODEL_META = {
  general: { short: "Qwen3.8 27B", tab: "chat" },
  reasoning: { short: "Qwen3.8 27B", tab: "chat" },
  agent: { short: "Qwen3.8 27B", tab: "agentic" },
  coder: { short: "Qwen3-Coder 30B", tab: "agentic" },
};

function normalizeMode(mode) {
  if (mode === "code" || mode === "computer" || mode === "agentic") return "agentic";
  return "chat";
}

function isAgenticMode(mode = state.mode) {
  return normalizeMode(mode) === "agentic";
}

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? undefined : { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = j.detail || j.message || JSON.stringify(j);
    } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Vector Aether ring built from a conic gradient. Scales cleanly at any size. */
function ringMarkup() {
  return `<span class="ring-spin" aria-hidden="true"><span class="ring-disc"></span></span>`;
}

function fillRings(root) {
  (root || document).querySelectorAll(".aether-ring:not([data-svg]), .brand-mark:not([data-svg])").forEach((el) => {
    el.innerHTML = ringMarkup();
    el.dataset.svg = "1";
  });
}

function renderMath(el) {
  if (!el || typeof window.renderMathInElement !== "function") return;
  try {
    window.renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    });
  } catch (_) {}
}

function mdFallback(text) {
  // Offline fallback: headings, lists, emphasis, code.
  const fences = [];
  let s = String(text || "").replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const i = fences.length;
    fences.push(`<pre><code class="lang-${lang}">${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000FENCE${i}\u0000`;
  });
  s = escapeHtml(s);
  s = s.replace(/^######\s+(.+)$/gm, "<h6>$1</h6>");
  s = s.replace(/^#####\s+(.+)$/gm, "<h5>$1</h5>");
  s = s.replace(/^####\s+(.+)$/gm, "<h4>$1</h4>");
  s = s.replace(/^###\s+(.+)$/gm, "<h3>$1</h3>");
  s = s.replace(/^##\s+(.+)$/gm, "<h2>$1</h2>");
  s = s.replace(/^#\s+(.+)$/gm, "<h1>$1</h1>");
  s = s.replace(/^&gt;\s?(.+)$/gm, "<blockquote>$1</blockquote>");
  s = s.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>");
  s = s.replace(/^\s*\d+\.\s+(.+)$/gm, "<li data-ol=\"1\">$1</li>");
  s = s.replace(/(?:<li>.*?<\/li>\n?)+/gs, (block) => {
    if (block.includes('data-ol="1"')) {
      return `<ol>${block.replace(/\s*data-ol="1"/g, "")}</ol>`;
    }
    return `<ul>${block}</ul>`;
  });
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  s = s.replace(/^---$/gm, "<hr>");
  s = s.replace(/\n{2,}/g, "</p><p>");
  s = `<p>${s}</p>`;
  s = s.replace(/<p>\s*(<h[1-6]>)/g, "$1");
  s = s.replace(/(<\/h[1-6]>)\s*<\/p>/g, "$1");
  s = s.replace(/<p>\s*(<ul>|<ol>|<blockquote>|<pre>|<hr>)/g, "$1");
  s = s.replace(/(<\/ul>|<\/ol>|<\/blockquote>|<\/pre>|<hr>)\s*<\/p>/g, "$1");
  s = s.replace(/<p>\s*<\/p>/g, "");
  fences.forEach((html, i) => {
    s = s.replace(`\u0000FENCE${i}\u0000`, html);
  });
  return s;
}

function sanitizeHtml(html) {
  const allowed = new Set([
    "A", "BLOCKQUOTE", "BR", "CODE", "DEL", "EM", "H1", "H2", "H3", "H4", "H5", "H6",
    "HR", "IMG", "LI", "OL", "P", "PRE", "STRONG", "TABLE", "TBODY", "TD", "TH", "THEAD",
    "TR", "UL",
  ]);
  const attrs = {
    A: new Set(["href", "title"]),
    CODE: new Set(["class"]),
    IMG: new Set(["src", "alt", "title"]),
    OL: new Set(["start"]),
  };
  const dangerous = new Set(["BASE", "EMBED", "FORM", "IFRAME", "LINK", "META", "OBJECT", "SCRIPT", "STYLE"]);
  const template = document.createElement("template");
  template.innerHTML = String(html || "");

  [...template.content.querySelectorAll("*")].forEach((el) => {
    if (!allowed.has(el.tagName)) {
      if (dangerous.has(el.tagName)) el.remove();
      else el.replaceWith(...el.childNodes);
      return;
    }
    const keep = attrs[el.tagName] || new Set();
    [...el.attributes].forEach((attr) => {
      if (!keep.has(attr.name.toLowerCase())) el.removeAttribute(attr.name);
    });
    for (const name of ["href", "src"]) {
      if (!el.hasAttribute(name)) continue;
      const value = el.getAttribute(name).trim();
      const safe = value.startsWith("/") || value.startsWith("#") ||
        /^(https?:|mailto:)/i.test(value) || (name === "src" && /^data:image\/(png|jpeg|gif|webp);base64,/i.test(value));
      if (!safe) el.removeAttribute(name);
    }
    if (el.tagName === "A") {
      el.setAttribute("rel", "noopener noreferrer");
      el.setAttribute("target", "_blank");
    }
  });
  return template.innerHTML;
}

function mdLite(text) {
  if (!text) return "";
  const lib = window.marked;
  if (lib && typeof lib.parse === "function") {
    try {
      if (typeof lib.setOptions === "function") {
        lib.setOptions({ gfm: true, breaks: true });
      }
      return sanitizeHtml(lib.parse(String(text)));
    } catch (_) {}
  }
  return sanitizeHtml(mdFallback(text));
}

function fillContent(el, text, { streaming = false } = {}) {
  if (!el) return;
  el.classList.toggle("streaming", !!streaming);
  el.classList.add("md-rich");
  el.classList.remove("md-raw");
  el.innerHTML = mdLite(text || "");
  if (!streaming) renderMath(el);
}

async function copyMessageText(el, btn) {
  const raw = el?.dataset?.raw || el?.querySelector(".content")?.innerText || "";
  try {
    await navigator.clipboard.writeText(raw);
    if (btn) {
      const prev = btn.textContent;
      btn.textContent = "Copied";
      btn.classList.add("copied");
      setTimeout(() => {
        btn.textContent = prev || "Copy";
        btn.classList.remove("copied");
      }, 1200);
    }
    setStatus("Copied to clipboard");
  } catch (e) {
    setStatus(e.message || "Copy failed");
  }
}

const copyAssistantText = copyMessageText;

function setStatus(msg) {
  $("#statusLine").textContent = msg || "";
}

function showCompact(show) {
  $("#compactOverlay").hidden = !show;
}

function currentModelKey(mode = state.mode) {
  if (isAgenticMode(mode)) {
    // Computer use needs vision; coder has none, so it falls back to the agent.
    if (state.computerUse) return "agent";
    return state.agentModel === "agent" ? "agent" : "coder";
  }
  if (state.modelKey === "reasoning") return "reasoning";
  return "general";
}

function syncModelPicker() {
  const key = currentModelKey();
  state.modelKey = key;
  const labelKey = isAgenticMode() ? state.agentModel : key;
  const meta = MODEL_META[labelKey] || MODEL_META[key] || MODEL_META.general;
  $("#modelTriggerLabel").textContent = meta.short;
  syncThinkToggle();

  // Show the right option group for the active tab
  $$(".model-group").forEach((g) => {
    g.hidden = g.dataset.group !== (isAgenticMode() ? "agentic" : "chat");
  });

  const pickerValue = isAgenticMode() ? state.agentModel : key;
  $$(".model-option").forEach((btn) => {
    const selected = btn.dataset.model === pickerValue;
    btn.classList.toggle("selected", selected);
    const check = btn.querySelector(".mo-check");
    if (check) check.hidden = !selected;
  });

  const cu = $("#btnComputerUse");
  const cuCheck = $("#computerUseCheck");
  if (cu) {
    cu.classList.toggle("active", !!state.computerUse);
    if (cuCheck) cuCheck.hidden = !state.computerUse;
  }
  syncThinkToggle();
}

function showModelWarm(show, label) {
  const el = $("#modelWarmOverlay");
  if (!el) return;
  el.hidden = !show;
  const t = $("#modelWarmTitle");
  if (t) t.textContent = label ? `Warming ${label}…` : "Warming model…";
  if (show) fillRings(el);
}

// Ollama may already hold the model from an earlier run, so ask the server what
// is resident before covering the UI with a warm-up overlay.
async function alreadyResident(key) {
  try {
    const st = await api("/api/warmup/status");
    return Array.isArray(st.resident) && st.resident.includes(key);
  } catch (_) {
    return false;
  }
}

async function warmModelKey(key) {
  if (!key || !MODEL_META[key]) return;
  if (state.warmedKey === key) return;
  if (await alreadyResident(key)) {
    state.warmedKey = key;
    return;
  }
  const meta = MODEL_META[key] || {};
  showModelWarm(true, meta.short || meta.label || key);
  try {
    await api(`/api/warmup/model/${key}`, { method: "POST", body: "{}" });
    state.warmedKey = key;
  } finally {
    showModelWarm(false);
  }
}

/** Offload previous + warm target whenever the active model key changes. */
async function ensureModelWarm(key, { confirm = false, title = "Switch model?" } = {}) {
  if (!key || !MODEL_META[key]) return false;
  if (state.warmedKey !== key && (await alreadyResident(key))) state.warmedKey = key;
  if (state.warmedKey === key) {
    state.modelKey = key;
    return true;
  }
  const label = (MODEL_META[key] && (MODEL_META[key].short || MODEL_META[key].label)) || key;
  if (confirm) {
    const ok = await confirmAction({
      title,
      body: `${label} may need a short warm-up before it’s ready. Continue?`,
      ok: "Warm & switch",
    });
    if (!ok) return false;
  }
  try {
    await warmModelKey(key);
    state.modelKey = key;
    return true;
  } catch (e) {
    setStatus(e.message || "Warmup failed");
    return false;
  }
}

/** Persist the active chat's model selection so a reload/reopen keeps it. */
async function patchChatModel(cid = state.chatId) {
  if (!cid) return;
  const body = {
    mode: isAgenticMode() ? "agentic" : "chat",
    reasoning: currentModelKey() === "reasoning",
    computer_use: isAgenticMode() && state.computerUse,
  };
  if (isAgenticMode()) body.agent_model = state.agentModel;
  try {
    await api(`/api/chats/${cid}`, { method: "PATCH", body: JSON.stringify(body) });
  } catch (_) {}
}

async function selectModel(key) {
  if (!MODEL_META[key]) return;
  const current = isAgenticMode() ? state.agentModel : currentModelKey();
  if (key === current) {
    closeModelMenus();
    return;
  }
  closeModelMenus();
  const ok = await ensureModelWarm(key, { confirm: true, title: "Switch model?" });
  if (!ok) return;
  // Chat and Agentic are separate transcripts, so crossing that line starts a
  // new one. Swapping models within a tab must not: being forced into a fresh
  // conversation is how a pick got reverted to the default on the way out.
  const crossesTab = MODEL_META[key].tab !== (isAgenticMode() ? "agentic" : "chat");
  if (key === "coder" || key === "agent") {
    state.mode = "agentic";
    state.agentModel = key;
    state.modelKey = key;
    // coder has no vision, so selecting it drops computer use.
    if (key !== "agent") state.computerUse = false;
  } else {
    state.mode = "chat";
    state.computerUse = false;
    state.modelKey = key;
  }
  syncModeUI();
  if (crossesTab || !state.chatId) {
    state.chatId = null;
    showEmpty();
  } else {
    await patchChatModel();
  }
  renderChatLists();
}

function updateEmpty() {
  if (isAgenticMode()) {
    $("#emptyTitle").textContent = state.computerUse ? "Computer use" : "Agentic";
  } else {
    $("#emptyTitle").textContent = state.modelKey === "reasoning" ? "Deep reasoning" : "Chat";
  }
  refreshTagline();
  syncThinkToggle();
}

function setContextMeter(pct, meta) {
  const meter = $("#contextMeter");
  const fill = $("#ctxRingFill");
  const p = Math.max(0, Math.min(100, pct || 0));
  if (fill) {
    // pathLength=100, so dasharray "p 100" draws the progress arc directly
    fill.setAttribute("stroke-dasharray", `${p} 100`);
  }
  $("#ctxPct").textContent = `${Math.round(p)}%`;
  meter.classList.toggle("warn", p >= 70 && p < 90);
  meter.classList.toggle("hot", p >= 90);
  if (meta) {
    _ctxMeta = { key: meta.key || currentModelKey(), used: meta.used || 0, limit: meta.limit || 0 };
  }
}

async function refreshContext() {
  if (!state.chatId) return setContextMeter(0);
  try {
    const c = await api(`/api/context/${state.chatId}`);
    setContextMeter(c.pct, c);
  } catch {}
}

function moveModeInk() {
  const active = document.querySelector(".mode-tab.active");
  const ink = $("#modeInk");
  if (!active || !ink) return;
  ink.style.width = `${active.offsetWidth}px`;
  ink.style.transform = `translateX(${active.offsetLeft - 3}px)`;
}

function syncModeUI() {
  state.mode = normalizeMode(state.mode);
  if (isAgenticMode()) {
    state.modelKey = currentModelKey();
  } else if (state.modelKey !== "reasoning") {
    state.modelKey = "general";
    state.computerUse = false;
  }

  $$(".mode-tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === state.mode));
  const app = $("#app");
  app.classList.toggle("agentic-mode", isAgenticMode());
  app.classList.remove("code-mode", "computer-mode");
  moveModeInk();
  syncModelPicker();
  updateEmpty();
  renderAttachChips();
  setComputerOverlay(isAgenticMode() && state.computerUse);
  renderAgentTodos(state.agentTodos || []);
}

function attachmentPreviewSrc(a) {
  if (!a) return null;
  if (a.url) return a.url;
  if (a.image_b64) {
    if (String(a.image_b64).startsWith("data:")) return a.image_b64;
    return `data:image/png;base64,${a.image_b64}`;
  }
  if (a.path && (a.kind === "image" || a.kind === "screenshot" || a.has_image)) {
    const name = String(a.path).split(/[/\\]/).pop();
    if (name) return `/uploads/${encodeURIComponent(name)}`;
  }
  return null;
}

function openLightbox(src) {
  const box = $("#imageLightbox");
  const img = $("#lightboxImg");
  if (!box || !img || !src) return;
  img.src = src;
  box.hidden = false;
}

function closeLightbox() {
  const box = $("#imageLightbox");
  const img = $("#lightboxImg");
  if (box) box.hidden = true;
  if (img) img.removeAttribute("src");
}

function fmtBytes(n) {
  const x = Number(n);
  if (!Number.isFinite(x) || x < 0) return "";
  if (x < 1024) return `${x} B`;
  if (x < 1024 * 1024) return `${(x / 1024).toFixed(1)} KB`;
  return `${(x / (1024 * 1024)).toFixed(1)} MB`;
}

function renderFileLibrary(items) {
  const box = $("#fileLibrary");
  const chips = $("#fileLibraryChips");
  if (!box || !chips) return;
  const list = Array.isArray(items) ? items : [];
  state.fileLibrary = list;
  if (!list.length) {
    box.hidden = true;
    chips.innerHTML = "";
    return;
  }
  chips.innerHTML = list
    .map((a) => {
      const kind = a.kind === "folder" ? "📁" : "📄";
      const size = fmtBytes(a.size);
      const meta = size ? ` · ${size}` : "";
      return `<span class="chip" title="${escapeHtml(a.path || "")}">${kind} ${escapeHtml(a.name || "file")}${escapeHtml(meta)}</span>`;
    })
    .join("");
  box.hidden = false;
}

function renderAttachChips() {
  const box = $("#attachChips");
  box.innerHTML = "";
  state.attachments.forEach((a, i) => {
    const el = document.createElement("span");
    const src = attachmentPreviewSrc(a);
    const isImg = !!(src || a.kind === "image" || a.kind === "screenshot");
    el.className = isImg && src ? "chip img-chip" : "chip";
    if (isImg && src) {
      el.innerHTML = `<img src="${src}" alt="" /><span class="chip-name">${escapeHtml(a.name || "image")}</span><button type="button" title="Remove">✕</button>`;
      el.querySelector("img").onclick = (e) => { e.stopPropagation(); openLightbox(src); };
      el.onclick = (e) => {
        if (e.target.closest("button")) return;
        openLightbox(src);
      };
    } else {
      const kind = a.kind === "folder" ? "📁" : "📄";
      const size = fmtBytes(a.size);
      const meta = size ? ` · ${size}` : "";
      el.innerHTML = `<span class="chip-name">${kind} ${escapeHtml(a.name || a.kind || "file")}${escapeHtml(meta)}</span> <button type="button">✕</button>`;
    }
    el.querySelector("button").onclick = (e) => {
      e.stopPropagation();
      state.attachments.splice(i, 1);
      renderAttachChips();
    };
    box.appendChild(el);
  });
}

try {
  const savedEffort = localStorage.getItem("aether.effort");
  if (savedEffort) state.effort = savedEffort;
  state.think = localStorage.getItem("aether.think") === "1";
} catch (_) {}

async function loadHealth() {
  const h = await api("/api/health");
  state.settings = h.settings || {};
  if (Array.isArray(h.effort_levels) && h.effort_levels.length) {
    state.effortLevels = h.effort_levels.map((l) => l.key);
    state.effortMeta = Object.fromEntries(h.effort_levels.map((l) => [l.key, l]));
  }
  (h.models || []).forEach((m) => {
    if (m.key && m.effort) state.modelDefaults[m.key] = m.effort;
  });
  syncThinkToggle();
  $("#modelStatus").innerHTML = (h.models || [])
    .map((m) => {
      const cls = m.installed ? "ok" : "bad";
      return `<div class="${cls}">${m.installed ? "●" : "○"} ${m.label} <code>${m.id}</code></div>`;
    })
    .join("");
  $("#setCompact").value = Math.round((state.settings.auto_compact_at || 0.85) * 100);
  $("#setCompactVal").textContent = `${$("#setCompact").value}%`;
  $("#setShowThinking").checked = state.settings.show_thinking !== false;
  if ($("#setClarifyFirst")) $("#setClarifyFirst").checked = !!state.settings.clarify_first_turn;
  if ($("#setUnrestrictedFs")) $("#setUnrestrictedFs").checked = !!state.settings.unrestricted_fs;
  if ($("#setShellApproval")) {
    $("#setShellApproval").value =
      state.settings.shell_approval_mode ||
      (state.settings.confirm_shell_commands === false ? "never_ask" : "always_ask");
  }
  $("#setAgentRepeatLimit").value = state.settings.agent_repeat_limit || 3;
  if ($("#setChatCtx")) $("#setChatCtx").value = String(state.settings.chat_context || 32768);
  if ($("#setReasonCtx")) $("#setReasonCtx").value = String(state.settings.reasoning_context || 32768);
  if ($("#setCoderCtx")) $("#setCoderCtx").value = String(state.settings.coder_context || 32768);
  if ($("#setAgentCtx")) $("#setAgentCtx").value = String(state.settings.agent_context || 32768);
  state.webSearch = !!state.settings.web_search_default;
  syncWebSearchUI();
  syncModelPicker();
}

function syncWebSearchUI() {
  $("#webSearchCheck").hidden = !state.webSearch;
  $("#btnWebSearchToggle").classList.toggle("active", state.webSearch);
}

async function loadChats() {
  state.chats = await api("/api/chats");
  renderChatLists();
}

function renderChatLists() {
  const boxes = {
    chat: $("#chatListChat"),
    agentic: $("#chatListAgentic"),
  };
  Object.values(boxes).forEach((b) => (b.innerHTML = ""));
  state.chats.forEach((c) => {
    const item = document.createElement("div");
    item.className = "chat-item" + (c.id === state.chatId ? " active" : "");
    item.innerHTML = `
      <div class="row">
        <div class="t">${escapeHtml(c.title || "Untitled")}</div>
        <div class="item-actions">
          <button type="button" class="ren" title="Rename">✎</button>
          <button type="button" class="del" title="Delete">✕</button>
        </div>
      </div>
      <div class="p">${escapeHtml(c.preview || normalizeMode(c.mode))}</div>`;
    item.onclick = (e) => {
      if (e.target.closest(".item-actions")) return;
      openChat(c.id);
    };
    item.querySelector(".ren").onclick = async (e) => {
      e.stopPropagation();
      const title = await promptEdit({ title: "Rename chat", label: "Title", value: c.title || "", multiline: false });
      if (!title) return;
      await api(`/api/chats/${c.id}`, { method: "PATCH", body: JSON.stringify({ title }) });
      await loadChats();
    };
    item.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      const ok = await confirmAction({
        title: "Delete chat?",
        body: `Delete “${c.title || "Untitled"}”? This cannot be undone.`,
        ok: "Delete chat",
      });
      if (!ok) return;
      await api(`/api/chats/${c.id}`, { method: "DELETE" });
      if (state.chatId === c.id) {
        state.chatId = null;
        showEmpty();
      }
      await loadChats();
    };
    const bucket = normalizeMode(c.mode);
    (boxes[bucket] || boxes.chat).appendChild(item);
  });
}

function showEmpty() {
  $("#emptyState").hidden = false;
  $("#messages").hidden = true;
  $("#messages").innerHTML = "";
  updateEmpty();
  setContextMeter(0);
  renderAgentTodos([]);
  renderFileLibrary([]);
}

async function openChat(id) {
  const chat = await api(`/api/chats/${id}`);
  const nextMode = normalizeMode(chat.mode || "chat");
  const nextComputer = !!(chat.computer_use || chat.mode === "computer");
  // Chats saved before hybrid was removed fall back to the executor. A chat with
  // no agent_model at all carries no opinion, so the picker's current selection
  // stands. Overwriting it here is what reverted an explicit pick on a new chat.
  if (chat.agent_model) {
    state.agentModel = chat.agent_model === "agent" ? "agent" : "coder";
  }
  let nextKey = "general";
  if (nextMode === "agentic") {
    // Computer use needs vision and the server forces the agent for it, so
    // warming coder here loads the wrong 20GB of weights.
    nextKey = nextComputer ? "agent" : state.agentModel;
  } else {
    nextKey = chat.reasoning ? "reasoning" : "general";
  }

  // History switch across models must offload + warm just like the model picker
  if (state.warmedKey && state.warmedKey !== nextKey) {
    const ok = await ensureModelWarm(nextKey, {
      confirm: true,
      title: "Open chat with another model?",
    });
    if (!ok) return;
  } else if (!state.warmedKey) {
    await ensureModelWarm(nextKey, { confirm: false });
  }

  state.chatId = chat.id;
  state.mode = nextMode;
  state.computerUse = nextComputer;
  state.modelKey = nextKey;
  syncModeUI();
  renderMessages(chat.messages || []);
  renderAgentTodos(chat.agent_todos || []);
  renderFileLibrary(chat.file_library || []);
  renderChatLists();
  if (chat.project_id) {
    $("#projectSelect").value = chat.project_id;
    await loadProjectTree(chat.project_id);
  }
  await refreshContext();
}

function todoMark(status) {
  if (status === "completed") return "✓";
  if (status === "in_progress") return "●";
  if (status === "cancelled") return "–";
  return "";
}

function renderAgentTodos(todos) {
  const panel = $("#agentTodoPanel");
  const list = $("#agentTodoList");
  const countEl = $("#agentTodoCount");
  const fill = $("#agentTodoBarFill");
  if (!panel || !list) return;
  const items = Array.isArray(todos) ? todos : [];
  state.agentTodos = items;
  const finished =
    items.length > 0 &&
    items.every((t) => t.status === "completed" || t.status === "cancelled");
  // Keep a finished plan up while the run is still going so the last item is
  // visibly ticked off, then clear it. A completed plan from an old job is
  // stale UI, and it used to reappear on every reopen.
  if (!items.length || state.mode !== "agentic" || state.computerUse ||
      (finished && !state.streaming)) {
    panel.hidden = true;
    list.innerHTML = "";
    return;
  }
  const done = items.filter((t) => t.status === "completed" || t.status === "cancelled").length;
  const total = items.length;
  const pct = total ? Math.round((100 * done) / total) : 0;
  const active = items.find((t) => t.status === "in_progress");
  countEl.textContent = active
    ? `${done} of ${total} done · ${active.activeForm || active.content}`
    : `${done} of ${total} done`;
  fill.style.width = `${pct}%`;
  panel.classList.toggle("is-collapsed", !!state.agentTodoCollapsed);
  const toggle = $("#agentTodoToggle");
  if (toggle) toggle.setAttribute("aria-expanded", state.agentTodoCollapsed ? "false" : "true");
  list.innerHTML = items
    .map((t) => {
      const st = t.status || "pending";
      const label =
        st === "in_progress" && t.activeForm ? escapeHtml(t.activeForm) : escapeHtml(t.content || "");
      const statusLabel =
        st === "in_progress"
          ? `<span class="agent-todo-status-label">in progress</span>`
          : st === "pending"
            ? `<span class="agent-todo-status-label">pending</span>`
            : "";
      return `<li class="agent-todo-item is-${escapeHtml(st)}">
        <span class="agent-todo-mark">${todoMark(st)}</span>
        <div class="agent-todo-text">${label}${statusLabel}</div>
      </li>`;
    })
    .join("");
  panel.hidden = false;
}

function renderMessages(messages) {
  const box = $("#messages");
  box.innerHTML = "";
  if (!messages.length) {
    showEmpty();
    return;
  }
  $("#emptyState").hidden = true;
  box.hidden = false;
  messages.forEach((m) => appendMessage(m));
  $("#chatArea").scrollTop = $("#chatArea").scrollHeight;
}

function thinkingHtml(thinking, { open = false } = {}) {
  const show = $("#setShowThinking").checked !== false || (typeof isAgenticMode === "function" && isAgenticMode());
  if (!show || !thinking) return "";
  return `<details class="thinking"${open ? " open" : ""}>
    <summary>Thought process</summary>
    <div class="think-body"></div>
  </details>`;
}

function wireMsgActions(el, m, role) {
  if (!m.id || el.dataset.streaming || m.compact_notice) return;
  const canEdit = role === "user";
  const editBtn = el.querySelector(".edit");
  const delBtn = el.querySelector(".del");
  const copyBtn = el.querySelector(".user-copy-btn, .asst-copy-btn");
  if (editBtn && canEdit) {
    editBtn.onclick = (e) => {
      e.stopPropagation();
      beginInlineUserEdit(el, m);
    };
  }
  if (delBtn) {
    delBtn.onclick = async (e) => {
      e.stopPropagation();
      const ok = await confirmAction({
        title: "Delete message?",
        body: "Remove this message and everything after it in the chat?",
        ok: "Delete",
      });
      if (!ok) return;
      await api(`/api/chats/${state.chatId}/messages/${m.id}`, { method: "DELETE" });
      const chat = await api(`/api/chats/${state.chatId}`);
      renderMessages(chat.messages || []);
      await refreshContext();
    };
  }
  if (copyBtn) {
    copyBtn.onclick = (e) => {
      e.stopPropagation();
      copyMessageText(el, copyBtn);
    };
  }
}

function appendMessage(m, { streaming } = {}) {
  // Internal agent nudges / empty tool-only steps stay in history for the model,
  // but should not look like separate user turns in the UI.
  if (m.hidden || m.agent_nudge) return null;
  // Quiet agent: hide tool dumps and interim drafts. Work lives in the dropdown.
  if (m.role === "tool" || m.quiet || m.interim) return null;
  if (
    !streaming &&
    m.role === "assistant" &&
    !(m.content || "").trim() &&
    !(m.thinking || "").trim() &&
    (m.tool_calls || []).length
  ) {
    return null;
  }
  if (
    !streaming &&
    m.role === "assistant" &&
    !(m.content || "").trim() &&
    (m.thinking || "").trim()
  ) {
    // Thinking-only interim steps. The final message carries the log.
    return null;
  }
  $("#emptyState").hidden = true;
  $("#messages").hidden = false;
  const box = $("#messages");
  const role = m.role === "tool" ? "tool" : m.role === "user" ? "user" : "assistant";
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (streaming) el.dataset.streaming = "1";
  el.dataset.mid = m.id || "";

  const atts = (m.attachments || [])
    .map((a) => {
      const src = attachmentPreviewSrc(a);
      if (src) {
        return `<img class="att-thumb" src="${src}" alt="${escapeHtml(a.name || "image")}" data-full="${src}" />`;
      }
      return `<span class="chip">${escapeHtml(a.name || a.kind)}</span>`;
    })
    .join("");
  const attBlock = atts ? `<div class="attach-preview">${atts}</div>` : "";

  if (role === "user") {
    const actionBar =
      m.id && !streaming && !m.compact_notice
        ? `<div class="user-action-bar">
            <button type="button" class="user-action-btn edit" title="Edit & resubmit">Edit</button>
            <button type="button" class="user-action-btn del" title="Delete">Delete</button>
            <button type="button" class="user-action-btn user-copy-btn" title="Copy message">Copy</button>
          </div>`
        : "";
    el.innerHTML = `<div class="bubble"><div class="content"></div>${attBlock}${actionBar}</div>`;
    el.dataset.raw = m.content || "";
    fillContent(el.querySelector(".content"), m.content || "");
  } else if (role === "tool") {
    if ((m.name || "") === "todo_write") return null;
    el.classList.add("is-done");
    el.innerHTML = `<div class="asst-head"><div class="aether-ring idle" aria-hidden="true"></div></div>
      <div class="bubble"><div class="tool-card"><div class="name">${escapeHtml(m.name || "tool")}</div><pre>${escapeHtml(String(m.content || "").slice(0, 4000))}</pre></div></div>`;
  } else {
    const status = streaming ? pickStatusWord() : "";
    const ringClass = streaming ? "aether-ring spin" : "aether-ring idle";
    if (!streaming) el.classList.add("is-done");
    const copyBar = (!streaming && !m.compact_notice)
      ? `<div class="asst-copy-bar"><button type="button" class="asst-copy-btn" title="Copy response">Copy</button></div>`
      : "";
    el.innerHTML = `
      <div class="asst-head">
        <div class="${ringClass}" aria-hidden="true"></div>
        <span class="status-word"${streaming ? "" : " hidden"}>${escapeHtml(status)}</span>
      </div>
      <div class="bubble">
        ${thinkingHtml(m.thinking, { open: false })}
        <div class="content${streaming ? " streaming" : ""}"></div>
        ${attBlock}
        ${copyBar}
      </div>`;
    el.dataset.raw = m.content || "";
    fillContent(el.querySelector(".content"), m.content || "", { streaming: !!streaming });
    const tb = el.querySelector(".think-body");
    if (tb && m.thinking) tb.textContent = m.thinking;
  }

  fillRings(el);
  wireMsgActions(el, m, role);
  el.querySelectorAll("img.att-thumb").forEach((img) => {
    img.onclick = () => openLightbox(img.dataset.full || img.src);
  });
  box.appendChild(el);
  $("#chatArea").scrollTop = $("#chatArea").scrollHeight;
  return el;
}

function updateStreamingEl(el, { content, thinking }) {
  if (!el) return;
  const bubble = el.querySelector(".bubble");
  const contentEl = el.querySelector(".content");
  const status = el.querySelector(".status-word");
  const ring = el.querySelector(".aether-ring");
  const hasContent = !!(content && String(content).trim());

  // Surgical DOM updates. Rebuilding the whole bubble flickers.
  // Agentic quiet mode always surfaces the thinking dropdown while working.
  const showThink = $("#setShowThinking").checked !== false || (typeof isAgenticMode === "function" && isAgenticMode());
  if (thinking && showThink) {
    let details = bubble.querySelector("details.thinking");
    if (!details) {
      details = document.createElement("details");
      details.className = "thinking";
      details.open = true;
      details.innerHTML = `<summary>Thought process</summary><div class="think-body"></div>`;
      bubble.insertBefore(details, contentEl);
    }
    const tb = details.querySelector(".think-body");
    if (tb && tb.textContent !== thinking) {
      const nearBottom = tb.scrollHeight - tb.scrollTop - tb.clientHeight < 40;
      tb.textContent = thinking;
      if (nearBottom) tb.scrollTop = tb.scrollHeight;
    }
  }

  if (hasContent) {
    el.classList.add("is-done"); // Claude layout as soon as text appears
    if (status) status.hidden = true;
    if (ring) {
      ring.classList.remove("spin");
      ring.classList.add("idle");
      const spinEl = ring.querySelector(".ring-spin");
      if (spinEl) spinEl.style.transform = "";
    }
    if (contentEl) {
      el.dataset.raw = content || "";
      fillContent(contentEl, content, { streaming: true });
      if (!bubble.querySelector(".asst-copy-bar")) {
        const bar = document.createElement("div");
        bar.className = "asst-copy-bar";
        bar.innerHTML = `<button type="button" class="asst-copy-btn" title="Copy response">Copy</button>`;
        bubble.appendChild(bar);
        bar.querySelector(".asst-copy-btn").onclick = (e) => {
          e.stopPropagation();
          copyAssistantText(el, bar.querySelector(".asst-copy-btn"));
        };
      }
    }
  } else {
    el.classList.remove("is-done");
    if (status) status.hidden = false;
    if (ring) {
      ring.classList.add("spin");
      ring.classList.remove("idle");
    }
  }
  $("#chatArea").scrollTop = $("#chatArea").scrollHeight;
}

function newChatBody() {
  return {
    mode: isAgenticMode() ? "agentic" : "chat",
    title: isAgenticMode() ? (state.computerUse ? "Computer use" : "New agentic session") : "New chat",
    project_id: isAgenticMode() ? $("#projectSelect").value || null : null,
    agent_model: isAgenticMode() ? state.agentModel : null,
  };
}

async function ensureChat() {
  if (state.chatId) return state.chatId;
  const chat = await api("/api/chats", {
    method: "POST",
    body: JSON.stringify(newChatBody()),
  });
  state.chatId = chat.id;
  await patchChatModel(chat.id);
  await loadChats();
  return chat.id;
}

function setSendMode(mode) {
  syncCompactBtn();
  const btn = $("#btnSend");
  if (!btn) return;
  const stop = mode === "stop";
  btn.classList.toggle("is-stop", stop);
  btn.title = stop ? "Stop" : "Send";
  btn.setAttribute("aria-label", stop ? "Stop" : "Send");
  btn.disabled = false;
}

function finishStreamingUI() {
  state.streaming = false;
  state.abortController = null;
  stopPendingPoll();
  // The run is over, so the chip goes back to naming the selection.
  syncModelPicker();
  // Re-render the plan now that streaming has stopped: a finished one clears.
  renderAgentTodos(state.agentTodos || []);
  setSendMode("send");
  showCompact(false);
  document.querySelectorAll(".content.streaming").forEach((n) => n.classList.remove("streaming"));
  document.querySelectorAll(".thinking[data-live]").forEach((n) => n.removeAttribute("data-live"));
}

async function stopMessage() {
  if (!state.streaming) return;
  const cid = state.chatId;
  try {
    if (state.abortController) state.abortController.abort();
  } catch (_) {}
  if (cid) {
    try { await api(`/api/chats/${cid}/stop`, { method: "POST", body: "{}" }); } catch (_) {}
  }
  finishStreamingUI();
  setStatus("Stopped");
}

async function sendMessage() {
  if (state.streaming) {
    await stopMessage();
    return;
  }
  const text = $("#input").value.trim();
  if (!text && !state.attachments.length) return;
  $("#input").value = "";
  autosizeInput();

  const attachments = state.attachments.slice();
  state.attachments = [];
  renderAttachChips();

  await ensureChat();
  const userEl = appendMessage({
    role: "user",
    content: text || "(attachment)",
    attachments: attachments.map((a) => ({
      name: a.name,
      kind: a.kind,
      path: a.path || null,
      url: a.url || null,
      image_b64: a.image_b64 || null,
      has_image: !!(a.image_b64 || a.url || a.kind === "image" || a.kind === "screenshot"),
    })),
  });

  state.streaming = true;
  state.abortController = new AbortController();
  setSendMode("stop");
  startPendingPoll();
  const wantsSearch =
    state.webSearch ||
    /\b(weather|temperature|forecast|humidity|rain(?:ing)?|snow(?:ing)?|score|price|news|latest|who won)\b/i.test(
      text || ""
    ) ||
    /^\s*((hey|hi|hello|ok|okay|please)[,!]?\s+)*((can|could|would|will)\s+you\s+)?(please\s+)?(search|look(\s+it)?\s+up|google|check online)\b/i.test(
      text || ""
    );
  setStatus(wantsSearch ? "Searching the web…" : "Loading…");
  let streamEl = appendMessage({ role: "assistant", content: "", thinking: "", model: "…" }, { streaming: true });
  let unlocked = false;

  try {
    const res = await fetch(`/api/chats/${state.chatId}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: state.abortController.signal,
      body: JSON.stringify({
        content: text || "Please review the attachments.",
        model_key: currentModelKey(),
        agent_model: isAgenticMode() ? state.agentModel : null,
        think: !!(modelSupportsThink() && state.think),
        effort: currentEffort(),
        reasoning: currentModelKey() === "reasoning",
        computer_use: isAgenticMode() && state.computerUse,
        project_id: isAgenticMode() ? $("#projectSelect").value || null : null,
        web_search: state.webSearch,
        attachments,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        let ev;
        try {
          ev = JSON.parse(line.slice(5).trim());
        } catch (_) {
          continue;   // one unreadable frame must not end the stream
        }
        if (ev.type === "user_message" && ev.message && userEl) {
          userEl.dataset.mid = ev.message.id || "";
          // Refresh thumbs from server (urls) so every image is expandable
          const preview = userEl.querySelector(".attach-preview");
          const serverAtts = ev.message.attachments || [];
          if (serverAtts.length) {
            const html = serverAtts
              .map((a) => {
                const src = attachmentPreviewSrc(a);
                if (src) {
                  return `<img class="att-thumb" src="${src}" alt="${escapeHtml(a.name || "image")}" data-full="${src}" />`;
                }
                return `<span class="chip">${escapeHtml(a.name || a.kind)}</span>`;
              })
              .join("");
            if (preview) {
              preview.innerHTML = html;
            } else if (html) {
              const bubble = userEl.querySelector(".bubble");
              if (bubble) {
                const div = document.createElement("div");
                div.className = "attach-preview";
                div.innerHTML = html;
                bubble.appendChild(div);
              }
            }
            userEl.querySelectorAll("img.att-thumb").forEach((img) => {
              img.onclick = () => openLightbox(img.dataset.full || img.src);
            });
          }
          // re-wire bottom actions now that we have an id
          const m = ev.message;
          if (!userEl.querySelector(".user-action-bar") && m.id) {
            const bubble = userEl.querySelector(".bubble") || userEl;
            const actions = document.createElement("div");
            actions.className = "user-action-bar";
            actions.innerHTML = `
              <button type="button" class="user-action-btn edit" title="Edit & resubmit">Edit</button>
              <button type="button" class="user-action-btn del" title="Delete">Delete</button>
              <button type="button" class="user-action-btn user-copy-btn" title="Copy message">Copy</button>`;
            bubble.appendChild(actions);
            userEl.dataset.raw = m.content || userEl.dataset.raw || "";
            wireMsgActions(userEl, m, "user");
          }
        }
        streamEl = handleSSE(ev, streamEl) || streamEl;
        if (ev.type === "done" || ev.type === "stopped" || ev.type === "error") {
          if (!unlocked) {
            unlocked = true;
            finishStreamingUI();
          }
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      setStatus("Stopped");
    } else {
      setStatus(e.message);
      updateStreamingEl(streamEl, { content: `⚠️ ${e.message}`, thinking: "" });
    }
  } finally {
    if (!unlocked) finishStreamingUI();
    await loadChats();
    await refreshContext();
    if (state.chatId) {
      try {
        const chat = await api(`/api/chats/${state.chatId}`);
        renderFileLibrary(chat.file_library || []);
        renderAgentTodos(chat.agent_todos || []);
      } catch {}
    }
    // auto-title finishes in the background after the stream closes
    setTimeout(() => { loadChats().catch(() => {}); }, 2500);
  }
}

function handleSSE(ev, streamEl) {
  if (ev.type === "memory_saved") {
    setStatus("Saved to lasting memory");
    loadMemory();
  } else if (ev.type === "memory_recall") {
    setStatus(ev.hits ? `Recalled ${ev.hits} memor${ev.hits === 1 ? "y" : "ies"}` : "No lasting memory matched");
  } else if (ev.type === "file_router") {
    const names = (ev.names || []).slice(0, 3).join(", ") || "files";
    setStatus(`Opening ${names}…`);
  } else if (ev.type === "compacting") {
    showCompact(true);
    setStatus(ev.deferred ? "Compacting for next turn…" : "Compacting conversation…");
  } else if (ev.type === "compacted") {
    showCompact(false);
    setStatus(ev.deferred ? "Ready for next message" : "Compacted, continuing");
    if (ev.context) setContextMeter(ev.context.pct);
    if (Array.isArray(ev.messages)) {
      const notice = {
        role: "assistant",
        content: "Conversation compacted to free context. Earlier details stay available for this chat.",
        model: "aether",
        compact_notice: true,
      };
      renderMessages([notice, ...ev.messages]);
    }
    // Resume streaming bubble if we compacted mid-turn (before the model reply)
    if (!ev.deferred && state.streaming) {
      return appendMessage({ role: "assistant", content: "", thinking: "", model: "…" }, { streaming: true });
    }
    return null;
  } else if (ev.type === "status") {
    showCompact(false);
    setStatus(`Using ${ev.model.label}`);
    if (ev.key) {
      state.modelKey = ev.key;
      state.warmedKey = ev.key;
      syncModelPicker();
    }
  } else if (ev.type === "delta") {
    showCompact(false);
    const quiet = !!ev.quiet || (ev.thinking && !ev.content);
    setStatus(quiet ? "Thinking…" : ev.thinking && !ev.content ? "Thinking…" : "Generating…");
    // Only open an assistant bubble when real text arrives, not between tools.
    if (!streamEl || !streamEl.isConnected) {
      streamEl = appendMessage({ role: "assistant", content: "", thinking: "", model: "…" }, { streaming: true });
    }
    // Quiet agent: stream thinking into the dropdown; keep main content empty until debrief.
    updateStreamingEl(streamEl, {
      content: ev.quiet ? (streamEl.dataset.raw || "") : ev.content,
      thinking: ev.thinking,
    });
    if (ev.quiet && ev.thinking) {
      const details = streamEl.querySelector("details.thinking");
      if (details) {
        details.open = true;
        details.setAttribute("data-live", "1");
      }
      const status = streamEl.querySelector(".status-word");
      if (status) {
        status.hidden = false;
        status.textContent = "Working…";
      }
      const ring = streamEl.querySelector(".aether-ring");
      if (ring) {
        ring.classList.add("spin");
        ring.classList.remove("idle");
      }
    }
  } else if (ev.type === "message") {
    const msg = ev.message || {};
    const toolOnly =
      !(msg.content || "").trim() &&
      !(msg.thinking || "").trim() &&
      (msg.tool_calls || []).length > 0;
    const interim = !!msg.interim || (!(msg.content || "").trim() && !!(msg.thinking || "").trim());
    // Tool-only / interim steps stay invisible as assistant bubbles (one continuous turn).
    if (toolOnly || interim) {
      if (streamEl && streamEl.isConnected && msg.thinking) {
        updateStreamingEl(streamEl, { content: "", thinking: msg.thinking });
        const details = streamEl.querySelector("details.thinking");
        if (details) {
          details.open = true;
          details.setAttribute("data-live", "1");
        }
      }
      return streamEl && streamEl.isConnected ? streamEl : null;
    }
    if (streamEl && streamEl.isConnected) {
      streamEl.removeAttribute("data-streaming");
      streamEl.classList.add("is-done");
      const ring = streamEl.querySelector(".aether-ring");
      const status = streamEl.querySelector(".status-word");
      if (ring) {
        ring.classList.remove("spin");
        ring.classList.add("idle");
        const spinEl = ring.querySelector(".ring-spin");
        if (spinEl) spinEl.style.transform = "";
      }
      if (status) status.hidden = true;
      streamEl.querySelector(".content")?.classList.remove("streaming");
      const liveThink = streamEl.querySelector("details.thinking");
      if (liveThink) liveThink.removeAttribute("data-live");
      streamEl.remove();
      appendMessage(msg);
    } else {
      appendMessage(msg);
    }
    return null;
  } else if (ev.type === "context") {
    setContextMeter(ev.pct, ev);
    return streamEl && streamEl.isConnected ? streamEl : null;
  } else if (ev.type === "shell_approval" || ev.type === "ask_user") {
    setStatus(ev.type === "ask_user" ? "Waiting for you…" : "Waiting for approval…");
    queuePrompt(ev);
    return streamEl && streamEl.isConnected ? streamEl : null;
  } else if (ev.type === "shell_approval_done" || ev.type === "ask_user_done") {
    // Also fires when the prompt expired server-side, so the card must go even
    // though nobody answered it here.
    const kind = ev.type === "ask_user_done" ? "ask_user" : "shell_approval";
    dropPrompt((p) => p.kind === kind);
    setStatus(ev.cancelled ? (kind === "ask_user" ? "Assuming defaults…" : "Command denied…") : "Working…");
    return streamEl && streamEl.isConnected ? streamEl : null;
  } else if (ev.type === "tool_start") {
    setStatus(ev.name === "todo_write" ? "Updating plan…" : `${ev.name}…`);
  } else if (ev.type === "todos") {
    renderAgentTodos(ev.todos || []);
    const open = Number(ev.open || 0);
    const done = Number(ev.done || 0);
    const total = Number(ev.total || 0);
    if (total) setStatus(open ? `Plan ${done}/${total}` : `Plan complete (${total})`);
  } else if (ev.type === "project_tree") {
    if (ev.project_id && $("#projectSelect").value === ev.project_id) {
      loadProjectTree(ev.project_id);
    }
    return streamEl && streamEl.isConnected ? streamEl : null;
  } else if (ev.type === "tool_result") {
    // Quiet agent: tool dumps stay out of the transcript (activity is in thinking).
    if (ev.summary && streamEl && streamEl.isConnected) {
      const tb = streamEl.querySelector(".think-body");
      const prev = tb ? tb.textContent : "";
      updateStreamingEl(streamEl, {
        content: "",
        thinking: prev && !prev.includes(ev.summary) ? `${prev}\n${ev.summary}` : prev || ev.summary,
      });
    }
    return streamEl && streamEl.isConnected ? streamEl : null;
  } else if (ev.type === "title") {
    if (ev.title && (!state.chatId || state.chatId === ev.chat_id)) {
      loadChats();
    }
  } else if (ev.type === "done") {
    setStatus("");
    if (ev.context) setContextMeter(ev.context.pct);
    if (streamEl && streamEl.isConnected && !(streamEl.querySelector(".content")?.textContent || "").trim()) {
      // Keep bubble if it has thinking (user can expand); otherwise remove empty spinner.
      const hasThink = !!(streamEl.querySelector(".think-body")?.textContent || "").trim();
      if (!hasThink) streamEl.remove();
      else {
        streamEl.removeAttribute("data-streaming");
        streamEl.classList.add("is-done");
        const liveThink = streamEl.querySelector("details.thinking");
        if (liveThink) liveThink.removeAttribute("data-live");
      }
    }
    return null;
  } else if (ev.type === "stopped") {
    setStatus(ev.interrupted ? "Stopped. Plan kept, send a correction to continue." : "Stopped");
    if (ev.todos) renderAgentTodos(ev.todos.todos || ev.todos);
    if (streamEl && streamEl.isConnected) {
      streamEl.removeAttribute("data-streaming");
      const liveThink = streamEl.querySelector("details.thinking");
      if (liveThink) liveThink.removeAttribute("data-live");
      const status = streamEl.querySelector(".status-word");
      if (status) {
        status.hidden = false;
        status.textContent = "Stopped";
      }
      const ring = streamEl.querySelector(".aether-ring");
      if (ring) {
        ring.classList.remove("spin");
        ring.classList.add("idle");
      }
    }
    return null;
  } else if (ev.type === "error") {
    showCompact(false);
    setStatus(ev.message);
    if (streamEl && streamEl.isConnected) updateStreamingEl(streamEl, { content: `⚠️ ${ev.message}`, thinking: "" });
  }
  return streamEl;
}

async function newChat() {
  const chat = await api("/api/chats", {
    method: "POST",
    body: JSON.stringify(newChatBody()),
  });
  await patchChatModel(chat.id);
  state.chatId = chat.id;
  await loadChats();
  showEmpty();
  await openChat(chat.id);
  $("#input").focus();
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  const list = $("#projectList");
  const sel = $("#projectSelect");
  list.innerHTML = "";
  sel.innerHTML = `<option value="">No project</option>`;
  state.projects.forEach((p) => {
    const b = document.createElement("div");
    b.className = "project-item";
    b.tabIndex = 0;
    b.dataset.id = p.id;
    if ($("#projectSelect").value === p.id) b.classList.add("active");
    b.innerHTML = `<div class="row"><div class="t"><span class="folder-ico" aria-hidden="true">📁</span><span class="name">${escapeHtml(p.name)}</span></div>
      <div class="item-actions">
        <button type="button" class="ren" title="Rename">✎</button>
        <button type="button" class="del" title="Delete">✕</button>
      </div></div>
      <div class="p">${escapeHtml(p.root)}</div>`;
    b.onclick = async (e) => {
      if (e.target.closest(".item-actions")) return;
      sel.value = p.id;
      $$(".project-item").forEach((el) => el.classList.toggle("active", el.dataset.id === p.id));
      await loadProjectTree(p.id);
      if (state.chatId) {
        await api(`/api/chats/${state.chatId}`, { method: "PATCH", body: JSON.stringify({ project_id: p.id }) });
      }
    };
    b.querySelector(".ren").onclick = async (e) => {
      e.stopPropagation();
      const name = await promptEdit({ title: "Rename project", label: "Name", value: p.name, multiline: false });
      if (!name) return;
      await api(`/api/projects/${p.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
      await loadProjects();
    };
    b.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      const ok = await confirmAction({
        title: "Delete project?",
        body: `Remove project “${p.name}”? Chats stay; only the project link is deleted.`,
        ok: "Delete project",
      });
      if (!ok) return;
      await api(`/api/projects/${p.id}`, { method: "DELETE" });
      await loadProjects();
    };
    list.appendChild(b);
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
}

async function loadProjectTree(pid) {
  if (!pid) {
    $("#projectTree").innerHTML = `<div class="entry">Select a project</div>`;
    return;
  }
  try {
    const tree = await api(`/api/projects/${pid}/tree`);
    $("#projectTree").innerHTML =
      (tree.entries || []).map((e) => `<div class="entry">${escapeHtml(e)}</div>`).join("") ||
      `<div class="entry">(empty)</div>`;
  } catch (e) {
    $("#projectTree").innerHTML = `<div class="entry">${escapeHtml(e.message)}</div>`;
  }
}

async function loadMemory() {
  const mem = await api("/api/memory/global");
  const box = $("#memoryList");
  box.innerHTML = "";
  (mem.notes || []).forEach((n) => {
    const el = document.createElement("div");
    el.className = "memory-item";
    el.innerHTML = `<div class="t">${escapeHtml(n.text)}</div>
      <div class="item-actions">
        <button type="button" class="ren" title="Edit">✎</button>
        <button type="button" class="del" title="Delete">✕</button>
      </div>`;
    el.querySelector(".ren").onclick = async () => {
      const text = await promptEdit({ title: "Edit memory", label: "Note", value: n.text, multiline: true });
      if (!text) return;
      await api(`/api/memory/global/${n.id}`, { method: "PATCH", body: JSON.stringify({ text }) });
      loadMemory();
    };
    el.querySelector(".del").onclick = async () => {
      const ok = await confirmAction({
        title: "Delete memory?",
        body: "Remove this lasting note? The model will no longer see it.",
        ok: "Delete memory",
      });
      if (!ok) return;
      await api(`/api/memory/global/${n.id}`, { method: "DELETE" });
      loadMemory();
    };
    box.appendChild(el);
  });
}

function autosizeInput() {
  const ta = $("#input");
  ta.style.height = "auto";
  ta.style.height = Math.min(180, ta.scrollHeight) + "px";
}

async function setComputerOverlay(enabled) {
  try {
    await api("/api/computer-overlay", { method: "POST", body: JSON.stringify({ enabled: !!enabled }) });
  } catch {}
  document.body.classList.toggle("cu-active", !!enabled);
}

function closePlus() {
  $("#plusMenu").hidden = true;
  $("#btnPlus").classList.remove("open");
}

async function addFiles(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    const up = await api("/api/upload", { method: "POST", body: fd });
    state.attachments.push({
      name: up.name,
      kind: up.kind,
      path: up.path,
      url: up.url || null,
      size: up.size || null,
      image_b64: up.image_b64 || null,
    });
  }
  renderAttachChips();
}

async function addClipboardImages(items) {
  const files = [];
  for (const item of items || []) {
    if (!item || item.kind !== "file") continue;
    const type = item.type || "";
    if (!type.startsWith("image/")) continue;
    const file = item.getAsFile ? item.getAsFile() : null;
    if (file) files.push(file);
  }
  if (!files.length) return false;
  await addFiles(files);
  setStatus(files.length === 1 ? "Image pasted" : `${files.length} images pasted`);
  return true;
}

function closeModelMenus() {
  $("#modelMenu").hidden = true;
  $("#modelTrigger").classList.remove("open");
  $("#modelTrigger").setAttribute("aria-expanded", "false");
}

function wireUI() {
  const todoToggle = $("#agentTodoToggle");
  if (todoToggle) {
    todoToggle.onclick = () => {
      state.agentTodoCollapsed = !state.agentTodoCollapsed;
      renderAgentTodos(state.agentTodos || []);
    };
  }

  $$(".side-tab").forEach((tab) => {
    tab.onclick = () => {
      $$(".side-tab").forEach((t) => t.classList.remove("active"));
      $$(".side-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $(`#panel-${tab.dataset.panel}`).classList.add("active");
    };
  });

  $$(".mode-tab").forEach((tab) => {
    tab.onclick = async () => {
      const nextMode = tab.dataset.mode;
      if (normalizeMode(nextMode) === normalizeMode(state.mode)) return;

      if (nextMode === "agentic") {
        // currentModelKey() reads state.mode, still "chat" here. Without the
        // override it warms the chat model and leaves the agentic one cold.
        const key = currentModelKey("agentic");
        const ok = await ensureModelWarm(key, { confirm: true, title: "Switch to Agentic?" });
        if (!ok) return;
        state.mode = "agentic";
        state.modelKey = key;
      } else {
        const key = state.modelKey === "reasoning" ? "reasoning" : "general";
        const ok = await ensureModelWarm(key, { confirm: true, title: "Switch to Chat?" });
        if (!ok) return;
        state.mode = "chat";
        state.computerUse = false;
        if (state.modelKey === "coder" || state.modelKey === "agent") state.modelKey = "general";
      }
      state.chatId = null;
      syncModeUI();
      showEmpty();
      renderChatLists();
    };
  });

  $("#modelTrigger").onclick = (e) => {
    e.stopPropagation();
    const menu = $("#modelMenu");
    const open = menu.hidden;
    closePlus();
    menu.hidden = !open;
    $("#modelTrigger").classList.toggle("open", open);
    $("#modelTrigger").setAttribute("aria-expanded", open ? "true" : "false");
  };

  $$(".model-option").forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      await selectModel(btn.dataset.model);
    };
  });

  const effortSlider = $("#effortSlider");
  if (effortSlider) {
    // stopPropagation so dragging the slider does not close the model menu
    effortSlider.addEventListener("click", (e) => e.stopPropagation());
    effortSlider.addEventListener("input", (e) => {
      e.stopPropagation();
      const idx = Number(e.target.value);
      state.effort = state.effortLevels[idx] || null;
      try {
        localStorage.setItem("aether.effort", state.effort || "");
      } catch (_) {}
      syncThinkToggle();
    });
  }

  const thinkBtn = $("#btnThinkToggle");
  if (thinkBtn) {
    thinkBtn.onclick = (e) => {
      e.stopPropagation();
      state.think = !state.think;
      try {
        localStorage.setItem("aether.think", state.think ? "1" : "0");
      } catch (_) {}
      syncThinkToggle();
    };
  }

  $("#btnComputerUse").onclick = async (e) => {
    e.stopPropagation();
    const next = !state.computerUse;
    // currentModelKey() still sees computerUse=true when turning it off, so it
    // reports "agent" and warms the model we are switching away from.
    const key = next ? "agent" : state.agentModel === "agent" ? "agent" : "coder";
    const ok = await ensureModelWarm(key, {
      confirm: true,
      title: next ? "Enable computer use?" : "Switch to coder?",
    });
    if (!ok) return;
    state.computerUse = next;
    state.modelKey = key;
    state.mode = "agentic";
    if (next) state.agentModel = "agent";
    syncModeUI();
    await setComputerOverlay(state.computerUse);
    await patchChatModel();
  };

  $("#btnNewChat").onclick = newChat;
  $("#btnSend").onclick = sendMessage;
  $("#btnToggleSidebar").onclick = () => $("#app").classList.toggle("sidebar-collapsed");

  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!state.streaming) sendMessage();
    }
  });
  $("#input").addEventListener("input", autosizeInput);

  $("#btnPlus").onclick = (e) => {
    e.stopPropagation();
    const menu = $("#plusMenu");
    const open = menu.hidden;
    closeModelMenus();
    menu.hidden = !open;
    $("#btnPlus").classList.toggle("open", open);
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".plus-wrap")) closePlus();
    if (!e.target.closest(".model-picker")) closeModelMenus();
  });

  $("#plusMenu").onclick = async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    closePlus();
    if (action === "files") {
      try {
        const picked = await api("/api/pick-files", { method: "POST", body: "{}" });
        const paths = picked.paths || [];
        if (paths.length) {
          for (const path of paths) {
            try {
              const up = await api("/api/ingest-path", {
                method: "POST",
                body: JSON.stringify({ path }),
              });
              state.attachments.push({
                name: up.name,
                kind: up.kind,
                path: up.path,
                url: up.url || null,
                size: up.size || null,
                image_b64: up.image_b64 || null,
              });
            } catch {
              const name = path.split(/[/\\]/).pop();
              const isImg = /\.(png|jpe?g|gif|webp|bmp)$/i.test(name);
              state.attachments.push({ name, kind: isImg ? "image" : "file", path });
            }
          }
          renderAttachChips();
          setStatus(`${paths.length} attached`);
        } else {
          $("#filePicker").click();
        }
      } catch {
        $("#filePicker").click();
      }
    }
    if (action === "attach-folder") {
      try {
        const picked = await api("/api/pick-folder", { method: "POST", body: "{}" });
        if (picked.ok && picked.path) {
          const name = picked.path.split(/[/\\]/).pop() || picked.path;
          state.attachments.push({ name, kind: "folder", path: picked.path });
          renderAttachChips();
          setStatus("Folder attached");
        }
      } catch (err) {
        setStatus(err.message);
      }
    }
    if (action === "screenshot") {
      setStatus("Capturing…");
      try {
        const shot = await api("/api/screenshot", { method: "POST", body: "{}" });
        state.attachments.push({
          name: shot.name,
          kind: "screenshot",
          path: shot.path,
          url: shot.url || null,
          image_b64: shot.image_b64,
        });
        renderAttachChips();
        setStatus("Screenshot attached");
        if (state.mode === "chat") {
          // Computer vision is strongest on Computer tab / VL model
        }
      } catch (err) {
        setStatus(err.message);
      }
    }
    if (action === "folder") {
      const form = $("#projectDialogForm");
      if (form) form.reset();
      $("#projectDialog").showModal();
    }
    if (action === "websearch") {
      state.webSearch = !state.webSearch;
      syncWebSearchUI();
    }
  };

  $("#filePicker").onchange = async (e) => {
    const files = [...(e.target.files || [])];
    e.target.value = "";
    if (!files.length) return;
    try {
      await addFiles(files);
      setStatus(`${files.length} attached`);
    } catch (err) {
      setStatus(err.message);
    }
  };

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "u") {
      e.preventDefault();
      $("#filePicker").click();
    }
  });

  $("#memoryForm").onsubmit = async (e) => {
    e.preventDefault();
    const text = $("#memoryInput").value.trim();
    if (!text) return;
    await api("/api/memory/global", { method: "POST", body: JSON.stringify({ text }) });
    $("#memoryInput").value = "";
    loadMemory();
  };

  $("#btnNewProject").onclick = () => {
    const form = $("#projectDialogForm");
    if (form) form.reset();
    $("#btnNewProject").blur();
    $("#projectDialog").showModal();
  };
  const browseBtn = $("#btnBrowseProject");
  if (browseBtn) {
    browseBtn.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      browseBtn.disabled = true;
      try {
        const picked = await api("/api/pick-folder", { method: "POST", body: "{}" });
        if (picked.ok && picked.path) {
          const root = $("#projectRootInput");
          if (root) root.value = picked.path;
          const nameInput = document.querySelector('#projectDialogForm input[name="name"]');
          if (nameInput && !nameInput.value) {
            nameInput.value = picked.path.split(/[/\\]/).pop() || "Project";
          }
        }
      } catch (err) {
        setStatus(err.message || "Folder picker failed");
        alert(err.message || "Folder picker failed");
      } finally {
        browseBtn.disabled = false;
      }
    };
  }
  const projectCancel = $("#btnProjectCancel");
  if (projectCancel) {
    projectCancel.onclick = (e) => {
      e.preventDefault();
      const dlg = $("#projectDialog");
      const form = $("#projectDialogForm");
      if (form) form.reset();
      if (dlg) dlg.close();
    };
  }
  $("#btnProjectOk").onclick = async (e) => {
    e.preventDefault();
    const form = $("#projectDialogForm");
    if (!form.reportValidity()) return;
    const fd = new FormData(form);
    try {
      await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({
          name: fd.get("name"),
          root: fd.get("root"),
          description: fd.get("description") || "",
        }),
      });
      $("#projectDialog").close();
      form.reset();
      await loadProjects();
    } catch (err) {
      alert(err.message);
    }
  };

  $("#projectSelect").onchange = async (e) => {
    const pid = e.target.value || null;
    await loadProjectTree(pid);
    if (state.chatId) {
      await api(`/api/chats/${state.chatId}`, { method: "PATCH", body: JSON.stringify({ project_id: pid }) });
    }
  };

  const ctxMeter = $("#contextMeter");
  if (ctxMeter) {
    ctxMeter.onclick = (e) => {
      e.stopPropagation();
      const wrap = $("#ctxWrap");
      if (wrap.classList.contains("open")) closeCtxDropdown();
      else openCtxDropdown();
    };
  }
  // A mouse click leaves focus on the button and :focus-within then holds the
  // hover bar open indefinitely. Keyboard activation reports detail 0 and keeps
  // its focus, since that rule exists so the bar is reachable without a mouse.
  // Capture phase, because the handlers call stopPropagation.
  document.addEventListener(
    "click",
    (e) => {
      if (!e.detail) return;
      const btn = e.target.closest && e.target.closest(".user-action-btn, .asst-copy-btn");
      if (btn) setTimeout(() => btn.blur(), 0);
    },
    true
  );

  const compactBtn = $("#btnCompactNow");
  if (compactBtn) compactBtn.onclick = (e) => { e.stopPropagation(); compactNow(); };

  const ctxSlider = $("#ctxSlider");
  if (ctxSlider) {
    ctxSlider.oninput = () => {
      const key = currentModelKey();
      const steps = CTX_STEPS[key] || CTX_STEPS.general;
      const idx = Number(ctxSlider.value);
      const limit = steps[idx];
      const used = Number(_ctxMeta.used || 0);
      const usedLabel = used < 1024 ? `${used} tok` : fmtCtx(used);
      $("#ctxDdVal").textContent = fmtCtx(limit);
      $("#ctxDdModel").textContent = `${MODEL_META[key]?.short || key} · ${usedLabel} / ${fmtCtx(limit)}`;
    };
    ctxSlider.onchange = () => applyCtxSlider();
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#ctxWrap")) closeCtxDropdown();
  });

  $("#setCompact").oninput = (e) => {
    $("#setCompactVal").textContent = `${e.target.value}%`;
  };
  $("#btnSaveSettings").onclick = async () => {
    await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({
        data: {
          auto_compact_at: Number($("#setCompact").value) / 100,
          show_thinking: $("#setShowThinking").checked,
          clarify_first_turn: !!($("#setClarifyFirst") || {}).checked,
          unrestricted_fs: !!($("#setUnrestrictedFs") || {}).checked,
          shell_approval_mode: ($("#setShellApproval") || {}).value || "safe_auto",
          agent_repeat_limit: Number($("#setAgentRepeatLimit").value),
          chat_context: Number($("#setChatCtx").value),
          reasoning_context: Number($("#setReasonCtx").value),
          coder_context: Number($("#setCoderCtx").value),
          agent_context: Number(($("#setAgentCtx") || {}).value || 32768),
          web_search_default: state.webSearch,
        },
      }),
    });
    setStatus("Saved");
  };

  window.addEventListener("resize", moveModeInk);

  const lbClose = $("#lightboxClose");
  if (lbClose) lbClose.onclick = closeLightbox;
  const lb = $("#imageLightbox");
  if (lb) lb.onclick = (e) => { if (e.target === lb) closeLightbox(); };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
  });

  // Paste images from clipboard (Ctrl+V / Cmd+V)
  async function handlePasteImages(e) {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items || !items.length) return;
    const hasImage = [...items].some((it) => it.kind === "file" && (it.type || "").startsWith("image/"));
    if (!hasImage) return;
    e.preventDefault();
    try {
      await addClipboardImages(items);
    } catch (err) {
      setStatus(err.message || "Paste failed");
    }
  }
  $("#input")?.addEventListener("paste", handlePasteImages);
  document.addEventListener("paste", (e) => {
    // If focus isn't the textarea, still accept image pastes into the composer
    if (e.target === $("#input")) return;
    handlePasteImages(e);
  });

  // Drag & drop files anywhere in the app
  let dragDepth = 0;
  const dropHint = document.createElement("div");
  dropHint.className = "drop-overlay";
  dropHint.textContent = "Drop files to attach";
  dropHint.hidden = true;
  const workspace = document.querySelector(".workspace") || $("#app");
  if (workspace) workspace.appendChild(dropHint);

  window.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth += 1;
    $("#app")?.classList.add("dragging");
    dropHint.hidden = false;
  });
  window.addEventListener("dragleave", (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) {
      $("#app")?.classList.remove("dragging");
      dropHint.hidden = true;
    }
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", async (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("#app")?.classList.remove("dragging");
    dropHint.hidden = true;
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    try {
      await addFiles(files);
      setStatus(`${files.length} attached`);
    } catch (err) {
      setStatus(err.message);
    }
  });
}

function setWarmSub(msg) {
  const el = $("#warmSub");
  if (el) el.textContent = msg || "";
}

async function dismissWarmSplash() {
  const splash = $("#warmSplash");
  const app = $("#app");
  document.body.classList.remove("is-warming");
  if (app) {
    app.classList.remove("app-hidden");
    // Force a frame so the opacity transition actually runs
    void app.offsetWidth;
    app.classList.add("app-ready");
  }
  if (!splash) return;
  splash.classList.add("is-leaving");
  await new Promise((r) => setTimeout(r, 580));
  splash.remove();
}

async function waitForWarmup() {
  setWarmSub("Starting warmup…");
  // Wait until API is actually reachable (fresh server after desktop restart)
  for (let i = 0; i < 60; i++) {
    try {
      const st = await fetch("/api/warmup/status").then((r) => r.json());
      if (st && (st.status || st.ready != null)) break;
    } catch (_) {}
    setWarmSub("Starting server…");
    await new Promise((r) => setTimeout(r, 250));
  }
  const poll = setInterval(async () => {
    try {
      const st = await fetch("/api/warmup/status").then((r) => r.json());
      if (st.label) {
        setWarmSub(`Warming ${st.label}…`);
      } else if (st.status === "warming") {
        setWarmSub("Loading chat model into VRAM…");
      }
    } catch (_) {}
  }, 700);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 900000);
  try {
    const res = await fetch("/api/warmup", { signal: ctrl.signal });
    if (!res.ok) throw new Error(`Warmup HTTP ${res.status}`);
    const data = await res.json();
    if (!data.ok) {
      setWarmSub(data.error ? `Warmup issue: ${data.error}` : "Warmup incomplete");
      await new Promise((r) => setTimeout(r, 900));
    } else {
      state.warmedKey = "general";
      state.modelKey = "general";
      setWarmSub("Ready");
      await new Promise((r) => setTimeout(r, 280));
    }
  } catch (e) {
    setWarmSub(`Warmup failed: ${e.message || "retry on next launch"}`);
    await new Promise((r) => setTimeout(r, 1200));
  } finally {
    clearTimeout(timer);
    clearInterval(poll);
  }
}

async function boot() {
  fillRings(document);
  wireUI();
  syncModeUI();
  refreshTagline();
  showEmpty();
  try {
    // Warm model (splash) in parallel with loading local UI data
    const warm = waitForWarmup();
    await Promise.all([loadHealth(), loadChats(), loadProjects(), loadMemory()]);
    await warm;
    requestAnimationFrame(moveModeInk);
  } catch (e) {
    setStatus(e.message);
    setWarmSub(e.message || "Something went wrong");
    await new Promise((r) => setTimeout(r, 600));
  }
  await dismissWarmSplash();
}

boot();
