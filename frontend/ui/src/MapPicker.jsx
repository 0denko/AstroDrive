import { useEffect, useRef } from 'react';
import L from 'leaflet';
import { MapContainer, Marker, TileLayer, useMap, useMapEvents } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import markerIcon from 'leaflet/dist/images/marker-icon.png';
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png';
import markerShadow from 'leaflet/dist/images/marker-shadow.png';

// Leaflet's CSS references marker images by relative path, which the bundler does not rewrite.
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({ iconUrl: markerIcon, iconRetinaUrl: markerIcon2x, shadowUrl: markerShadow });

function ClickTarget({ onPick }) {
  useMapEvents({ click: (event) => onPick({ latitude: event.latlng.lat.toFixed(6), longitude: event.latlng.lng.toFixed(6) }) });
  return null;
}

// MapContainer only reads `center` once, so a location loaded from the device after first paint
// would otherwise leave the map sitting at 0,0.
function Recenter({ latitude, longitude }) {
  const map = useMap();
  const framed = useRef(false);
  useEffect(() => {
    const lat = Number(latitude);
    const lng = Number(longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    if (lat === 0 && lng === 0) return;
    // frame the stored site once, then only chase points that leave the view, so clicking the
    // map never pans it out from under the cursor
    if (framed.current && map.getBounds().contains([lat, lng])) return;
    framed.current = true;
    map.setView([lat, lng], Math.max(map.getZoom(), 8));
  }, [map, latitude, longitude]);
  return null;
}

export default function MapPicker({ location, onPick }) {
  const center = [Number(location.latitude) || 0, Number(location.longitude) || 0];
  return <MapContainer center={center} zoom={location.latitude || location.longitude ? 8 : 2} style={{ height: 260, width: '100%' }}><TileLayer attribution="&copy; OpenStreetMap contributors" url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" /><ClickTarget onPick={onPick} /><Recenter latitude={location.latitude} longitude={location.longitude} /><Marker position={center} /></MapContainer>;
}
