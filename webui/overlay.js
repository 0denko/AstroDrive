/*
 * Astroberry OS Web UI Overlay (injected via Nginx sub_filter on http://astroberry.local)
 *
 * Adds a non-destructive "OnStepX" status pill + slide-over drawer directly inside
 * Astroberry Manager without modifying any files owned by the `astroberry-manager`
 * Debian package (100% safe across `apt upgrade`).
 */
(function () {
  if (window.__onstepAstroberryOverlayLoaded) return;
  window.__onstepAstroberryOverlayLoaded = true;

  const STYLE = `
    #onstep-overlay-pill {
      position: fixed;
      right: 16px;
      bottom: 16px;
      z-index: 99990;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.92);
      color: #e6edf7;
      border: 1px solid rgba(56, 189, 248, 0.45);
      box-shadow: 0 6px 22px rgba(0, 0, 0, 0.45);
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      backdrop-filter: blur(6px);
      transition: transform 0.15s ease, border-color 0.15s ease;
    }
    #onstep-overlay-pill:hover {
      transform: translateY(-1px);
      border-color: rgba(56, 189, 248, 0.9);
    }
    #onstep-overlay-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: #94a3b8;
    }
    #onstep-overlay-dot.ok { background: #22c55e; box-shadow: 0 0 8px #22c55e; }
    #onstep-overlay-dot.warn { background: #f59e0b; box-shadow: 0 0 8px #f59e0b; }
    #onstep-overlay-dot.err { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
    #onstep-overlay-backdrop {
      position: fixed;
      inset: 0;
      z-index: 99991;
      background: rgba(2, 6, 23, 0.6);
      display: none;
    }
    #onstep-overlay-drawer {
      position: fixed;
      top: 0;
      right: 0;
      width: min(680px, 100vw);
      height: 100vh;
      z-index: 99992;
      background: #0b0f17;
      border-left: 1px solid #243047;
      box-shadow: -8px 0 32px rgba(0, 0, 0, 0.65);
      display: none;
      flex-direction: column;
    }
    #onstep-overlay-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      background: #131a27;
      border-bottom: 1px solid #243047;
      color: #e6edf7;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      font-size: 13px;
      font-weight: 600;
    }
    #onstep-overlay-close {
      background: #1c273b;
      color: #e6edf7;
      border: 1px solid #243047;
      border-radius: 6px;
      padding: 4px 10px;
      cursor: pointer;
      font-size: 13px;
    }
    #onstep-overlay-frame {
      flex: 1;
      width: 100%;
      border: 0;
      background: #0b0f17;
    }
  `;

  function mount() {
    const styleEl = document.createElement('style');
    styleEl.textContent = STYLE;
    document.head.appendChild(styleEl);

    const pill = document.createElement('button');
    pill.id = 'onstep-overlay-pill';
    pill.type = 'button';
    pill.innerHTML = '<span id="onstep-overlay-dot"></span><span id="onstep-overlay-label">OnStepX</span>';

    const backdrop = document.createElement('div');
    backdrop.id = 'onstep-overlay-backdrop';

    const drawer = document.createElement('aside');
    drawer.id = 'onstep-overlay-drawer';
    drawer.innerHTML = `
      <div id="onstep-overlay-bar">
        <span>OnStepX Auto-Provisioner</span>
        <button id="onstep-overlay-close" type="button">✕ Close</button>
      </div>
      <iframe id="onstep-overlay-frame" title="OnStepX Auto-Provisioner"></iframe>
    `;

    document.body.appendChild(pill);
    document.body.appendChild(backdrop);
    document.body.appendChild(drawer);

    const frame = drawer.querySelector('#onstep-overlay-frame');
    let frameLoaded = false;

    function openDrawer() {
      if (!frameLoaded) {
        frame.src = '/onstep/';
        frameLoaded = true;
      }
      backdrop.style.display = 'block';
      drawer.style.display = 'flex';
    }

    function closeDrawer() {
      backdrop.style.display = 'none';
      drawer.style.display = 'none';
    }

    pill.addEventListener('click', openDrawer);
    backdrop.addEventListener('click', closeDrawer);
    drawer.querySelector('#onstep-overlay-close').addEventListener('click', closeDrawer);

    async function pollStatus() {
      const dot = document.getElementById('onstep-overlay-dot');
      const label = document.getElementById('onstep-overlay-label');
      if (!dot || !label) return;
      try {
        const res = await fetch('/onstep/api/status', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const s = await res.json();
        dot.className = '';
        if (s.provisioning_active) {
          dot.classList.add('warn');
          label.textContent = 'OnStepX • Provisioning…';
        } else if (s.indi_active) {
          dot.classList.add('ok');
          label.textContent = 'OnStepX • INDI Active';
        } else if (s.symlink_ready) {
          dot.classList.add('ok');
          label.textContent = 'OnStepX • Ready';
        } else if (s.device) {
          dot.classList.add('warn');
          label.textContent = 'OnStepX • Unverified';
        } else {
          dot.classList.add('err');
          label.textContent = 'OnStepX • Disconnected';
        }
      } catch (_) {
        dot.className = 'err';
        label.textContent = 'OnStepX • Offline';
      }
    }

    pollStatus();
    setInterval(pollStatus, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
