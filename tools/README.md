# DineLens Tunnel Manager

A small Windows toolkit for managing **Cloudflare Tunnel**: expose a locally running service (for example the DineLens web app) to the public internet over HTTPS — **no public IP, no router port forwarding, no ICP filing required**.

Two entry points:

| File | Purpose |
|---|---|
| `tunnel_gui.py` | Graphical interface (recommended for daily use), pure Tkinter, zero dependencies |
| `tunnel.py` | Command-line version with the same feature set, plus a guard mode |
| `启动 Tunnel 管理器.bat` | Local launcher for the GUI (personal file, not committed) |
| `tunnels.example.json` | Tunnel profile template (copy to `tunnels.json` and edit) |
| `services.example.json` | Local service list template (copy to `services.json` and edit) |

## 1. How It Works

### What a tunnel is

`cloudflared` on your machine dials **outbound** to the Cloudflare edge and keeps an encrypted, long-lived connection. Public requests travel from the Cloudflare edge through that connection to your local service.

```
phone / browser  →  https://app.example.com
                          │
                          ▼
               Cloudflare edge  ──(tunnel connection / QUIC)──►  local cloudflared.exe
                                                                       │
                                                                       ▼
                                                     http://localhost:8000 (your service)
```

Because the connection is initiated from inside your network, it works at home without a public IP or any port forwarding.

### Profile mechanism

Each tunnel's information (tunnel name, domain, credentials file, local port, edge IP version) is stored in `tunnels.json`. One profile corresponds to one tunnel:

```json
{
  "active": "myprofile",
  "profiles": {
    "myprofile": {
      "tunnel": "my-tunnel",
      "credentials_file": "C:\\Users\\<you>\\.cloudflared\\<tunnel-id>.json",
      "hostname": "app.example.com",
      "port": 8000,
      "edge_ip_version": 4
    }
  }
}
```

### What happens at start

1. A config file is generated from the active profile at `configs/<profile>.yml`:

   ```yaml
   tunnel: my-tunnel
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: app.example.com
       service: http://localhost:8000
     - service: http_status:404
   ```

2. cloudflared is started in the background. Note that `--config` and `--edge-ip-version` are **global** flags and must come before the `tunnel` subcommand:

   ```
   cloudflared --config configs\<profile>.yml --edge-ip-version 4 tunnel run my-tunnel
   ```

3. Logs are written to `tunnel.log`. When `Registered tunnel connection` appears, the tunnel is up.

Real account credentials (`cert.pem`, `<tunnel-id>.json`) are created by `cloudflared login` and `cloudflared tunnel create`, and live in `~/.cloudflared/` — not in this folder.

## 2. Requirements & Installation

1. Windows 10/11, Python 3.10+ (tkinter is included with Python; no third-party packages are needed).
2. Install cloudflared:

   ```bat
   winget install --id Cloudflare.cloudflared
   ```

3. Your local service should be listening on the port configured in the profile (for example 8000).

## 3. First-Time Setup

1. Double-click `启动 Tunnel 管理器.bat` (or create a desktop shortcut to it) to open the GUI.
2. Click **登录账号 (Login)**: a browser window opens the Cloudflare authorization page. Sign in and select the domain you want to use (this writes `cert.pem`).
3. Click **新建隧道 (Create tunnel)**: enter a profile name (English), a domain (must belong to that Cloudflare account), and the local port. The tool creates the tunnel and routes the DNS record automatically.
4. Click **启动隧道 (Start)**: wait until the status light turns green and tunnel connections appear.
5. Open `https://your-domain` to verify. A 502 means the local service is not listening on the configured port.

> If a `~/.cloudflared/config.yml` already exists on the machine, the first run automatically migrates it into a profile in `tunnels.json`.

## 4. Daily Use (GUI)

| Area | What it does |
|---|---|
| Status panel (refreshes every 4 s) | cloudflared presence, current profile, edge IP version, process PID, local port listening state, tunnel connection count, public self-check (HTTP / latency / edge location / direct IP), system proxy notice |
| Action buttons | Start / Stop / Restart / end-to-end self-check / open public URL |
| Guard mode | when checked, checks every 4 s and automatically restarts the tunnel if it drops |
| Tunnel profiles (accounts) | switch profile, log in to an account, create a tunnel, route DNS |
| Local service | pick a preset service or type a custom port and click switch to point the tunnel at it; the preset start command is shown below; **Start local service / Stop local service** buttons run that command or stop the process listening on the port |
| Tunnel settings | edge IP version (auto / IPv4 / IPv6); takes effect after a restart |
| Log area | live view of `tunnel.log`, auto-refresh toggle |

## 5. Command Line Usage

```bat
python tunnel.py                 :: interactive menu
python tunnel.py start           :: start the tunnel
python tunnel.py stop            :: stop the tunnel
python tunnel.py restart         :: restart the tunnel
python tunnel.py status          :: status dashboard
python tunnel.py test            :: end-to-end self-check (local + public)
python tunnel.py switch 8000     :: switch the local service port
python tunnel.py logs 80         :: show the last 80 log lines
python tunnel.py watch           :: guard mode (auto reconnect)
python tunnel.py open            :: open the public URL in a browser
python tunnel.py start-local     :: start the local service using the preset start_cmd
python tunnel.py stop-local      :: stop the process listening on the active profile's port

python tunnel.py profiles        :: list tunnel profiles
python tunnel.py use <name>      :: switch the active profile
python tunnel.py login           :: log in / switch Cloudflare account (browser)
python tunnel.py create <name> <domain> [port]   :: create a tunnel and route DNS
python tunnel.py route-dns <name> <domain>       :: add a DNS route to a tunnel
```

## 6. Configuration Files

| File | Committed? | Description |
|---|---|---|
| `tunnels.json` | no (git-ignored) | personal tunnel profiles; created and maintained by the tool |
| `services.json` | no (git-ignored) | local service presets; an entry may carry a `start_cmd` that the tool offers to run when the port is not listening |
| `configs/` | no (git-ignored) | cloudflared configs generated from profiles; delete-safe, regenerated automatically |
| `tunnel_state.json` | no (git-ignored) | runtime state (last process PID); created automatically |
| `tunnel.log` | no (git-ignored) | tunnel runtime log |

On a fresh clone: copy `tunnels.example.json` / `services.example.json` to the real filenames and edit them, or simply follow the login + create flow in section 3.

## 7. FAQ

**1. Public URL returns 502**
The local service is not listening on the profile's port. Check the "local service" row on the status panel — it should say listening.

**2. Self-check fails, or the site works intermittently**
Direct connections to Cloudflare's free IPs from some networks are intermittently interfered with (typical symptom: TCP connects but the TLS handshake is reset). This is a network condition, not a tunnel failure. Retry later, enable guard mode, or test from another network. The self-check deliberately bypasses any system proxy so that it reflects the real direct path.

**3. The tunnel may attach to a far-away overseas edge (slow / unstable)**
Cloudflare's free plan has no mainland-China edge (mainland POPs are Enterprise-only), and the edge your tunnel attaches to is selected automatically by network routing — it cannot be chosen manually. Depending on your ISP, the tunnel may end up anchored to an overseas region far away from your users, which can lead to:
- high latency — several seconds before the first byte on some links;
- intermittent failures, especially during peak hours, when the cross-border path is congested or interfered with;
- slow transfers, limited by the home connection's upload bandwidth.
This is a structural limitation of the free tier, not a misconfiguration. Guard mode reconnects after short outages but cannot reduce latency. If latency and stability really matter, a small VPS in a nearby region with a direct reverse-proxy setup (frp) is a better fit.

**4. Port switch has no effect**
After changing the profile, the tunnel must be **restarted**; the GUI asks automatically.

**5. Switching to another Cloudflare account**
Click Login to re-authorize (overwrites `cert.pem`), then create a tunnel (a tunnel ID cannot be moved across accounts). Remember to delete the old tunnel in the old account.

**6. Why not install as a Windows service?**
This toolkit intentionally never calls `cloudflared service install`; start/stop stays manual. For auto-start on boot, put a shortcut to the launcher in the Startup folder, or create a scheduled task.

**7. "edge-ip-version" not supported?**
The flag is a *global* cloudflared flag and must be placed before the `tunnel` subcommand; the tool does this correctly. If a particular cloudflared build has removed the flag, the tool falls back to automatic edge selection.
