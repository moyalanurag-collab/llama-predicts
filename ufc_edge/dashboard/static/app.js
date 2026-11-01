/* Llama Predicts dashboard: single-page app, no build step. */
(function () {
  "use strict";

  // ------------------------------------------------------------------ state
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  };
  let mode = store.get("ufc-mode", "paper");
  let backtestVariant = store.get("ufc-bt", "current");
  const main = document.getElementById("main");
  const dialog = document.getElementById("dialog");

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (p) => (p == null ? "–" : `${p < 0 ? "−" : ""}£${(Math.abs(p) / 100).toFixed(2)}`);
  const signed = (p) => (p == null ? "–" : `${p > 0 ? "+" : p < 0 ? "−" : ""}£${(Math.abs(p) / 100).toFixed(2)}`);
  const pct = (x, d = 0) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
  const spct = (x, d = 1) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x * 100).toFixed(d)}%`);
  const cls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
  const tz = { timeZone: "Europe/London" };
  const when = (iso, withDay = true) => {
    if (!iso) return "–";
    const d = new Date(iso);
    if (isNaN(d)) return esc(iso);
    return d.toLocaleString("en-GB", { ...tz, ...(withDay ? { weekday: "short" } : {}), day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  };
  const day = (iso) => (iso ? new Date(iso).toLocaleDateString("en-GB", { ...tz, day: "numeric", month: "short", year: "numeric" }) : "–");
  const ago = (iso) => {
    if (!iso) return "never";
    const m = (Date.now() - new Date(iso)) / 60000;
    if (m < 1) return "just now";
    if (m < 60) return `${Math.round(m)} min ago`;
    if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
    return `${Math.round(m / 1440)} days ago`;
  };

  async function get(path) {
    const sep = path.includes("?") ? "&" : "?";
    const r = await fetch(`${path}${sep}mode=${mode}`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-UFC-Edge": "1" }, body: JSON.stringify({ mode, ...body }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.role = "status"; t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4200);
  }

  // ------------------------------------------------------------------ icons
  const icon = {
    overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>',
    card: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3h8l5 5v8l-5 5H8l-5-5V8z"/></svg>',
    plan: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5h11M9 12h11M9 19h11"/><path d="m3 5 1.5 1.5L7 4M3 12l1.5 1.5L7 11M3 19l1.5 1.5L7 18"/></svg>',
    bankroll: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/><path d="M9 12h6"/></svg>',
    performance: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>',
    ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 3 2 21h20z"/><path d="M12 10v5M12 18v.5"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M8 2h8l6 6v8l-6 6H8l-6-6V8z"/><path d="M8 12h8"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>',
  };

  const pages = [
    ["overview", "Overview"], ["card", "Fight card"], ["plan", "Betting plan"],
    ["bankroll", "Bankroll"], ["performance", "Performance"], ["settings", "Settings"],
  ];

  // Risk-engine reason codes in plain English.
  const REASONS = {
    NOT_INITIALISED: "No bankroll set up yet",
    BANKROLL_EXHAUSTED: "Bankroll exhausted: betting is disabled until you authorise a restart",
    KILL_SWITCH: "The kill switch is on",
    SYSTEM_HALTED: "The system halted after a ledger check failed",
    MARKET_NOT_ALLOWED: "This market isn't allowed",
    UNKNOWN_EVENT: "Unknown event",
    EVENT_STARTED: "The event has already started",
    STALE_ODDS: "The odds are too old. Refresh odds just before betting",
    ODDS_TIMESTAMP_IN_FUTURE: "The odds timestamp can't be verified",
    ODDS_OUT_OF_RANGE: "Odds outside your allowed range",
    INSUFFICIENT_EDGE: "Edge too small after the risk engine's own check",
    STAKE_BELOW_MINIMUM: "Stake below your bookmaker minimum",
    WOULD_USE_PROTECTED_FUNDS: "Would need protected profit, which is never staked",
    STAKE_EXCEEDS_ACTIVE_BANKROLL: "Stake is more than the active bankroll",
    BELOW_MIN_BANKROLL: "Active bankroll is below the minimum",
    STAKE_EXCEEDS_PER_BET_LIMIT: "Stake above the per-bet limit",
    EVENT_EXPOSURE_LIMIT: "Would put too much on one event",
    TOTAL_EXPOSURE_LIMIT: "Would put too much on open bets overall",
    DAILY_LOSS_LIMIT: "Could breach the 24-hour loss limit",
    EVENT_LOSS_LIMIT: "Could breach the per-event loss limit",
    MAX_BETS_PER_EVENT: "Already at the maximum bets for this event",
    DUPLICATE_SELECTION: "Already bet on this fighter",
    IDEMPOTENCY_KEY_REUSED: "Conflicts with an earlier bet",
  };
  const reasonText = (r) => REASONS[r.split(":")[0]] || r;

  const LIMIT_NAMES = {
    kelly_fraction: "Kelly fraction", model_weight: "Model weight", min_edge: "Minimum edge",
    max_stake_fraction: "Largest single stake", min_stake_pence: "Smallest stake", max_event_exposure_fraction: "Most on one event",
    max_total_exposure_fraction: "Most on all open bets", max_daily_loss_fraction: "24-hour loss limit",
    max_event_loss_fraction: "Per-event loss limit", max_bets_per_event: "Bets per event", min_active_bankroll_pence: "Stop betting below",
    max_odds_age_seconds: "Odds must be fresher than", min_decimal_odds: "Shortest odds", max_decimal_odds: "Longest odds",
    allowed_markets: "Markets", execution_mode: "Execution",
  };

  const KINDS = {
    DEPOSIT: "Bankroll authorised", BET_PLACED: "Bet placed", BET_WON: "Bet won", BET_LOST: "Bet lost",
    BET_VOID: "Bet void (stake returned)", PROFIT_SWEEP: "Profit moved to protected", PROTECTED_TRANSFER_OUT: "Protected profit withdrawn",
  };

  function systemState(st) {
    if (!st || st.status === "UNINITIALISED") return { cls: "warn", icon: icon.warn, title: "Not set up yet", line: "Set up a bankroll to start recording bets." };
    if (st.status === "EXHAUSTED") return { cls: "stop", icon: icon.stop, title: "Betting disabled", line: "The bankroll is exhausted. Only a typed authorisation from the command line can restart it." };
    if (st.halted) return { cls: "stop", icon: icon.stop, title: "Halted", line: `A ledger check failed: ${st.halt_reason || ""}` };
    if (st.kill_switch) return { cls: "stop", icon: icon.stop, title: "Betting stopped", line: `Kill switch on: ${st.kill_reason || ""}` };
    return { cls: "good", icon: icon.ok, title: "Betting allowed", line: "All checks pass. Every bet still goes through the risk engine." };
  }

  // ------------------------------------------------------------------ chrome
  function renderNav() {
    const current = location.hash.slice(1) || "overview";
    document.getElementById("nav").innerHTML = pages.map(([k, label]) =>
      `<a href="#${k}" ${k === current ? 'aria-current="page"' : ""}>${icon[k]}${label}</a>`).join("");
    document.querySelectorAll(".segmented button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  }

  function renderFreshness(f) {
    if (!f) return;
    const oddsStale = !f.odds_captured_at || (Date.now() - new Date(f.odds_captured_at)) / 3.6e6 > 24;
    document.getElementById("freshness").innerHTML = `
      <dt>Results up to</dt><dd>${day(f.newest_result)}</dd>
      <dt>Odds snapshot</dt><dd class="${oddsStale ? "stale" : ""}">${f.odds_captured_at ? ago(f.odds_captured_at) : "none yet"}</dd>
      <dt>Model trained on data to</dt><dd>${day(f.model_data_through)}</dd>`;
  }

  // ------------------------------------------------------------------ overview
  function boundary(st) {
    const cap = st.bankroll_cap_pence || 4000;
    const A = st.active_pence, O = st.open_stakes_pence, P = st.protected_pence + st.withdrawn_pence;
    const H = Math.max(0, cap - A - O);
    const D = cap + Math.max(P, cap * 0.32) + cap * 0.02;
    const w = (v) => `${(v / D) * 100}%`;
    const fence = cap / D;
    const gap = (cap * 0.02) / D;
    return `
      <div class="boundary" role="img" aria-label="Capital boundary: ${money(A)} active, ${money(O)} in open bets, ${money(P)} protected outside the ${money(cap)} limit">
        <div class="boundary-track">
          <div class="boundary-seg active" style="left:0;width:${w(A)}"></div>
          <div class="boundary-seg open" style="left:${w(A)};width:${w(O)}"></div>
          ${H > 0 ? `<div class="boundary-seg headroom" style="left:${w(A + O)};width:${w(H)}"></div>` : ""}
          <div class="boundary-fence" style="left:${fence * 100}%"><span>${money(cap)} limit</span></div>
          ${P > 0 ? `<div class="boundary-seg protected" style="left:${(fence + gap) * 100}%;width:${w(P)}"></div>`
                  : `<div class="boundary-seg headroom" style="left:${(fence + gap) * 100}%;width:${w(cap * 0.3)};opacity:.55"></div>`}
        </div>
        <div class="legend" style="margin-top:6px">
          <span><i style="background:var(--active);height:9px;width:9px"></i>Active bankroll <b>${money(A)}</b></span>
          <span><i style="background:var(--open);height:9px;width:9px"></i>In open bets <b>${money(O)}</b></span>
          ${H > 0 ? `<span><i style="background:var(--rule-strong);height:9px;width:9px"></i>Lost so far <b>${money(H)}</b></span>` : ""}
          <span><i style="background:var(--protected);height:9px;width:9px"></i>Protected profit <b>${money(P)}</b>${st.withdrawn_pence ? ` (${money(st.withdrawn_pence)} withdrawn)` : ""}</span>
        </div>
        <p class="boundary-note">Everything left of the line is the most of your own money that can ever be at risk. Profit above ${money(cap)} moves across the line as soon as a bet settles, and it can never be staked again.
        ${st.protected_pence ? "" : " Protected profit is tracked in the ledger; it only counts as physically separate once you record a transfer out."}</p>
      </div>`;
  }

  async function renderOverview() {
    const d = await get("/api/overview");
    renderFreshness(d.freshness);
    const st = d.status, s = systemState(st), rec = d.record;
    const card = d.card;
    const model = d.freshness.model_summary;
    main.innerHTML = `
      <div class="page-head"><div><h1>Overview</h1><p class="lede">${mode === "paper" ? "Paper ledger: hypothetical bets at real odds, no money involved." : "Live ledger: bets you have actually placed."}</p></div></div>
      <section class="panel hero" aria-labelledby="state">
        <div class="hero-top">
          <div>
            <div class="status ${s.cls}">${s.icon}<span>${mode === "paper" ? "Paper" : "Live"} ledger</span></div>
            <p class="hero-state ${s.cls === "stop" ? "stop" : ""}" id="state">${s.title}</p>
            <p class="lede">${esc(s.line)}</p>
            ${st.status === "UNINITIALISED" ? `<p style="margin-top:14px"><button class="btn primary" data-action="init">Set up a ${mode} bankroll</button></p>` : ""}
          </div>
          <dl class="hero-figs">
            <div class="fig"><dt>Net betting result</dt><dd class="${cls(st.net_pnl_pence)}">${signed(st.net_pnl_pence)}</dd></div>
            <div class="fig"><dt>Protected profit</dt><dd>${money(st.protected_pence + st.withdrawn_pence)}</dd></div>
            <div class="fig"><dt>Your money in</dt><dd>${money(st.owner_contributed_pence)}</dd></div>
          </dl>
        </div>
        ${st.status !== "UNINITIALISED" ? boundary(st) : ""}
      </section>

      <section class="panel"><div class="band">
        <div>
          <h3>Next card</h3>
          ${card ? `<div class="big">${card.fights} fights</div><p>${when(card.first)}. ${card.value_flags ? `${card.value_flags} with possible value.` : "None flagged as possible value."} <a href="#card">See the card</a></p>`
                 : `<div class="big">Nothing scheduled</div><p>Run <code>python -m ufc_edge weekly</code> in fight week.</p>`}
        </div>
        <div>
          <h3>${mode === "paper" ? "Paper" : "Live"} record</h3>
          ${rec.graded ? `<div class="big ${cls(rec.pnl_pence)}">${signed(rec.pnl_pence)}</div><p>${rec.graded} settled bets, ${rec.won} won. Return on stakes ${spct(rec.roi)}.${rec.avg_clv != null ? ` Beat the closing odds by ${spct(rec.avg_clv)} on average.` : ""}</p>`
                       : `<div class="big">No settled bets yet</div><p>Results appear here after each event is settled. Treat any figure under 100 bets as noise.</p>`}
        </div>
        <div>
          <h3>Model against the market</h3>
          ${model ? `<div class="big">${model.beats_market ? "Slightly ahead" : "Not ahead"}</div><p>Prediction error ${model.final_log_loss.toFixed(4)} against the bookmakers' ${model.market_log_loss.toFixed(4)} (lower is better). Edge not yet proven. <a href="#performance">Why</a></p>`
                  : `<div class="big">Not trained</div><p>Run <code>python -m ufc_edge train</code>.</p>`}
        </div>
      </div></section>

      <div class="grid-2" style="margin-top:16px">
        <section class="panel"><div class="panel-head"><div><h2>Bankroll over time</h2><span class="muted small">Every ledger transaction</span></div></div><div id="ovChart"></div></section>
        <section class="panel"><div class="panel-head"><h2>Open bets</h2><span class="muted small">${d.open_bets.length} open, ${money(st.open_stakes_pence)} staked</span></div>${betsTable(d.open_bets, true)}</section>
      </div>`;
    bindActions();
    historyChart(document.getElementById("ovChart"), d.history);
  }

  function historyChart(node, history) {
    if (!history || history.length < 2) {
      node.innerHTML = `<div class="empty"><h3>No history yet</h3><p>The chart starts once there are bets in this ledger.</p></div>`;
      return;
    }
    const pts = (k) => history.map((h) => ({ x: new Date(h.at), y: h[k] / 100 }));
    Charts.lineChart(node, {
      ariaLabel: "Active bankroll and protected profit over time", step: true, height: 240,
      yFormat: (v) => `£${v.toFixed(v < 10 ? 2 : 0)}`,
      xFormat: (() => {
        const span = (new Date(history[history.length - 1].at) - new Date(history[0].at)) / 864e5;
        return span < 2 ? (v) => new Date(v).toLocaleString("en-GB", { ...tz, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
                        : (v) => new Date(v).toLocaleDateString("en-GB", { ...tz, day: "numeric", month: "short" });
      })(),
      tipTitle: (p) => when(p.x),
      series: [
        { name: "Active", color: "var(--active)", points: pts("active") },
        { name: "Protected", color: "var(--protected)", points: pts("protected") },
      ],
    });
    node.insertAdjacentHTML("afterbegin", `<div class="legend"><span><i style="background:var(--active)"></i>Active bankroll</span><span><i style="background:var(--protected)"></i>Protected profit</span></div>`);
  }

  function betsTable(bets, compact) {
    if (!bets.length) return `<div class="empty"><p>No ${compact ? "open " : ""}bets.</p></div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Bet</th><th class="num">Odds</th><th class="num">Stake</th><th class="num">${compact ? "Could return" : "Result"}</th>${compact ? "" : "<th class=num>Closing odds</th>"}</tr></thead><tbody>
      ${bets.map((b) => {
        const res = b.status === "OPEN" ? money(Math.floor(b.stake_pence * b.decimal_odds)) : `<span class="${b.status === "WON" ? "pos" : b.status === "LOST" ? "neg" : ""}">${b.status === "VOID" ? "Void" : signed((b.returned_pence || 0) - b.stake_pence)}</span>`;
        const beyond = b.beyond_limits ? ` <span class="tag warn" title="Paper experiment: placed beyond your risk limits">${icon.warn}Next safest bet, beyond risk limits</span>` : "";
        return `<tr><td><b>${esc(b.selection)}</b>${beyond}<span class="sub">vs ${esc(b.opponent_name || "–")}, ${when(b.commence_time || b.placed_at)}</span></td>
          <td class="num">${Number(b.decimal_odds).toFixed(2)}</td><td class="num">${money(b.stake_pence)}</td><td class="num">${res}</td>
          ${compact ? "" : `<td class="num">${b.closing_odds ? Number(b.closing_odds).toFixed(2) : "–"}${b.clv != null ? `<span class="sub ${cls(b.clv)}">${spct(b.clv)} vs close</span>` : ""}</td>`}</tr>`;
      }).join("")}</tbody></table></div>`;
  }

  // ------------------------------------------------------------------ fight card
  function parseFactors(s) {
    return String(s || "").split(";").map((x) => x.trim()).filter(Boolean).map((x) => { const [f, who] = x.split("→").map((y) => y.trim()); return { f, who }; });
  }

  // Bookmaker home pages for the "Open bookmaker" button (you place the bet there yourself).
  const BOOKS = {
    "Betfair": "https://www.betfair.com/exchange/plus/", "Betfair Sportsbook": "https://www.betfair.com/sport/",
    "Sky Bet": "https://www.skybet.com/", "Paddy Power": "https://www.paddypower.com/", "Betway": "https://betway.com/en/sports",
    "Unibet (UK)": "https://www.unibet.co.uk/", "Unibet": "https://www.unibet.co.uk/", "888sport": "https://www.888sport.com/",
    "LiveScore Bet": "https://www.livescorebet.com/", "Virgin Bet": "https://www.virginbet.com/",
    "Grosvenor": "https://www.grosvenorcasinos.com/sport", "Matchbook": "https://www.matchbook.com/",
    "William Hill": "https://sports.williamhill.com/", "Coral": "https://sports.coral.co.uk/",
    "Ladbrokes": "https://sports.ladbrokes.com/", "Betfred": "https://www.betfred.com/sport",
    "BoyleSports": "https://www.boylesports.com/", "Smarkets": "https://smarkets.com/", "Bet Victor": "https://www.betvictor.com/",
    "BetVictor": "https://www.betvictor.com/", "Casumo": "https://www.casumo.com/en-gb/sports/", "LeoVegas": "https://www.leovegas.com/en-gb/betting",
  };
  const extIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>';
  const bookLink = (name) => (BOOKS[name] ? `<a class="btn link" href="${BOOKS[name]}" target="_blank" rel="noopener noreferrer">${extIcon}Open ${esc(name)}</a>` : "");

  const initials = (name) => String(name || "").split(/\s+/).filter(Boolean).map((w) => w[0]).filter((c) => /\p{L}/u.test(c)).slice(0, 2).join("").toUpperCase();
  const num = (v) => (v == null || Number.isNaN(Number(v)) ? null : Number(v));

  function corner(f, side, fav, value) {
    const name = f[`fighter_${side}`];
    const w = num(f[`wins_${side}`]), l = num(f[`losses_${side}`]), st = num(f[`streak_${side}`]);
    const reach = num(f[`reach_cm_${side}`]), age = num(f[`age_${side}`]), stance = f[`stance_${side}`];
    const styles = String(f[`styles_${side}`] || "").split(";").map((x) => x.trim()).filter(Boolean);
    const p = side === 1 ? f.p_model_1 : 1 - f.p_model_1;
    const nf = num(f[`n_fights_${side}`]);
    const facts = [];
    if (nf === 0) facts.push('<span class="fact"><b>UFC debut</b></span>');
    else if (w != null) facts.push(`<span class="fact">UFC <b>${w}–${l}</b></span>`);
    if (st && Math.abs(st) >= 2) facts.push(`<span class="fact"><b>${Math.abs(st)}</b> ${st > 0 ? "wins" : "losses"} in a row</span>`);
    if (age) facts.push(`<span class="fact">Age <b>${Math.floor(age)}</b></span>`);
    if (reach) facts.push(`<span class="fact">Reach <b>${Math.round(reach)}</b> cm</span>`);
    if (stance) facts.push(`<span class="fact">${esc(stance)}</span>`);
    styles.forEach((x) => facts.push(`<span class="fact style-${side === 1 ? "a" : "b"}">${esc(x)}</span>`));
    return `<div class="corner ${side === 1 ? "left" : "right"} ${fav ? "fav" : ""}">
      <div class="head"><span class="badge" aria-hidden="true">${esc(initials(name))}</span><div class="name">${esc(name)}</div></div>
      <div class="pct">${pct(p)}</div>
      <div class="odds">Best odds ${Number(f[`best_odds_${side}`]).toFixed(2)} at ${esc(f[`best_book_${side}`])}</div>
      ${value ? `<div class="book-row"><span class="tag value">Possible value ${spct(f[`ev_${side}`])}</span>${bookLink(f[`best_book_${side}`])}</div>` : ""}
      <div class="tape-facts">${facts.join("")}</div>
    </div>`;
  }

  function bout(f) {
    const p1 = f.p_model_1, p2 = 1 - p1, m1 = f.p_market_1;
    const fav1 = p1 >= 0.5;
    const value1 = f.value_flag && f.value_flag === f.fighter_1, value2 = f.value_flag && f.value_flag === f.fighter_2;
    const warn = String(f.warnings || "").split(";").map((x) => x.trim()).filter(Boolean);
    const methods = ["KO_TKO", "SUB", "DEC"];
    const mLabel = { KO_TKO: "KO/TKO", SUB: "Submission", DEC: "Decision" };
    const mrow = (side) => methods.map((m) => {
      const v = f[`f${side}_${m}`] || 0;
      return `<span>${mLabel[m]}</span><span class="track"><span class="fill" style="width:${v * 100}%;background:${side === 1 ? "var(--fighter-a)" : "var(--fighter-b)"}"></span></span><span>${pct(v)}</span>`;
    }).join("");
    const factors = parseFactors(f.factors);
    return `
      <article class="bout" aria-label="${esc(f.fighter_1)} versus ${esc(f.fighter_2)}">
        <div class="bout-time">${when(f.commence_time)}
          ${warn.length ? `<span class="tag warn">${icon.warn}${warn.length === 1 ? esc(warn[0]) : `${warn.length} data warnings`}</span>` : ""}
          ${f.bookmakers < 3 ? `<span class="tag">${f.bookmakers} bookmakers</span>` : ""}
        </div>
        <div class="tape">
          ${corner(f, 1, fav1, value1)}
          <div>
            <div class="meter" role="img" aria-label="Model gives ${esc(f.fighter_1)} ${pct(p1)}; market ${pct(m1)}">
              <div class="bar"><span class="a" style="width:${p1 * 100}%"></span><span class="b" style="width:${p2 * 100}%"></span></div>
              <div class="mid"></div>
              <div class="mkt" style="left:${m1 * 100}%" title="Market ${pct(m1)}"></div>
            </div>
            <div class="meter-legend"><span><i></i>Market says ${pct(m1)} / ${pct(1 - m1)}</span></div>
          </div>
          ${corner(f, 2, !fav1, value2)}
        </div>
        <details><summary>Why, and how it might end</summary>
          <div class="bout-detail">
            <div><h3>Strongest factors</h3>${factors.length ? `<ul>${factors.map((x) => `<li>${esc(x.f.charAt(0).toUpperCase() + x.f.slice(1))} favours <b>${esc(x.who)}</b></li>`).join("")}</ul>` : '<p class="muted">No single factor stands out.</p>'}
              ${warn.length ? `<h3 style="margin-top:12px">Data warnings</h3><ul>${warn.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}</div>
            <div><h3>Method of victory</h3>
              <p class="small muted" style="margin:0 0 6px">${esc(f.fighter_1)}</p><div class="mini-bars">${mrow(1)}</div>
              <p class="small muted" style="margin:10px 0 6px">${esc(f.fighter_2)}</p><div class="mini-bars">${mrow(2)}</div></div>
            <div><h3>Where the number comes from</h3>
              <table><tbody>
                <tr><td>Stats model alone</td><td class="num">${pct(f.p_stats_1)} / ${pct(1 - f.p_stats_1)}</td></tr>
                <tr><td>Bookmakers (margin removed)</td><td class="num">${pct(m1)} / ${pct(1 - m1)}</td></tr>
                <tr><td><b>Blend shown above</b></td><td class="num"><b>${pct(p1)} / ${pct(p2)}</b></td></tr>
              </tbody></table>
              <p class="small muted">The blend leans mostly on the market, which is usually right. The stats model moves it a little.</p></div>
          </div>
        </details>
      </article>`;
  }

  async function renderCard() {
    const d = await get("/api/card");
    renderFreshness(d.freshness);
    if (!d.fights.length) {
      main.innerHTML = `<div class="page-head"><h1>Fight card</h1></div><section class="panel empty"><h3>No upcoming fights with odds</h3><p>Run <code>python -m ufc_edge weekly</code>, or refresh the odds here (uses one of your monthly Odds API requests).</p><p><button class="btn primary" data-action="refresh">${icon.refresh}Refresh odds</button></p></section>`;
      bindActions();
      return;
    }
    // One card = fights within 12 hours of each other (prelims can start the evening before the main card).
    const groups = {};
    let lastT = null, key = null;
    [...d.fights].sort((x, y) => new Date(x.commence_time) - new Date(y.commence_time)).forEach((f) => {
      const t = new Date(f.commence_time);
      if (!lastT || t - lastT > 12 * 3.6e6) key = `Card of ${t.toLocaleDateString("en-GB", { ...tz, weekday: "long", day: "numeric", month: "long" })}`;
      (groups[key] = groups[key] || []).push(f);
      lastT = t;
    });
    main.innerHTML = `
      <div class="page-head"><div><h1>Fight card</h1><p class="lede">The model's view of each fight next to the bookmakers'. A favourite isn't automatically a good bet. Only "possible value" fights reach the betting plan.</p></div>
        <div class="card-meta">Odds snapshot ${ago(d.captured_at)} <button class="btn" data-action="refresh">${icon.refresh}Refresh odds</button></div></div>
      ${Object.entries(groups).map(([k, fs]) => `<section class="panel"><div class="panel-head"><h2>${k}</h2><span class="muted small">${fs.length} fights</span></div>${fs.map(bout).join("")}</section>`).join("")}`;
    bindActions();
  }

  // ------------------------------------------------------------------ betting plan
  async function renderPlan() {
    const d = await get("/api/plan");
    const st = d.status, cfg = d.config;
    const capital = st.active_pence + st.open_stakes_pence;
    const maxAge = cfg.max_odds_age_seconds / 60;
    const stale = d.odds_age_minutes != null && d.odds_age_minutes > maxAge;
    const approved = d.proposals.filter((p) => p.status === "approved");
    const rejected = d.proposals.filter((p) => p.status === "rejected");
    const totalLimit = Math.floor(cfg.max_total_exposure_fraction * capital);
    const atRisk = d.approved_stake_pence + st.open_stakes_pence;
    const banners = [];
    if (st.status === "UNINITIALISED") banners.push(["warn", icon.warn, "No bankroll set up", `Set up a ${mode} bankroll to get a plan. <button class="btn" data-action="init">Set up</button>`]);
    if (stale) banners.push(["warn", icon.warn, `Odds are ${Math.round(d.odds_age_minutes)} minutes old`, `${d.proposals.length ? `The risk engine refuses odds older than ${maxAge} minutes, so the proposals below stay blocked until you refresh.` : "Prices move. Refresh just before betting so the plan uses current odds."} <button class="btn" data-action="refresh">${icon.refresh}Refresh odds</button>`]);
    if (mode === "live") banners.push(["info", icon.info, "You place live bets yourself", "Place the bet at your bookmaker first, then record it here with the odds and stake you actually got. The risk engine checks it again before it is recorded."]);
    main.innerHTML = `
      <div class="page-head"><div><h1>Betting plan</h1><p class="lede">Every fight where the edge clears ${pct(cfg.min_edge)} after the risk settings, checked one by one by the risk engine against a copy of your ledger. Nothing is recorded until you say so.</p></div></div>
      ${banners.map(([c, ic, t, body]) => `<div class="banner ${c}">${ic}<div><b>${t}</b><p>${body}</p></div></div>`).join("")}
      <section class="panel"><div class="band">
        <div><h3>Approved, not yet recorded</h3><div class="big">${approved.filter((p) => !p.already_recorded).length} bet${approved.filter((p) => !p.already_recorded).length === 1 ? "" : "s"}</div><p>${rejected.length} blocked by the risk engine, ${d.proposals.filter((p) => p.already_recorded).length} already recorded.</p></div>
        <div><h3>Total stake if all placed</h3><div class="big">${money(d.approved_stake_pence)}</div><p>That's the most you could lose. If every bet won, you'd get back ${money(d.approved_return_pence)}.</p></div>
        <div><h3>Money riding on open bets</h3><div class="big">${money(atRisk)}</div>
          <div class="meter-limit" role="img" aria-label="${money(atRisk)} of the ${money(totalLimit)} limit"><div class="track"><div class="fill" style="width:${Math.min(100, (atRisk / Math.max(1, totalLimit)) * 100)}%"></div></div><div class="lbl"><span>Already open ${money(st.open_stakes_pence)}, plus this plan</span><span>limit ${money(totalLimit)}</span></div></div>
          <p>Each event also has its own ${money(Math.floor(cfg.max_event_exposure_fraction * capital))} cap, checked per bet.</p></div>
      </div></section>
      <section class="panel" style="margin-top:16px">
        ${!d.has_predictions ? `<div class="empty"><h3>No predictions yet</h3><p>Run <code>python -m ufc_edge weekly</code> in fight week to fetch odds and predict the card.</p></div>`
          : !d.proposals.length ? `<div class="empty"><h3>No bets this card</h3><p>No fight's edge clears your ${pct(cfg.min_edge)} minimum after the risk settings, so placing no bets is the correct decision. The fight card still shows every prediction.</p></div>`
          : d.proposals.map(planRow).join("")}
      </section>`;
    bindActions();
  }

  function planRow(p) {
    const ok = p.status === "approved";
    const statusTag = p.already_recorded ? `<span class="tag good">${icon.ok}Recorded</span>`
      : ok ? `<span class="tag good">${icon.ok}Approved</span>` : `<span class="tag stop">${icon.x}Blocked</span>`;
    const action = ok && !p.already_recorded ? `<button class="btn ${mode === "live" ? "primary" : ""}" data-action="record" data-key="${esc(p.idempotency_key)}" data-odds="${esc(p.decimal_odds)}" data-stake="${(p.stake_pence / 100).toFixed(2)}" data-who="${esc(p.fighter_name)}">${mode === "live" ? "I placed this bet" : "Add to paper record"}</button>` : "";
    const open = ok && !p.already_recorded && mode === "live" ? bookLink(p.bookmaker) : "";
    return `<div class="plan-row">
      <div><div class="who">${esc(p.fighter_name)}</div><div class="vs">to beat ${esc(p.opponent_name)}, ${when(p.commence_time)}</div></div>
      <div><div class="k">Odds</div><div class="v">${Number(p.decimal_odds).toFixed(2)}</div><div class="k">${esc(p.bookmaker)}</div></div>
      <div><div class="k">Stake</div><div class="v">${money(p.stake_pence)}</div><div class="k">returns ${money(p.potential_return_pence)}</div></div>
      <div><div class="k">Chance</div><div class="v">${pct(p.p_used)}</div><div class="k">market ${pct(p.p_market)}</div></div>
      <div><div class="k">Edge per £1</div><div class="v ${cls(p.ev)}">${spct(p.ev)}</div></div>
      <div>${statusTag} ${open} ${action}${p.reasons && p.reasons.length ? `<ul class="reasons">${p.reasons.map((r) => `<li>${esc(reasonText(r))}</li>`).join("")}</ul>` : ""}</div>
    </div>`;
  }

  // ------------------------------------------------------------------ bankroll
  async function renderBankroll() {
    const d = await get("/api/bankroll");
    const st = d.status;
    const after = Object.fromEntries((d.history || []).map((h) => [h.id, h]));
    const acct = (v) => (v ? `<span class="${cls(v)}">${signed(v)}</span>` : '<span class="muted">–</span>');
    main.innerHTML = `
      <div class="page-head"><div><h1>Bankroll</h1><p class="lede">Every balance comes from the transactions below, and nothing is ever edited or deleted. Each transaction's lines add to zero, so the money always reconciles.</p></div></div>
      ${st.status !== "UNINITIALISED" ? `<section class="panel hero">${boundary(st)}</section>` : `<section class="panel empty"><h3>No ${mode} bankroll yet</h3><p><button class="btn primary" data-action="init">Set up a ${mode} bankroll</button></p></section>`}
      <section class="panel" style="margin-top:16px"><div class="panel-head"><h2>Balances over time</h2></div><div id="bkChart"></div></section>
      <section class="panel"><div class="panel-head"><h2>Ledger</h2><span class="muted small">Newest first, up to 200 transactions</span></div>
        ${d.transactions.length ? `<div class="table-wrap"><table><thead><tr><th>When</th><th>What happened</th><th class="num">Active</th><th class="num">Open bets</th><th class="num">Protected</th><th class="num">Active after</th><th class="num">Protected after</th></tr></thead><tbody>
          ${d.transactions.map((t) => `<tr><td>${when(t.at)}</td><td>${KINDS[t.kind] || t.kind}<span class="sub">${esc(t.memo)}</span></td>
            <td class="num">${acct(t.lines.ACTIVE)}</td><td class="num">${acct(t.lines.OPEN_STAKES)}</td><td class="num">${acct(t.lines.PROTECTED)}</td>
            <td class="num">${after[t.id] ? money(after[t.id].active) : ""}</td><td class="num">${after[t.id] ? money(after[t.id].protected) : ""}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty"><p>No transactions yet.</p></div>`}
      </section>
      <section class="panel"><div class="panel-head"><h2>All bets</h2></div>${betsTable(d.bets, false)}</section>`;
    bindActions();
    historyChart(document.getElementById("bkChart"), d.history);
  }

  // ------------------------------------------------------------------ performance
  async function renderPerformance() {
    const d = await get("/api/performance");
    renderFreshness(d.freshness);
    const m = d.model, bt = d.backtest, rec = d.record;
    const v = bt && (bt.variants[backtestVariant] || Object.values(bt.variants)[0]);
    const s = v && v.summary;
    main.innerHTML = `
      <div class="page-head"><div><h1>Performance</h1><p class="lede">Two separate questions: how good are the predictions, and would betting on them have made money? A model can pick plenty of winners and still lose money if it only backs short prices.</p></div></div>

      <section class="panel">
        <div class="panel-head"><div><h2>How good are the predictions?</h2><span class="muted small">Every year predicted by models trained only on earlier years</span></div></div>
        ${m ? `<div class="grid-2">
            <div><div class="table-wrap"><table><thead><tr><th>Approach</th><th class="num">Fights</th><th class="num">Error (log-loss)</th><th class="num">Picked winner</th></tr></thead><tbody>
              ${m.scores.map((r) => `<tr><td>${r.model === "Final (blended)" ? `<b>${r.model}</b>` : r.model}</td><td class="num">${r.fights.toLocaleString()}</td><td class="num">${r.log_loss.toFixed(4)}</td><td class="num">${pct(r.accuracy, 1)}</td></tr>`).join("")}
            </tbody></table></div><p class="small muted">Lower error is better. A coin flip scores 0.6931. The bookmakers' prices are the bar to beat.</p></div>
            <div><h3>Error by year, model against market</h3><div id="llChart"></div></div>
          </div>
          <div class="grid-2" style="margin-top:18px">
            <div><h3>Calibration</h3><p class="small muted" style="margin-top:0">When the model gives its pick X%, how often did that fighter win? Dots on the dashed line mean the probabilities can be trusted at face value.</p><div id="calChart"></div></div>
            <div><h3>Reading this</h3><p class="small">The stats model alone picks about ${pct(m.scores.find((r) => r.model.startsWith("Gradient"))?.accuracy, 0)} of winners, and the market about ${pct(m.scores.find((r) => r.model === "Market")?.accuracy, 0)}. Blending the two makes predictions marginally sharper than the market's. So the model mostly agrees with the bookmakers, and disagrees a little where its data says so. Those small disagreements are the only possible source of an edge.</p></div>
          </div>` : `<div class="empty"><h3>No model yet</h3><p>Run <code>python -m ufc_edge train</code>.</p></div>`}
      </section>

      <section class="panel">
        <div class="panel-head"><div><h2>What would have happened to £40</h2><span class="muted small">${s ? `Every event ${day(s.first_date)} to ${day(s.last_date)}, through the real risk engine and ledger` : ""}</span></div>
          ${bt ? `<div class="segmented" role="group" aria-label="Backtest settings" style="min-width:340px">${Object.entries(bt.variants).map(([k, x]) => `<button type="button" data-variant="${k}" aria-pressed="${k === backtestVariant}">${k === "current" ? "Your settings" : "Trust blended model"}</button>`).join("")}</div>` : ""}</div>
        ${s && s.bets ? `
          <p class="small muted" style="margin-top:-6px">${esc(v.label)}.</p>
          <div class="band" style="margin:8px 0 18px">
            <div><h3>Result</h3><div class="big ${cls(s.pnl_pence)}">${signed(s.pnl_pence)}</div><p>${s.bets} bets, ${money(s.staked_pence)} staked. Return on stakes ${spct(s.roi_on_stake)}, plausible range ${spct(s.roi_ci_low)} to ${spct(s.roi_ci_high)}.</p></div>
            <div><h3>Where the £40 ended up</h3><div class="big">${money(s.final_active_pence)} + ${money(s.final_protected_pence)}</div><p>Active bankroll plus protected profit. ${s.status === "EXHAUSTED" ? `Bankroll exhausted on ${day(s.exhausted_on)}.` : "Never exhausted."}</p></div>
            <div><h3>The rough patches</h3><div class="big">${money(-s.max_drawdown_pence)}</div><p>Largest fall from a high point, around ${day(s.max_drawdown_date)}. Longest losing run: ${s.longest_losing_streak} bets.</p></div>
          </div>
          <div class="banner ${s.roi_ci_low < 0 ? "warn" : "info"}">${s.roi_ci_low < 0 ? icon.warn : icon.info}<div><b>${s.roi_ci_low < 0 ? "Promising, not proven" : "Positive, but check the caveats"}</b><p>${s.roi_ci_low < 0 ? "The plausible range includes losing money, so this result could be luck." : "The range is above zero, but several model versions were tried, which flatters the best one."} The odds are approximate closing prices from a public dataset, not the prices your bookmaker would have offered. Paper trading against real, timestamped UK odds is the real test.</p></div></div>
          <div class="grid-2">
            <div><h3>Money over time</h3><div id="eqChart"></div></div>
            <div><h3>Return on stakes by year</h3><div id="yrChart"></div></div>
          </div>
          <div class="grid-2" style="margin-top:14px">
            <div><h3>By odds</h3><div class="table-wrap"><table><thead><tr><th>Odds</th><th class="num">Bets</th><th class="num">Staked</th><th class="num">Result</th><th class="num">Return</th></tr></thead><tbody>
              ${v.by_odds.map((r) => `<tr><td>${esc(r.odds)}</td><td class="num">${r.bets}</td><td class="num">${money(r.staked)}</td><td class="num ${cls(r.pnl)}">${signed(r.pnl)}</td><td class="num ${cls(r.roi)}">${spct(r.roi)}</td></tr>`).join("")}</tbody></table></div></div>
            <div><h3>Bets the risk engine blocked</h3>${Object.keys(s.rejections).length ? `<table><tbody>${Object.entries(s.rejections).map(([k, n]) => `<tr><td>${esc(reasonText(k))}</td><td class="num">${n}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">None.</p>'}</div>
          </div>` : `<div class="empty"><h3>No backtest yet</h3><p>Run <code>python -m ufc_edge backtest</code> after training.</p></div>`}
      </section>

      <section class="panel">
        <div class="panel-head"><div><h2>${mode === "paper" ? "Paper" : "Live"} record</h2><span class="muted small">Real fights, real odds, settled against real results</span></div></div>
        ${rec.graded ? `<div class="band" style="margin-bottom:14px">
            <div><h3>Result</h3><div class="big ${cls(rec.pnl_pence)}">${signed(rec.pnl_pence)}</div><p>${rec.graded} bets, ${money(rec.staked_pence)} staked, ${spct(rec.roi)} return.</p></div>
            <div><h3>Hit rate</h3><div class="big">${pct(rec.won / rec.graded)}</div><p>${rec.won} of ${rec.graded} won. Winning often isn't the goal; winning at good prices is.</p></div>
            <div><h3>Against the closing odds</h3><div class="big ${cls(rec.avg_clv)}">${spct(rec.avg_clv)}</div><p>Average from ${rec.clv_bets} bets. Consistently beating the final price is the best early sign of a real edge.</p></div>
          </div>${betsTable(d.settled_bets, false)}`
          : `<div class="empty"><h3>Nothing settled yet</h3><p>Paper bets settle automatically when <code>python -m ufc_edge weekly</code> runs after an event. Give it 100+ bets before reading much into the results.</p></div>`}
      </section>`;
    bindActions();
    document.querySelectorAll("[data-variant]").forEach((b) => b.addEventListener("click", () => { backtestVariant = b.dataset.variant; store.set("ufc-bt", backtestVariant); renderPerformance(); }));
    if (m) {
      Charts.lineChart(document.getElementById("llChart"), {
        ariaLabel: "Log-loss by year: market and model", height: 230,
        yFormat: (x) => x.toFixed(3), xFormat: (x) => String(Math.round(x)), tipTitle: (p) => String(p.x),
        series: [
          { name: "Market", color: "var(--open)", points: m.by_year.map((r) => ({ x: r.year, y: r.market })), label: () => "Market" },
          { name: "Model", color: "var(--active)", points: m.by_year.map((r) => ({ x: r.year, y: r.model })), label: () => "Model" },
        ],
      });
      document.getElementById("llChart").insertAdjacentHTML("afterbegin", '<div class="legend"><span><i style="background:var(--active)"></i>Model (blended)</span><span><i style="background:var(--open)"></i>Market</span></div>');
      Charts.calibrationChart(document.getElementById("calChart"), { points: m.calibration.map((c) => ({ predicted: c.predicted, actual: c.actual, n: c.fights })) });
    }
    if (s && s.bets) {
      Charts.lineChart(document.getElementById("eqChart"), {
        ariaLabel: "Backtest: total money and protected profit over time", height: 240, step: true,
        yFormat: (x) => `£${x.toFixed(0)}`, refLine: (bt.bankroll_pence || 4000) / 100,
        series: [
          { name: "Total (active + protected)", color: "var(--active)", points: v.curve.map((c) => ({ x: new Date(c.date), y: c.total / 100 })), label: (p) => `£${p.y.toFixed(2)}` },
          { name: "Protected", color: "var(--protected)", points: v.curve.map((c) => ({ x: new Date(c.date), y: c.protected / 100 })), label: (p) => `£${p.y.toFixed(2)}` },
        ],
      });
      document.getElementById("eqChart").insertAdjacentHTML("afterbegin", '<div class="legend"><span><i style="background:var(--active)"></i>Total money</span><span><i style="background:var(--protected)"></i>Protected profit</span><span><i style="background:var(--ink-3);height:1px"></i>£40 start</span></div>');
      Charts.barChart(document.getElementById("yrChart"), {
        ariaLabel: "Return on stakes by year", height: 240, valueName: "Return on stakes",
        yFormat: (x) => `${Math.round(x * 100)}%`,
        bars: v.by_year.map((r) => ({ label: `'${String(r.year).slice(2)}`, value: r.roi, sub: `${r.bets} bets, ${signed(r.pnl)}` })),
      });
    }
  }

  // ------------------------------------------------------------------ settings
  async function renderSettings() {
    const d = await get("/api/settings");
    const st = d.status, s = systemState(st);
    const fmtVal = (k, v) => {
      if (k.endsWith("_pence")) return money(v);
      if (k.endsWith("_fraction") || k === "min_edge") return pct(v, v < 0.1 ? 1 : 0);
      if (k === "max_odds_age_seconds") return `${v / 60} minutes`;
      if (Array.isArray(v)) return v.join(", ");
      return esc(v);
    };
    main.innerHTML = `
      <div class="page-head"><div><h1>Settings and controls</h1><p class="lede">Stopping is always one click away. Anything that could put money back at risk needs a typed confirmation, and every such action is written to a permanent log.</p></div></div>
      <section class="panel">
        <div class="panel-head"><h2>Emergency stop</h2><div class="status ${s.cls}">${s.icon}${s.title}</div></div>
        <p class="lede" style="margin-top:0">${esc(s.line)}</p>
        <p style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px">
          ${st.kill_switch ? `<button class="btn primary" data-action="release">Release the kill switch</button>` : `<button class="btn danger" data-action="kill">Stop all betting now</button>`}
          ${st.status === "UNINITIALISED" ? `<button class="btn primary" data-action="init">Set up a ${mode} bankroll</button>` : ""}
        </p>
        ${st.status === "EXHAUSTED" ? `<div class="banner stop" style="margin-top:14px">${icon.stop}<div><b>Restart is command-line only</b><p>To authorise a new bankroll run <code>python -m ufc_edge new-bankroll --deposit 40 --mode ${mode}</code> and type the confirmation phrase.</p></div></div>` : ""}
      </section>
      <section class="panel"><div class="panel-head"><div><h2>Risk limits</h2><span class="muted small">Read-only here. Change them with the command line and a typed authorisation.</span></div></div>
        <div class="table-wrap"><table><tbody>${d.config.map((c) => `<tr><td style="width:44%"><b>${esc(LIMIT_NAMES[c.key] || c.key)}</b><span class="sub">${esc(c.help)}</span></td><td class="num" style="font-size:16px;font-weight:650">${fmtVal(c.key, c.value)}</td></tr>`).join("")}</tbody></table></div>
        ${d.config.find((c) => c.key === "model_weight")?.value < 1 ? `<div class="banner info" style="margin-top:14px">${icon.info}<div><b>A decision for you: model weight</b><p>The model's probability is already blended with the market, and this setting shrinks it towards the market a second time, so fewer fights qualify.${d.backtest_bets ? ` In the backtest your settings made ${d.backtest_bets.current} bets, against ${d.backtest_bets.trust_blend} with a weight of 1.0.` : ""} Compare both on the Performance page. Changing it needs a typed authorisation on the command line.</p></div></div>` : ""}
      </section>
      <div class="grid-2" style="margin-top:16px">
        <section class="panel"><div class="panel-head"><h2>Authorisations</h2></div>${d.authorisations.length ? `<table><thead><tr><th>When</th><th>What</th><th>By</th></tr></thead><tbody>${d.authorisations.map((a) => `<tr><td>${when(a.created_at)}</td><td>${esc(a.kind.replace(/_/g, " ").toLowerCase())}<span class="sub">${esc(a.reason)}</span></td><td>${esc(a.operator)}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">None yet.</p>'}</section>
        <section class="panel"><div class="panel-head"><h2>Audit log</h2><span class="muted small">Latest 40 entries</span></div>${d.audit.length ? `<table><thead><tr><th>When</th><th>Who</th><th>What</th></tr></thead><tbody>${d.audit.map((a) => `<tr><td>${when(a.created_at)}</td><td>${esc(a.actor)}</td><td>${esc(a.action.replace(/_/g, " ").toLowerCase())}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">Empty.</p>'}</section>
      </div>`;
    bindActions();
  }

  // ------------------------------------------------------------------ dialogs and actions
  function openDialog({ title, body, fields = [], phrase, submit, danger, onSubmit }) {
    dialog.innerHTML = `<form method="dialog">
      <h2>${title}</h2><div class="muted small">${body || ""}</div>
      ${fields.map((f) => `<label>${f.label}<input name="${f.name}" value="${esc(f.value || "")}" ${f.type ? `type="${f.type}"` : ""} ${f.step ? `step="${f.step}"` : ""} required autocomplete="off"></label>`).join("")}
      ${phrase ? `<label>Type exactly <span class="phrase">${esc(phrase)}</span><input name="confirmation" required autocomplete="off" spellcheck="false"></label>` : ""}
      <p class="err" role="alert"></p>
      <div class="row"><button type="button" class="btn" value="cancel">Cancel</button><button type="submit" class="btn ${danger ? "danger" : "primary"}">${submit}</button></div>
    </form>`;
    const form = dialog.querySelector("form");
    form.querySelector('[value="cancel"]').addEventListener("click", () => dialog.close());
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      const btn = form.querySelector('[type="submit"]');
      btn.disabled = true;
      try {
        const msg = await onSubmit(data);
        dialog.close();
        if (msg) toast(msg);
        route();
      } catch (e) {
        form.querySelector(".err").textContent = e.message;
        btn.disabled = false;
      }
    });
    dialog.showModal();
    const first = form.querySelector("input");
    if (first) first.focus();
  }

  const actions = {
    kill: () => openDialog({
      title: "Stop all betting", danger: true, submit: "Stop betting",
      body: `No new ${mode} bets will be accepted until you release the kill switch. Open bets stay tracked until they settle.`,
      fields: [{ name: "reason", label: "Reason (for the log)", value: "Stopped from the dashboard" }],
      onSubmit: async (d) => { await post("/api/kill", d); return "Kill switch on. No new bets will be accepted."; },
    }),
    release: () => openDialog({
      title: "Release the kill switch", submit: "Release",
      body: "This lets the risk engine accept bets again.",
      fields: [{ name: "operator", label: "Your name", value: "" }, { name: "reason", label: "Why it's safe to restart" }],
      phrase: "RELEASE KILL SWITCH",
      onSubmit: async (d) => { await post("/api/release", d); return "Kill switch released."; },
    }),
    init: () => openDialog({
      title: `Set up the ${mode} bankroll`, submit: "Authorise bankroll",
      body: mode === "paper" ? "Paper money only. This is the starting point for tracking hypothetical bets." : "This is the most of your own money the system may ever put at risk.",
      fields: [{ name: "bankroll", label: "Bankroll (£)", value: "40", type: "number", step: "0.01" }, { name: "operator", label: "Your name", value: "" }, { name: "reason", label: "Reason", value: mode === "paper" ? "Start paper trading" : "Start live tracking" }],
      phrase: "AUTHORISE BANKROLL 40.00",
      onSubmit: async (d) => { await post("/api/init", d); return "Bankroll authorised."; },
    }),
    refresh: async (btn) => {
      if (!confirm("Take a fresh odds snapshot? This uses 1 of your monthly Odds API requests.")) return;
      btn.disabled = true;
      btn.textContent = "Refreshing…";
      try {
        const r = await post("/api/refresh", {});
        toast(`Odds refreshed: ${r.fights} fights. ${r.requests_remaining ?? "?"} API requests left this month.`);
        route();
      } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = "Refresh odds"; }
    },
    record: (btn) => {
      const live = mode === "live";
      openDialog({
        title: live ? `Record your bet on ${btn.dataset.who}` : `Add ${btn.dataset.who} to the paper record`,
        submit: live ? "Record bet" : "Add to paper record",
        body: live ? "Enter the odds and stake you actually got. The risk engine checks the bet again before recording it, so a bet that breaks a limit is refused." : "Records a hypothetical bet at these odds. The risk engine checks it first.",
        fields: [{ name: "odds", label: "Decimal odds", value: btn.dataset.odds, type: "number", step: "0.01" }, { name: "stake", label: `Stake (£, at most ${btn.dataset.stake})`, value: btn.dataset.stake, type: "number", step: "0.01" }],
        phrase: live ? "I PLACED THIS BET" : "RECORD PAPER BET",
        onSubmit: async (d) => {
          const r = await post("/api/record", { ...d, key: btn.dataset.key });
          if (!r.ok) throw new Error(`Refused: ${r.reasons.map(reasonText).join("; ")}`);
          return r.created ? "Bet recorded." : "This bet was already recorded.";
        },
      });
    },
  };

  function bindActions() {
    main.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => actions[b.dataset.action](b)));
  }

  // ------------------------------------------------------------------ router
  const renderers = { overview: renderOverview, card: renderCard, plan: renderPlan, bankroll: renderBankroll, performance: renderPerformance, settings: renderSettings };
  let routing = 0;
  async function route() {
    const page = location.hash.slice(1) || "overview";
    renderNav();
    const token = ++routing;
    try {
      await (renderers[page] || renderOverview)();
    } catch (e) {
      if (token !== routing) return;
      main.innerHTML = `<div class="banner stop">${icon.stop}<div><b>Couldn't load this page</b><p>${esc(e.message)}. Check the black window running the dashboard for details.</p></div></div>`;
    }
  }

  // theme
  function applyTheme(t) {
    if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
    const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.getElementById("themeBtn").textContent = dark ? "Switch to light mode" : "Switch to dark mode";
  }
  applyTheme(store.get("ufc-theme", ""));
  document.getElementById("themeBtn").addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark" || (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    store.set("ufc-theme", next);
    applyTheme(next);
    route();
  });
  document.querySelectorAll(".segmented button[data-mode]").forEach((b) => b.addEventListener("click", () => { mode = b.dataset.mode; store.set("ufc-mode", mode); route(); }));
  document.getElementById("killBtn").addEventListener("click", () => actions.kill());
  window.addEventListener("hashchange", route);
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(route, 250); });
  route();
})();
