/* web/static/app.js
 *
 * Thin renderer for /api/ops. All the real derivation (machine levels,
 * mining slots, alerts, wealth ranking...) happens once, server-side, in
 * web/queries.py using the bot's own formulas - this file just walks the
 * JSON it gets back and builds DOM. Client-side state is only ever "which
 * server/player is selected" and "what's in the search box"; changing a
 * setting (anonymize, thresholds...) re-fetches, matching the Claude Design
 * prototype's tweakable props.
 */
(() => {
  const state = { view: 'overview', serverKey: null, playerKey: null, query: '' };
  let DATA = null;

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  const $ = (id) => document.getElementById(id);

  function settingsQuery() {
    const p = new URLSearchParams();
    p.set('anonymize', $('setAnonymize').checked);
    p.set('hideDeparted', $('setHideDeparted').checked);
    p.set('dormantDays', $('setDormantDays').value);
    p.set('burnFloor', $('setBurnFloor').value);
    p.set('stalledDays', $('setStalledDays').value);
    return p.toString();
  }

  async function fetchData() {
    $('loading').hidden = false;
    $('loading').textContent = 'Reading dragonhoard.db…';
    try {
      const res = await fetch('/api/ops?' + settingsQuery());
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      DATA = await res.json();
      if (!state.serverKey && DATA.serverRail.length) state.serverKey = DATA.serverRail[0].guild_id;
      if (!state.playerKey && DATA.playerRail.length) state.playerKey = DATA.playerRail[0].user_id;
      $('loading').hidden = true;
      renderChrome();
      render();
    } catch (err) {
      $('loading').textContent = 'Could not read the database: ' + err.message;
    }
  }

  function renderChrome() {
    $('capturedAt').textContent = 'read ' + new Date(DATA.meta.captured_at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
    $('dbLine').textContent = `${DATA.meta.db_path} · ${DATA.meta.row_count} rows scanned across ${DATA.meta.table_count} tables`;
    $('versionLine').textContent = `Dragonhoard by Isaac Day · Version ${DATA.meta.version}`;
  }

  function setView(view, extra) {
    state.view = view;
    Object.assign(state, extra || {});
    render();
    window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
  }

  function render() {
    document.querySelectorAll('.dho-tab').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.view === state.view);
    });
    $('viewOverview').hidden = state.view !== 'overview';
    $('viewServers').hidden = state.view !== 'servers';
    $('viewPlayers').hidden = state.view !== 'players';
    if (!DATA) return;
    if (state.view === 'overview') renderOverview();
    if (state.view === 'servers') renderServers();
    if (state.view === 'players') renderPlayers();
  }

  /* ── Overview ─────────────────────────────────────────────────────── */
  function renderOverview() {
    const o = DATA.overview;
    const statCard = (label, value, sub, size = '') =>
      `<div class="card"><div class="stat-label">${esc(label)}</div><div class="stat-value ${size}">${esc(value)}</div><div class="stat-sub">${esc(sub)}</div></div>`;

    const alerts = o.alerts.length
      ? o.alerts.map((a) => `
        <div class="alert-row" data-alert>
          <span class="alert-kicker" style="color:${a.accent}">${esc(a.kicker)}</span>
          <div><div class="alert-title">${esc(a.title)}</div><div class="alert-detail">${esc(a.detail)}</div></div>
          <button type="button" class="alert-action" data-view="${a.view}" data-guild="${a.guild_id ?? ''}">${esc(a.action)}</button>
        </div>`).join('')
      : `<div class="card" style="font-size:15px">Nothing flagged. Every server has a stocked pool, a moving queue and a burn ratio inside its band.</div>`;

    const serverTable = `
      <div class="table-wrap">
        <table class="dho">
          <thead><tr><th>Server</th><th>Players</th><th>Drills</th><th>Pool</th><th>Invested</th><th>Slots</th><th>Minted</th><th>Burned</th><th>In hand</th><th>Burn</th></tr></thead>
          <tbody>
          ${o.serverRows.map((s) => `
            <tr class="clickable" data-view="servers" data-guild="${esc(s.guild_id)}">
              <td>${esc(s.name)} <span style="font-size:12px;color:var(--text-subtle)">${esc(s.currency)}</span></td>
              <td>${esc(s.players)}</td><td>${esc(s.drills)}</td>
              <td style="color:${s.poolColor}">${esc(s.pool)}</td>
              <td>${esc(s.invested)}</td><td>${esc(s.slots)}</td>
              <td>${esc(s.minted)}</td><td>${esc(s.burned)}</td>
              <td style="color:var(--text-primary)">${esc(s.circulating)}</td>
              <td style="color:${s.burnColor}">${esc(s.burnPct)}</td>
            </tr>`).join('')}
          </tbody>
        </table>
        <div class="table-note">Currency columns are each server's own currency. Invested is lifetime fees across all five machines — what buys mining slots.</div>
      </div>`;

    const machineTable = `
      <div class="table-wrap" style="margin-top:20px">
        <table class="dho">
          <thead><tr><th>Machine</th><th>Median level</th><th>Highest</th><th>Past level 1</th><th>Open jobs</th><th>Queued</th><th>Default fee</th></tr></thead>
          <tbody>
          ${o.machineRows.map((m) => `
            <tr><td style="color:var(--text-primary)">${esc(m.label)}</td><td>${esc(m.median)}</td><td>${esc(m.max)}</td>
              <td>${esc(m.past)}</td><td>${esc(m.jobs)}</td><td>${esc(m.items)}</td>
              <td style="color:var(--text-subtle)">${esc(m.fee)}</td></tr>`).join('')}
          </tbody>
        </table>
        <div class="table-note">Levels are derived on read from fees collected, 5 × 5^(level−2) per step. The blast furnace counts batches of 100, not items.</div>
      </div>`;

    const poolRows = o.poolRows.map((p) => `
      <div class="pool-row">
        <div class="name">${esc(p.name)}</div>
        <div class="progress"><div style="width:${p.pct}%"></div></div>
        <div class="label">${esc(p.label)}</div>
      </div>`).join('');

    const wealthTable = `
      <div class="table-wrap">
        <table class="dho">
          <thead><tr><th>#</th><th>Player</th><th>Server</th><th>Balance</th><th>Share of supply</th></tr></thead>
          <tbody>
          ${o.wealthRows.map((w) => `
            <tr class="clickable" data-view="players" data-player="${esc(w.user_id)}">
              <td style="color:var(--text-subtle)">${esc(w.rank)}</td>
              <td style="color:var(--text-primary)">${esc(w.player)}</td>
              <td>${esc(w.server)}</td><td>${esc(w.balance)}</td>
              <td style="color:var(--text-primary)">${esc(w.share)}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>`;

    $('viewOverview').innerHTML = `
      <div class="stack-72">
        <div class="hero">
          <div>
            <div class="section-eyebrow">Everything, at once</div>
            <h1>Dragonhoard is running in <span class="accent">${esc(o.kServers)} servers</span>.</h1>
            <p>Read straight out of <span style="color:var(--text-primary)">dragonhoard.db</span>. Every currency figure is that server's own currency — the totals are never added across servers, because they aren't the same money.</p>
          </div>
        </div>

        <div class="grid-4">
          ${statCard('Active servers', o.kServers, o.kServersSub)}
          ${statCard('Players', o.kPlayers, o.kPlayersSub)}
          ${statCard('Drills in the ground', o.kDrills, o.kDrillsSub)}
          ${statCard('Open production jobs', o.kJobs, o.kJobsSub)}
        </div>

        <section>
          <div class="section-header"><div class="section-eyebrow">Needs a look</div><h2 class="section-title">Alerts</h2><div class="section-underline"></div></div>
          <div style="display:flex;flex-direction:column;gap:12px;margin-top:28px">${alerts}</div>
        </section>

        <section>
          <div class="section-header"><div class="section-eyebrow">Scale</div><h2 class="section-title">Every server</h2><div class="section-underline"></div></div>
          <div style="margin-top:28px">${serverTable}</div>
        </section>

        <section>
          <div class="section-header"><div class="section-eyebrow">Currency</div><h2 class="section-title">Faucets and sinks</h2><div class="section-underline"></div></div>
          <div class="grid-3" style="margin-top:28px">
            ${statCard('Median burn ratio', o.medianBurn, 'Of everything ever minted, the share that has been burned back out. Ratios compare across servers; the amounts don’t.', 'md')}
            ${statCard('Inflating servers', o.inflatingCount, o.inflatingSub, 'md')}
            ${statCard('DragonCoin in existence', o.dragoncoin, 'The global currency has a column and no faucet.', 'md')}
          </div>
          ${machineTable}
        </section>

        <section>
          <div class="section-header"><div class="section-eyebrow">Resources</div><h2 class="section-title">Where the materials are</h2><div class="section-underline"></div></div>
          <div class="grid-4" style="margin-top:28px">
            ${statCard('Sitting in drills', o.uncollected, 'Raw materials mined and never collected.', 'md')}
            ${statCard('Full and stopped', o.fullDrills, 'Drills mining nothing until someone runs /collect.', 'md')}
            ${statCard('Median pool left', o.medianPool, 'Of a full bag. Refills the moment it empties.', 'md')}
            ${statCard('Gems above ground', o.gemCount, o.gemSub, 'md')}
          </div>
          <div class="card" style="margin-top:20px;padding:26px 26px 8px">
            <div class="stat-label" style="margin-bottom:20px">Mining bag remaining, per server</div>
            ${poolRows}
          </div>
        </section>

        <section>
          <div class="section-header"><div class="section-eyebrow">Wealth</div><h2 class="section-title">Who holds it</h2><div class="section-underline"></div></div>
          <div style="margin-top:28px">${wealthTable}</div>
        </section>
      </div>`;

    wireNav($('viewOverview'));
  }

  /* ── Servers ──────────────────────────────────────────────────────── */
  function renderServers() {
    if (!DATA.servers[state.serverKey]) state.serverKey = DATA.serverRail[0]?.guild_id;
    const s = DATA.servers[state.serverKey];
    if (!s) { $('viewServers').innerHTML = '<div class="card">No servers in this database yet.</div>'; return; }

    const rail = DATA.serverRail.map((r) => `
      <button type="button" class="rail-item ${r.guild_id === state.serverKey ? 'active' : ''}" data-view="servers" data-guild="${esc(r.guild_id)}">
        <div class="name">${esc(r.name)}</div><div class="meta">${esc(r.meta)}</div>
      </button>`).join('');

    const machines = s.machines.map((m) => `
      <div class="machine-row">
        <div style="color:var(--text-primary)">${esc(m.label)}</div>
        <div style="text-align:right">${esc(m.level)}</div>
        <div style="text-align:right">${esc(m.banked)}</div>
        <div><div class="progress"><div style="width:${m.pct}%"></div></div><div class="next-note">${esc(m.next)}</div></div>
        <div style="text-align:right;color:var(--text-subtle)">${esc(m.fee)}</div>
      </div>`).join('');

    const poolComp = s.poolComp.map((c) => `<div class="poolcomp-row"><span>${esc(c.name)}</span><span style="color:var(--text-primary)">${esc(c.qty)}</span></div>`).join('');
    const stock = s.stock.map((st) => `
      <div class="stock-row"><div class="name">${esc(st.name)}</div><div class="progress"><div style="width:${st.pct}%"></div></div><div class="label">${esc(st.label)}</div></div>`).join('');

    const jobs = s.jobs.length ? s.jobs.map((j) => `
      <div class="job-row">
        <div style="color:var(--text-subtle)">${esc(j.machine)}</div>
        <div style="color:var(--text-primary)">${esc(j.target)}</div>
        <div>${esc(j.owner)}</div>
        <div style="text-align:right;color:${j.ageColor}">${esc(j.age)}</div>
      </div>`).join('') : `<div style="padding:14px 24px 24px;border-top:1px solid var(--border-default);font-size:14px;color:var(--text-subtle)">Nothing queued on any machine here.</div>`;

    const board = s.board
      ? `<div style="font-family:var(--font-display);font-size:22px;font-weight:800;color:var(--text-primary)">${esc(s.board.quantity)}× ${esc(s.board.material)}</div>
         <div style="font-size:14px;margin-top:10px">Pays ${esc(s.board.reward.toFixed(2))} per completion, every time, no daily cap.</div>
         <div style="display:flex;gap:10px;margin-top:18px"><span class="tag">${esc(s.board.completions)} completions</span><span class="tag">${esc(s.board.participants)} players</span></div>`
      : `<div style="font-family:var(--font-display);font-size:22px;font-weight:800;color:var(--text-primary)">Not posted today</div>
         <div style="font-size:14px;margin-top:10px">The board posts lazily — nobody here has looked at it or sold into it today.</div>
         <div style="display:flex;gap:10px;margin-top:18px"><span class="tag">0 completions</span><span class="tag">0 players</span></div>`;

    const members = s.members2.length ? s.members2.map((m) => `
      <div class="member-row clickable" data-view="players" data-player="${esc(m.user_id)}">
        <div style="color:var(--text-subtle)">${esc(m.rank)}</div>
        <div style="color:var(--text-primary)">${esc(m.name)}</div>
        <div style="text-align:right">${esc(m.balance)}</div>
        <div style="text-align:right">${esc(m.share)}</div>
        <div style="text-align:right">${esc(m.drills)}</div>
        <div style="text-align:right">${esc(m.stored)}</div>
      </div>`).join('') : `<div style="padding:14px 24px 24px;border-top:1px solid var(--border-default);font-size:14px;color:var(--text-subtle)">No players here yet.</div>`;

    $('viewServers').innerHTML = `
      <div class="rail-layout">
        <div class="rail"><div class="rail-heading">${DATA.serverRail.length} servers</div>${rail}</div>
        <div class="detail-stack">
          <div class="detail-head">
            <div><h2>${esc(s.name)}</h2><div class="detail-idline">${esc(s.idLine)}</div></div>
            <span class="chip">${esc(s.currencyChip)}</span>
          </div>

          <div class="grid-4">
            <div class="card"><div class="stat-label">Minted</div><div class="stat-value xs">${esc(s.minted)}</div><div class="stat-sub" style="font-size:12px">Market buying from players, plus job board</div></div>
            <div class="card"><div class="stat-label">Burned</div><div class="stat-value xs">${esc(s.burned)}</div><div class="stat-sub" style="font-size:12px">Fees, donations, market resales</div></div>
            <div class="card"><div class="stat-label">In circulation</div><div class="stat-value xs">${esc(s.circulating)}</div><div class="stat-sub" style="font-size:12px">${esc(s.reconcile)}</div></div>
            <div class="card"><div class="stat-label">Burn ratio</div><div class="stat-value xs">${esc(s.burnPct)}</div><div class="stat-sub" style="font-size:12px">${esc(s.burnNote)}</div></div>
          </div>

          <div class="split-13-1">
            <div class="table-wrap">
              <div class="panel-title">Machines</div>
              <div class="machine-list-head"><div>Machine</div><div style="text-align:right">Lv</div><div style="text-align:right">Banked</div><div>To next level</div><div style="text-align:right">Fee</div></div>
              ${machines}
            </div>
            <div style="display:flex;flex-direction:column;gap:24px">
              <div class="card">
                <div class="stat-label">Mining slots</div>
                <div style="display:flex;align-items:baseline;gap:10px"><span style="font-family:var(--font-display);font-size:34px;font-weight:800;color:var(--text-primary)">${esc(s.slots)}</span><span style="font-size:14px">drills per player</span></div>
                <div style="margin-top:16px" class="progress"><div style="width:${s.slotPct}%"></div></div>
                <div class="stat-sub" style="margin-top:10px">${esc(s.slotNote)}</div>
              </div>
              <div class="card">
                <div class="stat-label">Configuration</div>
                <div style="display:flex;flex-direction:column;gap:12px">
                  <div class="kv-row"><span>Currency</span><span>${esc(s.currencyName)}</span></div>
                  <div class="kv-row"><span>Bot channel</span><span>${esc(s.channel)}</span></div>
                  <div class="kv-row"><span>Replies</span><span>${esc(s.replies)}</span></div>
                  <div class="kv-row"><span>Setup prompt</span><span>${esc(s.prompt)}</span></div>
                  <div class="kv-row"><span>Members</span><span>${esc(s.members)}</span></div>
                </div>
              </div>
            </div>
          </div>

          <div class="split-11">
            <div class="card">
              <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:18px"><div class="stat-label" style="margin-bottom:0">Mining bag</div><div style="font-size:13px;color:var(--text-primary)">${esc(s.poolLabel)}</div></div>
              <div class="progress"><div style="width:${s.poolPct}%"></div></div>
              <div style="margin-top:20px;display:flex;flex-direction:column;gap:10px">${poolComp}</div>
            </div>
            <div class="card">
              <div class="stat-label">Market stock vs. target (target uses an approx. member count — docs/ops-dashboard.md)</div>
              <div style="display:flex;flex-direction:column;gap:14px">${stock}</div>
            </div>
          </div>

          <div class="split-13-1b">
            <div class="table-wrap">
              <div style="padding:22px 24px 14px;display:flex;justify-content:space-between;align-items:baseline">
                <span class="stat-label" style="margin-bottom:0">Production queue</span><span style="font-size:13px;color:var(--text-primary)">${esc(s.queueSummary)}</span>
              </div>
              ${jobs}
            </div>
            <div class="card">
              <div class="stat-label">Today's job board</div>
              ${board}
            </div>
          </div>

          <div class="table-wrap">
            <div class="panel-title">Players here</div>
            <div class="member-head"><div>#</div><div>Player</div><div style="text-align:right">Balance</div><div style="text-align:right">Share</div><div style="text-align:right">Drills</div><div style="text-align:right">Uncollected</div></div>
            ${members}
          </div>
        </div>
      </div>`;

    wireNav($('viewServers'));
  }

  /* ── Players ──────────────────────────────────────────────────────── */
  function renderPlayers() {
    if (!DATA.players[state.playerKey]) state.playerKey = DATA.playerRail[0]?.user_id;
    const p = DATA.players[state.playerKey];
    if (!p) { $('viewPlayers').innerHTML = '<div class="card">No players in this database yet.</div>'; return; }

    const q = state.query.trim().toLowerCase();
    const filtered = DATA.playerRail.filter((r) => !q || r.name.toLowerCase().includes(q));
    const rail = filtered.map((r) => `
      <button type="button" class="rail-item ${r.user_id === state.playerKey ? 'active' : ''}" data-view="players" data-player="${esc(r.user_id)}">
        <div class="name">${esc(r.name)}</div><div class="meta">${esc(r.meta)}</div>
      </button>`).join('');

    const unlocks = p.unlocks.map((u) => `<span class="chip">${esc(u.label)}</span>`).join('');
    const balances = p.balances.length ? p.balances.map((b) => `
      <div class="balance-row" data-view="servers" data-guild="${esc(b.guild_id)}">
        <div style="color:var(--text-primary)">${esc(b.server)}</div><div style="text-align:right">${esc(b.amount)}</div><div style="text-align:right;color:var(--text-subtle)">${esc(b.share)}</div>
      </div>`).join('') : `<div style="padding:14px 24px;color:var(--text-subtle);font-size:14px">No balances anywhere.</div>`;
    const inventory = p.inventory.length ? p.inventory.map((i) => `
      <div class="inv-row"><div style="color:var(--text-primary)">${esc(i.name)}</div><div style="text-align:right">${esc(i.qty)}</div></div>`).join('') : `<div style="padding:14px 24px;color:var(--text-subtle);font-size:14px">Empty inventory.</div>`;
    const drills = p.drills.length ? p.drills.map((d) => `
      <div class="drill-row">
        <div style="color:var(--text-subtle)">${esc(d.id)}</div><div style="color:var(--text-primary)">${esc(d.type)}</div>
        <div style="text-align:right">${esc(d.level)}</div><div style="color:var(--text-subtle)">${esc(d.container)}</div>
        <div>${esc(d.where)}</div><div style="text-align:right;color:${d.holdColor}">${esc(d.holding)}</div>
      </div>`).join('') : `<div style="padding:14px 24px;color:var(--text-subtle);font-size:14px">No drills owned.</div>`;
    const jobs = p.jobs.length ? p.jobs.map((j) => `
      <div class="job-row player-job">
        <div style="color:var(--text-subtle)">${esc(j.machine)}</div><div style="color:var(--text-primary)">${esc(j.target)}</div>
        <div>${esc(j.server)}</div><div style="text-align:right;color:${j.ageColor}">${esc(j.age)}</div>
      </div>`).join('') : `<div style="padding:14px 24px 24px;border-top:1px solid var(--border-default);font-size:14px;color:var(--text-subtle)">Nothing queued anywhere.</div>`;

    $('viewPlayers').innerHTML = `
      <div class="rail-layout">
        <div class="rail">
          <div class="field"><input type="text" id="playerSearch" placeholder=" " value="${esc(state.query)}"><label>Find a player</label></div>
          <div class="rail-heading">${filtered.length} of ${DATA.playerRail.length} players</div>
          ${rail}
        </div>
        <div class="detail-stack">
          <div class="detail-head">
            <div><h2>${esc(p.name)}</h2><div class="detail-idline">${esc(p.idLine)}</div></div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end;max-width:520px">${unlocks}</div>
          </div>

          <div class="grid-4">
            <div class="card"><div class="stat-label">Servers</div><div class="stat-value sm">${esc(p.serverCount)}</div></div>
            <div class="card"><div class="stat-label">Drills owned</div><div class="stat-value sm">${esc(p.drillCount)}</div><div class="stat-sub" style="font-size:12px">${esc(p.drillSub)}</div></div>
            <div class="card"><div class="stat-label">Uncollected</div><div class="stat-value sm">${esc(p.stored)}</div><div class="stat-sub" style="font-size:12px">${esc(p.storedSub)}</div></div>
            <div class="card"><div class="stat-label">DragonCoin</div><div class="stat-value sm">${esc(p.dragoncoin)}</div><div class="stat-sub" style="font-size:12px">Global, no faucet yet</div></div>
          </div>

          <div class="split-11">
            <div class="table-wrap"><div class="panel-title">Balances by server</div>${balances}</div>
            <div class="table-wrap"><div class="panel-title">Inventory, top holdings</div>${inventory}</div>
          </div>

          <div class="table-wrap">
            <div class="panel-title">Drills</div>
            <div class="drill-head"><div>ID</div><div>Type</div><div style="text-align:right">Lv</div><div>Container</div><div>Where</div><div style="text-align:right">Holding</div></div>
            ${drills}
          </div>

          <div class="table-wrap">
            <div class="panel-title">Outstanding jobs</div>
            ${jobs}
          </div>
        </div>
      </div>`;

    wireNav($('viewPlayers'));
    const search = $('playerSearch');
    if (search) {
      search.addEventListener('input', (e) => {
        state.query = e.target.value;
        const caret = e.target.selectionStart;
        renderPlayers();
        // Re-rendering rebuilds the input node, which drops focus - put it back
        // where the cursor was so typing a search term doesn't stutter.
        const revived = $('playerSearch');
        revived.focus();
        revived.setSelectionRange(caret, caret);
      });
    }
  }

  /* ── shared click wiring: any element with data-view routes on click ── */
  function wireNav(root) {
    root.querySelectorAll('[data-view]').forEach((el) => {
      el.addEventListener('click', () => {
        const view = el.dataset.view;
        const extra = {};
        if (el.dataset.guild) extra.serverKey = el.dataset.guild;
        if (el.dataset.player) extra.playerKey = el.dataset.player;
        setView(view, extra);
      });
    });
  }

  document.querySelectorAll('.dho-tab').forEach((btn) => {
    btn.addEventListener('click', () => setView(btn.dataset.view));
  });
  $('btnRefresh').addEventListener('click', fetchData);
  ['setAnonymize', 'setHideDeparted', 'setDormantDays', 'setBurnFloor', 'setStalledDays'].forEach((id) => {
    $(id).addEventListener('change', fetchData);
  });

  fetchData();
})();
