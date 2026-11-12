'use strict';
(() => {
  const phases = ['manifest', 'folders', 'rename', 'sqlite', 'cleanup', 'verification'];
  const states = new Set(['WAIT', 'WORK', 'PASS', 'STOP']);
  const stateLabels = {WAIT:'Čeká se', WORK:'Právě probíhá', PASS:'Dokončeno', STOP:'Zastaveno, vyžaduje zásah'};
  const phaseLabels = {manifest:'Připravuje se přesný návrh a ověřují podklady; soubory se zatím nepřesouvají.',
    folders:'Připravují se schválené cílové adresáře.', rename:'Probíhají schválené přesuny souborů včetně karantény.',
    sqlite:'Ověřené nové cesty se zapisují do databáze.', cleanup:'Ověřují se prázdné adresáře a samostatně schválený úklid.',
    verification:'Ověřuje se výsledný stav souborů a databáze.', app_initializing:'Aplikace se vrací do provozu; její dostupnost zatím není potvrzená.',
    app_restart_failed:'Návrat aplikace vyžaduje kontrolu správce.', normal:'Schválená reorganizace byla dokončena.',
    ready:'Přesný návrh je připravený ke kontrole a schválení.', stopped:'Práce byla zastavena; je potřeba zásah správce.'};
  const sha = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  const requestedSource = new URLSearchParams(location.search).get('source');
  const requestedJob = location.pathname.match(/^\/reorganization\/jobs\/([a-f0-9]{64})$/)?.[1];
  const message = document.getElementById('message');
  const result = document.getElementById('result');
  const confirmed = document.getElementById('confirmed');
  const stage = document.getElementById('workflow-stage');
  const cleanupPanel = document.getElementById('cleanup-panel');
  const feedback = document.getElementById('cleanup-feedback');
  const selected = new Set();
  let snapshot = null, serverTime = 0, receivedAt = 0, reachable = false;
  let cleanup = null, cleanupPage = 0, decidedProposal = null, submitting = false;
  let selectionProposal = null, submissionState = null, decisionEpoch = 0, polling = false;
  let cleanupSource = sha(requestedSource) ? requestedSource : null, cleanupOutcome = null;
  const element = (tag, text) => {
    const node = document.createElement(tag); node.textContent = text; return node;
  };
  function valid(data) {
    return data && data.version === 1 && /^[a-f0-9]{32}$/.test(data.run_id) &&
      typeof data.active === 'boolean' && Number.isFinite(data.updated_at) &&
      ['NORMAL', 'STOP', 'UNVERIFIED'].includes(data.outcome) &&
      Number.isInteger(data.completed) && data.completed >= 0 &&
      Number.isInteger(data.rename_completed) && data.rename_completed >= 0 &&
      (data.total === null || (Number.isInteger(data.total) && data.total >= data.completed && data.total >= data.rename_completed)) &&
      Array.isArray(data.phases) && data.phases.length === phases.length &&
      data.phases.every((row, i) => row.id === phases[i] && states.has(row.state)) &&
      (!requestedSource || (sha(requestedSource) && data.source_sha256 === requestedSource));
  }
  function render() {
    const data = snapshot;
    const age = data ? serverTime + (performance.now() - receivedAt) / 1000 - data.updated_at : Infinity;
    const fresh = reachable && age >= -5 && age <= 16;
    if (!data) {
      message.textContent = requestedSource
        ? 'Čeká na hostitelský koordinátor. Zahájení ani dokončení požadavku zatím není potvrzené.'
        : 'Aplikace je momentálně nedostupná. Nemáme potvrzení, že probíhá reorganizace.';
      result.textContent = 'Stav neověřen';
      return;
    }
    // Only fresh progress for this cleanup request can settle its decision.
    if (!cleanupOutcome && cleanupSource && fresh && data.source_sha256 === cleanupSource &&
        !data.active && ['NORMAL', 'STOP'].includes(data.outcome)) {
      cleanupOutcome = data.outcome;
    }
    renderCleanupOutcome();
    for (const row of data.phases) {
      const cells = document.querySelector(`[data-phase="${row.id}"]`).cells;
      cells[1].textContent = stateLabels[row.state]; cells[1].className = row.state;
      cells[2].textContent = data.total !== null && ['rename', 'sqlite'].includes(row.id)
        ? `${row.id === 'rename' ? data.rename_completed : data.completed} / ${data.total}` : '';
    }
    stage.textContent = data.waiting_for_operator ? 'Čeká se na vaše výslovné rozhodnutí o prázdných adresářích.'
      : phaseLabels[data.stage] || (!data.active && ['NORMAL','STOP'].includes(data.outcome) ? phaseLabels[data.outcome === 'NORMAL' ? 'normal' : 'stopped'] : null) || phaseLabels[data.current_phase] || 'Průběh a potvrzené změny jsou uvedené níže.';
    document.getElementById('service-technical').textContent = JSON.stringify({stage:data.stage, current_phase:data.current_phase,
      outcome:data.outcome, detail:data.detail, source_sha256:data.source_sha256, phases:data.phases, last_confirmed:data.last_confirmed}, null, 2);
    result.dataset.outcome = data.outcome;
    const completion = document.getElementById('service-completion');
    completion.hidden = data.active || data.outcome !== 'NORMAL';
    completion.textContent = `Dokončené souborové operace: ${data.completed}${data.total === null ? '' : ' z ' + data.total}. ` +
      (data.phases.find(row => row.id === 'sqlite')?.state === 'PASS' ? 'Změny cest v databázi ověřeny. ' : 'Ověření databáze není v přehledu doložené. ') +
      'Potvrzený počet odstraněných adresářů najdete ve výsledném reportu.';
    const last = data.last_confirmed;
    const phaseName = last && phases.includes(last.phase)
      ? document.querySelector(`[data-phase="${last.phase}"] th`).textContent : 'Ověření průběhu';
    confirmed.textContent = last ? `Poslední potvrzený krok: ${phaseName}. Dokončených operací: ${last.completed}.` : 'Zatím bez potvrzené změny.';
    if (!data.active && ['NORMAL', 'STOP'].includes(data.outcome)) {
      result.textContent = data.outcome === 'NORMAL' ? 'Reorganizace dokončena' : 'Zastaveno, vyžaduje zásah';
      message.textContent = data.outcome === 'STOP'
        ? (data.stage === 'execution_not_started'
          ? 'Příprava spuštění selhala, E7 nezačalo. Opravte uvedenou příčinu a připravte nový požadavek.'
          : 'Reorganizace byla zastavena. Je nutný zásah správce; požadavek se nesmí opakovat.')
        : 'Reorganizace dokončena. Čeká se na hostitelem ověřený restart a dostupnou aplikaci.';
      if (data.outcome === 'NORMAL' && data.app_restart_state === 'failed') {
        message.textContent = 'Reorganizace dokončena. Restart aplikace selhal; je nutný zásah správce.';
      } else if (data.outcome === 'NORMAL' && data.app_restart_state === 'timed_out') {
        message.textContent = 'Reorganizace dokončena. Maximální čas pro dostupnost aplikace vypršel; je nutný zásah správce.';
      }
      if (data.outcome === 'NORMAL' && data.app_ready === true) message.textContent = 'Reorganizace dokončena. Návrat aplikace byl potvrzen hostitelem.';
      if (data.outcome === 'STOP' && data.error) confirmed.textContent += ` ${data.error.message}`;
    } else if (!fresh) {
      result.textContent = 'Stav neověřen';
      message.textContent = 'Aktualizace průběhu nejsou potvrzené. Tabulka ukazuje poslední známý stav, nikoli živý běh.';
    } else {
      result.textContent = 'Reorganizace knihovny';
      message.textContent = data.waiting_for_operator
        ? 'Worker čeká na výslovné rozhodnutí o živém návrhu cleanupu níže.'
        : 'Hostitel zpracovává požadavek. Fáze a potvrzené změny jsou uvedeny níže.';
    }
  }
  function renderCandidates() {
    if (!cleanup) return;
    const query = document.getElementById('cleanup-search').value.toLocaleLowerCase();
    const rows = cleanup.proposal.candidates.filter(row => row.path.toLocaleLowerCase().includes(query));
    const pages = Math.max(1, Math.ceil(rows.length / 50));
    cleanupPage = Math.min(cleanupPage, pages - 1);
    document.getElementById('cleanup-total').textContent = cleanup.proposal.candidates.length;
    document.getElementById('cleanup-count').textContent = `Kandidátů: ${cleanup.proposal.candidates.length} · ve filtru: ${rows.length}`;
    document.getElementById('cleanup-candidates').replaceChildren(...rows.slice(cleanupPage * 50, (cleanupPage + 1) * 50).map(row => {
      const label = element('label', ''); label.className = 'candidate';
      const check = document.createElement('input'); check.type = 'checkbox';
      check.dataset.cleanupPath = row.path; check.checked = selected.has(row.path);
      check.disabled = submitting || decidedProposal === cleanup.proposal.proposal_sha256;
      check.addEventListener('change', () => {
        if (check.checked) {
          for (const candidate of cleanup.proposal.candidates) {
            if (candidate.path === row.path || candidate.path.startsWith(row.path + '/')) selected.add(candidate.path);
          }
        } else {
          selected.delete(row.path);
          for (const path of [...selected]) {
            if (row.path.startsWith(path + '/')) selected.delete(path);
          }
        }
        document.getElementById('cleanup-confirm').checked = false;
        renderCandidates();
      });
      const text = element('span', row.path); text.className = 'path';
      label.append(check, text); return label;
    }));
    document.getElementById('cleanup-page').textContent = `${cleanupPage + 1} / ${pages}`;
    document.getElementById('cleanup-prev').disabled = cleanupPage === 0;
    document.getElementById('cleanup-next').disabled = cleanupPage + 1 === pages;
    document.getElementById('cleanup-selected').textContent = `Odstranit: ${selected.size} · zachovat: ${cleanup.proposal.candidates.length - selected.size} (z kandidátů návrhu)`;
    for (const id of ['cleanup-all', 'cleanup-none']) {
      document.getElementById(id).disabled = submitting || decidedProposal === cleanup.proposal.proposal_sha256;
    }
  }
  function disableDecision(disabled) {
    for (const id of ['cleanup-preserve', 'cleanup-delete', 'cleanup-stop', 'cleanup-all', 'cleanup-none']) document.getElementById(id).disabled = disabled;
  }
  function renderCleanupOutcome() {
    if (!cleanupOutcome) return false;
    cleanupPanel.hidden = true; cleanup = null; disableDecision(true);
    feedback.dataset.outcome = cleanupOutcome;
    feedback.textContent = cleanupOutcome === 'STOP'
      ? 'Reorganizace byla zastavena. Je nutný zásah správce; požadavek se automaticky neopakuje.'
      : 'Reorganizace dokončena. Ověření úklidu prázdných adresářů je dokončené.';
    return true;
  }
  async function showCleanup(status) {
    if (renderCleanupOutcome()) return;
    if (!status || typeof status.waiting !== 'boolean') throw new Error('invalid cleanup status');
    if (!status.waiting) {
      disableDecision(true); cleanupPanel.hidden = true; cleanup = null;
      if (status.decision_state === 'STOP') {
        feedback.textContent = 'STOP: doklad cleanup rozhodnutí není platný. Worker nesmí pokračovat.';
      } else if (status.decision_state === 'CONSUMED' && submissionState !== 'acknowledged') {
        feedback.textContent = 'Rozhodnutí již bylo převzato. Čeká se na E7H ověření; další rozhodnutí nelze odeslat.';
      }
      return;
    }
    if (!sha(status.source_sha256) || !sha(status.approved_sha256) || !sha(status.proposal_sha256) ||
        status.decision_state !== 'WAITING' || !Number.isInteger(status.candidate_count) || status.candidate_count < 0 ||
        (requestedSource && status.source_sha256 !== requestedSource)) {
      disableDecision(true); cleanupPanel.hidden = true; cleanup = null;
      feedback.textContent = 'STOP: cleanup návrh neodpovídá tomuto požadavku.'; return;
    }
    cleanupSource = status.source_sha256;
    if (submissionState === 'acknowledged' && decidedProposal === status.proposal_sha256) return;
    if (!cleanup || cleanup.proposal.proposal_sha256 !== status.proposal_sha256) {
      disableDecision(true); cleanupPanel.hidden = true;
      feedback.textContent = 'Načítám kandidátní adresáře pro přesný návrh…';
      const response = await fetch('/_service/control/cleanup/proposal?sha=' + status.proposal_sha256,
        {cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout(30000)});
      if (!response.ok) throw new Error('cleanup detail unavailable');
      const data = await response.json();
      if (renderCleanupOutcome()) return;
      if (!data || data.waiting !== true || data.source_sha256 !== status.source_sha256 ||
          data.approved_sha256 !== status.approved_sha256 || typeof data.csrf !== 'string' ||
          !data.proposal || data.proposal.proposal_sha256 !== status.proposal_sha256 ||
          !Array.isArray(data.proposal.candidates) || data.proposal.candidates.length !== status.candidate_count ||
          !data.proposal.candidates.every(row => typeof row.path === 'string' && typeof row.disposition === 'string')) {
        throw new Error('cleanup detail binding differs');
      }
      if (selectionProposal !== status.proposal_sha256) {
        selected.clear(); cleanupPage = 0; selectionProposal = status.proposal_sha256;
        for (const row of data.proposal.candidates) selected.add(row.path);
        document.getElementById('cleanup-sha').value = '';
        document.getElementById('cleanup-confirm').checked = false;
        document.getElementById('cleanup-search').value = '';
      }
      cleanup = data;
      document.getElementById('cleanup-proposal-sha').textContent = status.proposal_sha256;
      document.getElementById('cleanup-json').textContent = JSON.stringify(data.proposal, null, 2);
    }
    if (submissionState === 'uncertain') {
      feedback.textContent = 'Hostitel potvrzuje, že rozhodnutí dosud nepřevzal. Zkontrolujte jméno, přesný SHA a potvrzení; váš výběr zůstal zachován. Poté rozhodněte znovu.';
      submissionState = null; decidedProposal = null;
    } else if (submissionState === null) { feedback.textContent = ''; }
    cleanupPanel.hidden = false;
    disableDecision(submitting || decidedProposal === status.proposal_sha256);
    renderCandidates();
  }
  async function refreshCleanup() {
    const response = await fetch('/_service/control/cleanup',
      {cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('cleanup status unavailable');
    await showCleanup(await response.json());
  }
  async function decide(mode) {
    if (renderCleanupOutcome() || !cleanup || submitting || decidedProposal === cleanup.proposal.proposal_sha256) return;
    const actor = document.getElementById('cleanup-actor').value.trim();
    if (!actor || actor.length > 128 || /[\x00-\x1f]/.test(actor)) {
      feedback.textContent = 'Uveďte jméno správce pro evidenci rozhodnutí.'; return;
    }
    if (mode === 'delete' && (!selected.size || document.getElementById('cleanup-sha').value !== cleanup.proposal.proposal_sha256 ||
        !document.getElementById('cleanup-confirm').checked)) {
      feedback.textContent = 'Vyberte adresáře, opište přesný SHA-256 návrhu a výslovně potvrďte odstranění.'; return;
    }
    const body = {source_sha256: cleanup.source_sha256, approved_sha256: cleanup.approved_sha256,
      proposal_sha256: cleanup.proposal.proposal_sha256, actor, csrf: cleanup.csrf,
      directories: mode === 'delete' ? [...selected].sort() : [], confirm: true};
    if (mode === 'stop') body.decision = 'stop';
    submitting = true; decidedProposal = cleanup.proposal.proposal_sha256; decisionEpoch++;
    disableDecision(true); renderCandidates();
    feedback.textContent = 'Předávám jednorázové rozhodnutí hostiteli…';
    try {
      const response = await fetch('/_service/control/cleanup', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: AbortSignal.timeout(5000)});
      if (!response.ok || (await response.json()).accepted !== true) throw new Error('decision unconfirmed');
      if (renderCleanupOutcome()) return;
      submissionState = 'acknowledged'; cleanupPanel.hidden = true;
      feedback.textContent = mode === 'stop' ? 'Odmítnutí předáno. Čeká se na potvrzení STOP.'
        : 'Rozhodnutí předáno. Čeká se na E7H ověření a výsledek NORMAL nebo STOP.';
    } catch (error) {
      if (!renderCleanupOutcome()) {
        submissionState = 'uncertain'; cleanup = null;
        feedback.textContent = 'Výsledek předání není potvrzený. Ověřuji aktuální jednorázový stav; nic se automaticky neopakuje.';
      }
    } finally {
      submitting = false; decisionEpoch++;
      if (submissionState === 'uncertain' && !cleanupOutcome) {
        try { await refreshCleanup(); }
        catch (_) { if (!renderCleanupOutcome()) { disableDecision(true); feedback.textContent = 'Aktuální stav rozhodnutí není dostupný. Počkejte na ověření hostitele.'; } }
      }
    }
  }
  document.getElementById('cleanup-search').addEventListener('input', () => { cleanupPage = 0; renderCandidates(); });
  document.getElementById('cleanup-prev').addEventListener('click', () => { cleanupPage--; renderCandidates(); });
  document.getElementById('cleanup-next').addEventListener('click', () => { cleanupPage++; renderCandidates(); });
  for (const mode of ['all', 'none']) document.getElementById('cleanup-' + mode).addEventListener('click', () => {
    if (!cleanup || submitting || decidedProposal === cleanup.proposal.proposal_sha256) return;
    selected.clear();
    if (mode === 'all') for (const row of cleanup.proposal.candidates) selected.add(row.path);
    document.getElementById('cleanup-confirm').checked = false;
    renderCandidates();
  });
  for (const mode of ['preserve', 'delete', 'stop']) document.getElementById('cleanup-' + mode).addEventListener('click', () => decide(mode));
  async function poll() {
    if (polling) return;
    polling = true;
    const epoch = decisionEpoch;
    const [status, health, control] = await Promise.allSettled([
      fetch('/_service/progress.json', {cache: 'no-store', signal: AbortSignal.timeout(3000)}),
      fetch('/_service/app-health', {cache: 'no-store', signal: AbortSignal.timeout(3000)}),
      fetch('/_service/control/cleanup', {cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout(3000)})
    ]);
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
    if (control.status === 'fulfilled' && control.value.ok && !submitting && epoch === decisionEpoch) {
      try { await showCleanup(await control.value.json()); }
      catch (_) { disableDecision(true); feedback.textContent = 'Živý cleanup stav není potvrzený.'; }
    } else if (!submitting && epoch === decisionEpoch) { disableDecision(true); }
    render();
    const canonicalResult = snapshot && sha(snapshot.source_sha256) &&
      snapshot.result_url === '/reorganization/approval/' + snapshot.source_sha256;
    const prepared = !requestedSource && snapshot?.stage === 'ready' && canonicalResult;
    const preparationStopped = !requestedSource && requestedJob && snapshot?.outcome === 'STOP' &&
      snapshot.result_url === '/reorganization/jobs/' + requestedJob;
    const executionNotStarted = requestedSource && snapshot?.stage === 'execution_not_started' &&
      snapshot.outcome === 'STOP' && canonicalResult;
    if (reachable && snapshot.app_ready === true && health.status === 'fulfilled' && health.value.ok &&
        snapshot.active === false && ((snapshot.outcome === 'NORMAL' && canonicalResult) || prepared || preparationStopped || executionNotStarted)) {
      const link = document.getElementById('workflow-result');
      link.hidden = false; link.querySelector('a').href = snapshot.result_url;
      location.replace(snapshot.result_url);
    }
    polling = false;
  }
  poll(); setInterval(poll, 5000); setInterval(render, 1000);
})();
