let currentSessionId = null;
let currentSessionData = null;
let isProcessing = false;

// DOM Elements
const sessionForm = document.getElementById("session-form");
const topicInput = document.getElementById("topic-input");
const roundsSelect = document.getElementById("rounds-select");
const btnCreate = document.getElementById("btn-create");
const btnAdvance = document.getElementById("btn-advance");
const btnAutoRun = document.getElementById("btn-autorun");
const btnRetry = document.getElementById("btn-retry");
const btnAccept = document.getElementById("btn-accept");
const btnReject = document.getElementById("btn-reject");
const decisionNotes = document.getElementById("decision-notes");
const decisionResult = document.getElementById("decision-result");

const headerStatus = document.getElementById("header-status");
const ribbonTopic = document.getElementById("ribbon-topic");
const ribbonRound = document.getElementById("ribbon-round");
const ribbonMaxRounds = document.getElementById("ribbon-max-rounds");
const ribbonSpeaker = document.getElementById("ribbon-speaker");

const sessionMeta = document.getElementById("session-meta");
const recentList = document.getElementById("recent-sessions-list");
const messagesStream = document.getElementById("messages-stream");
const messageCount = document.getElementById("message-count");
const verdictSection = document.getElementById("verdict-section");

// Init
document.addEventListener("DOMContentLoaded", () => {
  loadRecentSessions();
  setupEventListeners();
});

function setupEventListeners() {
  sessionForm.addEventListener("submit", handleCreateSession);
  btnAdvance.addEventListener("click", handleAdvanceTurn);
  btnAutoRun.addEventListener("click", handleAutoRun);
  btnRetry.addEventListener("click", handleRetry);
  btnAccept.addEventListener("click", () => handleHumanDecision("ACCEPTED"));
  btnReject.addEventListener("click", () => handleHumanDecision("REJECTED"));
}

async function loadRecentSessions() {
  try {
    const res = await fetch("/api/sessions?limit=10");
    if (!res.ok) return;
    const sessions = await res.json();
    renderRecentList(sessions);
  } catch (err) {
    console.error("Failed to load sessions:", err);
  }
}

function renderRecentList(sessions) {
  if (!sessions || sessions.length === 0) {
    recentList.innerHTML = '<div class="empty-text">No prior sessions recorded.</div>';
    return;
  }

  recentList.innerHTML = sessions.map(s => `
    <div class="recent-item ${s.id === currentSessionId ? 'active' : ''}" onclick="selectSession('${s.id}')">
      <div class="recent-item-title">${escapeHtml(s.topic)}</div>
      <div class="recent-item-meta">
        <span>${s.status}</span>
        <span>Round ${s.current_round}/${s.max_rounds}</span>
      </div>
    </div>
  `).join("");
}

async function selectSession(sessionId) {
  currentSessionId = sessionId;
  await fetchSessionDetails(sessionId);
  loadRecentSessions();
}

async function handleCreateSession(e) {
  e.preventDefault();
  const topic = topicInput.value.trim();
  const maxRounds = parseInt(roundsSelect.value, 10);
  if (!topic) return;

  btnCreate.disabled = true;
  btnCreate.innerText = "Initializing Session...";

  try {
    const res = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, max_rounds: maxRounds })
    });

    if (!res.ok) {
      const err = await res.json();
      alert(`Error creating session: ${err.detail || "Unknown error"}`);
      return;
    }

    const session = await res.json();
    topicInput.value = "";
    selectSession(session.id);
  } catch (err) {
    alert(`Network error creating session: ${err.message}`);
  } finally {
    btnCreate.disabled = false;
    btnCreate.innerText = "Start Boardroom Session";
  }
}

async function handleAdvanceTurn() {
  if (!currentSessionId || isProcessing) return;
  setProcessing(true, "Executing turn...");

  try {
    const res = await fetch(`/api/sessions/${currentSessionId}/advance`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idempotency_key: `client-turn-${Date.now()}` })
    });

    if (!res.ok) {
      const err = await res.json();
      alert(`Advance error: ${err.detail}`);
    }
    await fetchSessionDetails(currentSessionId);
  } catch (err) {
    alert(`Network error: ${err.message}`);
  } finally {
    setProcessing(false);
  }
}

async function handleAutoRun() {
  if (!currentSessionId || isProcessing) return;
  setProcessing(true, "Auto-running debate rounds...");

  try {
    const res = await fetch(`/api/sessions/${currentSessionId}/auto-run`, {
      method: "POST"
    });
    if (!res.ok) {
      const err = await res.json();
      alert(`Auto-run error: ${err.detail}`);
    }
    await fetchSessionDetails(currentSessionId);
  } catch (err) {
    alert(`Network error: ${err.message}`);
  } finally {
    setProcessing(false);
  }
}

async function handleRetry() {
  if (!currentSessionId || isProcessing) return;
  setProcessing(true, "Retrying failed step...");

  try {
    const res = await fetch(`/api/sessions/${currentSessionId}/retry`, {
      method: "POST"
    });
    if (!res.ok) {
      const err = await res.json();
      alert(`Retry error: ${err.detail}`);
    }
    await fetchSessionDetails(currentSessionId);
  } catch (err) {
    alert(`Network error: ${err.message}`);
  } finally {
    setProcessing(false);
  }
}

async function handleHumanDecision(decision) {
  if (!currentSessionId || isProcessing) return;
  const rationale = decisionNotes.value.trim() || undefined;

  setProcessing(true, `Submitting ${decision}...`);

  try {
    const res = await fetch(`/api/sessions/${currentSessionId}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, rationale })
    });

    if (!res.ok) {
      const err = await res.json();
      alert(`Decision error: ${err.detail}`);
    }
    await fetchSessionDetails(currentSessionId);
  } catch (err) {
    alert(`Network error: ${err.message}`);
  } finally {
    setProcessing(false);
  }
}

async function fetchSessionDetails(sessionId) {
  try {
    const res = await fetch(`/api/sessions/${sessionId}`);
    if (!res.ok) return;
    const session = await res.json();
    currentSessionData = session;
    renderSession(session);
  } catch (err) {
    console.error("Failed to fetch session details:", err);
  }
}

function renderSession(s) {
  headerStatus.innerText = s.status;
  ribbonTopic.innerText = s.topic;
  ribbonRound.innerText = s.current_round;
  ribbonMaxRounds.innerText = s.max_rounds;

  const nextRole = getNextExpectedSpeaker(s.status);
  ribbonSpeaker.innerText = nextRole || "None";

  // Update session meta
  sessionMeta.classList.remove("empty-state");
  sessionMeta.innerHTML = `
    <p><strong>ID:</strong> <code style="font-size:0.75rem;">${s.id}</code></p>
    <p><strong>Status:</strong> ${s.status}</p>
    <p><strong>Rounds:</strong> ${s.current_round} / ${s.max_rounds}</p>
    ${s.error_message ? `<p style="color:#ef4444;"><strong>Error:</strong> ${escapeHtml(s.error_message)}</p>` : ''}
  `;

  // Controls enable/disable
  const isAwaitingHuman = s.status === "AWAITING_HUMAN_DECISION";
  const isFinished = s.status === "COMPLETED" || s.status === "TERMINATED";
  const isFailed = s.status === "FAILED";

  btnAdvance.disabled = isProcessing || isAwaitingHuman || isFinished || isFailed;
  btnAutoRun.disabled = isProcessing || isAwaitingHuman || isFinished || isFailed;
  btnRetry.style.display = isFailed ? "block" : "none";

  // Render Messages
  renderMessages(s.messages);

  // Render Verdict
  renderVerdict(s.verdict, s.decision, isAwaitingHuman);
}

function renderMessages(messages) {
  messageCount.innerText = `${messages.length} turn${messages.length === 1 ? '' : 's'}`;

  if (!messages || messages.length === 0) {
    messagesStream.innerHTML = `
      <div class="empty-timeline">
        <div class="empty-icon">⏳</div>
        <h3>Session Initialized</h3>
        <p>Click "Next Turn" or "Auto-Run All Turns" to begin the debate.</p>
      </div>
    `;
    return;
  }

  messagesStream.innerHTML = messages.map(m => `
    <div class="message-card role-${m.speaker}">
      <div class="message-header">
        <span class="speaker-badge speaker-${m.speaker}">
          ${getRoleIcon(m.speaker)} ${m.speaker}
        </span>
        <span class="message-meta-info">Round ${m.round} • Turn #${m.sequence_number}</span>
      </div>
      <div class="message-body">${escapeHtml(m.content)}</div>
    </div>
  `).join("");

  messagesStream.scrollTop = messagesStream.scrollHeight;
}

function renderVerdict(verdict, decision, isAwaitingHuman) {
  if (!verdict) {
    verdictSection.style.display = "none";
    return;
  }

  verdictSection.style.display = "block";
  document.getElementById("v-strong-supp").innerText = verdict.strongest_supporting;
  document.getElementById("v-weak-supp").innerText = verdict.supporting_weaknesses;
  document.getElementById("v-strong-opp").innerText = verdict.strongest_opposing;
  document.getElementById("v-weak-opp").innerText = verdict.opposing_weaknesses;
  document.getElementById("v-reasoning").innerText = verdict.reasoning;
  document.getElementById("v-assessment").innerText = verdict.final_assessment;

  const decisionActions = document.querySelector(".decision-inputs");
  if (decision) {
    decisionActions.style.display = "none";
    decisionResult.style.display = "block";
    decisionResult.className = `decision-result ${decision.decision.toLowerCase()}`;
    decisionResult.innerHTML = `
      <strong>Decision Recorded:</strong> ${decision.decision}<br>
      <small>Timestamp: ${new Date(decision.decided_at).toLocaleString()}</small>
      ${decision.rationale ? `<br><small>Rationale: ${escapeHtml(decision.rationale)}</small>` : ''}
    `;
  } else if (isAwaitingHuman) {
    decisionActions.style.display = "block";
    decisionResult.style.display = "none";
  } else {
    decisionActions.style.display = "none";
    decisionResult.style.display = "none";
  }
}

function getNextExpectedSpeaker(status) {
  switch (status) {
    case "CREATED": return "MODERATOR";
    case "MODERATOR_OPENING": return "SUPPORTING";
    case "SUPPORTING_ARGUMENT": return "OPPOSING";
    case "OPPOSING_REBUTTAL": return "SUPPORTING / JUDGE";
    case "SUPPORTING_RESPONSE": return "OPPOSING";
    case "OPPOSING_RESPONSE": return "SUPPORTING / JUDGE";
    case "JUDGE_VERDICT": return "HUMAN";
    case "AWAITING_HUMAN_DECISION": return "HUMAN";
    default: return null;
  }
}

function getRoleIcon(role) {
  switch (role) {
    case "MODERATOR": return "🎙️";
    case "SUPPORTING": return "🟢";
    case "OPPOSING": return "🔴";
    case "JUDGE": return "⚖️";
    case "HUMAN": return "👤";
    default: return "💬";
  }
}

function setProcessing(loading, statusText) {
  isProcessing = loading;
  if (loading) {
    btnAdvance.disabled = true;
    btnAutoRun.disabled = true;
    headerStatus.innerText = statusText || "BUSY";
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/[&<>'"]/g, 
    tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag] || tag)
  );
}
