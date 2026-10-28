/* The saved server timestamp is authoritative; JavaScript only draws the clock. */
(() => {
  const clocks = document.querySelectorAll('[data-started-at-us]');
  const serverNow = Number(document.body.dataset.serverNowUs);
  if (!clocks.length || !Number.isFinite(serverNow)) return;
  const loadedAt = performance.now();

  function formatDuration(microseconds) {
    const seconds = Math.floor(Math.max(0, microseconds) / 1_000_000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    return [hours, minutes, remainder].map(value => String(value).padStart(2, '0')).join(':');
  }

  function updateClocks() {
    const now = serverNow + (performance.now() - loadedAt) * 1000;
    clocks.forEach(clock => {
      const startedAt = Number(clock.dataset.startedAtUs);
      if (Number.isFinite(startedAt)) clock.textContent = formatDuration(now - startedAt);
    });
  }

  updateClocks();
  window.setInterval(updateClocks, 1000);
  document.addEventListener('visibilitychange', updateClocks);
})();
