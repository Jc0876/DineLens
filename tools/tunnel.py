"""
DineLens Tunnel Manager（多档案版）
管理 Cloudflare Tunnel 的启停、本地服务切换、隧道档案（账号）与自检。

用法:
  python tunnel.py                交互菜单
  python tunnel.py start          启动隧道
  python tunnel.py stop           停止隧道
  python tunnel.py restart        重启隧道
  python tunnel.py status         状态面板
  python tunnel.py switch 8000    切换本地服务端口
  python tunnel.py test           端到端自检
  python tunnel.py logs [行数]     查看隧道日志
  python tunnel.py watch          守护模式（断线自动重连）
  python tunnel.py open           浏览器打开公网地址
  python tunnel.py start-local    启动本地服务（执行 services.json 里的 start_cmd）
  python tunnel.py stop-local     停止监听当前档案端口的本地服务
  python tunnel.py deploy         部署前端：提交并推送仓库（Pages 自动构建，--dry-run 仅预览）
  python tunnel.py profiles       列出隧道档案
  python tunnel.py use <档案名>    切换隧道档案
  python tunnel.py login          登录/切换 Cloudflare 账号（浏览器授权）
  python tunnel.py create <档案名> <域名> [端口]   新建隧道并路由 DNS
  python tunnel.py route-dns <档案名> <域名>       给隧道补一条 DNS 路由
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
CLOUDFLARED_HOME = HOME / ".cloudflared"

BASE = Path(__file__).resolve().parent
PROFILE_FILE = BASE / "tunnels.json"
SERVICES_FILE = BASE / "services.json"
STATE_FILE = BASE / "tunnel_state.json"
DEPLOY_FILE = BASE / "deploy.json"
LOG_FILE = BASE / "tunnel.log"
CONFIGS_DIR = BASE / "configs"

DEFAULT_SERVICES = [
    {"name": "DineLens Web (8000)", "port": 8000, "start_cmd": ""},
    {"name": "前端开发服务器 Vite (5173)", "port": 5173, "start_cmd": ""},
    {"name": "通用 Node 服务 (3000)", "port": 3000, "start_cmd": ""},
]

DEFAULT_STATE = {"last_pid": None, "local_pid": None}

DEFAULT_DEPLOY = {
    "repo_dir": str(BASE.parent),
    "web_dir": "web",
    "branch": "main",
    "pages_project": "my-pages-project",
    "pages_url": "",
    "mode": "git",
}

DETACHED = 0x00000008
NEW_GROUP = 0x00000200
NO_WINDOW = 0x08000000


def resolve_cloudflared():
    candidates = [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe"),
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    try:
        out = subprocess.run(["where", "cloudflared"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            if line.strip():
                return line.strip()
    except OSError:
        pass
    return None


def load_json(path, default=None):
    if Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_name(text):
    return re.sub(r"[^\w.-]+", "_", text).strip("_") or "profile"


def parse_legacy_config(path):
    if not Path(path).exists():
        return None
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    tunnel = re.search(r"^tunnel:\s*(\S+)", text, re.M)
    cred = re.search(r"^credentials-file:\s*(.+)$", text, re.M)
    host = re.search(r"-\s*hostname:\s*(\S+)", text)
    port = re.search(r"service:\s*http://(?:localhost|127\.0\.0\.1):(\d+)", text)
    if not (tunnel and cred):
        return None
    return {
        "tunnel": tunnel.group(1).strip(),
        "credentials_file": cred.group(1).strip().strip('"'),
        "hostname": host.group(1).strip() if host else "",
        "port": int(port.group(1)) if port else 8000,
        "edge_ip_version": 4,
    }


def load_profile_db():
    db = load_json(PROFILE_FILE)
    if db and "profiles" in db:
        return db
    legacy = parse_legacy_config(CLOUDFLARED_HOME / "config.yml")
    db = {"active": "", "profiles": {}}
    if legacy:
        name = safe_name(legacy["tunnel"])
        db["profiles"][name] = legacy
        db["active"] = name
        save_json(PROFILE_FILE, db)
    return db


def save_profile_db(db):
    save_json(PROFILE_FILE, db)


def active_profile():
    db = load_profile_db()
    name = db.get("active", "")
    profile = db.get("profiles", {}).get(name)
    return name, profile


def public_url():
    _, profile = active_profile()
    if profile and profile.get("hostname"):
        return f"https://{profile['hostname']}"
    return ""


def tunnel_name():
    _, profile = active_profile()
    return profile.get("tunnel", "") if profile else ""


def load_state():
    return load_json(STATE_FILE, dict(DEFAULT_STATE)) or dict(DEFAULT_STATE)


def save_state(state):
    save_json(STATE_FILE, state)


def load_services():
    services = load_json(SERVICES_FILE)
    if services is None:
        services = json.loads(json.dumps(DEFAULT_SERVICES))
        save_json(SERVICES_FILE, services)
    return services


def load_deploy():
    cfg = load_json(DEPLOY_FILE)
    if cfg is None:
        cfg = json.loads(json.dumps(DEFAULT_DEPLOY))
        save_json(DEPLOY_FILE, cfg)
    return cfg


def save_deploy(cfg):
    save_json(DEPLOY_FILE, cfg)


def generate_config(name=None, profile=None):
    if profile is None:
        name, profile = active_profile()
    if not profile:
        raise RuntimeError("没有可用的隧道档案，请先 login 并 create")
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIGS_DIR / f"{safe_name(name)}.yml"
    content = (
        f"tunnel: {profile['tunnel']}\n"
        f"credentials-file: {profile['credentials_file']}\n\n"
        "ingress:\n"
        f"  - hostname: {profile['hostname']}\n"
        f"    service: http://localhost:{profile.get('port', 8000)}\n"
        "  - service: http_status:404\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def run_quiet(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=NO_WINDOW if os.name == "nt" else 0).stdout or ""
    except Exception:
        return ""


def find_tunnel_pids():
    name = tunnel_name()
    if not name:
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '{re.escape(name)}' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    out = run_quiet(["powershell.exe", "-NoProfile", "-Command", script])
    return [int(x) for x in re.findall(r"\d+", out)]


def is_tunnel_pid_alive(pid):
    if not pid:
        return False
    out = run_quiet(["tasklist", "/FI", f"PID eq {pid}", "/FI",
                     "IMAGENAME eq cloudflared.exe", "/NH"])
    return str(pid) in out


def tunnel_running():
    if find_tunnel_pids():
        return True
    return is_tunnel_pid_alive(load_state().get("last_pid"))


def read_config_port():
    _, profile = active_profile()
    return profile.get("port") if profile else None


def port_listening(port, host="127.0.0.1", timeout=2):
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_listen_pids(port):
    out = run_quiet([
        "powershell.exe", "-NoProfile", "-Command",
        f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique",
    ])
    return sorted({int(x) for x in re.findall(r"\d+", out)})


def process_name(pid):
    out = run_quiet(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    match = re.match(r'"([^"]+)"', out.strip())
    return match.group(1) if match else ""


def start_local_service(service):
    port = (service or {}).get("port")
    cmd = ((service or {}).get("start_cmd") or "").strip()
    if not port or not cmd:
        print("该服务没有配置 start_cmd，无法自动启动")
        return False
    if port_listening(port):
        print(f"localhost:{port} 已有服务在监听")
        return True
    print(f"启动本地服务: {cmd}")
    subprocess.Popen(cmd, shell=True, creationflags=DETACHED | NEW_GROUP, cwd=str(BASE))
    for _ in range(8):
        time.sleep(1)
        if port_listening(port):
            break
    pids = find_listen_pids(port)
    state = load_state()
    state["local_pid"] = pids[0] if pids else None
    save_state(state)
    ok = port_listening(port)
    print(f"本地服务{'已启动' if ok else '启动失败'} (localhost:{port})")
    return ok


def stop_local_service(port=None):
    port = port or read_config_port()
    if not port:
        print("没有可用的端口")
        return False
    pids = find_listen_pids(port)
    if not pids:
        print(f"localhost:{port} 没有服务在监听")
        return False
    state = load_state()
    tracked = state.get("local_pid")
    targets = [tracked] if tracked in pids else pids
    for pid in targets:
        name = process_name(pid)
        run_quiet(["taskkill", "/PID", str(pid), "/F"])
        print(f"已停止本地服务: PID {pid} ({name})")
    state["local_pid"] = None
    save_state(state)
    return True


def _git(args, cwd, timeout=180):
    try:
        return subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        return None


def deploy_frontend(dry_run=False, message=None):
    cfg = load_deploy()
    repo = Path(cfg.get("repo_dir") or BASE.parent)
    web = Path(cfg.get("web_dir") or "web")
    if not web.is_absolute():
        web = repo / web
    if not (repo / ".git").exists():
        print(f"不是 git 仓库: {repo}")
        return False
    if not web.exists():
        print(f"找不到前端目录: {web}")
        return False

    status = _git(["status", "--porcelain"], repo)
    if status is None:
        print("无法执行 git，请确认已安装 Git 并加入 PATH")
        return False
    changes = [line for line in status.stdout.splitlines() if line.strip()]

    ahead = 0
    rev = _git(["rev-list", "--count", "@{u}..HEAD"], repo)
    if rev is not None and rev.returncode == 0 and rev.stdout.strip().isdigit():
        ahead = int(rev.stdout.strip())

    print(f"仓库: {repo}")
    print(f"前端目录: {web}")
    print(f"未提交改动: {len(changes)} 处 | 未推送提交: {ahead} 个")
    for line in changes[:20]:
        print("   ", line)

    if not changes and ahead == 0:
        print("没有需要部署的内容（工作区干净且已与远端同步）")
        return True

    if dry_run:
        print("dry-run: 以上是将要提交/推送的内容，未做任何改动")
        return True

    if changes:
        add = _git(["add", "-A"], repo)
        if add is None or add.returncode != 0:
            print("git add 失败")
            return False
        msg = message or f"Deploy frontend: update web ({time.strftime('%Y-%m-%d %H:%M:%S')})"
        commit = _git(["commit", "-m", msg], repo)
        if commit is None or commit.returncode != 0:
            err = ((commit.stderr or "") if commit else "").strip()
            print(err[:300] or "git commit 失败")
            return False
        print(f"已提交: {msg}")

    push = _git(["push"], repo, timeout=300)
    if push is None or push.returncode != 0:
        err = (((push.stderr or "") + (push.stdout or "")) if push else "").strip()
        print(err[:400])
        print("git push 失败。首次使用可能需要在命令行手动 push 一次完成 GitHub 登录。")
        return False

    print("已推送，Cloudflare Pages 会在约 1 分钟内自动构建并发布。")
    url = (cfg.get("pages_url") or "").strip()
    if url:
        print(f"前端地址: {url}")
    return True


def http_check(url, timeout=20):
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        args = [curl, "-sS", "-D", "-", "-o", os.devnull, "--noproxy", "*",
                "--max-time", str(timeout), "-w",
                "__META__%{http_code}|%{time_total}|%{remote_ip}", url]
        out = run_quiet(args, timeout=timeout + 5)
        code, elapsed, ip = None, 0.0, ""
        for line in out.splitlines():
            if line.startswith("__META__"):
                parts = line[len("__META__"):].split("|")
                if parts and parts[0].isdigit() and parts[0] != "000":
                    code = int(parts[0])
                if len(parts) > 1:
                    try:
                        elapsed = float(parts[1])
                    except ValueError:
                        elapsed = 0.0
                if len(parts) > 2:
                    ip = parts[2]
        headers = {"remote_ip": ip}
        for line in out.splitlines():
            if line.lower().startswith("cf-ray:"):
                headers["cf-ray"] = line.split(":", 1)[1].strip()
        return code, elapsed, headers
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "dinelens-tunnel-manager"})
    start = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, time.time() - start, dict(resp.headers)
    except Exception as exc:
        return None, time.time() - start, {"error": str(exc)}


def log_tail(lines=40):
    if not LOG_FILE.exists():
        return ""
    data = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def wait_registered(timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        tail = log_tail(60)
        if "Registered tunnel connection" in tail:
            count = tail.count("Registered tunnel connection")
            print(f"  已注册 {count} 条隧道连接")
            return True
        time.sleep(2)
        print("  等待隧道注册中...")
    return False


def start_tunnel(silent=False):
    cf = resolve_cloudflared()
    if not cf:
        print("未找到 cloudflared，请先安装：winget install --id Cloudflare.cloudflared")
        return False
    name, profile = active_profile()
    if not profile:
        print("还没有隧道档案。先 login 登录账号，再 create 新建隧道（或运行 menu 的 p 菜单）")
        return False
    if not Path(profile.get("credentials_file", "")).exists():
        print(f"缺少凭据文件: {profile.get('credentials_file')}")
        return False
    if not profile.get("hostname"):
        print("档案缺少 hostname（域名）")
        return False
    if tunnel_running():
        print("隧道已经在运行（如需重载请用 restart）")
        return True

    cfg = generate_config(name, profile)
    edge = profile.get("edge_ip_version", 0)
    port = profile.get("port", 8000)
    args = [cf, "--config", str(cfg)]
    if edge in (4, 6):
        args += ["--edge-ip-version", str(edge)]
    args += ["tunnel", "run", profile["tunnel"]]

    with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as logf:
        logf.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                   f"(profile {name}, edge-ipv{edge if edge else 'auto'}, port {port}) =====\n")
        logf.flush()
        proc = subprocess.Popen(
            args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=DETACHED | NEW_GROUP, close_fds=True,
        )
    state = load_state()
    state["last_pid"] = proc.pid
    save_state(state)
    if not silent:
        print(f"隧道已启动 (PID {proc.pid}) | 档案 {name} | {profile['hostname']} -> localhost:{port}")
        if not port_listening(port):
            print(f"提示: 本地 {port} 端口当前没有服务在监听，网页会返回 502")
        wait_registered()
    return True


def stop_tunnel():
    pids = find_tunnel_pids()
    if not pids:
        state = load_state()
        if is_tunnel_pid_alive(state.get("last_pid")):
            pids = [state["last_pid"]]
    if not pids:
        print("隧道未在运行")
        return False
    for pid in pids:
        run_quiet(["taskkill", "/PID", str(pid), "/F"])
        print(f"已停止 cloudflared (PID {pid})")
    state = load_state()
    state["last_pid"] = None
    save_state(state)
    return True


def show_status():
    print("=" * 62)
    print("DineLens 隧道状态")
    print("=" * 62)
    cf = resolve_cloudflared()
    name, profile = active_profile()
    url = public_url()
    print(f"cloudflared      : {cf or '未安装'}")
    print(f"当前档案         : {name or '未配置'}")
    if profile:
        edge = profile.get("edge_ip_version", 0)
        print(f"隧道 / 域名      : {profile.get('tunnel')} / {profile.get('hostname') or '未设置'}")
        print(f"边缘 IP 版本     : {'自动' if edge not in (4, 6) else f'IPv{edge}'}")
    pids = find_tunnel_pids()
    running = bool(pids) or is_tunnel_pid_alive(load_state().get("last_pid"))
    print(f"隧道进程         : {'运行中 PID ' + ', '.join(map(str, pids)) if running else '未运行'}")
    port = read_config_port()
    print(f"本地服务端口     : {port or '未配置'}")
    if port:
        print(f"本地服务状态     : {'监听中' if port_listening(port) else '未监听（公网会 502）'}")
    if running and cf and profile:
        info = run_quiet([cf, "tunnel", "info", profile["tunnel"]], timeout=40)
        lines = [l for l in info.splitlines() if "CONNECTOR" in l or re.search(r"\d+x\w+", l)]
        print("隧道连接         :")
        for line in lines:
            print("   ", line.strip())
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if proxy:
        print(f"系统代理         : 检测到 {proxy}（自检已强制绕过代理）")
    if url:
        code, elapsed, headers = http_check(url)
        ray = headers.get("cf-ray", "")
        pop = ray.split("-")[-1] if "-" in ray else "?"
        if code:
            print(f"公网测试         : HTTP {code} | {elapsed:.2f}s | 边缘: {pop} | 直连 IP: {headers.get('remote_ip', '')}")
        else:
            print(f"公网测试         : 失败 | {elapsed:.2f}s")
            print("                   直连路径可能被干扰（TCP 可达但 TLS 被重置），可稍后重试")
    else:
        print("公网测试         : 未配置域名，先创建档案")
    print("=" * 62)


def end_to_end_test():
    url = public_url()
    port = read_config_port()
    print("端到端自检")
    print("-" * 40)
    if port:
        if port_listening(port):
            code, elapsed, _ = http_check(f"http://127.0.0.1:{port}/", timeout=10)
            print(f"[本地] 127.0.0.1:{port}  HTTP {code if code else '失败'} | {elapsed:.2f}s")
        else:
            print(f"[本地] 127.0.0.1:{port}  未监听（启动你的 Web 服务后再试）")
    if url:
        code, elapsed, _ = http_check(url)
        print(f"[公网] {url}  HTTP {code if code else '失败'} | {elapsed:.2f}s")
        if code and code >= 500:
            print("       公网 5xx 通常是本地服务没有在监听对应端口")
        return code
    print("[公网] 未配置域名")
    return None


def switch_service(port_arg=None):
    name, profile = active_profile()
    if not profile:
        print("还没有隧道档案")
        return False
    services = load_services()
    current = profile.get("port")
    if port_arg:
        target_port = int(port_arg)
        start_cmd = ""
    else:
        print(f"当前隧道指向: localhost:{current}")
        print("-" * 50)
        for i, svc in enumerate(services, 1):
            print(f"  {i}. {svc['name']:<34} -> localhost:{svc['port']}")
        print(f"  {len(services) + 1}. 自定义端口")
        print("-" * 50)
        choice = input("请选择要转发到的本地服务 (回车取消): ").strip()
        if not choice:
            return False
        start_cmd = ""
        if choice.isdigit() and int(choice) == len(services) + 1:
            target_port = int(input("请输入端口号: ").strip())
        elif choice.isdigit() and 1 <= int(choice) <= len(services):
            idx = int(choice) - 1
            target_port = services[idx]["port"]
            start_cmd = services[idx].get("start_cmd", "")
        else:
            print("无效选择")
            return False
        if start_cmd and not port_listening(target_port):
            if input(f"本地 {target_port} 端口未监听，是否运行预设命令启动? (y/N): ").strip().lower() == "y":
                subprocess.Popen(start_cmd, shell=True, creationflags=DETACHED | NEW_GROUP,
                                 cwd=str(BASE))
                print("已启动本地服务，等待 3 秒...")
                time.sleep(3)

    profile["port"] = target_port
    db = load_profile_db()
    db["profiles"][name] = profile
    save_profile_db(db)
    generate_config(name, profile)
    print(f"已把档案 {name} 指向 localhost:{target_port}")
    if tunnel_running():
        if input("隧道正在运行，是否重启以生效? (Y/n): ").strip().lower() != "n":
            stop_tunnel()
            time.sleep(1)
            start_tunnel()
    return True


def show_logs(lines=40):
    text = log_tail(lines)
    print(text if text else "暂无日志")


def open_public():
    url = public_url()
    if not url:
        print("未配置域名")
        return
    print(f"打开 {url}")
    webbrowser.open(url)


def cmd_login():
    cf = resolve_cloudflared()
    if not cf:
        print("未找到 cloudflared")
        return False
    print("即将打开浏览器：请登录 Cloudflare 账号并选择要使用的域名。")
    result = subprocess.run([cf, "tunnel", "login"], check=False)
    if result.returncode == 0:
        print("登录完成，cert.pem 已更新。现在可以 create 新建隧道档案。")
        return True
    print("登录未完成")
    return False


def register_profile(name, tunnel, hostname, port, credentials_file, edge=4):
    db = load_profile_db()
    db["profiles"][name] = {
        "tunnel": tunnel,
        "hostname": hostname,
        "credentials_file": credentials_file,
        "port": port,
        "edge_ip_version": edge,
    }
    db["active"] = name
    save_profile_db(db)
    generate_config(name, db["profiles"][name])


def cmd_create(name, hostname, port=8000):
    cf = resolve_cloudflared()
    if not cf:
        print("未找到 cloudflared")
        return False
    tunnel = name
    print(f"[1/2] 创建隧道 {tunnel} ...")
    out = subprocess.run([cf, "tunnel", "create", tunnel],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((out.stdout or out.stderr or "").strip()[-500:])
    match = re.search(r"credentials written to\s+(.+\.json)", out.stdout or "", re.I)
    cred = match.group(1).strip() if match else str(CLOUDFLARED_HOME / f"{tunnel}.json")
    print(f"[2/2] 路由 DNS {hostname} -> {tunnel} ...")
    route = subprocess.run([cf, "tunnel", "route", "dns", tunnel, hostname],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((route.stdout or route.stderr or "").strip()[-300:])
    register_profile(safe_name(name), tunnel, hostname, int(port), cred)
    print(f"档案 {name} 已创建并设为当前（http://localhost:{port}）")
    return True


def cmd_route_dns(name, hostname):
    cf = resolve_cloudflared()
    db = load_profile_db()
    profile = db["profiles"].get(name)
    if not profile:
        print(f"档案 {name} 不存在")
        return False
    out = subprocess.run([cf, "tunnel", "route", "dns", profile["tunnel"], hostname],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((out.stdout or out.stderr or "").strip()[-300:])
    profile["hostname"] = hostname
    db["profiles"][name] = profile
    save_profile_db(db)
    if db.get("active") == name:
        generate_config(name, profile)
    print(f"已把 {hostname} 路由到隧道 {profile['tunnel']}")
    return True


def cmd_use(name):
    db = load_profile_db()
    if name not in db["profiles"]:
        print(f"档案 {name} 不存在。现有: {', '.join(db['profiles']) or '无'}")
        return False
    db["active"] = name
    save_profile_db(db)
    profile = db["profiles"][name]
    generate_config(name, profile)
    print(f"已切换到档案 {name}: {profile['hostname']} -> localhost:{profile['port']}")
    if tunnel_running():
        print("隧道正在运行，请执行 restart 使其生效")
    return True


def list_profiles():
    db = load_profile_db()
    if not db["profiles"]:
        print("暂无档案。流程：login 登录账号 -> create 新建隧道")
        return
    for name, profile in db["profiles"].items():
        marker = "*" if name == db.get("active") else " "
        print(f" {marker} {name:<16} {profile.get('hostname', ''):<28} -> localhost:{profile.get('port', 8000)}")


def guard_mode():
    print("守护模式：每 30 秒检查一次，隧道掉线自动重连；Ctrl+C 退出")
    try:
        while True:
            if not tunnel_running():
                print(f"[{time.strftime('%H:%M:%S')}] 检测到隧道离线，正在拉起...")
                start_tunnel(silent=True)
            else:
                print(f"[{time.strftime('%H:%M:%S')}] 隧道正常")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n已退出守护模式（隧道保持运行）")


def settings():
    name, profile = active_profile()
    if not profile:
        print("还没有隧道档案")
        return
    current = profile.get("edge_ip_version", 0)
    print(f"当前档案 {name} 的边缘 IP 版本: {'自动' if current not in (4, 6) else f'IPv{current}'}")
    print("  1. 自动    2. IPv4（推荐）    3. IPv6")
    choice = input("选择 (回车取消): ").strip()
    if choice == "1":
        profile["edge_ip_version"] = 0
    elif choice == "2":
        profile["edge_ip_version"] = 4
    elif choice == "3":
        profile["edge_ip_version"] = 6
    else:
        return
    db = load_profile_db()
    db["profiles"][name] = profile
    save_profile_db(db)
    print("已保存，重启隧道后生效")
    if tunnel_running() and input("现在重启隧道? (Y/n): ").strip().lower() != "n":
        stop_tunnel()
        time.sleep(1)
        start_tunnel()


def profile_menu():
    while True:
        print()
        print("隧道档案（账号）")
        print("-" * 50)
        list_profiles()
        print("-" * 50)
        print(" 数字 = 切换到该档案    l = 登录/切换 Cloudflare 账号")
        print(" c = 新建隧道档案       d = 路由 DNS    0 = 返回")
        choice = input("请选择: ").strip().lower()
        db = load_profile_db()
        names = list(db["profiles"])
        if choice == "0":
            return
        if choice == "l":
            cmd_login()
        elif choice == "c":
            name = input("档案名（=隧道名，建议英文）: ").strip()
            hostname = input("域名（如 xxx.example.com）: ").strip()
            port = input("本地端口 [8000]: ").strip() or "8000"
            if name and hostname:
                cmd_create(name, hostname, port)
        elif choice == "d":
            if not names:
                print("还没有档案")
                continue
            name = input(f"档案名 {names}: ").strip()
            hostname = input("新域名: ").strip()
            if name and hostname:
                cmd_route_dns(name, hostname)
        elif choice.isdigit() and 1 <= int(choice) <= len(names):
            cmd_use(names[int(choice) - 1])
        else:
            print("无效输入")


MENU = """
============================================================
  DineLens Tunnel Manager
============================================================
  1. 启动隧道          2. 停止隧道          3. 重启隧道
  4. 状态面板          5. 切换本地服务       6. 端到端自检
  7. 查看日志          8. 守护模式(自动重连)  9. 打开公网地址
  p. 档案管理(账号/隧道)  s. 设置(边缘IP版本)   0. 退出
============================================================
"""


def menu():
    while True:
        print(MENU)
        choice = input("请选择: ").strip().lower()
        if choice == "1":
            start_tunnel()
        elif choice == "2":
            stop_tunnel()
        elif choice == "3":
            stop_tunnel()
            time.sleep(1)
            start_tunnel()
        elif choice == "4":
            show_status()
        elif choice == "5":
            switch_service()
        elif choice == "6":
            end_to_end_test()
        elif choice == "7":
            show_logs()
        elif choice == "8":
            guard_mode()
        elif choice == "9":
            open_public()
        elif choice == "p":
            profile_menu()
        elif choice == "s":
            settings()
        elif choice == "0":
            print("再见")
            return
        else:
            print("无效输入")


def main():
    args = sys.argv[1:]
    if not args:
        try:
            menu()
        except KeyboardInterrupt:
            print("\n已退出")
        return
    cmd = args[0].lower()
    if cmd == "start":
        start_tunnel()
    elif cmd == "stop":
        stop_tunnel()
    elif cmd == "restart":
        stop_tunnel()
        time.sleep(1)
        start_tunnel()
    elif cmd == "status":
        show_status()
    elif cmd == "switch":
        switch_service(args[1] if len(args) > 1 else None)
    elif cmd == "test":
        end_to_end_test()
    elif cmd == "logs":
        show_logs(int(args[1]) if len(args) > 1 else 40)
    elif cmd == "watch":
        guard_mode()
    elif cmd == "open":
        open_public()
    elif cmd == "start-local":
        port = read_config_port()
        services = load_services()
        target = next((s for s in services if s.get("port") == port and s.get("start_cmd")), None)
        target = target or next((s for s in services if s.get("start_cmd")), None)
        if target:
            start_local_service(target)
        else:
            print("services.json 里没有配置 start_cmd 的服务")
    elif cmd == "stop-local":
        stop_local_service()
    elif cmd == "deploy":
        deploy_frontend(dry_run="--dry-run" in args)
    elif cmd == "profiles":
        list_profiles()
    elif cmd == "use" and len(args) > 1:
        cmd_use(args[1])
    elif cmd == "login":
        cmd_login()
    elif cmd == "create" and len(args) > 2:
        cmd_create(args[1], args[2], args[3] if len(args) > 3 else 8000)
    elif cmd == "route-dns" and len(args) > 2:
        cmd_route_dns(args[1], args[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
