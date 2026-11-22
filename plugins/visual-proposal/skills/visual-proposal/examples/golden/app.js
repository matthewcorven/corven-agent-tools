(() => {
  const data = JSON.parse(document.getElementById('deck-data').textContent);
  const slides = [...document.querySelectorAll('#deck section')];
  const picker = document.getElementById('slide-picker');
  const status = document.getElementById('live-status');
  const follow = document.getElementById('follow');
  const storageKey = `visual-proposal-follow:${location.pathname}`;
  let current = Math.max(0, data.ids.indexOf(location.hash.slice(1) || data.focus));
  let following = sessionStorage.getItem(storageKey) !== 'off';
  let reading = false;
  let zoomed = false;
  for (const [index, slide] of slides.entries()) {
    slide.dataset.slideId = data.ids[index];
    slide.setAttribute('aria-label', `${index + 1}. ${data.titles[index]}`);
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = `${index + 1} / ${slides.length} - ${data.titles[index]}`;
    picker.append(option);
  }
  function layout() {
    const available = document.getElementById('stage').clientWidth - (innerWidth <= 640 ? 20 : 40);
    const compact = available < 680 && !zoomed;
    document.body.classList.toggle('compact', compact);
    document.body.classList.toggle('zoomed', zoomed);
    const scale = zoomed ? Math.max(1.7, available / 1280) : Math.min(1.2, available / 1280);
    const deck = document.getElementById('deck');
    deck.style.width = `${compact ? available : 1280 * scale}px`;
    deck.style.height = compact ? 'auto' : `${720 * scale}px`;
    slides.forEach(slide => {
      slide.style.transform = compact ? 'none' : `scale(${scale})`;
      slide.style.marginBottom = reading && !compact ? `${20 - 720 * (1 - scale)}px` : '0';
    });
  }
  function show(index, manual = false) {
    current = Math.max(0, Math.min(slides.length - 1, index));
    if (manual) {
      following = false;
      sessionStorage.setItem(storageKey, 'off');
    }
    follow.textContent = `Follow latest: ${following ? 'on' : 'off'}`;
    follow.setAttribute('aria-pressed', String(following));
    slides.forEach((slide, i) => { slide.hidden = !reading && i !== current; });
    picker.value = String(current);
    document.getElementById('counter').textContent = `${current + 1} of ${slides.length}`;
    document.getElementById('previous').disabled = current === 0;
    document.getElementById('next').disabled = current === slides.length - 1;
    history.replaceState(null, '', `#${data.ids[current]}`);
    layout();
  }
  picker.addEventListener('change', () => show(Number(picker.value), true));
  document.getElementById('previous').onclick = () => show(current - 1, true);
  document.getElementById('next').onclick = () => show(current + 1, true);
  follow.onclick = () => {
    following = !following;
    sessionStorage.setItem(storageKey, following ? 'on' : 'off');
    show(following ? data.ids.indexOf(data.focus) : current);
  };
  document.getElementById('reading').onclick = event => {
    reading = !reading;
    document.body.classList.toggle('reading', reading);
    event.target.textContent = reading ? 'Slide view' : 'Read all';
    show(current);
  };
  document.getElementById('zoom').onclick = event => {
    zoomed = !zoomed;
    event.target.textContent = zoomed ? 'Fit slide' : 'Zoom diagram';
    layout();
  };
  document.getElementById('source').onclick = () => {
    const url = URL.createObjectURL(new Blob([data.markdown], { type: 'text/markdown' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = data.downloadName;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  addEventListener('keydown', event => {
    if (event.target.closest('button, select, input, textarea, a, summary')) return;
    if (['ArrowRight', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); show(current + 1, true); }
    if (['ArrowLeft', 'PageUp'].includes(event.key)) { event.preventDefault(); show(current - 1, true); }
    if (event.key === 'Home') show(0, true);
    if (event.key === 'End') show(slides.length - 1, true);
  });
  addEventListener('resize', layout);
  show(current);
  if (location.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) {
    status.textContent = 'Standalone edition - everything is embedded';
    follow.disabled = true;
    return;
  }
  async function poll() {
    try {
      const response = await fetch('/__status', { cache: 'no-store' });
      if (!response.ok) throw new Error(`Preview status: HTTP ${response.status}`);
      const latest = await response.json();
      status.textContent = latest.error ? `Build failed: ${latest.error}` : `Live edition - ${latest.phase}`;
      if (latest.error || latest.building) return;
      if (!latest.ids.includes(latest.focus)) throw new Error(`Unknown focus: ${latest.focus}`);
      if (latest.revision !== data.revision) {
        const target = following ? latest.focus : data.ids[current];
        location.replace(`${location.pathname}?revision=${encodeURIComponent(latest.revision)}#${target}`);
        return;
      }
      data.focus = latest.focus;
      if (following && current !== data.ids.indexOf(data.focus)) show(data.ids.indexOf(data.focus));
    } catch (error) {
      status.textContent = `Preview disconnected - retained slide remains available (${error.message})`;
    } finally {
      setTimeout(poll, 1200);
    }
  }
  poll();
})();
