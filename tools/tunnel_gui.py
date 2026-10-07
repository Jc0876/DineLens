"""
DineLens Tunnel Manager GUI（多档案版）
图形界面：启停、本地服务切换、隧道档案（账号）管理、状态监控、自检、日志。

用法:
  python tunnel_gui.py            打开界面
  python tunnel_gui.py --selftest 自检模式（不显示窗口）
"""

import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tunnel as core

COLOR_OK = "#1a7f37"
COLOR_ERR = "#c62828"
COLOR_WARN = "#b26a00"
COLOR_MUTED = "#666666"
BG = "#f5f7fa"


class TunnelApp:
    def __init__(self, root):
        self.root = root
        self.busy = False
        self.fast_busy = False
        self.full_busy = False
        self.guard_var = tk.BooleanVar(value=False)
        self.auto_log_var = tk.BooleanVar(value=True)
        active = core.active_profile()[1] or {}
        self.edge_var = tk.StringVar(value=str(active.get("edge_ip_version", 4)))
        self.port_var = tk.StringVar()
        self.service_var = tk.StringVar()
        self.profile_var = tk.StringVar()
        self.values = {}
        self.services = core.load_services()
        self.build()
        self.root.after(300, self.refresh_fast_async)
        self.root.after(800, self.refresh_full_async)
        self.root.after(3500, self.fast_tick)
        self.root.after(60000, self.full_tick)
        self.root.after(2500, self.log_tick)

    def build(self):
        self.root.title("DineLens Tunnel Manager")
        self.root.geometry("820x780")
        self.root.minsize(760, 700)
        self.root.configure(bg=BG)

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(header, text="DineLens Tunnel Manager", bg=BG,
                 font="{Microsoft YaHei UI} 15 bold").pack(side="left")
        self.dot = tk.Canvas(header, width=14, height=14, bg=BG, highlightthickness=0)
        self.dot.pack(side="right", padx=(0, 6))
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill=COLOR_MUTED, outline="")
        self.status_text = tk.Label(header, text="正在检测...", bg=BG, fg=COLOR_MUTED,
                                    font="{Microsoft YaHei UI} 10")
        self.status_text.pack(side="right", padx=6)

        info = tk.LabelFrame(self.root, text=" 状态 ", bg=BG, padx=10, pady=8)
        info.pack(fill="x", padx=14, pady=6)
        rows = [
            ("cloudflared", "cf"),
            ("当前档案", "profile"),
            ("边缘 IP 版本", "edge"),
            ("隧道进程", "proc"),
            ("本地服务", "port"),
            ("隧道连接", "conn"),
            ("公网测试", "public"),
            ("系统代理", "proxy"),
        ]
        for i, (label, key) in enumerate(rows):
            tk.Label(info, text=label + "：", bg=BG, fg=COLOR_MUTED,
                     font="{Microsoft YaHei UI} 9").grid(row=i, column=0, sticky="e", pady=1)
            value = tk.Label(info, text="-", bg=BG, anchor="w",
                             font="{Microsoft YaHei UI} 9")
            value.grid(row=i, column=1, sticky="w", padx=(4, 0), pady=1)
            self.values[key] = value

        actions = tk.Frame(self.root, bg=BG)
        actions.pack(fill="x", padx=14, pady=(2, 6))
        self.action_buttons = []
        for text, cmd in (
            ("启动隧道", self.on_start),
            ("停止隧道", self.on_stop),
            ("重启隧道", self.on_restart),
            ("端到端自检", self.on_test),
            ("打开公网地址", self.on_open),
        ):
            btn = tk.Button(actions, text=text, command=cmd, width=11,
                            font="{Microsoft YaHei UI} 9")
            btn.pack(side="left", padx=(0, 6))
            self.action_buttons.append(btn)
        tk.Checkbutton(actions, text="守护模式（掉线自动重连）", variable=self.guard_var,
                       bg=BG, font="{Microsoft YaHei UI} 9").pack(side="right")

        profile_frame = tk.LabelFrame(self.root, text=" 隧道档案（账号） ", bg=BG, padx=10, pady=8)
        profile_frame.pack(fill="x", padx=14, pady=6)
        self.profile_combo = ttk.Combobox(profile_frame, state="readonly", width=44,
                                          textvariable=self.profile_var)
        self.profile_combo.pack(side="left")
        self.select_current_profile()
        tk.Button(profile_frame, text="切换档案", command=self.on_use_profile,
                  font="{Microsoft YaHei UI} 9").pack(side="left", padx=6)
        tk.Button(profile_frame, text="登录账号", command=self.on_login,
                  font="{Microsoft YaHei UI} 9").pack(side="right")
        tk.Button(profile_frame, text="路由 DNS", command=self.on_route_dns,
                  font="{Microsoft YaHei UI} 9").pack(side="right", padx=6)
        tk.Button(profile_frame, text="新建隧道", command=self.on_create_tunnel,
                  font="{Microsoft YaHei UI} 9").pack(side="right")

        service = tk.LabelFrame(self.root, text=" 本地服务（当前档案指向的端口） ", bg=BG, padx=10, pady=8)
        service.pack(fill="x", padx=14, pady=6)
        self.service_combo = ttk.Combobox(
            service, state="readonly", width=42, textvariable=self.service_var,
            values=[f"{s['name']}  →  localhost:{s['port']}" for s in self.services])
        self.service_combo.pack(side="left")
        self.select_current_service()
        tk.Entry(service, textvariable=self.port_var, width=8).pack(side="left", padx=8)
        tk.Label(service, text="自定义端口", bg=BG, fg=COLOR_MUTED,
                 font="{Microsoft YaHei UI} 9").pack(side="left")
        tk.Button(service, text="切换并生效", command=self.on_switch_service,
                  font="{Microsoft YaHei UI} 9").pack(side="right")

        settings = tk.LabelFrame(self.root, text=" 隧道设置（重启后生效） ", bg=BG, padx=10, pady=8)
        settings.pack(fill="x", padx=14, pady=6)
        tk.Label(settings, text="边缘 IP 版本：", bg=BG, font="{Microsoft YaHei UI} 9").pack(side="left")
        for label, value in (("自动", "0"), ("IPv4（推荐）", "4"), ("IPv6", "6")):
            tk.Radiobutton(settings, text=label, value=value, variable=self.edge_var,
                           bg=BG, font="{Microsoft YaHei UI} 9").pack(side="left", padx=4)
        tk.Button(settings, text="保存设置", command=self.on_save_settings,
                  font="{Microsoft YaHei UI} 9").pack(side="right")

        log_frame = tk.LabelFrame(self.root, text=" 隧道日志 ", bg=BG, padx=10, pady=8)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(6, 12))
        self.log_box = ScrolledText(log_frame, height=9, state="disabled",
                                    font=("Consolas", 9), bg="#ffffff")
        self.log_box.pack(fill="both", expand=True)
        bar = tk.Frame(log_frame, bg=BG)
        bar.pack(fill="x", pady=(4, 0))
        tk.Button(bar, text="刷新日志", command=self.refresh_log,
                  font="{Microsoft YaHei UI} 9").pack(side="left")
        tk.Checkbutton(bar, text="自动刷新", variable=self.auto_log_var,
                       bg=BG, font="{Microsoft YaHei UI} 9").pack(side="left", padx=8)

    def select_current_profile(self):
        db = core.load_profile_db()
        names = list(db.get("profiles", {}))
        self.profile_combo["values"] = names
        active = db.get("active")
        if active in names:
            self.profile_combo.current(names.index(active))
        elif names:
            self.profile_combo.current(0)

    def select_current_service(self):
        port = core.read_config_port()
        for i, svc in enumerate(self.services):
            if svc["port"] == port:
                self.service_combo.current(i)
                return
        if self.services:
            self.service_combo.current(0)

    def set_status(self, text, color=COLOR_MUTED, dot=None):
        self.status_text.config(text=text, fg=color)
        if dot:
            self.dot.itemconfig(self.dot_id, fill=dot)

    def set_busy(self, busy, status=None):
        self.busy = busy
        for btn in self.action_buttons:
            btn.config(state="disabled" if busy else "normal")
        if status:
            self.set_status(status, COLOR_WARN if busy else COLOR_MUTED)

    def bg(self, work, done=None):
        def runner():
            try:
                result = work()
                error = None
            except Exception as exc:
                result, error = None, exc
            if done:
                self.root.after(0, lambda: done(result, error))
        threading.Thread(target=runner, daemon=True).start()

    def wait_registered(self, timeout=45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "Registered tunnel connection" in core.log_tail(80):
                return True
            time.sleep(2)
        return core.tunnel_running()

    def on_start(self):
        if self.busy:
            return
        self.set_busy(True, "启动中...")
        def work():
            return {"ok": core.start_tunnel(silent=True) and self.wait_registered()}
        def done(result, error):
            ok = bool(result and result.get("ok")) and not error
            self.set_busy(False, "隧道已启动" if ok else "启动未完成，请看日志")
            self.refresh_fast_async()
            self.refresh_full_async()
        self.bg(work, done)

    def on_stop(self):
        if self.busy:
            return
        self.set_busy(True, "停止中...")
        def done(result, error):
            self.set_busy(False, "已停止" if result else "隧道未在运行")
            self.refresh_fast_async()
        self.bg(core.stop_tunnel, done)

    def on_restart(self):
        if self.busy:
            return
        self.set_busy(True, "重启中...")
        def work():
            core.stop_tunnel()
            time.sleep(1)
            core.start_tunnel(silent=True)
            return {"ok": self.wait_registered()}
        def done(result, error):
            ok = bool(result and result.get("ok")) and not error
            self.set_busy(False, "隧道已重启" if ok else "重启未完成，请看日志")
            self.refresh_fast_async()
            self.refresh_full_async()
        self.bg(work, done)

    def on_test(self):
        if self.busy:
            return
        self.set_busy(True, "自检中...")
        def work():
            url = core.public_url()
            port = core.read_config_port()
            local = None
            if port and core.port_listening(port):
                code, elapsed, _ = core.http_check(f"http://127.0.0.1:{port}/", timeout=10)
                local = (port, code, elapsed)
            code, elapsed, headers = (None, 0, {})
            if url:
                code, elapsed, headers = core.http_check(url)
            return {"local": local, "code": code, "elapsed": elapsed, "headers": headers, "url": url}
        def done(result, error):
            self.set_busy(False, "自检完成")
            if error or not result:
                messagebox.showerror("自检失败", str(error or "未知错误"))
                return
            lines = []
            if result["local"]:
                port, code, elapsed = result["local"]
                lines.append(f"本地  127.0.0.1:{port}  HTTP {code if code else '失败'}（{elapsed:.2f}s）")
            else:
                lines.append("本地服务未监听（启动你的 Web 服务后再测）")
            if not result["url"]:
                lines.append("未配置域名，先创建档案")
            elif result["code"]:
                lines.append(f"公网  {result['url']}  HTTP {result['code']}（{result['elapsed']:.2f}s）")
            else:
                lines.append("公网访问失败：直连 Cloudflare 可能被干扰，稍后重试")
            messagebox.showinfo("端到端自检", "\n".join(lines))
            self.refresh_full_async()
        self.bg(work, done)

    def on_open(self):
        url = core.public_url()
        if not url:
            messagebox.showwarning("打开公网地址", "还没有可用的档案/域名")
            return
        webbrowser.open(url)
        self.set_status(f"已在浏览器打开 {url}")

    def on_switch_service(self):
        if self.busy:
            return
        name, profile = core.active_profile()
        if not profile:
            messagebox.showwarning("切换本地服务", "还没有隧道档案")
            return
        services = core.load_services()
        custom = self.port_var.get().strip()
        start_cmd = ""
        if custom.isdigit():
            port = int(custom)
        else:
            idx = self.service_combo.current()
            if idx < 0 or idx >= len(services):
                messagebox.showwarning("切换本地服务", "请选择服务或输入自定义端口")
                return
            port = services[idx]["port"]
            start_cmd = services[idx].get("start_cmd", "")
        if not (1 <= port <= 65535):
            messagebox.showwarning("切换本地服务", "端口号无效")
            return
        db = core.load_profile_db()
        profile["port"] = port
        db["profiles"][name] = profile
        core.save_profile_db(db)
        core.generate_config(name, profile)
        self.set_status(f"档案 {name} 已指向 localhost:{port}")
        if start_cmd and not core.port_listening(port):
            if messagebox.askyesno("启动本地服务", f"本地 {port} 端口未监听，是否运行预设命令？\n\n{start_cmd}"):
                subprocess.Popen(start_cmd, shell=True, cwd=str(core.BASE),
                                 creationflags=core.DETACHED | core.NEW_GROUP)
                self.set_status("已启动本地服务")
        self.refresh_fast_async()
        if core.tunnel_running():
            if messagebox.askyesno("重启隧道", "切换已保存，需要重启隧道才能让新端口生效。现在重启？"):
                self.on_restart()

    def on_save_settings(self):
        name, profile = core.active_profile()
        if not profile:
            messagebox.showwarning("设置", "还没有隧道档案")
            return
        db = core.load_profile_db()
        profile["edge_ip_version"] = int(self.edge_var.get())
        db["profiles"][name] = profile
        core.save_profile_db(db)
        value = profile["edge_ip_version"]
        self.values["edge"].config(text="自动" if value not in (4, 6) else f"IPv{value}")
        if core.tunnel_running() and messagebox.askyesno("重启隧道", "设置已保存，重启隧道后生效。现在重启？"):
            self.on_restart()
        else:
            self.set_status("设置已保存")

    def on_use_profile(self):
        name = self.profile_combo.get()
        if not name or self.busy:
            return
        self.set_busy(True, f"切换档案 {name}...")
        def done(result, error):
            self.set_busy(False, "档案已切换" if result else "切换失败")
            self.refresh_fast_async()
            self.refresh_full_async()
            if result and core.tunnel_running():
                if messagebox.askyesno("重启隧道", "档案已切换，需要重启隧道生效。现在重启？"):
                    self.on_restart()
        self.bg(lambda: core.cmd_use(name), done)

    def on_login(self):
        if self.busy:
            return
        messagebox.showinfo("登录 Cloudflare", "即将打开浏览器。\n请登录目标账号并选择要使用的域名，完成后回到本窗口。")
        self.set_busy(True, "等待浏览器授权...")
        def done(result, error):
            self.set_busy(False, "登录完成" if result else "登录未完成")
            self.select_current_profile()
        self.bg(core.cmd_login, done)

    def on_create_tunnel(self):
        if self.busy:
            return
        name = simpledialog.askstring("新建隧道", "档案名（=隧道名，建议英文）:", parent=self.root)
        if not name:
            return
        hostname = simpledialog.askstring("新建隧道", "域名（如 xxx.example.com）:", parent=self.root)
        if not hostname:
            return
        port = simpledialog.askstring("新建隧道", "本地端口:", initialvalue="8000", parent=self.root)
        if not port:
            return
        self.set_busy(True, "创建隧道中...")
        def done(result, error):
            self.set_busy(False, "隧道已创建" if result else "创建失败，查看日志")
            self.select_current_profile()
            self.refresh_fast_async()
            self.refresh_full_async()
            if result and messagebox.askyesno("启动隧道", "档案已就绪，现在启动隧道？"):
                self.on_start()
        self.bg(lambda: core.cmd_create(name, hostname, port), done)

    def on_route_dns(self):
        name = self.profile_combo.get()
        if not name or self.busy:
            return
        hostname = simpledialog.askstring("路由 DNS", f"为档案 {name} 添加/更换域名:", parent=self.root)
        if not hostname:
            return
        self.set_busy(True, "路由 DNS 中...")
        def done(result, error):
            self.set_busy(False, "DNS 已路由" if result else "路由失败")
            self.refresh_fast_async()
            self.refresh_full_async()
        self.bg(lambda: core.cmd_route_dns(name, hostname), done)

    def refresh_fast_async(self):
        if self.fast_busy:
            return
        self.fast_busy = True
        def work():
            pids = core.find_tunnel_pids()
            running = bool(pids) or core.is_pid_alive(core.load_state().get("last_pid"))
            name, profile = core.active_profile()
            if not pids and running:
                pids = [core.load_state().get("last_pid")]
            port = profile.get("port") if profile else None
            listening = core.port_listening(port) if port else False
            cf = core.resolve_cloudflared()
            return {"running": running, "pids": pids, "port": port, "listening": listening,
                    "cf": cf, "profile": name, "hostname": profile.get("hostname") if profile else "",
                    "edge": profile.get("edge_ip_version", 0) if profile else 0}
        def done(result, error):
            self.fast_busy = False
            if error or not result:
                return
            edge = result["edge"]
            self.values["cf"].config(text=result["cf"] or "未安装")
            if result["profile"]:
                self.values["profile"].config(
                    text=f"{result['profile']}（{result['hostname']} → localhost:{result['port']}）")
            else:
                self.values["profile"].config(text="未配置（请先登录并新建隧道）", fg=COLOR_ERR)
            self.values["edge"].config(text="自动" if edge not in (4, 6) else f"IPv{edge}")
            if result["running"]:
                self.values["proc"].config(text="运行中  PID " + ", ".join(map(str, result["pids"])),
                                           fg=COLOR_OK)
                self.set_status("隧道运行中", COLOR_OK, COLOR_OK)
            else:
                self.values["proc"].config(text="未运行", fg=COLOR_ERR)
                self.set_status("隧道已停止", COLOR_ERR, COLOR_ERR)
                if self.guard_var.get() and not self.busy:
                    self.on_start()
            port = result["port"]
            if port:
                state_text = "监听中" if result["listening"] else "未监听（公网会 502）"
                color = COLOR_OK if result["listening"] else COLOR_WARN
                self.values["port"].config(text=f"localhost:{port}  {state_text}", fg=color)
            else:
                self.values["port"].config(text="未配置", fg=COLOR_ERR)
        self.bg(work, done)

    def refresh_full_async(self):
        if self.full_busy:
            return
        self.full_busy = True
        def work():
            cf = core.resolve_cloudflared()
            name, profile = core.active_profile()
            conn = ""
            if cf and profile and core.tunnel_running():
                info = core.run_quiet([cf, "tunnel", "info", profile["tunnel"]], timeout=40)
                pairs = re.findall(r"(\d+)x(\w+)", info)
                if pairs:
                    total = sum(int(n) for n, _ in pairs)
                    pops = ", ".join(sorted({pop for _, pop in pairs}))
                    conn = f"{total} 条（{pops}）"
            url = core.public_url()
            code, elapsed, headers = (None, 0, {})
            if url:
                code, elapsed, headers = core.http_check(url)
            proxies = urllib.request.getproxies()
            proxy = proxies.get("https") or proxies.get("http") or ""
            return {"conn": conn, "code": code, "elapsed": elapsed,
                    "ray": headers.get("cf-ray", ""), "ip": headers.get("remote_ip", ""),
                    "proxy": proxy, "url": url}
        def done(result, error):
            self.full_busy = False
            if error or not result:
                return
            self.values["conn"].config(text=result["conn"] or "无连接",
                                       fg=COLOR_OK if result["conn"] else COLOR_ERR)
            if not result["url"]:
                self.values["public"].config(text="未配置域名", fg=COLOR_ERR)
            elif result["code"]:
                pop = result["ray"].split("-")[-1] if "-" in result["ray"] else "?"
                self.values["public"].config(
                    text=f"HTTP {result['code']}  {result['elapsed']:.2f}s  边缘:{pop}  IP:{result['ip']}",
                    fg=COLOR_OK)
            else:
                self.values["public"].config(text="失败（直连路径可能被干扰，稍后重试）", fg=COLOR_ERR)
            self.values["proxy"].config(
                text=(f"检测到 {result['proxy']}（自检已绕过）" if result["proxy"] else "无"), fg=COLOR_MUTED)
        self.bg(work, done)

    def fast_tick(self):
        self.refresh_fast_async()
        self.root.after(4000, self.fast_tick)

    def full_tick(self):
        self.refresh_full_async()
        self.root.after(60000, self.full_tick)

    def refresh_log(self):
        text = core.log_tail(300)
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def log_tick(self):
        if self.auto_log_var.get():
            self.refresh_log()
        self.root.after(3000, self.log_tick)


def main():
    root = tk.Tk()
    try:
        root.option_add("*Font", "{Microsoft YaHei UI} 9")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        root.withdraw()
        TunnelApp(root)
        root.update_idletasks()
        root.update()
        root.destroy()
        print("selftest OK")
        return
    TunnelApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
