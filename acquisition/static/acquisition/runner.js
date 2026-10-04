/* The job runner, driven by this page.

   Render's free plan has no background worker, so the queue needs someone
   to turn the handle: while this tab is open and you press Run, it calls
   /owner/acquisition/jobs/run-next over and over. Each call does a little
   work and comes back, so no single request ever approaches the server's
   30-second limit.

   Closing the tab simply stops the loop. Whatever is still queued stays
   queued, and a job that was mid-flight is picked up again once its lock
   expires. When a background worker is added later, it calls the same
   endpoint's function in a loop and this file stops mattering. */
(function () {
  const button = document.getElementById('run-queue');
  if (!button) return;

  const statusLine = document.getElementById('run-status');
  const log = document.getElementById('run-log');
  const depth = document.getElementById('queue-depth');
  const token = button.getAttribute('data-csrf');

  let running = false;

  function say(text) {
    if (statusLine) statusLine.textContent = text;
  }

  function note(text, ok, kind) {
    if (!log) return;
    const line = document.createElement('div');
    line.className = 'run-line' + (kind ? ' ' + kind : ok === false ? ' bad' : '');
    line.textContent = text;
    log.prepend(line);
    while (log.children.length > 40) log.removeChild(log.lastChild);
  }

  async function step() {
    const response = await fetch('/owner/acquisition/jobs/run-next', {
      method: 'POST',
      headers: { 'X-CSRF-Token': token, 'Content-Type': 'application/json' },
      body: '{}'
    });
    if (!response.ok) throw new Error('The server answered ' + response.status);
    return response.json();
  }

  async function loop() {
    running = true;
    button.disabled = true;
    button.textContent = '⏳ Running...';
    try {
      while (running) {
        const outcome = await step();
        if (depth && typeof outcome.queue === 'number') {
          depth.textContent = outcome.queue;
        }
        (outcome.results || []).forEach((item) => {
          if (item.problem) {
            // It ran, it just came back with bad news. Saying nothing here is
            // how "Network is unreachable" hides behind a green tick.
            note(`⚠ ${item.type} #${item.job_id}: ${item.error}`, false, 'warn');
          } else {
            note(item.ok
              ? `✔ ${item.type} #${item.job_id} (${item.duration_ms} ms)`
              : `✖ ${item.type} #${item.job_id}: ${item.error}`, item.ok);
          }
        });
        if (!outcome.ran) {
          say(outcome.reason === 'stopped'
            ? 'Everything is stopped in Settings.'
            : 'Queue empty.');
          break;
        }
        say(`${outcome.queue} left in the queue.`);
      }
    } catch (error) {
      say('Stopped: ' + error.message);
      note('✖ ' + error.message, false);
    } finally {
      running = false;
      button.disabled = false;
      button.textContent = '▶ Run the queue';
    }
  }

  button.addEventListener('click', () => {
    if (running) { running = false; return; }
    if (log) log.innerHTML = '';
    say('Working...');
    loop();
  });
})();
