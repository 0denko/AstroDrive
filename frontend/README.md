# Web UI

## Setup

```bash
cd frontend/ui
npm install
npm run dev
```

Set `VITE_API_URL` when the API is not on `http://localhost:8000`, for example `VITE_API_URL=http://raspberrypi.local:8000 npm run build`. The camera URL is supplied by the backend status endpoint.

## Installing it on a phone

The UI ships a web app manifest (`public/manifest.json`) and the icons it needs, so the phone can
run it full screen with no browser chrome. `display` is `standalone`, and `viewport-fit=cover`
plus `env(safe-area-inset-*)` padding keeps content clear of the notch and the home indicator.

**iOS** works over plain HTTP today. Open the site in Safari, Share, *Add to Home Screen*. The
`apple-mobile-web-app-capable` meta tag makes the launcher open without the address bar.

**Android** does not. Chromium only treats a site as installable when it is served over HTTPS,
`localhost`, or a loopback address, so over `http://astrodrive.local` you get a plain bookmark that
opens in a normal tab. `deploy/astrodrive.nginx` listens on port 80 only. To get a real installable
app on Android, put a trusted certificate in front of it:

- Tailscale on the Pi, then `tailscale cert <machine>.<tailnet>.ts.net` and add a `listen 443 ssl`
  block. This is the least work and also gets you access from outside the LAN.
- A Cloudflare Tunnel, if the mount should be reachable over the internet.
- A self-signed certificate does **not** help. Chrome does not count an untrusted certificate as a
  secure context, so the install prompt still stays away, unless you install your own CA on the
  phone.

No service worker is used. One is not required for installability and an offline cache would only
get in the way of a UI whose whole job is live control.

