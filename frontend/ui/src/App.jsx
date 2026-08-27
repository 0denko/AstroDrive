import { useEffect, useRef, useState } from 'react';
import MapPicker from './MapPicker.jsx';

const API = import.meta.env.VITE_API_URL || '';
const UPDATE_POLL_MS = 2000;
// systemctl start --no-block returns before the unit reaches its activating state.
const UPDATE_START_GRACE_MS = 25000;
// the updater restarts the API and reloads nginx, so status polls drop out mid-run.
const UPDATE_MAX_OFFLINE_POLLS = 90;
const STACK_POLL_MS = 1500;
const initialMount = { ra_steps_per_revolution: 3200, dec_steps_per_revolution: 3200, ra_belt_ratio: 1, dec_belt_ratio: 1, driver_type: 'step_dir', ra_step_pin: 25, ra_dir_pin: 26, ra_enable_pin: 27, dec_step_pin: 14, dec_dir_pin: 12, dec_enable_pin: 13, enable_active_low: true };
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

export default function App() {
  const [status, setStatus] = useState(null);
  const [target, setTarget] = useState({ right_ascension: '5.5', declination: '22.0' });
  const [serialPort, setSerialPort] = useState('auto');
  const [mount, setMount] = useState(initialMount);
  const [location, setLocation] = useState(initialLocation);
  const [point, setPoint] = useState({ name: 'Alignment star', right_ascension: '5.5', declination: '22.0' });
  const [message, setMessage] = useState('');
  const [updating, setUpdating] = useState(false);
  const [updateStatus, setUpdateStatus] = useState({ state: 'idle', detail: '' });
  const updateOfflineRef = useRef(0);
  const [objectName, setObjectName] = useState('M31');
  const [objectResult, setObjectResult] = useState(null);
  const [satellites, setSatellites] = useState([]);
  const [cameraControls, setCameraControls] = useState({});
  const [cameraMeta, setCameraMeta] = useState({ available: false, controls: {}, error: '' });
  const [stack, setStack] = useState({ frames: 8, interval_ms: 250, stretch: 4, gamma: 2.2 });
  const [stackImage, setStackImage] = useState('');
  const [stackBusy, setStackBusy] = useState(false);

  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, { headers: { 'Content-Type': 'application/json' }, ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || 'Request unavailable');
    return body;
  }

  async function refreshStatus() {
    try { setStatus(await request('/api/status')); } catch { setStatus({ connected: false, esp32_connected: false, mount: 'unknown' }); }
  }

  async function run(action, success) {
    try { await action(); setMessage(success); refreshStatus(); } catch (error) { setMessage(error.message); }
  }

  function sendCommand(payload) { return run(() => request('/api/command', { method: 'POST', body: JSON.stringify(payload) }), 'Command accepted'); }
  function sendTarget(event) { event.preventDefault(); return run(() => request('/api/target', { method: 'POST', body: JSON.stringify({ right_ascension: Number(target.right_ascension), declination: Number(target.declination) }) }), 'Target queued'); }
  function saveSerialPort(event) { event.preventDefault(); return run(async () => { const result = await request('/api/settings/serial', { method: 'PUT', body: JSON.stringify({ port: serialPort }) }); setMessage(result.connected ? 'ESP32 connected' : 'Port saved; ESP32 not found'); }, 'Port saved'); }
  function saveSettings(event) { event.preventDefault(); return run(async () => { await request('/api/settings/mount', { method: 'PUT', body: JSON.stringify({ ...mount, ra_steps_per_revolution: Number(mount.ra_steps_per_revolution), dec_steps_per_revolution: Number(mount.dec_steps_per_revolution), ra_belt_ratio: Number(mount.ra_belt_ratio), dec_belt_ratio: Number(mount.dec_belt_ratio) }) }); await request('/api/settings/location', { method: 'PUT', body: JSON.stringify({ ...location, latitude: Number(location.latitude), longitude: Number(location.longitude), elevation_m: Number(location.elevation_m) }) }); }, 'Settings saved'); }
  function alignmentAction(action) { return run(() => request('/api/alignment', { method: 'POST', body: JSON.stringify({ action }) }), action === 'complete' ? 'Alignment complete' : `Alignment ${action}`); }
  function addPoint(event) { event.preventDefault(); return run(() => request('/api/alignment/point', { method: 'POST', body: JSON.stringify({ ...point, right_ascension: Number(point.right_ascension), declination: Number(point.declination) }) }), 'Alignment point recorded'); }
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
  async function saveCameraControls(event) {
    event.preventDefault();
    const payload = {};
    for (const field of CAMERA_FIELDS) {
      const value = cameraControls[field.key];
      if (!cameraMeta.controls[field.key] || value === undefined || value === '') continue;
      payload[field.key] = field.toggle ? Boolean(value) : Number(value);
    }
    try {
      const result = await request('/api/camera/controls', { method: 'PUT', body: JSON.stringify(payload) });
      setCameraMeta({ available: true, controls: result.controls || cameraMeta.controls, error: '' });
      setCameraControls(result.updated_controls || {});
      setMessage(result.failures?.length ? `Camera partly applied: ${result.failures.join('; ')}` : 'Camera settings applied');
    } catch (error) { setMessage(error.message); }
  }
  async function refreshStack() { try { const result = await request('/api/camera/stack'); if (result.state === 'complete') setStackImage(result.image_url); return result; } catch { return null; } }
  async function captureStack(event) {
    event.preventDefault();
    setStackBusy(true);
    setMessage('Stack capture started');
    try {
      await request('/api/camera/stack', { method: 'POST', body: JSON.stringify({ frames: Number(stack.frames), interval_ms: Number(stack.interval_ms), stretch: Number(stack.stretch), gamma: Number(stack.gamma) }) });
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, STACK_POLL_MS));
        const result = await refreshStack();
        if (!result || result.state === 'running') continue;
        setMessage(result.state === 'failed' ? `Stack failed: ${result.detail}` : result.detail || 'Stacked frame ready');
        break;
      }
    } catch (error) { setMessage(error.message); } finally { setStackBusy(false); }
  }

  useEffect(() => { refreshStatus(); refreshUpdateStatus(); refreshCameraControls(); refreshStack(); const timer = setInterval(refreshStatus, 5000); return () => clearInterval(timer); }, []);
  // keeps the button in sync with updates started by the boot unit, the timer, or another browser tab
  useEffect(() => { if (updating) return undefined; const timer = setInterval(refreshUpdateStatus, 5000); return () => clearInterval(timer); }, [updating]);
  useEffect(() => { if (status?.serial_port) setSerialPort(status.serial_port); if (status?.mount_config) setMount(status.mount_config); if (status?.location) setLocation(status.location); }, [status]);

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

  return <main>
    <header><div><span className="eyebrow">MOUNT CONTROL / LIVE</span><h1>AstroDrive</h1></div><div className="status-stack"><div className="status-badges"><span className={`connection ${status?.esp32_connected ? 'online' : ''}`}>{status?.esp32_connected ? 'ESP32 ONLINE' : 'ESP32 OFFLINE'}</span><span className={`connection ${status?.mount === 'aligned' || status?.mount === 'tracking' ? 'online' : ''}`}>MOUNT {status?.mount?.replaceAll('_', ' ').toUpperCase() ?? 'CHECKING'}</span></div><div className="update-row" title={updateLabel}><button className={`update-ring ${ringState}`} style={{ '--progress': ringPercent }} onClick={triggerUpdate} disabled={updateBusy} aria-label={updateLabel} role="progressbar" aria-valuenow={ringPercent}><span>{ringGlyph}</span></button>{updateStatus.state === 'failed' && <span className="update-detail">{updateStatus.detail}</span>}</div></div></header>
    <section className="grid">
      <div className="panel camera"><div className="panel-heading"><span>Sky camera</span><span className="live-dot">● LIVE</span></div>{status?.camera_url ? <img src={status.camera_url} alt="Live sky camera" /> : <div className="empty">Waiting for camera stream</div>}</div>
      <form className="panel camera-settings" onSubmit={saveCameraControls}><div className="panel-heading"><span>Camera controls</span><span>{cameraMeta.available ? 'V4L2' : 'UNAVAILABLE'}</span></div>{cameraMeta.available ? <><div className="fields">{CAMERA_FIELDS.filter((field) => cameraMeta.controls[field.key]).map((field) => { const meta = cameraMeta.controls[field.key]; return <label key={field.key}>{field.label}{field.toggle ? <select value={cameraControls[field.key] ? 'on' : 'off'} onChange={(event) => setCameraControls({ ...cameraControls, [field.key]: event.target.value === 'on' })}><option value="on">ON</option><option value="off">OFF</option></select> : <input type="number" min={meta.min ?? undefined} max={meta.max ?? undefined} step={meta.step || 1} title={meta.inactive ? 'Driver ignores this until the matching auto control is off' : `${meta.min}..${meta.max}`} value={cameraControls[field.key] ?? ''} onChange={(event) => setCameraControls({ ...cameraControls, [field.key]: event.target.value })} />}</label>; })}</div><button className="primary" type="submit">Apply camera settings</button></> : <p>{cameraMeta.error || 'This camera exposes no V4L2 controls'}</p>}</form>
      <form className="panel low-light" onSubmit={captureStack}><div className="panel-heading"><span>Low-light stack</span><span>AVERAGE FRAMES</span></div><div className="fields"><label>Frames<input type="number" min="2" max="60" value={stack.frames} onChange={(event) => setStack({ ...stack, frames: event.target.value })} /></label><label>Interval ms<input type="number" min="0" value={stack.interval_ms} onChange={(event) => setStack({ ...stack, interval_ms: event.target.value })} /></label><label>Brightness gain<input type="number" min="1" max="64" step="0.5" value={stack.stretch} onChange={(event) => setStack({ ...stack, stretch: event.target.value })} /></label><label>Gamma<input type="number" min="1" max="5" step="0.1" value={stack.gamma} onChange={(event) => setStack({ ...stack, gamma: event.target.value })} /></label></div><button className="primary" type="submit" disabled={stackBusy}>{stackBusy ? 'Stacking frames...' : 'Capture stacked preview'}</button>{stackImage && <img className="stack-preview" src={stackImage} alt="Stacked low-light preview" />}</form>
      <div className="panel controls"><div className="panel-heading"><span>Manual control</span><span>{status?.serial_port ?? 'auto'}</span></div><div className="dpad"><button onClick={() => sendCommand({ command: 'move', axis: 'dec', direction: 'forward', steps: 10 })}>DEC +</button><button onClick={() => sendCommand({ command: 'move', axis: 'ra', direction: 'backward', steps: 10 })}>RA −</button><button onClick={() => sendCommand({ command: 'stop' })}>STOP</button><button onClick={() => sendCommand({ command: 'move', axis: 'ra', direction: 'forward', steps: 10 })}>RA +</button><button onClick={() => sendCommand({ command: 'move', axis: 'dec', direction: 'backward', steps: 10 })}>DEC −</button></div><div className="actions"><button onClick={() => sendCommand({ command: 'enable' })}>Enable motors</button><button onClick={() => sendCommand({ command: 'disable' })}>Disable</button></div><form onSubmit={saveSerialPort}><label>ESP32 serial port<input value={serialPort} placeholder="auto or /dev/ttyACM0" onChange={(event) => setSerialPort(event.target.value)} /></label><button className="primary" type="submit">Save and reconnect</button></form></div>
      <form className="panel target" onSubmit={sendTarget}><div className="panel-heading"><span>Go-to target</span><span>{status?.alignment?.state === 'complete' ? 'READY' : 'ALIGN FIRST'}</span></div><label>Right ascension<input type="number" min="0" max="23.999" step="0.001" value={target.right_ascension} onChange={(event) => setTarget({ ...target, right_ascension: event.target.value })} /></label><label>Declination<input type="number" min="-90" max="90" step="0.001" value={target.declination} onChange={(event) => setTarget({ ...target, declination: event.target.value })} /></label><button className="primary" type="submit" disabled={status?.alignment?.state !== 'complete'}>Slew to target ↗</button></form>
      <form className="panel settings" onSubmit={saveSettings}><div className="panel-heading"><span>Mount + driver + site</span><span>{location.location_source}</span></div><div className="fields"><label>Driver type<select value={mount.driver_type} onChange={(event) => setMount({ ...mount, driver_type: event.target.value })}><option value="step_dir">STEP / DIR</option></select></label><label>Enable active<select value={mount.enable_active_low ? 'low' : 'high'} onChange={(event) => setMount({ ...mount, enable_active_low: event.target.value === 'low' })}><option value="low">LOW</option><option value="high">HIGH</option></select></label><label>RA STEP pin<input type="number" value={mount.ra_step_pin} onChange={(event) => setMount({ ...mount, ra_step_pin: event.target.value })} /></label><label>RA DIR pin<input type="number" value={mount.ra_dir_pin} onChange={(event) => setMount({ ...mount, ra_dir_pin: event.target.value })} /></label><label>RA ENABLE pin<input type="number" value={mount.ra_enable_pin} onChange={(event) => setMount({ ...mount, ra_enable_pin: event.target.value })} /></label><label>DEC STEP pin<input type="number" value={mount.dec_step_pin} onChange={(event) => setMount({ ...mount, dec_step_pin: event.target.value })} /></label><label>DEC DIR pin<input type="number" value={mount.dec_dir_pin} onChange={(event) => setMount({ ...mount, dec_dir_pin: event.target.value })} /></label><label>DEC ENABLE pin<input type="number" value={mount.dec_enable_pin} onChange={(event) => setMount({ ...mount, dec_enable_pin: event.target.value })} /></label><label>RA steps/rev<input type="number" value={mount.ra_steps_per_revolution} onChange={(event) => setMount({ ...mount, ra_steps_per_revolution: event.target.value })} /></label><label>DEC steps/rev<input type="number" value={mount.dec_steps_per_revolution} onChange={(event) => setMount({ ...mount, dec_steps_per_revolution: event.target.value })} /></label><label>RA belt ratio<input type="number" step="0.001" value={mount.ra_belt_ratio} onChange={(event) => setMount({ ...mount, ra_belt_ratio: event.target.value })} /></label><label>DEC belt ratio<input type="number" step="0.001" value={mount.dec_belt_ratio} onChange={(event) => setMount({ ...mount, dec_belt_ratio: event.target.value })} /></label><label>Latitude<input type="number" step="0.0001" value={location.latitude} onChange={(event) => setLocation({ ...location, latitude: event.target.value })} /></label><label>Longitude<input type="number" step="0.0001" value={location.longitude} onChange={(event) => setLocation({ ...location, longitude: event.target.value })} /></label><label>Elevation (m)<input type="number" value={location.elevation_m} onChange={(event) => setLocation({ ...location, elevation_m: event.target.value })} /></label><label>Location source<select value={location.location_source} onChange={(event) => setLocation({ ...location, location_source: event.target.value })}><option value="manual">Manual</option><option value="gps">GPS</option></select></label></div><MapPicker location={location} onPick={pickLocation} /><div className="actions"><button type="button" onClick={useBrowserLocation}>Use device location</button><button className="primary" type="submit">Save configuration</button></div></form>
      <div className="panel alignment"><div className="panel-heading"><span>Alignment</span><span>{status?.alignment?.state ?? 'not_started'} / {status?.alignment?.points?.length ?? 0} points</span></div><div className="actions"><button onClick={() => alignmentAction('start')}>Start</button><button onClick={() => alignmentAction('complete')} disabled={!status?.alignment?.points?.length}>Complete</button><button onClick={() => alignmentAction('reset')}>Reset</button><button onClick={toggleTracking} disabled={status?.alignment?.state !== 'complete'}>{status?.tracking ? 'Stop tracking' : 'Start tracking'}</button></div><form onSubmit={addPoint}><label>Known star / target name<input value={point.name} onChange={(event) => setPoint({ ...point, name: event.target.value })} /></label><div className="fields"><label>RA<input type="number" step="0.001" value={point.right_ascension} onChange={(event) => setPoint({ ...point, right_ascension: event.target.value })} /></label><label>DEC<input type="number" step="0.001" value={point.declination} onChange={(event) => setPoint({ ...point, declination: event.target.value })} /></label></div><button className="primary" type="submit" disabled={status?.alignment?.state !== 'collecting'}>Record alignment point</button></form></div>
      <div className="panel objects"><div className="panel-heading"><span>Sky objects</span><span>ONLINE CATALOGUES</span></div><form onSubmit={resolveObject}><label>Object name<input value={objectName} onChange={(event) => setObjectName(event.target.value)} placeholder="M31, Sirius, Moon" /></label><button className="primary" type="submit">Find coordinates</button></form>{objectResult && <p>{objectResult.name}: RA {objectResult.right_ascension}h / DEC {objectResult.declination}°</p>}<button className="primary" type="button" onClick={loadSatellites}>Load visible satellites</button>{satellites.length > 0 && <select size="4" onChange={(event) => { const selected = satellites.find((item) => item.name === event.target.value); if (selected) setObjectName(selected.name); }}>{satellites.map((satellite) => <option key={satellite.name} value={satellite.name}>{satellite.name} | Alt {satellite.altitude}° Az {satellite.azimuth}°</option>)}</select>}</div>
    </section>
    <footer><span>{message || 'System ready'}</span><span>{status?.timestamp ? new Date(status.timestamp).toLocaleTimeString() : 'No telemetry'}</span></footer>
  </main>;
}
