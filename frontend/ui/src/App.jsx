import { useEffect, useRef, useState } from 'react';
import MapPicker from './MapPicker.jsx';

const API = import.meta.env.VITE_API_URL || '';
const UPDATE_POLL_MS = 2000;
// systemctl start --no-block returns before the unit reaches its activating state.
const UPDATE_START_GRACE_MS = 25000;
// the updater restarts the API and reloads nginx, so status polls drop out mid-run.
const UPDATE_MAX_OFFLINE_POLLS = 90;
const STACK_POLL_MS = 1500;
const initialMount = { mount_type: 'equatorial', ra_steps_per_revolution: 3200, dec_steps_per_revolution: 3200, ra_belt_ratio: 1, dec_belt_ratio: 1, ra_reverse: false, dec_reverse: false, max_speed: 2000, acceleration: 4000, driver_type: 'step_dir', ra_step_pin: 25, ra_dir_pin: 26, ra_enable_pin: 27, dec_step_pin: 14, dec_dir_pin: 12, dec_enable_pin: 13, enable_active_low: true };
const initialLocation = { latitude: 0, longitude: 0, elevation_m: 0, location_source: 'manual' };
const CAMERA_FIELDS = [
  { key: 'auto_exposure', label: 'Auto exposure', toggle: true },
  { key: 'exposure', label: 'Exposure' },
  { key: 'gain', label: 'Gain' },
  { key: 'brightness', label: 'Brightness' },
  { key: 'contrast', label: 'Contrast' },
  { key: 'saturation', label: 'Saturation' },
  { key: 'auto_white_balance', label: 'Auto white balance', toggle: true },
  { key: 'white_balance_temperature', label: 'White balance K' },
  { key: 'auto_focus', label: 'Auto focus', toggle: true },
  { key: 'focus', label: 'Focus' },
];
const VIEWS = [
  { key: 'observe', label: 'Observe' },
  { key: 'control', label: 'Control' },
  { key: 'align', label: 'Align' },
  { key: 'objects', label: 'Objects' },
  { key: 'setup', label: 'Setup' },
];

export default function App() {
  const [status, setStatus] = useState(null);
  const [view, setView] = useState(() => localStorage.getItem('astrodrive.view') || 'observe');
  const [target, setTarget] = useState({ right_ascension: '5.5', declination: '22.0' });
  const [jogDegrees, setJogDegrees] = useState(1);
  const [testRate, setTestRate] = useState(200);
  const [testAxis, setTestAxis] = useState('ra');
  const [testRunning, setTestRunning] = useState(false);
  // the status poll rehydrates these forms, so an edit in progress has to block it or it is lost
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [serialDirty, setSerialDirty] = useState(false);
  const [serialPort, applySerialPort] = useState('auto');
  const [mount, applyMount] = useState(initialMount);
  const [location, applyLocation] = useState(initialLocation);
  const setSerialPort = (value) => { setSerialDirty(true); applySerialPort(value); };
  const setMount = (value) => { setSettingsDirty(true); applyMount(value); };
  const setLocation = (value) => { setSettingsDirty(true); applyLocation(value); };
  const [point, setPoint] = useState({ name: 'Alignment star', right_ascension: '5.5', declination: '22.0' });
  const [suggestions, setSuggestions] = useState([]);
  const [message, setMessage] = useState('');
  const [updating, setUpdating] = useState(false);
  const [updateStatus, setUpdateStatus] = useState({ state: 'idle', detail: '' });
  const updateOfflineRef = useRef(0);
  const [objectName, setObjectName] = useState('M31');
  const [objectResult, setObjectResult] = useState(null);
  const [satellites, setSatellites] = useState([]);
  const [cameraControls, setCameraControls] = useState({});
  const [cameraMeta, setCameraMeta] = useState({ available: false, controls: {}, error: '' });
  const [cameraStreamOk, setCameraStreamOk] = useState(true);
  const [stack, setStack] = useState({ frames: 8, interval_ms: 250, background: 0.15, gamma: 1 });
  const [stackImage, setStackImage] = useState('');
  const [stackBusy, setStackBusy] = useState(false);
  const [serialLog, setSerialLog] = useState([]);
  const [serialLink, setSerialLink] = useState({ connected: false, device: '' });
  const [rawCommand, setRawCommand] = useState('');
  const serialSeqRef = useRef(0);
  // sliders fire on every pixel of a drag, so the device only sees the value that was settled on
  const cameraApplyRef = useRef(null);
  const stackApplyRef = useRef(null);
  const tuneApplyRef = useRef(null);
  const testApplyRef = useRef(null);

  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, { headers: { 'Content-Type': 'application/json' }, ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || 'Request unavailable');
    return body;
  }

  async function refreshStatus() {
    try { setStatus(await request('/api/status')); } catch { setStatus({ connected: false, esp32_connected: false, mount: 'unknown' }); }
  }

  async function pollSerialLog() {
    try {
      const result = await request(`/api/serial/log?after=${serialSeqRef.current}`);
      serialSeqRef.current = result.last_seq;
      if (result.entries.length) setSerialLog((previous) => [...previous, ...result.entries].slice(-200));
      setSerialLink({ connected: result.connected, device: result.device });
    } catch { setSerialLink({ connected: false, device: '' }); }
  }

  async function sendSerial(command) {
    try {
      const result = await request('/api/serial/command', { method: 'POST', body: JSON.stringify({ command }) });
      await pollSerialLog();
      if (!result.replies.length) setMessage(`Sent "${command}" but the board said nothing back`);
    } catch (error) { setMessage(error.message); await pollSerialLog(); }
  }

  async function run(action, success) {
    try { await action(); setMessage(success); refreshStatus(); } catch (error) { setMessage(error.message); }
  }

  function sendCommand(payload) { return run(() => request('/api/command', { method: 'POST', body: JSON.stringify(payload) }), 'Command accepted'); }
  function sendTarget(event) { event.preventDefault(); return run(() => request('/api/target', { method: 'POST', body: JSON.stringify({ right_ascension: Number(target.right_ascension), declination: Number(target.declination) }) }), 'Target queued'); }
  // a raw step count means nothing at the eyepiece and changes meaning with the gearing
  function jogSteps(axis) { const config = status?.mount_config; if (!config) return 10; const perDegree = config[`${axis}_steps_per_revolution`] * config[`${axis}_belt_ratio`] / 360; return Math.max(1, Math.round(perDegree * jogDegrees)); }
  function stepsPerDegree(axis) { const config = status?.mount_config; return config ? config[`${axis}_steps_per_revolution`] * config[`${axis}_belt_ratio`] / 360 : 0; }
  function sendTuning(command, value) { return request('/api/command', { method: 'POST', body: JSON.stringify({ command, value }) }).catch((error) => setMessage(error.message)); }
  function changeTuning(key, value) { if (key === 'max_speed' && testRate > value) setTestRate(value); setMount({ ...mount, [key]: value }); clearTimeout(tuneApplyRef.current); tuneApplyRef.current = setTimeout(() => sendTuning(key === 'max_speed' ? 'speed' : 'accel', value), 200); }
  function runTest(rate) { return request('/api/command', { method: 'POST', body: JSON.stringify({ command: 'track', axis: testAxis, rate }) }).catch((error) => setMessage(error.message)); }
  function changeTestRate(rate) { setTestRate(rate); if (!testRunning) return; clearTimeout(testApplyRef.current); testApplyRef.current = setTimeout(() => runTest(rate), 120); }
  async function startTest() { try { await request('/api/command', { method: 'POST', body: JSON.stringify({ command: 'enable' }) }); await runTest(testRate); setTestRunning(true); setMessage('Test run started'); } catch (error) { setMessage(error.message); } }
  async function stopTest() { clearTimeout(testApplyRef.current); setTestRunning(false); try { await request('/api/command', { method: 'POST', body: JSON.stringify({ command: 'stop' }) }); setMessage('Test run stopped'); } catch (error) { setMessage(error.message); } }
  function saveSerialPort(event) { event.preventDefault(); return run(async () => { const result = await request('/api/settings/serial', { method: 'PUT', body: JSON.stringify({ port: serialPort }) }); setMessage(result.connected ? 'ESP32 connected' : 'Port saved; ESP32 not found'); setSerialDirty(false); }, 'Port saved'); }
  function saveSettings(event) { event.preventDefault(); return run(async () => { await request('/api/settings/mount', { method: 'PUT', body: JSON.stringify({ ...mount, ra_steps_per_revolution: Number(mount.ra_steps_per_revolution), dec_steps_per_revolution: Number(mount.dec_steps_per_revolution), ra_belt_ratio: Number(mount.ra_belt_ratio), dec_belt_ratio: Number(mount.dec_belt_ratio) }) }); await request('/api/settings/location', { method: 'PUT', body: JSON.stringify({ ...location, latitude: Number(location.latitude), longitude: Number(location.longitude), elevation_m: Number(location.elevation_m) }) }); setSettingsDirty(false); }, 'Settings saved'); }
  function alignmentAction(action) { return run(() => request('/api/alignment', { method: 'POST', body: JSON.stringify({ action }) }), action === 'complete' ? 'Alignment complete' : `Alignment ${action}`); }
  function addPoint(event) { event.preventDefault(); return run(() => request('/api/alignment/point', { method: 'POST', body: JSON.stringify({ ...point, right_ascension: Number(point.right_ascension), declination: Number(point.declination) }) }), 'Alignment point recorded'); }
  async function loadSuggestions() { try { const result = await request('/api/alignment/suggestions?limit=10'); setSuggestions(result.stars || []); if (!result.stars?.length) setMessage('Nothing bright is above the horizon for the saved site'); } catch (error) { setMessage(error.message); } }
  async function lookupPoint() { const name = point.name.trim(); if (!name) return; try { const result = await request(`/api/objects/resolve?name=${encodeURIComponent(name)}`); setPoint({ name: result.name, right_ascension: String(result.right_ascension), declination: String(result.declination) }); setMessage(`${result.name} found`); } catch (error) { setMessage(error.message); } }
  function toggleTracking() { return run(() => request('/api/tracking', { method: 'POST', body: JSON.stringify({ enabled: !status?.tracking }) }), status?.tracking ? 'Tracking stopped' : 'Tracking enabled'); }
  async function refreshUpdateStatus() { try { const result = await request('/api/update/status'); updateOfflineRef.current = 0; setUpdateStatus(result); return result; } catch { updateOfflineRef.current += 1; if (updateOfflineRef.current > 2) setUpdateStatus({ state: 'offline', detail: 'Device is restarting...' }); return null; } }
  async function triggerUpdate() {
    setUpdating(true);
    setMessage('Starting update...');
    let previousInvocation = '';
    try { previousInvocation = (await request('/api/update', { method: 'POST' })).previous_invocation_id || ''; } catch (error) { setMessage(`Update failed: ${error.message}`); setUpdating(false); return; }
    const startedAt = Date.now();
    let offline = 0;
    let result = null;
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, UPDATE_POLL_MS));
      const polled = await refreshUpdateStatus();
      if (!polled) {
        offline += 1;
        if (offline <= UPDATE_MAX_OFFLINE_POLLS) continue;
        setMessage('Update failed: lost contact with the device');
        setUpdating(false);
        return;
      }
      offline = 0;
      result = polled;
      if (polled.state === 'running') continue;
      const startedNewRun = polled.invocation_id ? polled.invocation_id !== previousInvocation : Date.now() - startedAt >= UPDATE_START_GRACE_MS;
      if (!startedNewRun && Date.now() - startedAt < UPDATE_START_GRACE_MS) continue;
      break;
    }
    setMessage(result.state === 'failed' ? `Update failed: ${result.detail}` : `Update complete: ${result.detail}`);
    setUpdating(false);
    refreshStatus();
  }
  function pickLocation(value) { setLocation({ ...location, ...value, location_source: 'manual' }); }
  function useBrowserLocation() { if (!navigator.geolocation) { setMessage('Browser location unavailable'); return; } navigator.geolocation.getCurrentPosition((position) => pickLocation({ latitude: position.coords.latitude.toFixed(6), longitude: position.coords.longitude.toFixed(6) }), () => setMessage('Location permission denied')); }
  async function resolveObject(event) { event.preventDefault(); try { const result = await request(`/api/objects/resolve?name=${encodeURIComponent(objectName)}`); setObjectResult(result); setTarget({ right_ascension: String(result.right_ascension), declination: String(result.declination) }); setMessage('Object coordinates loaded'); } catch (error) { setMessage(error.message); } }
  async function loadSatellites() { try { const result = await request('/api/objects/satellites'); setSatellites(result.objects); setMessage('Satellite list loaded'); } catch (error) { setMessage(error.message); } }
  async function refreshCameraControls() {
    try {
      const result = await request('/api/camera/controls');
      setCameraMeta({ available: result.available, controls: result.controls || {}, error: result.error || '' });
      setCameraControls(result.values || {});
    } catch (error) { setCameraMeta({ available: false, controls: {}, error: error.message }); }
  }
  async function pushCameraControls(values) {
    const payload = {};
    for (const field of CAMERA_FIELDS) {
      const value = values[field.key];
      if (!cameraMeta.controls[field.key] || value === undefined || value === '') continue;
      payload[field.key] = field.toggle ? Boolean(value) : Number(value);
    }
    try {
      // the whole set goes every time because the driver refuses an exposure change unless auto
      // exposure is re-asserted in the same call, and the API applies the auto controls first
      const result = await request('/api/camera/controls', { method: 'PUT', body: JSON.stringify(payload) });
      setCameraMeta({ available: true, controls: result.controls || cameraMeta.controls, error: '' });
      setMessage(result.failures?.length ? `Camera partly applied: ${result.failures.join('; ')}` : 'Camera settings applied');
    } catch (error) { setMessage(error.message); }
  }
  function setCameraControl(key, value) {
    const next = { ...cameraControls, [key]: value };
    setCameraControls(next);
    clearTimeout(cameraApplyRef.current);
    cameraApplyRef.current = setTimeout(() => pushCameraControls(next), 300);
  }
  function setStackField(key, value) {
    const next = { ...stack, [key]: value };
    setStack(next);
    // frames and interval only decide how the next run starts, so there is nothing live to update
    if (key !== 'background' && key !== 'gamma') return;
    clearTimeout(stackApplyRef.current);
    // a running stacker re-reads this between frames, so the preview restretches without losing any
    stackApplyRef.current = setTimeout(async () => {
      const body = JSON.stringify({ background: Number(next.background), gamma: Number(next.gamma) });
      try { await request('/api/camera/stack/tuning', { method: 'PUT', body }); } catch (error) { setMessage(error.message); }
    }, 250);
  }
  async function refreshStack() { try { const result = await request('/api/camera/stack'); if (result.image_url) setStackImage(result.image_url); return result; } catch { return null; } }
  async function runStack(event, frames) {
    if (event) event.preventDefault();
    setStackBusy(true);
    setMessage(frames === 0 ? 'Live stack started' : 'Stack capture started');
    try {
      await request('/api/camera/stack', { method: 'POST', body: JSON.stringify({ frames, interval_ms: Number(stack.interval_ms), background: Number(stack.background), gamma: Number(stack.gamma) }) });
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, STACK_POLL_MS));
        const result = await refreshStack();
        // each poll returns a fresh preview URL, so the image keeps improving while this waits
        if (!result || result.state === 'running') { if (result) setMessage(result.detail); continue; }
        setMessage(result.state === 'failed' ? `Stack failed: ${result.detail}` : result.detail || 'Stacked frame ready');
        break;
      }
    } catch (error) { setMessage(error.message); } finally { setStackBusy(false); }
  }
  async function stopStack() { try { await request('/api/camera/stack/stop', { method: 'POST' }); } catch (error) { setMessage(error.message); } }

  useEffect(() => { refreshStatus(); refreshUpdateStatus(); refreshCameraControls(); refreshStack(); const timer = setInterval(refreshStatus, 5000); return () => clearInterval(timer); }, []);
  // keeps the button in sync with updates started by the boot unit, the timer, or another browser tab
  useEffect(() => { if (updating) return undefined; const timer = setInterval(refreshUpdateStatus, 5000); return () => clearInterval(timer); }, [updating]);
  useEffect(() => { localStorage.setItem('astrodrive.view', view); }, [view]);
  // the list is only true for the moment it was computed, so refetch it whenever the tab opens
  useEffect(() => { if (view === 'align') loadSuggestions(); }, [view]);
  // never leave a motor running because the tab was switched away from
  useEffect(() => { if (view !== 'setup' && testRunning) stopTest(); }, [view]);
  useEffect(() => { if (view !== 'setup') return undefined; pollSerialLog(); const timer = setInterval(pollSerialLog, 2000); return () => clearInterval(timer); }, [view]);
  useEffect(() => {
    if (status?.serial_port && !serialDirty) applySerialPort(status.serial_port);
    if (status?.mount_config && !settingsDirty) applyMount(status.mount_config);
    if (status?.location && !settingsDirty) applyLocation(status.location);
  }, [status, serialDirty, settingsDirty]);

  const updateBusy = updating || updateStatus.state === 'running';
  const updatePercent = typeof updateStatus.progress === 'number' ? updateStatus.progress : null;
  const updateLabel = updateStatus.state === 'running' ? `Updating: ${updatePercent === null ? '' : `${updatePercent}% `}${updateStatus.detail}`
    : updateStatus.state === 'offline' ? updateStatus.detail
    : updateStatus.state === 'failed' ? `Update failed: ${updateStatus.detail}`
    : updating ? 'Waiting for the updater to start...'
    : 'Check for updates';
  // the ring tracks a live run only, so a finished run's lingering 100% does not read as pending work
  const ringPercent = updateBusy && updatePercent !== null ? updatePercent : 0;
  const ringState = updateStatus.state === 'failed' ? 'failed' : !updateBusy ? 'idle' : updatePercent === null ? 'spinning' : '';
  const ringGlyph = updateStatus.state === 'failed' ? '!' : !updateBusy ? '↓' : updatePercent === null ? '' : `${updatePercent}%`;
  // the axes are the same two motors either way, but an alt-az mount turns them in azimuth and
  // altitude, so calling them RA and DEC on screen would be a lie
  const axisLabel = status?.mount_config?.mount_type === 'altaz' ? { ra: 'AZ', dec: 'ALT' } : { ra: 'RA', dec: 'DEC' };

  return <main>
    <header><div><span className="eyebrow">MOUNT CONTROL / LIVE</span><h1>AstroDrive</h1></div><div className="status-stack"><div className="status-badges"><span className={`connection ${status?.esp32_connected ? 'online' : ''}`}>{status?.esp32_connected ? 'ESP32 ONLINE' : 'ESP32 OFFLINE'}</span><span className={`connection ${status?.mount === 'aligned' || status?.mount === 'tracking' ? 'online' : ''}`}>MOUNT {status?.mount?.replaceAll('_', ' ').toUpperCase() ?? 'CHECKING'}</span></div><div className="update-row" title={updateLabel}><button className={`update-ring ${ringState}`} style={{ '--progress': ringPercent }} onClick={triggerUpdate} disabled={updateBusy} aria-label={updateLabel} role="progressbar" aria-valuenow={ringPercent}><span>{ringGlyph}</span></button>{updateStatus.state === 'failed' && <span className="update-detail">{updateStatus.detail}</span>}</div></div></header>
    <nav className="tabs">{VIEWS.map((item) => <button key={item.key} type="button" aria-current={view === item.key ? 'page' : undefined} onClick={() => setView(item.key)}>{item.label}</button>)}</nav>
    {view === 'observe' && <section className="grid">
      <div className="panel camera"><div className="panel-heading"><span>Sky camera</span>{cameraStreamOk && status?.camera_url ? <span className="live-dot">● LIVE</span> : null}</div>{status?.camera_url ? <img src={status.camera_url} alt="Live sky camera" style={cameraStreamOk ? undefined : { display: 'none' }} onError={() => setCameraStreamOk(false)} onLoad={() => setCameraStreamOk(true)} /> : null}{status?.camera_url && cameraStreamOk ? null : <div className="empty">{cameraMeta.error || 'Waiting for camera stream'}</div>}</div>
      <div className="panel camera-settings">
        <div className="panel-heading"><span>Camera controls</span><span>{cameraMeta.available ? 'V4L2' : 'UNAVAILABLE'}</span></div>
        {cameraMeta.available ? <>
          <div className="fields">{CAMERA_FIELDS.filter((field) => cameraMeta.controls[field.key]).map((field) => {
            const meta = cameraMeta.controls[field.key];
            const hint = meta.inactive ? 'Driver ignores this until the matching auto control is off' : '';
            const set = (value) => setCameraControl(field.key, value);
            if (field.toggle) return <label key={field.key} title={hint}>{field.label}<select value={cameraControls[field.key] ? 'on' : 'off'} onChange={(event) => set(event.target.value === 'on')}><option value="on">ON</option><option value="off">OFF</option></select></label>;
            const value = cameraControls[field.key] ?? meta.value ?? meta.min ?? 0;
            // a few controls report no bounds, and a slider without them cannot say where it is
            if (meta.min === null || meta.max === null) return <label key={field.key} title={hint}>{field.label}<input type="number" step={meta.step || 1} value={value} onChange={(event) => set(event.target.value)} /></label>;
            return <label key={field.key} className={`slider${meta.inactive ? ' inactive' : ''}`} title={hint}>
              <span className="slider-head">{field.label}<b>{value}</b></span>
              <input type="range" min={meta.min} max={meta.max} step={meta.step || 1} value={value} onChange={(event) => set(Number(event.target.value))} />
              <span className="slider-scale"><span>{meta.min}</span><span>{meta.max}</span></span>
            </label>;
          })}</div>
        </> : <p>{cameraMeta.error || 'This camera exposes no V4L2 controls'}</p>}
      </div>
      <form className="panel low-light" onSubmit={(event) => runStack(event, Number(stack.frames))}><div className="panel-heading"><span>Low-light stack</span><span>{stackBusy ? 'STACKING' : 'AVERAGE FRAMES'}</span></div><div className="fields"><label>Frames<input type="number" min="2" max="600" value={stack.frames} onChange={(event) => setStackField('frames', event.target.value)} /></label><label>Interval ms<input type="number" min="0" value={stack.interval_ms} onChange={(event) => setStackField('interval_ms', event.target.value)} /></label><label className="slider"><span className="slider-head">Sky brightness<b>{Number(stack.background).toFixed(2)}</b></span><input type="range" min="0.02" max="0.6" step="0.01" value={stack.background} onChange={(event) => setStackField('background', Number(event.target.value))} /><span className="slider-scale"><span>dark sky</span><span>faint detail</span></span></label><label className="slider"><span className="slider-head">Extra lift<b>{Number(stack.gamma).toFixed(1)}</b></span><input type="range" min="1" max="5" step="0.1" value={stack.gamma} onChange={(event) => setStackField('gamma', Number(event.target.value))} /><span className="slider-scale"><span>none</span><span>max</span></span></label></div><div className="actions"><button className="primary" type="submit" disabled={stackBusy}>{stackBusy ? 'Stacking...' : 'Capture stacked preview'}</button>{stackBusy ? <button type="button" onClick={stopStack}>Stop</button> : <button type="button" onClick={() => runStack(null, 0)}>Stack live</button>}</div>{stackImage && <img className="stack-preview" src={stackImage} alt="Stacked low-light preview" />}</form>    </section>}
    {view === 'control' && <section className="grid">
      <div className="panel controls"><div className="panel-heading"><span>Manual control</span><span>{status?.serial_port ?? 'auto'}</span></div><div className="dpad"><button onClick={() => sendCommand({ command: 'move', axis: 'dec', direction: 'forward', steps: jogSteps('dec') })}>{axisLabel.dec} +</button><button onClick={() => sendCommand({ command: 'move', axis: 'ra', direction: 'backward', steps: jogSteps('ra') })}>{axisLabel.ra} −</button><button onClick={() => sendCommand({ command: 'stop' })}>STOP</button><button onClick={() => sendCommand({ command: 'move', axis: 'ra', direction: 'forward', steps: jogSteps('ra') })}>{axisLabel.ra} +</button><button onClick={() => sendCommand({ command: 'move', axis: 'dec', direction: 'backward', steps: jogSteps('dec') })}>{axisLabel.dec} −</button></div><label>Jog step<select value={jogDegrees} onChange={(event) => setJogDegrees(Number(event.target.value))}><option value="0.05">0.05° centring</option><option value="0.25">0.25°</option><option value="1">1°</option><option value="5">5° finding</option><option value="15">15° sweep</option></select></label><span className="hint">One press turns {jogDegrees}°: {jogSteps('ra')} steps on {axisLabel.ra}, {jogSteps('dec')} on {axisLabel.dec}. Swing the scope by hand to the rough bearing first, then use these.</span><div className="actions"><button onClick={() => sendCommand({ command: 'enable' })}>Enable motors</button><button onClick={() => sendCommand({ command: 'disable' })}>Disable</button></div><form onSubmit={saveSerialPort}><label>ESP32 serial port<input value={serialPort} placeholder="auto or /dev/ttyACM0" onChange={(event) => setSerialPort(event.target.value)} /></label><span className="hint">{status?.esp32_connected ? `Open on ${status.serial_device || 'unknown device'}` : 'No serial device found'}</span><button className="primary" type="submit">Save and reconnect</button></form></div>
      <form className="panel target" onSubmit={sendTarget}><div className="panel-heading"><span>Go-to target</span><span>{status?.alignment?.state === 'complete' ? 'READY' : 'ALIGN FIRST'}</span></div><label>Right ascension<input type="number" min="0" max="23.999" step="0.001" value={target.right_ascension} onChange={(event) => setTarget({ ...target, right_ascension: event.target.value })} /></label><label>Declination<input type="number" min="-90" max="90" step="0.001" value={target.declination} onChange={(event) => setTarget({ ...target, declination: event.target.value })} /></label><button className="primary" type="submit" disabled={status?.alignment?.state !== 'complete'}>Slew to target ↗</button></form>    </section>}
    {view === 'setup' && <section className="grid solo">
      <form className="panel settings" onSubmit={saveSettings}><div className="panel-heading"><span>Mount + driver + site</span><span>{location.location_source}</span></div><span className="hint">Pins are GPIO numbers, not the board silkscreen. Equatorial turns the first axis in hour angle and tracks at one fixed rate; alt-az turns it in azimuth and has to drive both axes.</span><div className="fields"><label>Mount type<select value={mount.mount_type} onChange={(event) => setMount({ ...mount, mount_type: event.target.value })}><option value="equatorial">EQUATORIAL</option><option value="altaz">ALT-AZ</option></select></label><label>Driver type<select value={mount.driver_type} onChange={(event) => setMount({ ...mount, driver_type: event.target.value })}><option value="step_dir">STEP / DIR</option></select></label><label>Enable active<select value={mount.enable_active_low ? 'low' : 'high'} onChange={(event) => setMount({ ...mount, enable_active_low: event.target.value === 'low' })}><option value="low">LOW</option><option value="high">HIGH</option></select></label><label>RA axis direction<select value={mount.ra_reverse ? 'reversed' : 'normal'} onChange={(event) => setMount({ ...mount, ra_reverse: event.target.value === 'reversed' })}><option value="normal">NORMAL</option><option value="reversed">REVERSED</option></select></label><label>DEC axis direction<select value={mount.dec_reverse ? 'reversed' : 'normal'} onChange={(event) => setMount({ ...mount, dec_reverse: event.target.value === 'reversed' })}><option value="normal">NORMAL</option><option value="reversed">REVERSED</option></select></label><label>RA STEP pin<input type="number" value={mount.ra_step_pin} onChange={(event) => setMount({ ...mount, ra_step_pin: event.target.value })} /></label><label>RA DIR pin<input type="number" value={mount.ra_dir_pin} onChange={(event) => setMount({ ...mount, ra_dir_pin: event.target.value })} /></label><label>RA ENABLE pin<input type="number" value={mount.ra_enable_pin} onChange={(event) => setMount({ ...mount, ra_enable_pin: event.target.value })} /></label><label>DEC STEP pin<input type="number" value={mount.dec_step_pin} onChange={(event) => setMount({ ...mount, dec_step_pin: event.target.value })} /></label><label>DEC DIR pin<input type="number" value={mount.dec_dir_pin} onChange={(event) => setMount({ ...mount, dec_dir_pin: event.target.value })} /></label><label>DEC ENABLE pin<input type="number" value={mount.dec_enable_pin} onChange={(event) => setMount({ ...mount, dec_enable_pin: event.target.value })} /></label><label>RA steps/rev<input type="number" value={mount.ra_steps_per_revolution} onChange={(event) => setMount({ ...mount, ra_steps_per_revolution: event.target.value })} /></label><label>DEC steps/rev<input type="number" value={mount.dec_steps_per_revolution} onChange={(event) => setMount({ ...mount, dec_steps_per_revolution: event.target.value })} /></label><label>RA belt ratio<input type="number" step="0.001" value={mount.ra_belt_ratio} onChange={(event) => setMount({ ...mount, ra_belt_ratio: event.target.value })} /></label><label>DEC belt ratio<input type="number" step="0.001" value={mount.dec_belt_ratio} onChange={(event) => setMount({ ...mount, dec_belt_ratio: event.target.value })} /></label><label>Latitude<input type="number" step="0.0001" value={location.latitude} onChange={(event) => setLocation({ ...location, latitude: event.target.value })} /></label><label>Longitude<input type="number" step="0.0001" value={location.longitude} onChange={(event) => setLocation({ ...location, longitude: event.target.value })} /></label><label>Elevation (m)<input type="number" value={location.elevation_m} onChange={(event) => setLocation({ ...location, elevation_m: event.target.value })} /></label><label>Location source<select value={location.location_source} onChange={(event) => setLocation({ ...location, location_source: event.target.value })}><option value="manual">Manual</option><option value="gps">GPS</option></select></label></div><MapPicker location={location} onPick={pickLocation} /><div className="actions"><button type="button" onClick={useBrowserLocation}>Use device location</button><button className="primary" type="submit">Save configuration</button></div></form>
      <div className="panel"><div className="panel-heading"><span>Motor tuning</span><span>{testRunning ? 'RUNNING' : 'IDLE'}</span></div><span className="hint">A stepper vibrates instead of turning across its resonant band. Run an axis, drag the rate until the buzzing drops away, then keep max speed clear of that band. Changes reach the board as you drag; Save settings keeps them.</span><div className="fields"><label>Max speed {Math.round(mount.max_speed)} steps/s<input type="range" min="50" max="4000" step="25" value={mount.max_speed} onChange={(event) => changeTuning('max_speed', Number(event.target.value))} /></label><label>Acceleration {Math.round(mount.acceleration)} steps/s²<input type="range" min="100" max="12000" step="100" value={mount.acceleration} onChange={(event) => changeTuning('acceleration', Number(event.target.value))} /></label></div><div className="fields"><label>Test axis<select value={testAxis} onChange={(event) => setTestAxis(event.target.value)}><option value="ra">{axisLabel.ra}</option><option value="dec">{axisLabel.dec}</option></select></label><label>Test rate {testRate} steps/s{stepsPerDegree(testAxis) ? ` = ${(testRate / stepsPerDegree(testAxis)).toFixed(2)}\u00b0/s` : ''}<input type="range" min="1" max={Math.round(mount.max_speed)} step="1" value={testRate} onChange={(event) => changeTestRate(Number(event.target.value))} /></label></div><div className="actions"><button type="button" onClick={startTest}>Run</button><button type="button" onClick={stopTest}>Stop</button></div></div>
      <div className="panel"><div className="panel-heading"><span>Serial console</span><span>{serialLink.connected ? serialLink.device : 'NO DEVICE'}</span></div><span className="hint">Every byte to and from the board. If a command shows no reply, nothing is running the firmware.</span><div className="actions"><button type="button" onClick={() => sendSerial('status')}>status</button><button type="button" onClick={() => sendSerial('enable')}>enable</button><button type="button" onClick={() => sendSerial('disable')}>disable</button><button type="button" onClick={() => sendSerial('stop')}>stop</button><button type="button" onClick={() => setSerialLog([])}>clear</button></div><form onSubmit={(event) => { event.preventDefault(); const command = rawCommand.trim(); if (command) { sendSerial(command); setRawCommand(''); } }}><label>Raw command<input value={rawCommand} onChange={(event) => setRawCommand(event.target.value)} placeholder="move ra forward 200" /></label></form><pre className="console">{serialLog.length ? serialLog.map((entry) => `${entry.direction === 'tx' ? '>' : entry.direction === 'rx' ? '<' : entry.direction === 'link' ? '=' : '!'} ${entry.text}`).join('\n') : 'No traffic yet. Press status.'}</pre></div>
    </section>}
    {view === 'align' && <section className="grid">
      <div className="panel alignment"><div className="panel-heading"><span>Alignment</span><span>{status?.alignment?.state ?? 'not_started'} / {status?.alignment?.points?.length ?? 0} points</span></div><div className="actions"><button onClick={() => alignmentAction('start')}>Start</button><button onClick={() => alignmentAction('complete')} disabled={!status?.alignment?.points?.length}>Complete</button><button onClick={() => alignmentAction('reset')}>Reset</button><button onClick={toggleTracking} disabled={status?.alignment?.state !== 'complete'}>{status?.tracking ? 'Stop tracking' : 'Start tracking'}</button></div><span className="hint">Start clears any points already recorded. Only the last point is used as the mount's known position, so record the star you leave the scope on.</span><form onSubmit={addPoint}><span className="hint">Centre the star in the eyepiece first, then record it: that is how the mount learns where it is standing. The name is only a label, so pick a suggestion or look the coordinates up.</span><label>Known star / target name<input value={point.name} onChange={(event) => setPoint({ ...point, name: event.target.value })} /></label><div className="actions"><button type="button" onClick={lookupPoint}>Look up coordinates</button></div><div className="fields"><label>RA<input type="number" step="0.001" value={point.right_ascension} onChange={(event) => setPoint({ ...point, right_ascension: event.target.value })} /></label><label>DEC<input type="number" step="0.001" value={point.declination} onChange={(event) => setPoint({ ...point, declination: event.target.value })} /></label></div><button className="primary" type="submit" disabled={status?.alignment?.state !== 'collecting'}>{status?.alignment?.state === 'collecting' ? 'Record alignment point' : 'Press Start to record points'}</button></form></div>
      <div className="panel"><div className="panel-heading"><span>Suggested stars</span><span>{suggestions.length ? `${suggestions.length} UP NOW` : 'NONE UP'}</span></div><span className="hint">Bright stars above 15° for your saved site, best first. Face the listed bearing, find the star, centre it, then record it.</span><div className="actions"><button type="button" onClick={loadSuggestions}>Refresh</button></div>{suggestions.length ? <select size="10" onChange={(event) => { const star = suggestions.find((item) => item.name === event.target.value); if (star) setPoint({ name: star.name, right_ascension: String(star.right_ascension), declination: String(star.declination) }); }}>{suggestions.map((star) => <option key={star.name} value={star.name}>{`${star.name} | mag ${star.magnitude} | ${Math.round(star.altitude)}° up | ${star.compass}`}</option>)}</select> : <p>Nothing bright is above the horizon for the saved site right now. Check the site is set in Setup.</p>}</div>
    </section>}
    {view === 'objects' && <section className="grid solo">
      <div className="panel objects"><div className="panel-heading"><span>Sky objects</span><span>ONLINE CATALOGUES</span></div><form onSubmit={resolveObject}><label>Object name<input value={objectName} onChange={(event) => setObjectName(event.target.value)} placeholder="M31, Sirius, Moon" /></label><button className="primary" type="submit">Find coordinates</button></form>{objectResult && <p>{objectResult.name}: RA {objectResult.right_ascension}h / DEC {objectResult.declination}°</p>}<button className="primary" type="button" onClick={loadSatellites}>Load visible satellites</button>{satellites.length > 0 && <select size="4" onChange={(event) => { const selected = satellites.find((item) => item.name === event.target.value); if (selected) setObjectName(selected.name); }}>{satellites.map((satellite) => <option key={satellite.name} value={satellite.name}>{satellite.name} | Alt {satellite.altitude}° Az {satellite.azimuth}°</option>)}</select>}</div>
    </section>}
    <footer><span>{message || 'System ready'}</span><span>{status?.timestamp ? new Date(status.timestamp).toLocaleTimeString() : 'No telemetry'}</span></footer>
  </main>;
}
