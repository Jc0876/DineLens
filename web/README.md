# web/ — Frontend on Cloudflare Pages + backend behind a Function proxy

This folder is deployed to **Cloudflare Pages**. The browser only ever talks to the
Pages domain; requests to `/api/*` are proxied **inside Cloudflare** to the tunnel
backend (the hostname exposed by your tunnel), so:

- the browser sees a single origin — no CORS configuration needed;
- the tunnel hostname is never exposed to the visitor's network (this also bypasses
  the SNI-based interference some mobile carriers apply to the tunnel hostname);
- if the home PC / tunnel is offline, the function returns a friendly
  `503 {"error":"backend_offline"}` instead of a raw error page.

## Layout

```
DineLens/
├── functions/                  # MUST be at the repository root (not inside web/)
│   └── api/
│       └── [[path]].js         # /api/*  ->  $BACKEND_URL/*
└── web/                        # Pages build output directory
    └── index.html              # placeholder frontend (replace with the real app)
```

> Cloudflare requirement: the `/functions` directory lives at the **root of the
> Pages project**, not inside the build output directory. The Pages build setting
> keeps `Build output directory: web`.

## Deploy

1. Cloudflare Dashboard → **Workers & Pages** → **Create** → **Pages** → **Connect to Git** → select this repository.
2. Build settings:
   - Framework preset: **None**
   - Build command: *(leave empty)*
   - Build output directory: **`web`**
3. Deploy. You get a `<project>.pages.dev` address.
4. **Custom domains** → add a subdomain that works on your networks,
   e.g. `app.example.com` (Cloudflare creates the DNS record automatically).
5. Open `https://app.example.com` and press the test button — it calls `/api/`,
   which the function proxies to the tunnel. It only succeeds while the home PC
   and the tunnel are running.

## Notes

- The backend address is **not hardcoded**: the function reads the `BACKEND_URL`
  environment variable (Pages → Settings → Environment variables), e.g.
  `BACKEND_URL = https://your-tunnel-host.example.com`. Set it for Production *and*
  Preview if you use preview deployments. Changing an environment variable requires
  a new deployment to take effect.
- If `BACKEND_URL` is missing, `/api/*` returns
  `500 {"error":"backend_not_configured"}` instead of failing silently.
- **Optional (recommended): protect the backend hostname with Cloudflare Access.**
  Create a Zero Trust **service token** and an Access application on the backend
  hostname with a *Service Auth* policy (deny everyone else). Store the token in two
  more environment variables, `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`;
  the function attaches them to the proxied request so only it can pass. Direct
  browser visits to the backend hostname are then blocked by Cloudflare.
- The real application will call endpoints like `/api/analyze` (FastAPI backend);
  the frontend uses relative paths (`/api/...`) and does not need to know about
  the tunnel at all.
