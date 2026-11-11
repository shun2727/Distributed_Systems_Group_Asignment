// app.js: what the page does when you click things.
// It never calls fetch() itself. It only calls Api.* (api.js) and draws the result.

const $ = (selector) => document.querySelector(selector);

// ---------- small helpers ----------

// Build an HTML element safely. Strings become TEXT, never HTML, so a guest name
// like "<script>" is shown as plain text instead of running (supports NFR2, security).
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

const note = (kind, text) => el("p", { class: `note ${kind}`, role: kind === "bad" ? "alert" : "status" }, text);
const show = (container, kind, text) => container.replaceChildren(note(kind, text));

function setBusy(button, busy, label) {
  if (busy) { button.dataset.label = button.textContent; button.textContent = label; button.disabled = true; }
  else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
}

const pad = (n) => String(n).padStart(2, "0");
function dateIn(daysFromToday) {
  const d = new Date();
  d.setDate(d.getDate() + daysFromToday);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
const nightsBetween = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 86400000);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Turn an error reply into a message a guest can act on. We read error.code,
// never the server's wording, because the code is the stable part of the contract.
const ERROR_TEXT = {
  ROOM_UNAVAILABLE: "That room is already booked for some of those nights. Try different dates or another room.",
  NOT_FOUND: "No booking was found with that ID.",
  BOOKING_STATE_CONFLICT: "This booking can't be cancelled in its current state.",
  SERVICE_UNAVAILABLE: "The booking system could not reach the room service. Try again in a moment.",
  UNAUTHORIZED: "The request was refused. Check the service key settings.",
};
function friendlyError(res) {
  const error = res.data && res.data.error;
  if (error && error.code === "NETWORK_ERROR") {
    return `Can't reach the Booking service at ${CONFIG.API_BASE}. Check that it is running, then try again.`;
  }
  if (error && ERROR_TEXT[error.code]) return ERROR_TEXT[error.code];
  if (error && error.message) return error.message;
  return `Something went wrong (HTTP ${res.status}).`;
}

// ---------- state for the booking in progress ----------
// key = the Idempotency-Key for the CURRENT booking attempt. It is created when a room
// is chosen and reused if the same attempt is retried, so a retry cannot double-book.
const state = { query: null, room: null, key: null };

// ---------- booking card (used by both tabs) ----------
const chip = (status) => el("span", { class: `chip ${status}` }, status);
const row = (label, value) => [el("dt", {}, label), el("dd", {}, String(value))];

function bookingCard(initial, opts = {}) {
  let repeat = !!opts.repeat;
  const slot = el("div", { "aria-live": "polite" });
  const card = el("article", { class: "booking" });

  async function run(button, label, work) {
    setBusy(button, true, label);
    await work();
    setBusy(button, false);
  }

  async function cancel(b) {
    if (!confirm(`Cancel booking ${b.booking_id}?`)) return;
    const res = await Api.cancelBooking(b.booking_id);                 // POST /api/bookings/{id}/cancel
    if (res.ok) { repeat = false; opts.resend = null; render(res.data); }
    else slot.replaceChildren(note("bad", friendlyError(res)));
  }

  // Demo for the duplicate-request evidence: send the SAME body with the SAME key again.
  async function resend() {
    const res = await Api.createBooking(opts.resend.body, opts.resend.key);
    if (res.ok) { repeat = res.status === 200; render(res.data); }
    else slot.replaceChildren(note("bad", friendlyError(res)));
  }

  function render(b) {
    slot.replaceChildren();
    const actions = [];
    if (b.status === "confirmed") {
      actions.push(el("button", { type: "button", class: "btn danger",
        onclick: (e) => run(e.currentTarget, "Cancelling...", () => cancel(b)) }, "Cancel booking"));
    }
    if (opts.resend) {
      actions.push(el("button", { type: "button", class: "btn ghost",
        onclick: (e) => run(e.currentTarget, "Sending...", resend) }, "Send the same request again"));
    }
    card.replaceChildren(
      el("div", { class: "booking-head" }, el("h3", {}, `Booking ${b.booking_id}`), chip(b.status)),
      ...(repeat ? [note("info", "The server recognised this request key and returned the original booking. No duplicate was created.")] : []),
      el("dl", {},
        row("Room", b.room_id),
        row("Guest", b.guest_name),
        row("Stay", `${b.check_in} to ${b.check_out} (${plural(nightsBetween(b.check_in, b.check_out), "night")})`),
        row("Guests", b.guest_count),
        row("Confirmation message", b.notification_status || "not available")),
      el("div", { class: "actions" }, actions),
      slot
    );
  }

  render(initial);
  return card;
}

// ---------- tab 1: search, choose, book ----------
function validateSearch(q) {
  if (!q.check_in || !q.check_out) return "Choose both a check-in and a check-out date.";
  if (q.check_out <= q.check_in) return "Check-out must be after check-in.";
  if (nightsBetween(q.check_in, q.check_out) > 30) return "Stays are limited to 30 nights.";
  if (!Number.isInteger(q.guest_count) || q.guest_count < 1 || q.guest_count > 8) return "Guests must be between 1 and 8.";
  return null;   // The server checks all of this again. This only saves a round trip.
}

function resetBooking() {
  state.room = null;
  $("#book-form").hidden = true;
  $("#book-result").replaceChildren();
}

async function onSearch(event) {
  event.preventDefault();
  const q = {
    check_in: $("#check_in").value,
    check_out: $("#check_out").value,
    guest_count: Number($("#guest_count").value),
  };
  resetBooking();
  const box = $("#rooms");
  const problem = validateSearch(q);
  if (problem) return show(box, "bad", problem);

  const button = $("#search-btn");
  setBusy(button, true, "Searching...");
  const res = await Api.searchRooms(q);                                // GET /api/rooms?...
  setBusy(button, false);

  if (!res.ok) return show(box, "bad", friendlyError(res));
  state.query = q;
  renderRooms(res.data.rooms);
}

function renderRooms(rooms) {
  const box = $("#rooms");
  if (!rooms.length) return show(box, "info", "No rooms are free for those dates and that many guests. Try different dates.");
  const nights = nightsBetween(state.query.check_in, state.query.check_out);
  box.replaceChildren(
    el("h2", {}, `${plural(rooms.length, "room")} free for ${plural(nights, "night")}`),
    el("ul", { class: "tags" }, rooms.map((r) =>
      el("li", { class: "tag" },
        el("span", { class: "tag-no" }, r.room_number),
        el("span", { class: "tag-type" }, r.room_type),
        el("span", { class: "tag-cap" }, `Sleeps ${r.capacity}`),
        el("button", { type: "button", class: "btn", onclick: () => selectRoom(r) }, "Book this room"))))
  );
}

function selectRoom(room) {
  state.room = room;
  state.key = crypto.randomUUID();            // a fresh key for this new booking attempt
  $("#book-result").replaceChildren();
  const q = state.query;
  $("#book-summary").textContent =
    `Room ${room.room_number} (${room.room_type}), ${q.check_in} to ${q.check_out}, ${plural(q.guest_count, "guest")}.`;
  $("#book-form").hidden = false;
  $("#guest_name").focus();
}

async function onBook(event) {
  event.preventDefault();
  const out = $("#book-result");
  const name = $("#guest_name").value.trim();
  if (!name || name.length > 60) return show(out, "bad", "Enter the guest name (up to 60 characters).");

  const body = {
    room_id: state.room.room_id,
    guest_name: name,
    check_in: state.query.check_in,
    check_out: state.query.check_out,
    guest_count: state.query.guest_count,
  };
  const key = state.key;

  const button = $("#book-btn");
  setBusy(button, true, "Booking...");
  const res = await Api.createBooking(body, key);                      // POST /api/bookings
  setBusy(button, false);

  if (res.ok) {
    $("#book-form").hidden = true;
    out.replaceChildren(bookingCard(res.data, { repeat: res.status === 200, resend: { body, key } }));
    return;
  }

  const unreachable = res.status === 0 || res.status >= 500;
  if (unreachable) {
    // No clear answer. KEEP the key: pressing Confirm again is a retry of the same attempt.
    out.replaceChildren(note("warn", friendlyError(res) + " Press Confirm booking to retry. The same request key is reused, so it cannot create a duplicate."));
  } else {
    // A definite "no" (for example the room is taken). The next attempt gets a new key.
    state.key = crypto.randomUUID();
    show(out, "bad", friendlyError(res));
  }
}

// ---------- tab 2: view and cancel ----------
async function onManage(event) {
  event.preventDefault();
  const out = $("#manage-result");
  const id = $("#booking_id").value.trim();
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(id)) {
    return show(out, "bad", "Booking IDs use letters, numbers, hyphens and underscores, for example B001.");
  }
  const button = $("#manage-btn");
  setBusy(button, true, "Looking up...");
  const res = await Api.getBooking(id);                                // GET /api/bookings/{id}
  setBusy(button, false);
  if (res.ok) out.replaceChildren(bookingCard(res.data));
  else show(out, "bad", friendlyError(res));
}

// ---------- tabs ----------
function showTab(name) {
  for (const tab of ["book", "manage"]) {
    $(`#tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`#panel-${tab}`).hidden = tab !== name;
  }
}

// ---------- request log ----------
function formatLog(e) {
  const lines = [`${e.method} ${e.url}`];
  for (const [name, value] of Object.entries(e.requestHeaders)) lines.push(`${name}: ${value}`);
  if (e.requestBody !== undefined) lines.push("", JSON.stringify(e.requestBody, null, 2));
  lines.push("", `Response: HTTP ${e.status || "no response"}`, JSON.stringify(e.data, null, 2));
  return lines.join("\n");
}

Api.onLog((e) => {
  const list = $("#log-list");
  list.prepend(el("li", {},
    el("details", {},
      el("summary", {},
        el("span", { class: `code c${Math.floor(e.status / 100)}` }, e.status || "ERR"),
        el("span", {}, `${e.method} ${e.target}`),
        el("span", {}, `${e.ms} ms`)),
      el("pre", {}, formatLog(e)))));
  $("#log-count").textContent = list.children.length;
  $("#log-empty").hidden = true;
});

// ---------- connection check ----------
async function checkService() {
  const res = await Api.health();                                      // GET /health
  $("#dot").className = "dot " + (res.ok ? "up" : "down");
  $("#service-text").textContent = CONFIG.USE_MOCK
    ? "Demo mode: using fake data in this browser"
    : res.ok ? `Booking service online at ${CONFIG.API_BASE}` : `Booking service not reachable at ${CONFIG.API_BASE}`;
}

// ---------- start ----------
$("#check_in").value = dateIn(1);
$("#check_out").value = dateIn(3);
$("#search-form").addEventListener("submit", onSearch);
$("#book-form").addEventListener("submit", onBook);
$("#book-back").addEventListener("click", resetBooking);
$("#manage-form").addEventListener("submit", onManage);
$("#tab-book").addEventListener("click", () => showTab("book"));
$("#tab-manage").addEventListener("click", () => showTab("manage"));
checkService();
