(() => {
  'use strict';
  const job = document.getElementById('reorganization-job');
  let finished = false;
  async function poll() {
    if (finished) return;
    try {
      const response = await fetch(job.dataset.statusUrl, {cache: 'no-store', signal: AbortSignal.timeout(4000)});
      if (!response.ok) throw new Error('Stav není potvrzený. Obnovte stránku nebo ověřte hostitelskou evidenci.');
      const data = await response.json();
      document.getElementById('job-state').textContent = data.state;
      document.getElementById('job-detail').textContent = data.detail;
      document.getElementById('job-phase').textContent = data.phase;
      if (data.state === 'READY' && /^\/reorganization\/approval\/[a-f0-9]{64}$/.test(data.result_url)) {
        const result = document.getElementById('job-result');
        result.hidden = false;
        result.querySelector('a').href = data.result_url;
        finished = true;
        location.replace(data.result_url);
      }
      if (data.state === 'STOP') finished = true;
    } catch (error) {
      document.getElementById('job-detail').textContent = error.message;
    }
  }
  poll(); setInterval(poll, 5000);
})();
