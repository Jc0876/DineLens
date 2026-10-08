# web/ — Frontend on Cloudflare Pages + backend behind a Function proxy

This folder is deployed to **Cloudflare Pages**. The browser only ever talks to the
Pages domain; requests to `/api/*` are proxied **inside Cloudflare** to the tunnel
backend (`dinelens.ccwu.cc`), so:

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
│       └── [[path]].js         # /api/*  ->  https://dinelens.ccwu.cc/*
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
   e.g. `www.ccwu.cc` (Cloudflare creates the DNS record automatically).
5. Open `https://www.ccwu.cc` and press the test button — it calls `/api/`,
   which the function proxies to the tunnel. It only succeeds while the home PC
   and the tunnel are running.

## Notes

- Change `BACKEND` in `functions/api/[[path]].js` if the tunnel hostname changes.
- The real application will call endpoints like `/api/analyze` (FastAPI backend);
  the frontend uses relative paths (`/api/...`) and does not need to know about
  the tunnel at all.
