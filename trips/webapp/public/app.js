// Trip app (Telegram Mini App). Everything shown comes from the snapshot the
// bot session pushed. The one thing it can change is people's names (and the
// owner can add people): those requests are queued by the Worker and applied
// by the bot in the chat's own ledger. All text from the data goes in through
// textContent / text nodes, never innerHTML.
"use strict";
(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  // Colour follows the category; stacks run in palette-slot order (validated adjacent pairs).
  const CATS = ["food", "transport", "lodging", "activities", "shopping", "other", "drinks"];
  const KNOWN = new Set([...CATS, "settle-up"]);
  const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Wrapped cards: [background, ink, sticker 1, sticker 2], from the duck's world:
  // uniform navy, gold trim, sunset orange, terminal sky, cherry blossom, luggage silver.
  const CARD = {
    cover: ["#13213e", "#ffffff", "#e0ac3f", "#3d8be0"],
    total: ["#f28a30", "#13213e", "#ffd36b", "#f7b9cb"],
    genre: ["#63a8ec", "#13213e", "#f7b9cb", "#ffd36b"],
    top: ["#f2c14e", "#13213e", "#f28a30", "#ffffff"],
    peak: ["#f7b9cb", "#13213e", "#63a8ec", "#f2c14e"],
    awards: ["#dfe4ec", "#13213e", "#f7b9cb", "#f2c14e"],
    finale: ["#13213e", "#ffffff", "#f28a30", "#3d8be0"],
  };
  // Telegram's own header bar matches the navy shell (clients 6.9+ take a hex colour).
  const SHELL = { light: "#13213e", dark: "#142443" };
  const TABS = [
    ["balances", "Balances"], ["payments", "Payments"], ["charts", "Charts"], ["plan", "Plan"], ["places", "Places"], ["wrapped", "Wrapped"],
  ];

  const state = {
    trips: [], T: null, owner: false, tab: "balances", q: "", person: "", cat: "", day: "", sel: null, here: null, hereMsg: "",
    // People card: which inline form is open ({kind: "rename", name} or {kind: "add"}), its text, and the outcome.
    edit: null, draft: "", err: "", flash: "", busy: false,
  };
  // A name as the Worker accepts it: letters, digits, spaces and . ' - (1-31, starting with a letter or digit).
  const NAME_RE = /^[\p{L}\p{N}][\p{L}\p{M}\p{N} .'\-]{0,30}$/u;
  const SENT = "Sent. The bot will update the trip in about a minute.";

  // ------------------------------------------------------------- DOM helpers
  function setProps(n, props) {
    if (!props) return;
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "text") n.textContent = String(v);
      else if (k === "style") Object.assign(n.style, v); // CSSOM: allowed under the page's CSP
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : String(v));
    }
  }
  function add(n, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
    return n;
  }
  const fill = (n, ...kids) => { n.replaceChildren(); return add(n, kids); };
  const el = (tag, props, ...kids) => { const n = document.createElement(tag); setProps(n, props); return add(n, kids); };
  const sv = (tag, props, ...kids) => { const n = document.createElementNS(SVGNS, tag); setProps(n, props); return add(n, kids); };

  // -------------------------------------------------------------- formatting
  const nf2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const sgd = (c) => (c < 0 ? "-" : "") + "S$" + nf2.format(Math.abs(c) / 100);
  const sgd0 = (c) => "S$" + nf0.format(Math.round(c / 100));
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  const isDay = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s);
  const dt = (iso) => new Date(iso + "T00:00:00Z");
  const dayLabel = (iso) => (isDay(iso) ? `${WD[dt(iso).getUTCDay()]} ${dt(iso).getUTCDate()} ${MON[dt(iso).getUTCMonth()]}` : iso);
  const shortDay = (iso) => (isDay(iso) ? `${dt(iso).getUTCDate()} ${MON[dt(iso).getUTCMonth()]}` : iso);
  const catColour = (c) => `var(--c-${KNOWN.has(c) ? c : "other"})`;
  const sw = (colour) => el("i", { class: "sw", style: { background: colour }, "aria-hidden": "true" });
  // A luggage tag: tinted with the category's colour, the eyelet in full colour.
  const chip = (c) => {
    const n = el("span", { class: "chip" }, sw(catColour(c)), cap(c));
    n.style.setProperty("--cat", catColour(c));
    return n;
  };
  const youTag = () => el("span", { class: "me-tag", text: "you" });
  const icon = (d, cls) => sv("svg", { class: cls || "ico", viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" },
    sv("path", { d }));
  const PLANE = "M21 15.2v-1.9l-8-5V3.6a1.5 1.5 0 0 0-3 0v4.7l-8 5v1.9l8-2.5v4.6l-2 1.5v1.5l3.5-1 3.5 1v-1.5l-2-1.5v-4.6z";
  const PENCIL = "M3 17.25V21h3.75L17.81 9.94l-3.75-3.75zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75z";
  const PLUS = "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6z";
  const PIN = "M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5z";
  const spanText = (a, b) => {
    if (!isDay(a) || !isDay(b)) return "";
    const [x, y] = [dt(a), dt(b)];
    if (a === b) return shortDay(a);
    return x.getUTCMonth() === y.getUTCMonth() && x.getUTCFullYear() === y.getUTCFullYear()
      ? `${x.getUTCDate()}–${y.getUTCDate()} ${MON[y.getUTCMonth()]}` : `${shortDay(a)} – ${shortDay(b)}`;
  };
  function todayIn(tz) {
    try {
      return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
    } catch {
      return new Date().toISOString().slice(0, 10);
    }
  }
  function dueText(d, now) {
    if (!isDay(d || "")) return "";
    const days = Math.round((dt(d) - dt(now)) / 86400000);
    const rel = days < 0 ? "overdue" : days === 0 ? "today" : days === 1 ? "tomorrow" : `in ${days} days`;
    return `due ${shortDay(d)}, ${rel}`;
  }
  function niceStep(top) {
    const raw = top > 0 ? top / 4 : 1;
    const mag = 10 ** Math.floor(Math.log10(raw));
    return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw);
  }
  const card = (title, ...kids) => el("section", { class: "card" }, title ? el("h2", { text: title }) : null, ...kids);
  const kpi = (label, value) => el("div", { class: "kpi" }, el("span", { text: label }), el("b", { class: "num", text: value }));
  const empty = (text) => el("p", { class: "muted", text });
  function safeLink(url, label) {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return null;
    return el("a", {
      href: url, rel: "noopener noreferrer", target: "_blank",
      onclick: (e) => { if (tg && tg.openLink) { e.preventDefault(); tg.openLink(url); } },
    }, label || "link");
  }

  // ----------------------------------------------------------------- loading
  function status(text) {
    fill($("view"), el("p", { class: "status", text }));
  }
  function fail(text) {
    for (const id of ["top", "tabs"]) $(id).hidden = true;
    $("foot").hidden = false;
    $("asof").textContent = "";
    status(text);
  }

  async function load() {
    if (!tg || !tg.initData) {
      status("Open this from the trip's Telegram group: tap the app button the bot posts there.");
      return;
    }
    status("Loading…");
    let res;
    try {
      res = await fetch("/api/trips", {
        method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store",
        credentials: "same-origin", body: JSON.stringify({ initData: tg.initData }),
      });
    } catch {
      return fail("Couldn't reach the trip app. Check your connection, then tap Refresh.");
    }
    if (res.status === 401) return fail("Telegram couldn't confirm who you are. Close the app and open it again from the chat.");
    if (!res.ok) return fail(`The trip app had a problem (${res.status}). Try Refresh in a minute.`);
    const data = await res.json();
    state.owner = Boolean(data.owner);
    state.trips = (data.trips || []).slice().sort((a, b) =>
      (a.trip.status === "open" ? 0 : 1) - (b.trip.status === "open" ? 0 : 1)
      || String(b.trip.start || b.trip.created || "").localeCompare(String(a.trip.start || a.trip.created || "")));
    if (!state.trips.length) {
      return fail("You're not on a trip here yet. Post in your trip's group once (or send /join) so the bot links "
        + "your Telegram account to your name, then open this again.");
    }
    const want = (state.T && state.T.trip.id) || data.start_param;
    pick(state.trips.find((t) => t.trip.id === want) || state.trips[0], !state.T);
  }

  function pick(T, fresh = true) {
    const same = state.T && state.T.trip.id === T.trip.id;
    state.T = T;
    if (!same) {
      Object.assign(state, { q: "", person: "", cat: "", day: "", sel: null, edit: null, draft: "", err: "", flash: "" });
      if (fresh || !tabOk(state.tab)) state.tab = T.trip.phase === "planning" && T.plan ? "plan" : "balances";
    }
    if (!tabOk(state.tab)) state.tab = "balances";
    render();
  }

  const tabOk = (t) => (t === "plan" ? Boolean(state.T.plan) : t === "wrapped" ? Boolean(state.T.wrapped)
    : t === "places" ? Boolean(state.T.places && state.T.places.length) : true);

  // ------------------------------------------------------------------ chrome
  function render() {
    const T = state.T;
    const t = T.trip;
    $("top").hidden = false;
    $("trip-name").textContent = t.name;
    const phase = t.phase === "planning" ? "planning" : t.status === "closed" ? "closed" : "live";
    // No dates set on a running trip: show the days it has covered so far, like a flight's dates.
    const when = t.when === "dates not set" && phase !== "planning" && T.days.length
      ? spanText(T.days[0], T.days[T.days.length - 1]) || t.when : t.when;
    $("trip-sub").textContent = [when, plural(T.members.length, "person").replace("persons", "people")].join(" · ");
    $("trip-phase").textContent = cap(phase);
    $("trip-phase").dataset.phase = phase;
    document.title = t.name;

    const wrap = $("picker-wrap");
    wrap.hidden = state.trips.length < 2;
    if (state.trips.length > 1) {
      const sel = $("picker");
      fill(sel, ...state.trips.map((x) =>
        el("option", { value: x.trip.id, selected: x.trip.id === t.id }, x.trip.name + (x.trip.status === "closed" ? " (closed)" : ""))));
      sel.onchange = () => pick(state.trips.find((x) => x.trip.id === sel.value));
    }

    const tabs = $("tabs");
    tabs.hidden = false;
    fill(tabs, ...TABS.filter(([k]) => tabOk(k)).map(([k, label]) =>
      el("button", {
        type: "button", role: "tab", "aria-selected": String(k === state.tab),
        onclick: () => { state.tab = k; render(); window.scrollTo(0, 0); },
      }, label)));
    // More tabs than fit on a narrow phone: keep the current one in view, fade the edge until the end.
    const edge = () => tabs.classList.toggle("more", tabs.scrollLeft + tabs.clientWidth < tabs.scrollWidth - 2);
    const on = tabs.querySelector('[aria-selected="true"]');
    if (on && tabs.scrollWidth > tabs.clientWidth) {
      tabs.scrollLeft = Math.max(0, Math.min(on.offsetLeft - (tabs.clientWidth - on.offsetWidth) / 2, tabs.scrollWidth - tabs.clientWidth));
    }
    tabs.onscroll = edge;
    edge();

    const views = { balances: viewBalances, payments: viewPayments, charts: viewCharts, plan: viewPlan, places: viewPlaces, wrapped: viewWrapped };
    fill($("view"), ...views[state.tab](T));
    if (state.tab === "payments") refreshList();
    if (state.tab === "charts") drawDaily();
    if (state.tab === "wrapped") hookDeck();

    $("foot").hidden = false;
    let asof = T.generated_at;
    try {
      asof = new Intl.DateTimeFormat("en-GB", {
        timeZone: t.tz, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
      }).format(new Date(T.generated_at)) + " trip time";
    } catch { /* keep the raw timestamp */ }
    $("asof").textContent = `As of ${asof}.`;
  }

  // ---------------------------------------------------------------- balances
  function viewBalances(T) {
    const t = T.totals;
    // The key totals as a boarding pass: the main part and a tear-off stub.
    const out = [el("section", { class: "pass", "aria-label": "Trip totals" },
      el("div", { class: "pass-main" },
        el("div", { class: "kpi hero" }, el("span", { text: "Spent" }), el("b", { class: "num", text: sgd(t.spent) })),
        el("div", { class: "pass-fields" },
          kpi("Payments", String(t.payments)),
          T.me ? kpi("Passenger", T.me) : null)),
      el("div", { class: "pass-stub" },
        icon(PLANE, "ico plane"),
        kpi("Per person", sgd(t.per_head)), kpi("Per day", t.days ? sgd(t.per_day) : "–")))];

    const nets = Object.entries(T.nets).sort((a, b) => b[1].net - a[1].net || a[0].localeCompare(b[0]));
    const mx = Math.max(1, ...nets.map(([, v]) => Math.abs(v.net)));
    out.push(card("Who's owed, who owes",
      nets.length ? nets.map(([name, v]) => {
        const w = v.net ? Math.max(0.6, (Math.abs(v.net) / mx) * 50) : 0;
        const bar = v.net ? el("i", { class: v.net > 0 ? "pos" : "neg", style: { left: v.net > 0 ? "50%" : `${50 - w}%`, width: `${w}%` } }) : null;
        return el("div", { class: "bal" },
          el("div", { class: "line" },
            el("span", { class: "who" }, name, T.me === name ? youTag() : null),
            el("span", { class: "num" }, v.net > 0 ? `is owed ${sgd(v.net)}` : v.net < 0 ? `owes ${sgd(-v.net)}` : "square")),
          el("div", { class: "track", "aria-hidden": "true" }, bar),
          el("div", { class: "small num" }, `paid ${sgd(v.paid)} · share ${sgd(v.share)}`));
      }) : empty("Nobody on the trip yet."),
      el("p", { class: "note", text: "Bars: right is owed, left owes. All in SGD." })));

    // Settle-up as a departures board: who pays, who receives, how much.
    out.push(el("section", { class: "board" },
      el("h2", {}, icon(PLANE, "ico plane"), "To settle up"),
      T.settle.length
        ? [el("div", { class: "board-head", "aria-hidden": "true" },
          el("span", { text: "From" }), el("span", { text: "To" }), el("span", { text: "SGD" })),
        el("ol", { class: "plain settle" }, T.settle.map(([d, c, x]) =>
          el("li", { class: T.me && (T.me === d || T.me === c) ? "mine" : null },
            el("span", { class: "from" }, d, T.me === d ? youTag() : null),
            el("span", { class: "sr", text: " pays " }),
            el("span", { class: "to" }, c, T.me === c ? youTag() : null),
            el("span", { class: "num", text: sgd(x) }))))]
        : el("p", { class: "square", text: "Everyone is square." })));
    out.push(el("p", { class: "note board-note", text: `${cap(T.rate_note)}. The fewest transfers that clear every balance.` }));
    out.push(peopleCard(T));
    return out;
  }

  // ------------------------------------------------------------------ people
  // Rename (the owner: anyone; a member: themselves) and add people (the owner).
  // The request goes to the Worker's queue; the bot applies it in the chat and
  // the app shows it after the next sync. The Worker re-checks every rule.
  function peopleCard(T) {
    const open = T.trip.status === "open";
    const pending = T.pending || [];
    const renaming = new Map(pending.filter((a) => a.action === "rename_member").map((a) => [a.name, a.to]));
    const canRename = (name) => open && !renaming.has(name) && (state.owner || T.me === name);
    const ed = state.edit;

    const rows = T.members.map((m) => {
      if (ed && ed.kind === "rename" && ed.name === m.name) {
        return el("li", { class: "person-row editing" }, nameForm(T.me === m.name ? "Your new name" : `New name for ${m.name}`, "Save"));
      }
      const to = renaming.get(m.name);
      return el("li", { class: "person-row" },
        el("span", { class: "who" }, m.name, T.me === m.name ? youTag() : null,
          to ? el("span", { class: "wait-tag" }, `→ ${to}`) : null),
        canRename(m.name) && !state.busy
          ? el("button", {
            type: "button", class: "icon-btn", "aria-label": `Rename ${m.name}`, title: `Rename ${m.name}`,
            onclick: () => startEdit({ kind: "rename", name: m.name }, m.name),
          }, icon(PENCIL))
          : null);
    });
    for (const a of pending.filter((x) => x.action === "add_member")) {
      rows.push(el("li", { class: "person-row waiting" }, el("span", { class: "who" }, a.name, el("span", { class: "wait-tag", text: "joining" }))));
    }

    const kids = [el("ul", { class: "plain people" }, rows)];
    if (ed && ed.kind === "add") kids.push(el("div", { class: "add-form" }, nameForm("Add someone to the trip", "Add")));
    else if (open && state.owner && !state.busy) {
      kids.push(el("button", { type: "button", class: "add-btn", onclick: () => startEdit({ kind: "add" }, "") }, icon(PLUS), "Add person"));
    }
    if (state.flash) kids.push(el("p", { class: "flash", role: "status", text: state.flash }));
    if (pending.length && !state.flash) kids.push(el("p", { class: "note", text: "Waiting for the bot to apply the changes marked with an arrow. Tap Refresh in a minute." }));
    kids.push(el("p", { class: "note", text: !open
      ? "This trip is closed, so names can't be changed."
      : state.owner ? "Changes go through the bot, which posts them in the group."
        : "You can change your own name here. To change someone else's, ask the trip's owner." }));
    return card("People", kids);
  }

  function nameForm(label, verb) {
    const input = el("input", {
      id: "person-input", class: "name-input", type: "text", value: state.draft, maxlength: 40,
      autocomplete: "off", autocapitalize: "words", spellcheck: "false", enterkeyhint: "done",
      placeholder: "Name", "aria-invalid": state.err ? "true" : null,
      "aria-describedby": state.err ? "person-err" : null, disabled: state.busy,
      oninput: (e) => { state.draft = e.target.value; },
      onkeydown: (e) => { if (e.key === "Escape") cancelEdit(); },
    });
    return el("form", {
      class: "name-form", novalidate: true,
      onsubmit: (e) => { e.preventDefault(); submitEdit(); },
    },
    el("label", { class: "form-label", for: "person-input", text: label }),
    input,
    el("div", { class: "form-actions" },
      el("button", { type: "submit", class: "btn btn-sm", disabled: state.busy }, state.busy ? "Sending…" : verb),
      el("button", { type: "button", class: "btn-ghost", disabled: state.busy, onclick: cancelEdit }, "Cancel")),
    state.err ? el("p", { id: "person-err", class: "form-err", role: "alert", text: state.err }) : null);
  }

  function startEdit(edit, draft) {
    Object.assign(state, { edit, draft, err: "", flash: "" });
    render();
    const input = $("person-input");
    if (input) { input.focus(); input.select(); }
  }
  function cancelEdit() {
    Object.assign(state, { edit: null, draft: "", err: "" });
    render();
  }

  function checkName(T, name, except) {
    if (!NAME_RE.test(name) || name.trim() !== name) return "A name is 1–31 letters or digits; spaces and . ' - are fine.";
    const taken = [...T.members.map((m) => m.name).filter((n) => n !== except),
      ...(T.pending || []).map((a) => (a.action === "add_member" ? a.name : a.to)).filter(Boolean)];
    if (taken.some((n) => n.toLowerCase() === name.toLowerCase())) return `${name} is already on this trip.`;
    return "";
  }

  async function submitEdit() {
    const T = state.T;
    const ed = state.edit;
    if (!ed || state.busy) return;
    const name = state.draft.trim();
    if (ed.kind === "rename" && name === ed.name) return cancelEdit();
    const err = checkName(T, name, ed.kind === "rename" ? ed.name : null);
    if (err) { state.err = err; render(); const i = $("person-input"); if (i) i.focus(); return; }
    const body = ed.kind === "rename" ? { action: "rename_member", name: ed.name, to: name } : { action: "add_member", name };
    state.busy = true;
    state.err = "";
    render();
    let res, data = {};
    try {
      res = await fetch("/api/action", {
        method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store", credentials: "same-origin",
        body: JSON.stringify({ initData: tg.initData, trip_id: T.trip.id, ...body }),
      });
      data = await res.json().catch(() => ({}));
    } catch {
      res = null;
    }
    state.busy = false;
    if (res && res.ok && data.ok) {
      T.pending = [...(T.pending || []), { action: body.action, name: body.name, to: body.to || null }];
      Object.assign(state, { edit: null, draft: "", err: "", flash: SENT });
      render();
      return;
    }
    state.err = !res ? "Couldn't reach the trip app. Check your connection and try again."
      : res.status === 401 ? "Telegram couldn't confirm who you are. Close the app and open it again from the chat."
        : typeof data.error === "string" && data.error ? data.error : `The trip app had a problem (${res.status}). Try again in a minute.`;
    render();
    const i = $("person-input");
    if (i) i.focus();
  }

  // ---------------------------------------------------------------- payments
  function splitText(r, T) {
    const names = Object.keys(r.shares);
    if (r.kind === "transfer") return "";
    if (r.split_mode === "equal") {
      const all = T.members.length === names.length && T.members.every((m) => names.includes(m.name));
      return all ? `Split equally, everyone (${names.length})` : `Split equally: ${names.join(", ")}`;
    }
    return names.sort((a, b) => r.shares[b] - r.shares[a]).map((k) => `${k} ${sgd(r.shares[k])}`).join(", ");
  }

  function viewPayments(T) {
    const rows = T.rows;
    const people = Object.keys(T.nets);
    const cats = [...new Set(rows.map((r) => r.category))];
    const days = [...new Set(rows.map((r) => r.date))].sort().reverse();
    const select = (key, all, opts) => el("select", {
      "aria-label": all,
      onchange: (e) => { state[key] = e.target.value; refreshList(); },
    }, el("option", { value: "" }, all), opts.map(([v, label]) => el("option", { value: v, selected: state[key] === v }, label)));
    return [
      el("input", {
        class: "search", type: "search", placeholder: "Search payments", value: state.q, "aria-label": "Search payments",
        oninput: (e) => { state.q = e.target.value; refreshList(); },
      }),
      el("div", { class: "filters" },
        select("person", "Anyone", people.map((p) => [p, p])),
        select("cat", "Any category", cats.map((c) => [c, cap(c)])),
        select("day", "Any day", days.map((d) => [d, dayLabel(d)]))),
      el("p", { class: "count", id: "count" }),
      el("section", { class: "card" }, el("ol", { class: "plain", id: "pay-list" })),
    ];
  }

  function refreshList() {
    const T = state.T;
    const q = state.q.trim().toLowerCase();
    const hits = T.rows.filter((r) =>
      (!state.person || r.payer === state.person || Object.prototype.hasOwnProperty.call(r.shares, state.person))
      && (!state.cat || r.category === state.cat)
      && (!state.day || r.date === state.day)
      && (!q || [r.desc, r.payer, r.category, r.local, `#${r.entry}`].join(" ").toLowerCase().includes(q)))
      .sort((a, b) => b.date.localeCompare(a.date) || b.entry - a.entry);
    const exp = hits.filter((r) => r.kind === "expense");
    const tr = hits.length - exp.length;
    $("count").textContent = `${plural(exp.length, "payment")} · ${sgd(exp.reduce((s, r) => s + r.sgd, 0))}`
      + (tr ? ` · ${plural(tr, "settle-up")}` : "");
    fill($("pay-list"), ...(hits.length ? hits.map((r) => {
      const to = r.kind === "transfer" ? Object.keys(r.shares)[0] : null;
      return el("li", { class: "pay" },
        el("div", { class: "line" }, el("span", { class: "desc", text: r.desc }), el("span", { class: "num", text: sgd(r.sgd) })),
        el("div", { class: "meta" },
          chip(r.category),
          el("span", { text: to ? `${r.payer} → ${to}` : `${r.payer} paid` }),
          r.currency !== "SGD" ? el("span", { class: "num", text: r.local }) : null,
          el("span", { text: dayLabel(r.date) }),
          el("span", { class: "muted", text: `#${r.entry}` })),
        to ? null : el("div", { class: "split", text: splitText(r, T) }));
    }) : [el("li", { class: "pay muted", text: "No payments match." })]));
  }

  // ------------------------------------------------------------------ charts
  function viewCharts(T) {
    const out = [];
    const present = CATS.filter((c) => T.days.some((d) => (T.by_day_cat[d] || {})[c]));
    out.push(card("Spending per day (SGD)",
      present.length
        ? [el("div", { class: "legend" }, present.map((c) => el("span", {}, sw(catColour(c)), cap(c)))),
          el("div", { id: "daily" }), el("div", { id: "day-detail", class: "detail" })]
        : empty("Nothing spent yet.")));

    const total = T.cat_totals.reduce((s, [, v]) => s + v, 0);
    const top = Math.max(1, ...T.cat_totals.map(([, v]) => v));
    out.push(card("By category",
      T.cat_totals.length ? T.cat_totals.map(([c, v, n]) => el("div", { class: "hbar" },
        el("span", { class: "kv" }, el("span", { class: "k" }, sw(catColour(c)), cap(c))),
        el("span", { class: "bar", title: plural(n, "payment") }, el("i", { style: { width: `${Math.max(1, (100 * v) / top)}%`, background: catColour(c) } })),
        el("span", { class: "v num", text: `${sgd(v)} · ${Math.round((100 * v) / (total || 1))}%` })))
        : empty("Nothing spent yet.")));

    const nets = Object.entries(T.nets);
    const mx = Math.max(1, ...nets.flatMap(([, v]) => [v.paid, v.share]));
    out.push(card("Paid vs share",
      nets.map(([name, v]) => el("div", { class: "person" },
        el("div", { class: "who" }, name, T.me === name ? youTag() : null),
        [["Paid", v.paid, "var(--paid)"], ["Share", v.share, "var(--share)"]].map(([label, x, colour]) =>
          el("div", { class: "pair" }, el("span", { text: label }),
            el("span", { class: "bar" }, el("i", { style: { width: `${x ? Math.max(0.6, (100 * x) / mx) : 0}%`, background: colour } })),
            el("span", { class: "num", text: sgd(x) }))))),
      el("p", { class: "note", text: "Paid: what they fronted for the group. Share: their part of everything. The gap is their balance." })));
    return out;
  }

  function drawDaily() {
    const box = $("daily");
    if (!box) return;
    const T = state.T;
    const days = T.days;
    const data = (d) => T.by_day_cat[d] || {};
    const cats = CATS.filter((c) => days.some((d) => data(d)[c]));
    const totals = days.map((d) => Object.values(data(d)).reduce((s, v) => s + v, 0));
    if (!state.sel || !days.includes(state.sel)) {
      const spent = days.filter((d, i) => totals[i] > 0);
      state.sel = spent[spent.length - 1] || days[days.length - 1];
    }
    const W = Math.max(260, box.clientWidth || 320);
    const H = 210, L = 46, R = 6, TOP = 20, B = 26;
    const step = niceStep(Math.max(...totals, 100));
    const top = step * Math.max(1, Math.ceil(Math.max(...totals, 1) / step));
    const slot = (W - L - R) / days.length;
    const bw = Math.max(2, Math.min(30, slot * 0.62));
    const y = (v) => TOP + (H - TOP - B) * (1 - v / top);
    const svg = sv("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Spending per day in SGD, by category" });
    for (let v = 0; v <= top + 1e-9; v += step) {
      svg.append(sv("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), style: { stroke: v ? "var(--grid)" : "var(--axis)" } }));
      svg.append(sv("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", "font-size": 11, style: { fill: "var(--muted)" } }, sgd0(v)));
    }
    const every = Math.max(1, Math.ceil(days.length / Math.max(1, Math.floor((W - L - R) / 46))));
    days.forEach((d, i) => {
      const cx = L + slot * (i + 0.5);
      const x0 = cx - bw / 2;
      const on = d === state.sel;
      if (on) svg.append(sv("rect", { x: L + slot * i + 1, y: TOP - 16, width: Math.max(1, slot - 2), height: H - TOP - B + 16, rx: 6, style: { fill: "var(--mid)" } }));
      let base = 0;
      const segs = cats.filter((c) => data(d)[c]).map((c) => [c, data(d)[c]]);
      segs.forEach(([c, val], j) => {
        const yTop = y(base + val);
        const gap = j ? 2 : 0; // 2px surface gap between stacked segments
        const h = Math.max(y(base) - yTop - gap, 0.5);
        const fill = { fill: catColour(c) };
        if (j === segs.length - 1) { // rounded data end on the top segment
          const r = Math.min(4, h, bw / 2);
          svg.append(sv("path", {
            d: `M${x0},${yTop + h} V${yTop + r} Q${x0},${yTop} ${x0 + r},${yTop} H${x0 + bw - r} Q${x0 + bw},${yTop} ${x0 + bw},${yTop + r} V${yTop + h} Z`,
            style: fill,
          }));
        } else {
          svg.append(sv("rect", { x: x0, y: yTop, width: bw, height: h, style: fill }));
        }
        base += val;
      });
      if (totals[i] && (slot >= 34 || on)) {
        svg.append(sv("text", { x: cx, y: y(totals[i]) - 5, "text-anchor": "middle", "font-size": 11, "font-weight": on ? 700 : 400, style: { fill: "var(--ink2)" } }, sgd0(totals[i])));
      }
      if (i % every === 0 || on) {
        svg.append(sv("text", { x: cx, y: H - B + 16, "text-anchor": "middle", "font-size": 11, "font-weight": on ? 700 : 400, style: { fill: on ? "var(--ink)" : "var(--muted)" } }, shortDay(d)));
      }
      svg.append(sv("rect", {
        class: "hit", x: L + slot * i, y: 0, width: slot, height: H, style: { fill: "transparent" },
        onclick: () => { state.sel = d; drawDaily(); },
      }, sv("title", {}, `${dayLabel(d)}: ${sgd(totals[i])}`)));
    });
    fill(box, svg);

    const d = state.sel;
    const parts = cats.filter((c) => data(d)[c]).sort((a, b) => data(d)[b] - data(d)[a]);
    const pays = T.rows.filter((r) => r.kind === "expense" && r.date === d).sort((a, b) => b.sgd - a.sgd);
    fill($("day-detail"), 
      el("div", { class: "head" }, el("span", { text: dayLabel(d) }), el("span", { class: "num", text: sgd(totals[days.indexOf(d)]) })),
      parts.length ? parts.map((c) => el("div", { class: "kv" }, el("span", { class: "k" }, sw(catColour(c)), cap(c)), el("span", { class: "num", text: sgd(data(d)[c]) })))
        : empty("Nothing spent this day."),
      pays.length ? el("h3", { text: "Payments" }) : null,
      pays.map((r) => el("div", { class: "kv" }, el("span", { class: "k" }, `${r.desc} · ${r.payer}`), el("span", { class: "num", text: sgd(r.sgd) }))),
      el("p", { class: "note", text: "Tap a bar for that day." }));
  }

  // -------------------------------------------------------------------- plan
  function viewPlan(T) {
    const p = T.plan;
    const t = T.trip;
    const now = todayIn(t.tz);
    const out = [];
    const facts = [["When", t.when + (t.phase === "planning" && isDay(t.start || "") ? ` (${plural(Math.round((dt(t.start) - dt(now)) / 86400000), "day")} to go)` : "")]];
    if (p.destinations && p.destinations.length) facts.push(["Where", p.destinations.join(", ")]);
    if (p.budget_pp_sgd) facts.push(["Budget", `${sgd(Math.round(p.budget_pp_sgd * 100))} per person`]);
    facts.push(["Who", T.members.map((m) => m.name).join(", ") || "nobody yet"]);
    if (T.totals.spent && t.phase === "planning") facts.push(["Paid", `${sgd(T.totals.spent)} so far (deposits etc.)`]);
    out.push(card("The plan",
      el("dl", { class: "facts" }, facts.map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v })))),
      (p.notes || []).slice(-3).map((n) => el("p", { class: "note", text: n }))));

    if (p.decisions.length) {
      out.push(card("Decided", el("ul", { class: "plain" }, p.decisions.map((d) =>
        el("li", { class: "item" }, el("b", { text: `${d.topic}: ` }), d.outcome,
          el("div", { class: "small", text: `${d.how}, ${shortDay(d.date)}` }))))));
    }

    const open = p.polls.filter((x) => x.status === "open");
    if (open.length) {
      out.push(card("Open polls", open.map((x) => {
        const mx = Math.max(1, ...x.tally);
        return el("div", { class: "item poll" },
          el("b", { text: `#${x.n} ${x.question}` }),
          x.options.map((o, i) => el("div", { class: "opt" }, el("span", { text: o }),
            el("span", { class: "bar" }, el("i", { style: { width: `${(100 * x.tally[i]) / mx}%` } })),
            el("span", { class: "num", text: String(x.tally[i]) }))),
          el("div", { class: "small", text: x.not_voted.length ? `Not voted: ${x.not_voted.join(", ")}` : "Everyone voted" }));
      })));
    }

    const todos = p.todos.filter((x) => x.status !== "done");
    const done = p.todos.length - todos.length;
    if (p.todos.length) {
      const by = new Map();
      for (const x of todos) {
        const k = x.owner || "unassigned";
        if (!by.has(k)) by.set(k, []);
        by.get(k).push(x);
      }
      out.push(card("To-dos",
        [...by].map(([owner, items]) => el("div", { class: "item" }, el("b", { text: owner }),
          items.map((x) => el("div", {}, `#${x.n} ${x.task}`, x.due ? el("span", { class: "small", text: ` · ${dueText(x.due, now)}` }) : null)))),
        done ? el("p", { class: "note", text: `${done} done.` }) : null));
    }

    if (p.bookings.length) {
      const est = p.bookings.reduce((s, b) => s + (b.est_sgd || 0), 0);
      out.push(card("Bookings",
        el("ul", { class: "plain" }, p.bookings.map((b) => el("li", { class: "item" },
          el("span", { class: "status-icon", "data-status": ["booked", "held"].includes(b.status) ? b.status : "todo", "aria-hidden": "true" }),
          el("b", { text: b.item }), ` · ${b.status === "todo" ? "to do" : b.status}`,
          el("div", { class: "small" }, [b.who || "nobody yet",
            b.est_sgd ? `~${sgd(Math.round(b.est_sgd * 100))}` : null,
            b.deadline && b.status !== "booked" ? dueText(b.deadline, now) : null].filter(Boolean).join(" · "),
          b.link ? [" · ", safeLink(b.link)] : null)))),
        est ? el("p", { class: "note", text: `Estimated bookings: ${sgd(Math.round(est * 100))} in total. The bot never books or pays.` }) : null));
    }

    const ideas = p.ideas.filter((i) => i.status !== "dropped")
      .sort((a, b) => ["chosen", "shortlisted", "idea"].indexOf(a.status) - ["chosen", "shortlisted", "idea"].indexOf(b.status));
    if (ideas.length) {
      out.push(card("Ideas", el("ul", { class: "plain" }, ideas.map((i) => el("li", { class: "item" },
        el("span", { text: `#${i.n} ${i.text}` }),
        el("div", { class: "small" }, [i.status !== "idea" ? i.status : null, i.kind !== "other" ? i.kind : null, i.by ? `by ${i.by}` : null].filter(Boolean).join(" · "),
          i.link ? [" · ", safeLink(i.link)] : null))))));
    }

    const days = Object.keys(p.itinerary || {}).sort();
    if (days.length) {
      out.push(card("Itinerary", days.map((d) => [
        el("div", { class: "day-head", text: dayLabel(d) }),
        el("ol", { class: "plain" }, (p.itinerary[d] || []).map((it) => el("li", { class: "item" },
          [it.time, it.what].filter(Boolean).join(" "),
          it.where ? el("span", { class: "small", text: ` @ ${it.where}` }) : null,
          it.booked ? el("span", { class: "small", text: " (booked)" }) : null))),
      ])));
    }
    if (out.length === 1) out.push(card(null, empty("Nothing decided yet. Ideas and polls go in the group chat.")));
    return out;
  }

  // ------------------------------------------------------------------ places
  // The bot's suggestions from the group chat. "Distances from me" asks the
  // phone for its position and works the distances out here, on the device:
  // the position is never sent anywhere.
  function kmBetween(a, b) {
    const p = Math.PI / 180;
    const h = Math.sin((b.lat - a.lat) * p / 2) ** 2
      + Math.cos(a.lat * p) * Math.cos(b.lat * p) * Math.sin((b.lon - a.lon) * p / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.sqrt(h));
  }
  function locateMe() {
    state.hereMsg = "Finding you…";
    render();
    const done = (lat, lon) => { state.here = { lat, lon, at: Date.now() }; state.hereMsg = ""; render(); };
    const failed = () => { state.hereMsg = "Couldn't get your location. Allow location for Telegram, or ask the bot in the chat."; render(); };
    const lm = tg && tg.LocationManager;
    if (lm && lm.init) {
      lm.init(() => {
        if (lm.isLocationAvailable) lm.getLocation((loc) => (loc ? done(loc.latitude, loc.longitude) : failed()));
        else if (navigator.geolocation) navigator.geolocation.getCurrentPosition((p) => done(p.coords.latitude, p.coords.longitude), failed, { timeout: 15000 });
        else failed();
      });
    } else if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition((p) => done(p.coords.latitude, p.coords.longitude), failed, { timeout: 15000 });
    } else failed();
  }
  function placeLine(pl) {
    const bits = [];
    if (state.here && pl.lat != null && pl.lon != null) {
      const d = kmBetween(state.here, pl) * 1.3; // streets aren't straight lines
      bits.push(`${d < 10 ? d.toFixed(1) : Math.round(d)} km from you now`);
      if (d <= 3) bits.push(`🚶 ~${Math.max(1, Math.round((d / 4.8) * 60))} min`);
      bits.push(`🛵 ~${Math.max(3, Math.round((d / 18) * 60 + 3))} min`);
    } else if (pl.km != null) {
      bits.push(`${pl.walk_km != null ? pl.walk_km : pl.km} km`);
      if (pl.walk_min && pl.walk_min <= 30) bits.push(`🚶 ${pl.walk_min} min`);
      if (pl.ride_min) bits.push(`🛵 ~${pl.ride_min} min`);
    }
    return bits.join(" · ");
  }
  function viewPlaces(T) {
    const t = T.trip;
    const out = [card(null,
      el("button", { type: "button", class: "btn", onclick: locateMe }, icon(PIN), state.here ? "Update distances from me" : "Distances from me"),
      el("p", { class: "small", text: state.hereMsg || (state.here
        ? "Distances from where you are now, worked out on your phone. Your location isn't sent anywhere."
        : "Shows how far each place is from where you are now. Your location stays on your phone.") }))];
    for (const s of T.places) {
      let when = "";
      try {
        when = new Intl.DateTimeFormat("en-GB", { timeZone: t.tz, weekday: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(s.at));
      } catch { /* leave blank */ }
      let places = s.places.slice();
      if (state.here) {
        places.sort((a, b) => (a.lat == null) - (b.lat == null)
          || (a.lat == null ? 0 : kmBetween(state.here, a) - kmBetween(state.here, b)));
      }
      out.push(card(cap(s.request || "Places"),
        el("p", { class: "small", text: [s.asked_by ? `asked by ${s.asked_by}` : null, when, s.near ? `near ${s.near}` : null].filter(Boolean).join(" · ") }),
        el("ol", { class: "plain" }, places.map((pl) => el("li", { class: "item" },
          el("b", {}, safeLink(pl.maps, pl.name) || pl.name),
          el("div", { class: "small", text: [placeLine(pl), pl.why, pl.price, pl.hours_today].filter(Boolean).join(" · ") }))))));
    }
    out.push(el("p", { class: "note", text: "Ask in the group: share your location, then e.g. \"dessert near me\". Hours change; check before heading out." }));
    return out;
  }

  // ----------------------------------------------------------------- wrapped
  function wcard(kind, ...kids) {
    const [bg, ink, a, b] = CARD[kind];
    return el("section", { class: `wcard w-${kind}`, style: { background: bg, color: ink } },
      kind === "cover" ? el("img", { class: "cover-art", src: "duck-terminal.webp", alt: "", width: 640, height: 360, decoding: "async" }) : null,
      el("i", { class: "blob b1", style: { background: a } }), el("i", { class: "blob b2", style: { background: b } }),
      el("div", { class: "inner" }, ...kids));
  }

  function viewWrapped(T) {
    const w = T.wrapped;
    const names = T.members.map((m) => m.name);
    const cards = [];
    cards.push(wcard("cover",
      el("p", { class: "kicker", text: `${dayLabel(w.first)} – ${dayLabel(w.last)}` }),
      el("h2", { class: "mega" }, `${T.trip.name},`, el("br"), "Wrapped"),
      el("p", { class: "lead", text: `${plural(names.length, "traveller")} · ${plural(w.n_days, "day")} · ${plural(w.n_payments, "payment")}` }),
      el("p", { text: names.join(" · ") })));
    cards.push(wcard("total",
      el("p", { class: "kicker", text: "Together you spent" }),
      el("p", { class: "huge num", text: sgd(w.total) }),
      w.local_line ? el("p", { class: "lead", text: w.local_line }) : null,
      el("div", { class: "pair2" }, el("div", {}, el("b", { class: "num", text: sgd(w.per_day) }), el("span", { text: "a day" })),
        el("div", {}, el("b", { class: "num", text: sgd(w.per_head) }), el("span", { text: "each, on average" }))),
      w.ride_avg ? el("p", { class: "aside", text: `That's ${nf0.format(w.rides)} rides at your average fare of ${sgd(w.ride_avg)}.` }) : null));
    const c0 = w.cats[0];
    cards.push(wcard("genre",
      el("p", { class: "kicker", text: "Your top category" }),
      el("p", { class: "huge", text: cap(w.top_cat) }),
      el("p", { class: "lead" }, `${c0[2]} of everything. You were `, el("b", { text: w.vibe }), `. ${w.vibe_line}`),
      el("ol", { class: "rows" }, w.cats.map(([c, v, pct], i) => el("li", {},
        el("span", { text: String(i + 1) }), el("span", { text: cap(c) }),
        el("span", { class: "wbar" }, el("i", { style: { width: `${Math.max(4, (100 * v) / c0[1])}%` } })),
        el("span", { class: "val", text: pct }))))));
    cards.push(wcard("top",
      el("p", { class: "kicker", text: `Your top ${w.top.length}` }),
      el("h2", { class: "title", text: "The bills you'll remember" }),
      el("ol", { class: "tracks" }, w.top.map((r, i) => el("li", {},
        el("span", { class: "no", text: String(i + 1) }),
        el("span", {}, el("b", { text: r.desc }), el("small", { text: `${r.payer} paid · ${dayLabel(r.date)}` })),
        el("span", { class: "amt num", text: sgd(r.sgd) }))))));
    const pmax = Math.max(1, ...w.days.map(([, v]) => v));
    cards.push(wcard("peak",
      el("p", { class: "kicker", text: "Your biggest day" }),
      el("p", { class: "huge", text: dayLabel(w.peak) }),
      el("p", { class: "lead", text: `${sgd(w.peak_sgd)}, ${w.peak_pct} of the whole trip. The highlights: ${w.peak_highlights.join(", ")}.` }),
      el("ol", { class: "rows days" }, w.days.map(([d, v]) => el("li", { class: d === w.peak ? "on" : null },
        el("span", { text: shortDay(d) }),
        el("span", { class: "wbar" }, el("i", { style: { width: `${Math.max(2, (100 * v) / pmax)}%` } })),
        el("span", { class: "val num", text: sgd(v) }))))));
    cards.push(wcard("awards",
      el("p", { class: "kicker", text: "The awards" }),
      el("h2", { class: "title", text: "Everyone brought something" }),
      el("div", { class: "tiles" }, w.awards.map((a) => el("div", { class: "tile" },
        el("span", { class: "emo", "aria-hidden": "true", text: a.emoji }), el("b", { text: a.title }),
        el("span", { class: "who", text: a.name }), el("small", { text: a.line }))))));
    cards.push(wcard("finale",
      el("p", { class: "kicker", text: "Closing the tab" }),
      w.settle.length
        ? [el("h2", { class: "title", text: `${plural(w.settle.length, "transfer")} and you're all square` }),
          el("ol", { class: "moves" }, w.settle.map(([d, c, x]) => el("li", {}, el("b", { text: d }), el("span", { text: "→" }), el("b", { text: c }), el("em", { class: "num", text: sgd(x) }))))]
        : el("h2", { class: "title", text: "Everyone's already square. Legendary." }),
      el("div", { class: "summary" },
        [["Spent", sgd(w.total)], ["Top category", cap(w.top_cat)], ["Biggest day", dayLabel(w.peak)], ["Paid the most", w.bank]]
          .map(([k, v]) => el("div", {}, el("span", { text: k }), el("b", { text: v })))),
      el("p", { class: "aside", text: "Same again next year?" })));
    return [el("div", { class: "deck", id: "deck" }, cards),
      el("div", { class: "dots", id: "dots" }, cards.map((_, i) =>
        el("button", { type: "button", "aria-label": `Card ${i + 1}`, onclick: () => goCard(i) })))];
  }

  function cardStep(deck) {
    const first = deck.firstElementChild;
    return first ? first.getBoundingClientRect().width + 10 : deck.clientWidth;
  }
  function goCard(i) {
    const deck = $("deck");
    const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    deck.scrollTo({ left: i * cardStep(deck), behavior: still ? "auto" : "smooth" });
  }
  function hookDeck() {
    const deck = $("deck");
    const dots = [...$("dots").children];
    const mark = () => {
      const i = Math.round(deck.scrollLeft / cardStep(deck));
      dots.forEach((d, k) => d.setAttribute("aria-current", String(k === i)));
    };
    deck.addEventListener("scroll", mark, { passive: true });
    mark();
  }

  // -------------------------------------------------------------------- boot
  function applyTheme() {
    if (!tg) return;
    const scheme = tg.colorScheme === "dark" ? "dark" : "light";
    document.documentElement.dataset.scheme = scheme;
    try {
      if (tg.isVersionAtLeast && tg.isVersionAtLeast("6.9")) {
        tg.setHeaderColor(SHELL[scheme]);
        tg.setBackgroundColor("secondary_bg_color");
      } else if (tg.isVersionAtLeast && tg.isVersionAtLeast("6.1")) {
        tg.setHeaderColor("secondary_bg_color");
        tg.setBackgroundColor("secondary_bg_color");
      }
    } catch { /* older clients */ }
  }

  if (tg) {
    tg.ready();
    tg.expand();
    applyTheme();
    tg.onEvent("themeChanged", () => { applyTheme(); if (state.T) render(); });
    try { if (tg.isVersionAtLeast && tg.isVersionAtLeast("7.7")) tg.disableVerticalSwipes(); } catch { /* optional */ }
  }
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.T && state.tab === "charts") drawDaily(); }, 150);
  });
  $("refresh").addEventListener("click", () => load());
  load();
})();
