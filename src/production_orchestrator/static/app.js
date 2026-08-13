/* Production Orchestrator — local judge-facing demo.
 *
 * Everything rendered here comes from the local API: the audit trail written
 * by the agent's real tool calls, the persisted immutable proposal, and the
 * verified completion report. No shop fact is invented client-side.
 */
"use strict";

const app = document.getElementById("app");
const scenarioNav = document.getElementById("scenarios");
const heroEyebrow = document.getElementById("hero-eyebrow");
const heroTitle = document.getElementById("hero-title");
const heroCopy = document.getElementById("hero-copy");

let meta = null;
let scenario = null;
let currentName = null;

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);

const rows = (items) =>
  items
    .map((pair) => `<div class="row"><span>${esc(pair[0])}</span><span>${esc(pair[1])}</span></div>`)
    .join("");

function materialName(materialId) {
  const match = /^THREAD-([A-Z]+)-\d+$/.exec(String(materialId));
  return match ? `${match[1].toLowerCase()} thread` : String(materialId);
}

function hours(value) {
  return `${value} hour${value === 1 ? "" : "s"}`;
}

function eventTime(createdAt) {
  const text = String(createdAt ?? "");
  return text.length >= 19 ? `${text.slice(11, 19)} UTC` : "";
}

function dayLabel(day, days) {
  const index = days.indexOf(day);
  const base = index === 0 ? "Today" : index === 1 ? "Tomorrow" : `Day ${index + 1}`;
  return `${base} · ${day.slice(5)}`;
}

/* ------------------------------------------------------------------ */
/* Data fetch                                                          */
/* ------------------------------------------------------------------ */

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "request_failed");
  return body;
}

async function boot() {
  try {
    meta = await request("/api/meta");
    renderScenarioChips();
    await newScenario(meta.scenarios[0].name);
  } catch (error) {
    app.innerHTML =
      '<div class="notice danger">The local demo API is not responding. ' +
      "Restart it with <code>uv run production-orchestrator-demo</code> and reload this page.</div>";
  }
}

async function newScenario(name) {
  currentName = name;
  renderScenarioChips(true);
  app.innerHTML =
    '<div class="empty"><span class="spinner"></span>' +
    "The agent is checking the shop and preparing a recommendation…</div>";
  try {
    scenario = await request("/api/scenarios", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario: name }),
    });
    render(scenario);
  } catch (error) {
    app.innerHTML =
      '<div class="notice danger">The agent run could not be completed and verified, so nothing was shown. ' +
      "Restart the local demo and try again; the on-disk audit state is preserved for inspection.</div>";
  } finally {
    renderScenarioChips();
  }
}

async function decide(decision) {
  document.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    scenario = await request(`/api/scenarios/${scenario.scenario_id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    });
    render(scenario);
  } catch (error) {
    app.innerHTML =
      '<div class="notice danger"><strong>This decision could not be verified end to end, so the scenario is locked.</strong><br>' +
      "That is the fail-closed design protecting the audit trail. Pick a scenario above to start a fresh run — " +
      "the locked run’s state and audit log remain on disk for inspection.</div>";
  } finally {
    renderScenarioChips();
  }
}

/* ------------------------------------------------------------------ */
/* Scenario switcher + hero                                            */
/* ------------------------------------------------------------------ */

function renderScenarioChips(busy = false) {
  if (!meta) return;
  scenarioNav.innerHTML = meta.scenarios
    .map(
      (spec) =>
        `<button type="button" class="scenario-chip${spec.name === currentName ? " active" : ""}"` +
        ` data-scenario="${esc(spec.name)}"${busy ? " disabled" : ""}>${esc(spec.title)}</button>`
    )
    .join("");
  scenarioNav.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => newScenario(button.dataset.scenario));
  });
}

function renderHero(data) {
  const done = data.phase !== "pending";
  heroEyebrow.textContent = `${data.scenario.title} · ${
    done ? `decision recorded — ${data.phase}` : "a production decision, ready for review"
  }`;
  heroTitle.textContent = data.scenario.question;
  heroCopy.textContent = done
    ? data.phase === "approved"
      ? "You approved the coordinated plan and the agent applied it exactly once. The board below shows the updated shop."
      : "You kept the current plan. The agent changed nothing — the shop state and audit trail below prove it."
    : `${data.scenario.summary} From one customer email, the agent created the order, checked the shop through its tools, and prepared one coordinated plan. Nothing changes unless you approve it.`;
}

/* ------------------------------------------------------------------ */
/* Agent activity feed                                                 */
/* ------------------------------------------------------------------ */

const FEED_VIEWS = {
  scenario_initialized: (d) => ({
    title: "Synthetic shop initialized",
    detail: `Deterministic shop state loaded at revision ${d.revision ?? 1}`,
  }),
  request_intake: (d) => ({
    title: "Read the customer request and created the order",
    detail: `Extraction validated against the product catalog — ${d.order_id ?? "order"}: ${
      d.duration_hours ?? "?"
    }h of work, priority ${d.priority ?? "?"}, due ${d.requested_day ?? "?"}`,
    tool: "intake_customer_request",
  }),
  active_orders_read: (d) => ({
    title: "Checked the active order queue",
    detail: `${(d.order_ids || []).length} orders: ${(d.order_ids || []).join(", ")}`,
    tool: "list_active_orders",
  }),
  inventory_read: (d) => ({
    title: "Checked thread inventory",
    detail: `Materials on hand: ${(d.material_ids || []).map(materialName).join(", ")}`,
    tool: "get_inventory",
  }),
  machine_capacity_read: (d) => ({
    title: "Checked machine capacity and the current schedule",
    detail: `Machines: ${(d.machine_ids || []).join(", ")} · scheduled: ${
      (d.scheduled_order_ids || []).join(", ") || "none"
    }`,
    tool: "get_machine_capacity",
  }),
  blockers_analyzed: (d) => ({
    title: `Analyzed blockers for ${d.target_order_id ?? "the target order"}`,
    detail: `Found: ${(d.blocker_kinds || []).map((kind) => kind.replaceAll("_", " ")).join(", ") || "none"}`,
    tool: "analyze_shop_blockers",
  }),
  proposal_created: (d) => ({
    title: "Proposed one coordinated plan",
    detail: `Immutable proposal ${d.proposal_id ?? ""} · base revision ${d.base_revision ?? ""} · content-addressed by hash`,
    tool: "propose_schedule",
  }),
  communications_drafted: (d) => ({
    title: "Drafted the outgoing messages",
    detail: `Unsent drafts prepared for: ${(d.audiences || []).join(", ")}`,
    tool: "draft_communications",
  }),
  approval_granted: () => ({
    title: "You approved the exact reviewed plan",
    detail: "The approval binds to the proposal hash you saw at the interrupt",
    human: true,
  }),
  approval_rejected: () => ({
    title: "You kept the current schedule",
    detail: "The rejection was recorded and the write was cancelled",
    human: true,
  }),
  plan_applied: () => ({
    title: "Applied the plan exactly once",
    detail: "Schedule, procurement task, revision, and audit appended atomically",
    tool: "apply_production_plan",
  }),
};

function feedCard(data) {
  const items = [];
  for (const event of data.audit) {
    const view = FEED_VIEWS[event.event_type];
    if (!view) continue;
    const built = view(event.details || {});
    items.push({ ...built, time: eventTime(event.created_at) });
  }
  if (data.phase === "pending") {
    items.push({
      title: "Stopped at a real Strands interrupt",
      detail:
        `apply_production_plan is held open for your decision · agent process ` +
        `${data.checkpoint.start_process_id} paused before any write`,
      interrupt: true,
    });
  }
  const toolCalls = items.filter((item) => item.tool).length;
  const body = items
    .map(
      (item, index) => `
      <li class="${item.interrupt ? "interrupt" : ""}${item.human ? "human" : ""}" data-delay="${index * 110}">
        <i></i>
        <div class="feed-body">
          <span class="feed-title">${esc(item.title)}</span>
          <span class="feed-detail">${esc(item.detail)}</span>
          <span class="feed-meta">
            ${item.tool ? `<span class="tool-badge">${esc(item.tool)}</span>` : ""}
            ${item.time ? `<time>${esc(item.time)}</time>` : ""}
          </span>
        </div>
      </li>`
    )
    .join("");
  return `
  <section class="card wide">
    <div class="cardhead">
      <div>
        <h2>What the agent did</h2>
        <div class="caption">Every step below is a recorded audit event from a real Strands tool call — not a script of this page.</div>
      </div>
      <span class="pill accent">${toolCalls} REAL TOOL CALLS</span>
    </div>
    <ol class="feed">${body}</ol>
  </section>`;
}

function emailCard(data) {
  const email = data.scenario.customer_email;
  if (!email) return "";
  const lines = email.split("\n");
  const subject = lines[0].replace(/^Subject:\s*/, "");
  const body = lines.slice(1).join("\n").trim();
  return `
  <section class="card wide email-card">
    <div class="cardhead">
      <div>
        <h2>The customer request that started this</h2>
        <div class="caption">Synthetic email — the agent's only unstructured input. Everything below came from it.</div>
      </div>
      <span class="pill accent">UNSTRUCTURED IN</span>
    </div>
    <div class="email-paper">
      <div class="email-subject">${esc(subject)}</div>
      <p class="email-body">${esc(body)}</p>
    </div>
  </section>`;
}

/* ------------------------------------------------------------------ */
/* Decision brief and outcome                                          */
/* ------------------------------------------------------------------ */

function blockerView(blocker) {
  if (blocker.kind === "capacity_conflict") {
    return {
      chip: `+${blocker.shortage}h`,
      title: `${hours(blocker.shortage)} over ${blocker.resource_id} capacity`,
      detail: `${hours(blocker.required)} would be needed, but only ${blocker.available} are available that day.`,
    };
  }
  if (blocker.kind === "inventory_shortage") {
    return {
      chip: `−${blocker.shortage}`,
      title: `${blocker.shortage} units of ${materialName(blocker.resource_id)} short`,
      detail: `The order needs ${blocker.required} units; the shop has ${blocker.available}.`,
    };
  }
  return { chip: "!", title: blocker.kind.replaceAll("_", " "), detail: "" };
}

function planItems(data) {
  const proposal = data.proposal;
  const orders = data.state.orders;
  const items = [];
  for (const change of proposal.schedule_changes) {
    const duration = orders[change.order_id] ? orders[change.order_id].duration_hours : "?";
    if (change.from_day) {
      items.push({
        title: `Move ${change.order_id} to ${change.to_day}`,
        detail: `Frees its ${duration}-hour slot on ${change.machine_id} using later available capacity.`,
      });
    } else {
      items.push({
        title: `Schedule ${change.order_id} on ${change.to_day}`,
        detail: `Uses ${duration} hours on ${change.machine_id} for the higher-priority order.`,
      });
    }
  }
  for (const action of proposal.procurement_actions) {
    items.push({
      title: `Create a procurement task for ${action.quantity} units of ${materialName(action.material_id)}`,
      detail: "Records the exact material shortage that must be covered before production.",
    });
  }
  if (proposal.communication_drafts.length) {
    items.push({
      title: `Prepare ${proposal.communication_drafts.length} coordinated messages`,
      detail: `${proposal.communication_drafts.map((draft) => draft.audience).join(", ")} drafts stay unsent until a person sends them.`,
    });
  }
  return items;
}

function decisionBrief(data) {
  const proposal = data.proposal;
  const moves = proposal.schedule_changes.filter((change) => change.from_day);
  const target = proposal.target_order_id;
  const blockers = proposal.evidence.map(blockerView);
  const plan = planItems(data);
  const keepConsequence =
    `Revision stays ${data.state.revision}. ${target} remains unscheduled` +
    (proposal.procurement_actions.length ? " and no procurement task is created." : ".");
  const approveConsequence =
    (moves.length ? `${moves.map((change) => change.order_id).join(" and ")} move${moves.length === 1 ? "s" : ""}, ` : "") +
    `${target} is scheduled` +
    (proposal.procurement_actions.length ? ", and the procurement task is created" : "") +
    " — exactly once, exactly as reviewed.";
  return `
  <section class="card wide decision">
    <div class="decision-head">
      <div>
        <div class="kicker">Decision needed for ${esc(target)}</div>
        <h2>Make room for the priority order without losing control of the shop</h2>
        <p>The agent found ${proposal.evidence.length === 1 ? "one blocker" : `${proposal.evidence.length} connected blockers`} and prepared one reviewable plan.</p>
      </div>
      <span class="pill warn">${proposal.evidence.length} BLOCKER${proposal.evidence.length === 1 ? "" : "S"}</span>
    </div>
    <div class="interrupt-strip">
      <b>A real Strands interrupt is holding this write.</b>
      The agent stopped inside <code>apply_production_plan</code> in process ${esc(data.checkpoint.start_process_id)}.
      Your decision resumes the persisted session in a fresh process — approval binds to proposal
      <code>${esc(data.checkpoint.proposal_hash.slice(0, 12))}…</code> and nothing else.
    </div>
    <div class="decision-grid">
      <div class="problem">
        <h3>What is blocking the order</h3>
        <p class="plain">Calculated by deterministic shop logic from tool evidence.</p>
        <div class="signal-list">
          ${blockers
            .map(
              (view) => `
          <div class="signal">
            <div class="signal-num">${esc(view.chip)}</div>
            <div><strong>${esc(view.title)}</strong><p>${esc(view.detail)}</p></div>
          </div>`
            )
            .join("")}
        </div>
      </div>
      <div class="recommendation">
        <h3>What the agent recommends</h3>
        <p class="plain">Schedule, materials, and communications as one reviewable proposal.</p>
        <div class="plan-list">
          ${plan
            .map(
              (item, index) => `
          <div class="plan-item">
            <i>${index + 1}</i>
            <div><strong>${esc(item.title)}</strong><span>${esc(item.detail)}</span></div>
          </div>`
            )
            .join("")}
        </div>
      </div>
    </div>
    <div class="value">
      <h3>Why this is useful</h3>
      <div class="value-grid">
        <div><b>One connected decision</b>Schedule, materials, and messages do not drift into separate ad hoc changes.</div>
        <div><b>Priority work stays on time</b>Lower-priority jobs move to capacity the agent verified through tools.</div>
        <div><b>A human stays accountable</b>No shop state changes until this exact plan is approved.</div>
      </div>
    </div>
    <div class="decision-actions">
      <div class="consequences">
        <div class="consequence"><b>Keep current schedule</b>${esc(keepConsequence)}</div>
        <div class="consequence"><b>Approve coordinated plan</b>${esc(approveConsequence)}</div>
      </div>
      <div class="actions">
        <button type="button" class="btn" data-decision="reject">Keep current schedule</button>
        <button type="button" class="btn primary" data-decision="approve">Approve coordinated plan</button>
      </div>
    </div>
  </section>`;
}

function outcomeCard(data) {
  const approved = data.phase === "approved";
  const proposal = data.proposal;
  const target = proposal.target_order_id;
  const moves = proposal.schedule_changes.filter((change) => change.from_day);
  const placement = proposal.schedule_changes.find((change) => !change.from_day);
  const results = approved
    ? [
        placement && {
          title: `${target} scheduled`,
          detail: `${data.state.orders[target].duration_hours} hours on ${placement.machine_id}, ${placement.to_day}.`,
        },
        moves.length && {
          title: `${moves.map((change) => change.order_id).join(", ")} moved`,
          detail: `Rescheduled to ${moves.map((change) => change.to_day).join(", ")}.`,
        },
        proposal.procurement_actions.length
          ? {
              title: "Material follow-up recorded",
              detail: proposal.procurement_actions
                .map((action) => `Procure ${action.quantity} units of ${materialName(action.material_id)}`)
                .join("; "),
            }
          : {
              title: "Drafts remain unsent",
              detail: "No customer, operator, or supplier message was sent.",
            },
      ].filter(Boolean)
    : [
        { title: `Revision remains ${data.state.revision}`, detail: "The deterministic shop state is unchanged." },
        { title: "Zero plans applied", detail: "No schedule or procurement mutation occurred." },
        { title: "Drafts remain unsent", detail: "No external communication occurred." },
      ];
  return `
  <section class="card wide outcome-card">
    <div class="kicker">${approved ? "Plan approved" : "Plan rejected"}</div>
    <h2>${approved ? "Priority order scheduled; follow-up work created" : "Current shop plan kept unchanged"}</h2>
    <p>${
      approved
        ? "The exact coordinated proposal was applied once by a fresh agent process. The board below shows the applied schedule; all communication drafts remain unsent."
        : "The fresh agent process recorded your rejection and wrote nothing. The priority order remains unscheduled so the shop can choose another response."
    }</p>
    <div class="result-grid">
      ${results
        .map((result) => `<div class="result"><b>${esc(result.title)}</b><span>${esc(result.detail)}</span></div>`)
        .join("")}
    </div>
    <div class="actions">
      <button type="button" class="btn" data-rerun="1">Run this scenario again</button>
    </div>
  </section>`;
}

/* ------------------------------------------------------------------ */
/* Production board                                                     */
/* ------------------------------------------------------------------ */

function boardCard(data) {
  const state = data.state;
  const proposal = data.proposal;
  const pending = data.phase === "pending";
  const approved = data.phase === "approved";
  const machines = Object.values(state.machines).sort((a, b) => a.machine_id.localeCompare(b.machine_id));
  const days = [...new Set(machines.flatMap((machine) => Object.keys(machine.daily_capacity)))].sort();
  const changedOrders = new Map(proposal.schedule_changes.map((change) => [change.order_id, change]));

  const chip = (entry, kind, badge) => `
    <div class="order-chip ${kind}" data-grow="${entry.duration_hours}">
      <span class="chip-id">${esc(entry.order_id)}</span>
      ${badge ? `<span class="chip-badge">${esc(badge)}</span>` : ""}
      <span class="chip-sub">${esc(entry.duration_hours)}h</span>
    </div>`;

  const cells = machines
    .map((machine) => {
      const cellsForDays = days
        .map((day) => {
          const entries = state.schedule.filter(
            (entry) => entry.machine_id === machine.machine_id && entry.day === day
          );
          const slots = [];
          let used = 0;
          for (const entry of entries) {
            used += entry.duration_hours;
            const change = changedOrders.get(entry.order_id);
            if (pending && change && change.from_day === day) {
              slots.push(chip(entry, "leaving", `moves → ${change.to_day.slice(5)}`));
            } else if (approved && change) {
              slots.push(chip(entry, "applied", change.from_day ? "moved" : "new"));
            } else {
              slots.push(chip(entry, "", ""));
            }
          }
          if (pending) {
            for (const change of proposal.schedule_changes) {
              if (change.machine_id === machine.machine_id && change.to_day === day) {
                const order = state.orders[change.order_id];
                slots.push(
                  chip(
                    { order_id: change.order_id, duration_hours: order ? order.duration_hours : 1 },
                    "ghost",
                    "proposed"
                  )
                );
              }
            }
          }
          const capacity = machine.daily_capacity[day] ?? 0;
          const over = used > capacity;
          const width = capacity ? Math.min(100, Math.round((used / capacity) * 100)) : 0;
          return `
          <div class="board-cell">
            <div class="board-capacity"><span>${used}h used</span><span>${capacity}h capacity</span></div>
            <div class="capacity-bar"><i class="${over ? "over" : ""}" data-width="${width}"></i></div>
            <div class="board-slots">${slots.join("") || ""}</div>
          </div>`;
        })
        .join("");
      return `<div class="board-machine">${esc(machine.machine_id)}</div>${cellsForDays}`;
    })
    .join("");

  const scheduledIds = new Set(state.schedule.map((entry) => entry.order_id));
  const unscheduled = Object.values(state.orders).filter((order) => !scheduledIds.has(order.order_id));
  const unscheduledRow = unscheduled.length
    ? `
    <div class="board-unscheduled">
      <span class="label">Unscheduled</span>
      ${unscheduled
        .map((order) =>
          chip(
            order,
            pending && changedOrders.has(order.order_id) ? "ghost" : "",
            pending && changedOrders.has(order.order_id)
              ? "waiting for approval"
              : data.phase === "rejected"
                ? "still unscheduled"
                : ""
          )
        )
        .join("")}
    </div>`
    : `
    <div class="board-unscheduled clear">
      <span class="label">Unscheduled</span>
      <span class="plain">Every order has a machine slot.</span>
    </div>`;

  const caption = pending
    ? "Current schedule with the proposed changes shown dashed. Nothing below moves until you approve."
    : approved
      ? "The applied schedule. Highlighted blocks are the changes from the approved proposal."
      : "The schedule is exactly as it was before the agent ran — the rejection changed nothing.";
  return `
  <section class="card wide">
    <div class="cardhead">
      <div>
        <h2>Production board</h2>
        <div class="caption">${esc(caption)}</div>
      </div>
      <span class="pill">REVISION ${esc(state.revision)}</span>
    </div>
    <div class="board" data-days="${days.length}">
      <div class="board-days">
        <div></div>
        ${days.map((day) => `<div class="board-day-label">${esc(dayLabel(day, days))}</div>`).join("")}
        ${cells}
      </div>
      ${unscheduledRow}
    </div>
  </section>`;
}

/* ------------------------------------------------------------------ */
/* Snapshot, drafts, technical proof                                    */
/* ------------------------------------------------------------------ */

function snapshotCard(data) {
  const state = data.state;
  const orderRows = Object.values(state.orders)
    .sort((a, b) => b.priority - a.priority)
    .map((order) => [
      order.order_id,
      `Priority ${order.priority} · ${order.duration_hours}h · due ${order.requested_day}`,
    ]);
  const inventoryRows = Object.entries(state.inventory).map(([materialId, quantity]) => [
    materialName(materialId),
    `${quantity} units`,
  ]);
  return `
  <section class="card">
    <div class="cardhead">
      <div>
        <h2>Shop snapshot</h2>
        <div class="caption">Deterministic state · revision ${esc(state.revision)}</div>
      </div>
      <span class="pill">${orderRows.length} ORDERS</span>
    </div>
    <div class="list">${rows(orderRows)}</div>
    <div class="caption subhead">Thread inventory</div>
    <div class="list">${rows(inventoryRows)}</div>
  </section>`;
}

function draftsCard(data) {
  const drafts = data.proposal.communication_drafts;
  return `
  <section class="card">
    <div class="cardhead">
      <div>
        <h2>Communication drafts</h2>
        <div class="caption">Written by the agent, bound to this proposal — open each to read it</div>
      </div>
      <span class="pill">${drafts.length} UNSENT</span>
    </div>
    <div class="list">
      ${drafts
        .map(
          (draft) => `
      <details class="draft">
        <summary><span class="audience">${esc(draft.audience)}</span><span class="subject">${esc(draft.subject)}</span></summary>
        <div class="draft-body">${esc(draft.body)}<br><span class="pill unsent">DRAFT · NOT SENT</span></div>
      </details>`
        )
        .join("")}
    </div>
    <p class="plain footnote">No customer, operator, or supplier message is ever sent by this demo.</p>
  </section>`;
}

function technicalProof(data) {
  const report = data.report;
  const checkpoint = data.checkpoint;
  const proposal = data.proposal;
  const strandsVersion = meta && meta.provider ? meta.provider.strands_agents_version : "";
  const processFact = report
    ? `Started in process <b>${esc(report.start_process_id)}</b>, resumed in fresh process <b>${esc(report.resume_process_id)}</b> — the persisted Strands session crossed a real process boundary. Applications: ${esc(report.plan_applied_count)} · final revision ${esc(report.final_state_revision)}.`
    : `Process <b>${esc(checkpoint.start_process_id)}</b> stopped at the real Strands interrupt <code>${esc(checkpoint.interrupt_name)}</code>. A fresh process will reconstruct the session and resume after your decision.`;
  return `
  <details class="technical">
    <summary>Technical proof — real Strands loop, exact proposal, process boundary, audit chain</summary>
    <div class="technical-body">
      <ul class="provider-facts">
        <li><b>Agent loop:</b> Strands Agents ${esc(strandsVersion)} — official tool loop, <code>BeforeToolCallEvent</code> interrupt, and <code>FileSessionManager</code> persistence.</li>
        <li><b>Model for this local run:</b> ${esc(checkpoint.model_id)} — a deterministic tool-calling sequence with no network inference. Every shop fact above came from a real tool call; the deterministic planner owns all calculations.</li>
        <li><b>Judged-provider evidence:</b> the same workflow is validated through Amazon Bedrock (<code>amazon.nova-lite-v1:0</code>, us-east-1); the executed reports are committed under <code>evidence/</code> in this repository.</li>
        <li><b>Process boundary:</b> ${processFact}</li>
      </ul>
      <div class="caption">Exact proposal hash — approval binds to this and nothing else</div>
      <div class="hash hash-lead">${esc(proposal.content_hash)}</div>
      <div class="grid">
        <div>
          <div class="cardhead">
            <div>
              <h2>Exact changes</h2>
              <div class="caption">Base revision ${esc(proposal.base_revision)} · ${esc(proposal.proposal_id)}</div>
            </div>
            <span class="pill ${report ? "ok" : "warn"}">${report ? "VERIFIED" : "PENDING"}</span>
          </div>
          <div class="list">
            ${rows(proposal.schedule_changes.map((change) => [change.order_id, `${change.from_day || "unscheduled"} → ${change.to_day}`]))}
            ${rows(proposal.procurement_actions.map((action) => [action.material_id, `${action.quantity} units`]))}
          </div>
        </div>
        <div>
          <div class="cardhead">
            <div>
              <h2>Audit chain</h2>
              <div class="caption">Every recorded event, in order</div>
            </div>
            <span class="pill">${data.audit.length} EVENTS</span>
          </div>
          <div class="timeline">
            ${data.audit
              .map(
                (event) => `
            <div class="event"><i></i><div>${esc(event.event_type.replaceAll("_", " "))}<small>#${esc(event.sequence)} · ${esc(
              (event.proposal_hash || "domain").slice(0, 12)
            )}</small></div></div>`
              )
              .join("")}
          </div>
        </div>
      </div>
    </div>
  </details>`;
}

/* ------------------------------------------------------------------ */
/* Page assembly                                                        */
/* ------------------------------------------------------------------ */

function phasebar(data) {
  const done = data.phase !== "pending";
  return `
  <div class="phasebar">
    <div class="step active"><b>01 · AGENT RUN</b>Read the request, checked facts, planned, drafted</div>
    <div class="step active"><b>02 · INTERRUPT</b>${done ? "Released by your decision" : "Holding the write for approval"}</div>
    <div class="step ${done ? "active" : ""}"><b>03 · DECIDE</b>${done ? esc(data.phase) : "Human approval required"}</div>
  </div>`;
}

function applyDynamicStyles(root) {
  // The CSP forbids inline style attributes; CSSOM writes are allowed.
  root.querySelectorAll("[data-delay]").forEach((el) => {
    el.style.animationDelay = `${el.dataset.delay}ms`;
  });
  root.querySelectorAll("[data-grow]").forEach((el) => {
    el.style.flexGrow = el.dataset.grow;
  });
  root.querySelectorAll("[data-width]").forEach((el) => {
    el.style.width = `${el.dataset.width}%`;
  });
  root.querySelectorAll("[data-days]").forEach((el) => {
    el.style.setProperty("--board-days", el.dataset.days);
  });
}

function render(data) {
  scenario = data;
  renderHero(data);
  const done = data.phase !== "pending";
  const sections = done
    ? [phasebar(data), outcomeCard(data), boardCard(data), emailCard(data), feedCard(data)]
    : [phasebar(data), emailCard(data), feedCard(data), decisionBrief(data), boardCard(data)];
  sections.push(
    `<div class="grid mt16">${snapshotCard(data)}${draftsCard(data)}</div>`,
    technicalProof(data)
  );
  app.innerHTML = sections.join("\n");
  applyDynamicStyles(app);
  app.querySelectorAll("button[data-decision]").forEach((button) => {
    button.addEventListener("click", () => decide(button.dataset.decision));
  });
  app.querySelectorAll("button[data-rerun]").forEach((button) => {
    button.addEventListener("click", () => newScenario(currentName));
  });
}

boot();
