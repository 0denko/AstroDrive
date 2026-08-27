import { useEffect, useState } from 'react';

const API = import.meta.env.VITE_API_URL || '';

export default function App() {
  const [status, setStatus] = useState(null);
  const [target, setTarget] = useState({ right_ascension: '5.5', declination: '22.0' });
  const [message, setMessage] = useState('');

  async function refreshStatus() {
    try { setStatus(await fetch(`${API}/api/status`).then((response) => response.json())); }
    catch { setStatus({ connected: false, mount: 'offline' }); }
  }

  async function sendCommand(payload) {
    const response = await fetch(`${API}/api/command`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    setMessage(response.ok ? 'Command accepted' : 'Command unavailable');
    refreshStatus();
  }

  async function sendTarget(event) {
    event.preventDefault();
    const response = await fetch(`${API}/api/target`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ right_ascension: Number(target.right_ascension), declination: Number(target.declination) }) });
    setMessage(response.ok ? 'Target queued' : 'Target unavailable');
  }

  useEffect(() => { refreshStatus(); const timer = setInterval(refreshStatus, 5000); return () => clearInterval(timer); }, []);

  return <main>
    <header><div><span className="eyebrow">MOUNT CONTROL / LIVE</span><h1>AstroDrive</h1></div><span className={`connection ${status?.connected ? 'online' : ''}`}>{status?.connected ? 'MQTT ONLINE' : 'MQTT OFFLINE'}</span></header>
    <section className="grid">
      <div className="panel camera"><div className="panel-heading"><span>Sky camera</span><span className="live-dot">● LIVE</span></div>{status?.camera_url ? <img src={status.camera_url} alt="Live sky camera" /> : <div className="empty">Waiting for camera stream</div>}</div>
      <div className="panel controls"><div className="panel-heading"><span>Manual control</span><span className="mount-state">{status?.mount ?? 'checking'}</span></div><div className="dpad"><button onClick={() => sendCommand({ command: 'move', axis: 'dec', direction: 'forward', steps: 10 })}>DEC +</button><button onClick={() => sendCommand({ command: 'move', axis: 'ra', direction: 'backward', steps: 10 })}>RA −</button><button onClick={() => sendCommand({ command: 'stop' })}>STOP</button><button onClick={() => sendCommand({ command: 'move', axis: 'ra', direction: 'forward', steps: 10 })}>RA +</button><button onClick={() => sendCommand({ command: 'move', axis: 'dec', direction: 'backward', steps: 10 })}>DEC −</button></div><div className="actions"><button onClick={() => sendCommand({ command: 'enable' })}>Enable motors</button><button onClick={() => sendCommand({ command: 'disable' })}>Disable</button></div></div>
      <form className="panel target" onSubmit={sendTarget}><div className="panel-heading"><span>Go-to target</span><span>J2000 / decimal hours</span></div><label>Right ascension<input type="number" min="0" max="23.999" step="0.001" value={target.right_ascension} onChange={(event) => setTarget({ ...target, right_ascension: event.target.value })} /></label><label>Declination<input type="number" min="-90" max="90" step="0.001" value={target.declination} onChange={(event) => setTarget({ ...target, declination: event.target.value })} /></label><button className="primary" type="submit">Slew to target ↗</button></form>
    </section>
    <footer><span>{message || 'System ready'}</span><span>{status?.timestamp ? new Date(status.timestamp).toLocaleTimeString() : 'No telemetry'}</span></footer>
  </main>;
}
