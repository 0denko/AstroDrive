import { useEffect } from 'react';
import { MapContainer, Marker, TileLayer, useMapEvents } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';

function ClickTarget({ onPick }) {
  useMapEvents({ click: (event) => onPick({ latitude: event.latlng.lat.toFixed(6), longitude: event.latlng.lng.toFixed(6) }) });
  return null;
}

export default function MapPicker({ location, onPick }) {
  const center = [Number(location.latitude) || 0, Number(location.longitude) || 0];
  return <MapContainer center={center} zoom={location.latitude || location.longitude ? 8 : 2} style={{ height: 260, width: '100%' }}><TileLayer attribution="&copy; OpenStreetMap contributors" url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" /><ClickTarget onPick={onPick} /><Marker position={center} /></MapContainer>;
}