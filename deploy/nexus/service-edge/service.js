'use strict';
(() => {
  const phases = ['manifest', 'folders', 'rename', 'sqlite', 'cleanup', 'verification'];
  const states = new Set(['WAIT', 'WORK', 'PASS', 'STOP']);
  const message = document.getElementById('message');
  const result = document.getElementById('result');
  const confirmed = document.getElementById('confirmed');
  let snapshot = null, serverTime = 0, receivedAt = 0, reachable = false;
  function valid(data) {
    return data && data.version === 1 && /^[a-f0-9]{32}$/.test(data.run_id) &&
      typeof data.active === 'boolean' && Number.isFinite(data.updated_at) &&
      ['NORMAL', 'STOP', 'UNVERIFIED'].includes(data.outcome) &&
      Number.isInteger(data.completed) && data.completed >= 0 &&
      Number.isInteger(data.rename_completed) && data.rename_completed >= 0 &&
      (data.total === null || (Number.isInteger(data.total) && data.total >= data.completed && data.total >= data.rename_completed)) &&
      Array.isArray(data.phases) && data.phases.length === phases.length &&
      data.phases.every((row, i) => row.id === phases[i] && states.has(row.state));
  }
  function render() {
    const data = snapshot;
    const age = data ? serverTime + (performance.now() - receivedAt) / 1000 - data.updated_at : Infinity;
    const fresh = reachable && age >= -5 && age <= 16;
    if (!data) {
      message.textContent = 'Aplikace je momentálně nedostupná. Nemáme potvrzení, že probíhá reorganizace.';
      result.textContent = 'Stav neověřen';
      return;
    }
    for (const row of data.phases) {
      const cells = document.querySelector(`[data-phase="${row.id}"]`).cells;
      cells[1].textContent = row.state; cells[1].className = row.state;
      cells[2].textContent = data.total !== null && ['rename', 'sqlite'].includes(row.id)
        ? `${row.id === 'rename' ? data.rename_completed : data.completed} / ${data.total}` : '';
    }
    const last = data.last_confirmed;
    confirmed.textContent = last ? `Poslední potvrzená změna: ${last.phase} · ${last.completed} dokončených akcí.` : 'Zatím bez potvrzené změny.';
    if (!data.active && ['NORMAL', 'STOP'].includes(data.outcome)) {
      result.textContent = `Poslední běh: ${data.outcome}`;
      message.textContent = data.outcome === 'STOP'
        ? 'Reorganizace byla zastavena. Je nutný zásah správce v terminálu. Aplikace je nedostupná.'
        : 'Poslední běh skončil NORMAL. Čeká se na dostupnou aplikaci; stránka ji sama nespouští.';
      if (data.outcome === 'STOP' && data.error) confirmed.textContent += ` ${data.error.message}`;
    } else if (!fresh) {
      result.textContent = 'Stav neověřen';
      message.textContent = 'Aktualizace průběhu nejsou potvrzené. Tabulka ukazuje poslední známý stav, nikoli živý běh.';
    } else {
      result.textContent = 'Reorganizace knihovny';
      message.textContent = data.waiting_for_operator
        ? 'Worker na Nexusu čeká na správce v terminálu: schválení přesného živého návrhu cleanupu.'
        : 'Aplikace je dočasně zastavena. Reorganizace probíhá na serveru Nexus.';
    }
  }
  async function poll() {
    const [status, health] = await Promise.allSettled([
      fetch('/_service/progress.json', {cache: 'no-store', signal: AbortSignal.timeout(3000)}),
      fetch('/_service/app-health', {cache: 'no-store', signal: AbortSignal.timeout(3000)})
    ]);
    if (health.status === 'fulfilled' && health.value.ok) { location.replace(location.href); return; }
    reachable = false;
    if (status.status === 'fulfilled' && status.value.ok) {
      try {
        const data = await status.value.json();
        const date = Date.parse(status.value.headers.get('date')) / 1000;
        if (valid(data) && Number.isFinite(date)) {
          snapshot = data; serverTime = date; receivedAt = performance.now(); reachable = true;
        }
      } catch (_) { /* Preserve the last confirmed snapshot; do not infer success. */ }
    }
    render();
  }
  poll(); setInterval(poll, 5000); setInterval(render, 1000);
})();
