"""Windows desktop entry point; monitoring is opt-in on every ordinary launch."""
import argparse
import copy
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from resetnotice import __version__
from resetnotice.core import DEFAULTS, FINAL, SOURCE, STAGES, Monitor, Notice, Store, iso, load_config, save_config, validate
from resetnotice.notify import DemoTransport, Sender, Transport
from resetnotice.vault import Secrets, WindowsVault

STATUS = {"pending": "待发送", "retry": "退避重试", "sending": "发送中",
          "accepted": "服务器已接受／送达未确认", "uncertain": "确认丢失／需核对",
          "blocked": "受阻／需处理", "cancelled": "已取消"}


class MemoryVault:
    """Offline demonstration only; never touches Windows credential storage."""
    def __init__(self):
        self.data = {}

    def read(self, key):
        return self.data.get(key, "")

    def write(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class InstanceLock:
    def __init__(self, directory):
        self.handle = None
        if os.name == "nt":
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            api.CreateMutexW.restype = ctypes.c_void_p
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            self.api = api
            digest = hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()
            self.handle = api.CreateMutexW(None, False, "Local\\ResetNotice." + digest)
            if not self.handle:
                raise OSError("Cannot acquire application mutex")
            if ctypes.get_last_error() == 183:
                self.close()
                raise ValueError("同一数据目录已有程序运行。请先关闭原窗口。")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def startup_command(directory):
    if getattr(sys, "frozen", False):
        args = [sys.executable]
    else:
        args = [str(Path(sys.executable).with_name("pythonw.exe")), str(Path(__file__).resolve())]
    return subprocess.list2cmdline(args + ["--data-dir", str(directory), "--start-monitor"])


def change_startup(enabled, directory):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "ResetNotice", 0, winreg.REG_SZ, startup_command(directory))
        else:
            try:
                winreg.DeleteValue(key, "ResetNotice")
            except FileNotFoundError:
                pass


class App:
    def __init__(self, root, directory, demo=False):
        self.root, self.directory, self.demo = root, directory, demo
        self.store = Store(directory)
        self.config = load_config(directory)
        self.secrets = Secrets(MemoryVault() if demo else WindowsVault(directory))
        self.sender = Sender(self.store, DemoTransport() if demo else Transport(self.secrets))
        self.monitor = Monitor(self.store, self.sender)
        self.worker, self.stop_event, self.done_event = None, threading.Event(), threading.Event()
        self.done_event.set()
        self.running, self.closing = False, False
        self.vars = {}
        self.selected_batch = ""
        root.title("Codex Reset Notification " + __version__ + (" · 离线演示" if demo else ""))
        root.geometry("1080x820")
        root.minsize(960, 720)
        root.option_add("*Font", "{Microsoft YaHei UI} 10")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f7fb")
        style.configure("TLabel", background="#f5f7fb", foreground="#1f2937")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Subtitle.TLabel", foreground="#59677d")
        style.configure("Accent.TButton", foreground="white", background="#235cce", padding=(16, 9))
        style.configure("TButton", padding=(10, 6))
        style.configure("Treeview", rowheight=28)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="重置公告监测", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="公共公告 · 本机通知 · 不读取个人额度", style="Subtitle.TLabel").pack(anchor="w", pady=(4, 12))
        credit = ttk.Label(frame, text="Data: codex-reset.com  ↗", foreground="#235cce", cursor="hand2")
        credit.pack(anchor="w")
        credit.bind("<Button-1>", lambda event: webbrowser.open(SOURCE))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=12)
        self.start_button = ttk.Button(actions, text="开始监测", command=self.start, style="Accent.TButton")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止监测", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.mock_button = ttk.Button(actions, text="模拟通知测试", command=self.mock_test)
        self.mock_button.pack(side="left")
        self.real_button = ttk.Button(actions, text="发送真实测试…", command=self.real_test)
        self.real_button.pack(side="left", padx=8)
        ttk.Button(actions, text="用户指南", command=self.guide).pack(side="right")
        self.status = tk.StringVar(value="离线演示：没有网络请求或实际通知" if demo else "已停止。配置完成后，请点击开始监测。")
        ttk.Label(frame, textvariable=self.status, wraplength=990).pack(fill="x", pady=(0, 8))
        self.book = ttk.Notebook(frame)
        self.book.pack(fill="both", expand=True)
        overview, channels, schedule, queue_tab, logs = [ttk.Frame(self.book, padding=14) for _ in range(5)]
        for tab, title in zip((overview, channels, schedule, queue_tab, logs), ("公告与批次", "通知配置", "监测与提醒", "发送队列", "运行日志")):
            self.book.add(tab, text=title)
        self.info = tk.StringVar()
        ttk.Label(overview, textvariable=self.info, wraplength=970).pack(fill="x", pady=(0, 10))
        self.batch_tree = self.tree(overview, ("kind", "stage", "at", "track"), ("类型", "公开证据状态", "证据时间", "定时提醒"), (100, 360, 210, 120), 8)
        self.batch_tree.bind("<<TreeviewSelect>>", self.batch_selected)
        batch_actions = ttk.Frame(overview)
        batch_actions.pack(fill="x", pady=8)
        ttk.Button(batch_actions, text="关注选中批次", command=lambda: self.track(True)).pack(side="left")
        ttk.Button(batch_actions, text="停止该批提醒（本人已核对等）", command=lambda: self.track(False)).pack(side="left", padx=8)
        ttk.Button(batch_actions, text="打开证据原帖", command=self.open_evidence).pack(side="right")
        self.details = tk.Text(overview, height=8, wrap="word", relief="flat", background="white", font=("Microsoft YaHei UI", 10))
        self.details.pack(fill="both", expand=True)
        self.write_text(self.details, "选择一条公告查看来源。首次运行的历史批次默认不推送；可手动关注仍待完成的批次。")
        columns = ttk.Frame(channels)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1)
        columns.columnconfigure(1, weight=1)
        smtp = ttk.LabelFrame(columns, text="邮件 · SMTP", padding=12)
        smtp.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        bark = ttk.LabelFrame(columns, text="可选 Bark", padding=12)
        bark.grid(row=0, column=1, sticky="nsew")
        self.check(smtp, "smtp_enabled", "启用邮件通知", 0)
        self.entry(smtp, "smtp_host", "SMTP 服务器", 1)
        self.entry(smtp, "smtp_port", "SMTP 端口", 2)
        self.entry(smtp, "smtp_tls", "加密方式", 3, choices=("STARTTLS", "SSL/TLS", "无 TLS"))
        self.entry(smtp, "smtp_user", "通知账户／SMTP 用户名", 4)
        self.smtp_password = self.secret_entry(smtp, "密码／应用专用密码", 5)
        self.check(smtp, "smtp_saved", "授权保存到 Windows 凭据管理器", 6)
        self.entry(smtp, "sender", "发件邮箱", 7)
        self.entry(smtp, "recipients", "收件邮箱（分号分隔）", 8)
        ttk.Label(smtp, text="多个收件人逐一发送，彼此看不到其他收件地址。\n建议使用邮箱服务的应用专用密码。", wraplength=420).grid(row=9, column=0, columnspan=2, sticky="w", pady=12)
        self.check(bark, "bark_enabled", "启用 Bark 通知", 0)
        self.entry(bark, "bark_server", "Bark HTTPS 服务器", 1)
        self.bark_key = self.secret_entry(bark, "设备密钥", 2)
        self.check(bark, "bark_saved", "授权保存到 Windows 凭据管理器", 3)
        ttk.Label(bark, text="只有您在本机输入的设备密钥用于推送。\n\n默认使用普通通知；能否发声由 iOS 和 Bark 设置决定，不能保证突破静音或专注模式。\n\n未勾选保存时，凭据仅在当前进程内存保留。已保存凭据的密码框留空会保持原值；取消保存会移除系统保存项。\n\n同一 Windows 用户下的其他程序可能读取通用凭据。配置、日志和队列含邮箱与公告，请勿公开上传运行目录。", wraplength=420, justify="left").grid(row=4, column=0, columnspan=2, sticky="w", pady=14)
        ttk.Label(channels, text="这里无需 OpenAI 或 X 登录。配置只用于通知，不会读取个人额度。", wraplength=970).pack(anchor="w", pady=10)
        self.entry(schedule, "interval", "轮询间隔（秒，最短 60）", 0)
        self.entry(schedule, "timezone", "提醒时区（IANA）", 1, choices=("Etc/UTC", "Asia/Shanghai", "Asia/Tokyo", "America/Los_Angeles", "Europe/London"), editable=True)
        self.check(schedule, "reminders", "公告后至明确完成前，发送额外定时提醒", 2)
        self.entry(schedule, "times", "每日提醒时间（逗号分隔）", 3)
        self.entry(schedule, "reader_contact", "接口识别联系信息", 4)
        self.entry(schedule, "note", "可选方案／账户备注（仅本机）", 5)
        ttk.Label(schedule, text="联系信息使用自己的 https://主页 或 mailto:联系邮箱；会随 User-Agent 提供给公开数据源。\n时区示例 Asia/Shanghai；默认 12:00,18:00。离线错过的旧时刻不会集中补发，只补当前 30 分钟窗口。\n\n关机、睡眠、离线或关闭窗口时，本机程序不能保证监测或提醒。恢复后补查接口仍提供的历史。\n首次成功检查建立基线；历史公告和历史批次的定时提醒均保持安静。\n\n未能关联原帖的到账观察单独显示，不按日期猜测批次；观察不证明您到账。", wraplength=950).grid(row=6, column=0, columnspan=2, sticky="w", pady=18)
        startup = ttk.Frame(schedule)
        startup.grid(row=7, column=0, columnspan=2, sticky="w")
        self.startup_on = ttk.Button(startup, text="启用登录开机启动…", command=lambda: self.startup(True))
        self.startup_on.pack(side="left")
        self.startup_off = ttk.Button(startup, text="移除本软件开机启动", command=lambda: self.startup(False))
        self.startup_off.pack(side="left", padx=8)
        self.startup_status = tk.StringVar(value="未自动设置开机启动；此功能须由您主动选择。")
        ttk.Label(schedule, textvariable=self.startup_status, wraplength=950).grid(row=8, column=0, columnspan=2, sticky="w", pady=10)
        self.queue_tree = self.tree(queue_tab, ("channel", "destination", "status", "tries", "due"), ("通道", "收件目标", "发送状态", "尝试", "下次尝试"), (65, 240, 330, 55, 180), 10)
        self.queue_tree.bind("<<TreeviewSelect>>", self.queue_selected)
        ttk.Button(queue_tab, text="重新排队选中项…", command=self.retry_selected).pack(anchor="w", pady=8)
        self.queue_details = tk.Text(queue_tab, height=7, wrap="word", relief="flat", font=("Microsoft YaHei UI", 10))
        self.queue_details.pack(fill="both", expand=True)
        self.write_text(self.queue_details, "服务器接受不等于进入收件箱或已被阅读。确认丢失时不自动重发；请核对后人工操作。")
        self.log_text = tk.Text(logs, wrap="word", relief="flat", font=("Microsoft YaHei UI", 10))
        self.log_text.pack(fill="both", expand=True)
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(12, 0))
        self.save_button = ttk.Button(footer, text="保存配置", command=self.save, style="Accent.TButton")
        self.save_button.pack(side="left")
        ttk.Button(footer, text="打开本机数据目录", command=lambda: os.startfile(str(directory))).pack(side="left", padx=8)
        ttk.Label(footer, text="退出窗口会停止本机监测。", style="Subtitle.TLabel").pack(side="right")
        if demo:
            for button in (self.start_button, self.real_button, self.startup_on, self.startup_off, self.save_button):
                button.configure(state="disabled")
            self.populate_demo()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = lambda *args: messagebox.showerror("操作失败", "操作未完成；为保护凭据，未显示原始异常。请查看本机日志。", parent=root)
        self.refresh()

    def tree(self, parent, columns, labels, widths, height):
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        tree = ttk.Treeview(holder, columns=columns, show="headings", height=height, selectmode="browse")
        for key, label, width in zip(columns, labels, widths):
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=40, stretch=True)
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        return tree

    def entry(self, parent, key, label, row, choices=None, editable=False):
        value = self.config[key]
        if isinstance(value, list):
            value = ("; " if key == "recipients" else ",").join(value)
        variable = tk.StringVar(value=str(value))
        self.vars[key] = variable
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 10))
        widget = ttk.Combobox(parent, textvariable=variable, values=choices, state="normal" if editable else "readonly") if choices else ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky="ew", pady=6)
        parent.columnconfigure(1, weight=1)
        return widget

    def secret_entry(self, parent, label, row):
        variable = tk.StringVar()
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 10))
        ttk.Entry(parent, textvariable=variable, show="●").grid(row=row, column=1, sticky="ew", pady=6)
        return variable

    def check(self, parent, key, label, row):
        variable = tk.BooleanVar(value=self.config[key])
        self.vars[key] = variable
        ttk.Checkbutton(parent, text=label, variable=variable).grid(row=row, column=0, columnspan=2, sticky="w", pady=8)

    def form_config(self):
        config = {key: variable.get() for key, variable in self.vars.items()}
        try:
            config["interval"], config["smtp_port"] = int(config["interval"]), int(config["smtp_port"])
        except ValueError:
            raise ValueError("间隔和端口须为整数。") from None
        config["recipients"] = [s.strip() for s in config["recipients"].replace(",", ";").split(";") if s.strip()]
        config["times"] = [s.strip() for s in config["times"].split(",") if s.strip()]
        return validate(config)

    def save(self, quiet=False):
        if self.running or not self.done_event.is_set() or self.demo:
            return False
        try:
            config = self.form_config()
            old_values = {key: self.secrets.get(key, self.config) for key in ("smtp", "bark")}
            values = {"smtp": self.smtp_password.get() or old_values["smtp"], "bark": self.bark_key.get() or old_values["bark"]}
            for key, value in values.items():
                if config[key + "_saved"] and not value:
                    raise ValueError("勾选保存凭据前，请先在对应密码框输入凭据。")
                if len(value.encode("utf-16-le")) > 5120:
                    raise ValueError("凭据过长，不能保存到系统凭据管理器。")
            changed = []
            try:
                for key, value in values.items():
                    if value or self.config[key + "_saved"]:
                        self.secrets.set(key, value, config[key + "_saved"])
                        changed.append(key)
                save_config(self.directory, config)
            except (OSError, ValueError):
                for key in reversed(changed):
                    try:
                        self.secrets.set(key, old_values[key], self.config[key + "_saved"])
                    except (OSError, ValueError):
                        pass
                raise
            self.config = config
            self.smtp_password.set("")
            self.bark_key.set("")
            if not config["reminders"]:
                self.store.cancel_reminders(time.time())
            with self.store.lock, self.store.db:
                self.store.log("CONFIG", "配置已保存；凭据未写入配置、日志或队列。", time.time())
            if not quiet:
                self.status.set("配置已保存。凭据仅在内存或已授权的系统凭据存储中。")
            return True
        except ValueError as error:
            messagebox.showerror("配置需要调整", str(error), parent=self.root)
        except OSError:
            messagebox.showerror("保存失败", "无法保存配置或访问系统凭据管理器；未降级为明文保存。", parent=self.root)
        return False

    def start(self):
        if self.demo or not self.save(quiet=True):
            return
        try:
            validate(self.config, require_contact=True)
        except ValueError as error:
            messagebox.showerror("启动需要配置", str(error), parent=self.root)
            return
        self.running = True
        self.stop_event.clear()
        self.done_event.clear()
        self.controls(True)
        config = copy.deepcopy(self.config)
        def run():
            try:
                while not self.stop_event.is_set():
                    self.monitor.tick(config, time.time(), self.stop_event.is_set)
                    self.stop_event.wait(1)
            except Exception:
                with self.store.lock, self.store.db:
                    self.store.log("STOPPED_ERROR", "发生未处理错误，已停止监测；未记录原始异常或凭据。", time.time())
            finally:
                self.stop_event.set()
                self.done_event.set()
        self.worker = threading.Thread(target=run, name="ResetNotice-monitor", daemon=True)
        self.worker.start()

    def controls(self, busy):
        for button in (self.start_button, self.mock_button, self.real_button, self.save_button):
            button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if self.running else "disabled")

    def stop(self):
        self.stop_event.set()
        self.running = False
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止；正在进行的网络请求结束后停止，不开始新的通知。")

    def test_async(self, operation):
        if not self.done_event.is_set():
            return
        self.done_event.clear()
        self.controls(True)
        def run():
            try:
                operation()
            finally:
                self.done_event.set()
        self.worker = threading.Thread(target=run, name="ResetNotice-test", daemon=True)
        self.worker.start()

    def mock_test(self):
        # Only an isolated temporary queue; never changes real pending queue or real credentials.
        def run():
            with tempfile.TemporaryDirectory(prefix="resetnotice-mock-") as directory:
                store = Store(directory)
                config = copy.deepcopy(DEFAULTS)
                config.update(smtp_enabled=True, recipients=["demo@example.invalid"], bark_enabled=True)
                with store.lock, store.db:
                    store.enqueue("mock", None, "模拟通知测试", "离线模拟；Data: codex-reset.com\n" + SOURCE, config, time.time())
                Sender(store, DemoTransport()).drain(config, time.time())
                passed = all(r["status"] == "accepted" for r in store.snapshot()["queue"])
                store.close()
            with self.store.lock, self.store.db:
                self.store.log("MOCK_OK" if passed else "MOCK_FAIL", "模拟通知测试：邮件与 Bark 队列通过；未连接外部通知服务器。", time.time())
        self.test_async(run)

    def real_test(self):
        if self.demo or not self.save(quiet=True):
            return
        targets = self.store.targets(self.config)
        if not targets:
            messagebox.showinfo("未启用通道", "请先启用并保存邮件或 Bark 配置。", parent=self.root)
            return
        summary = "将按当前配置真实发送测试通知。\n邮件收件人：" + "; ".join(self.config["recipients"] if self.config["smtp_enabled"] else [])
        summary += "\nBark：" + (self.config["bark_server"] if self.config["bark_enabled"] else "未启用")
        if not messagebox.askyesno("确认真实发送", summary + "\n\n确认后才会连接通知服务器。继续发送？", parent=self.root):
            return
        def run():
            topic = "test:" + uuid.uuid4().hex
            with self.store.lock, self.store.db:
                before = self.store.db.execute("SELECT COALESCE(MAX(id),0) FROM queue").fetchone()[0]
                self.store.enqueue(topic, None, "通知测试", "您在本机确认发送的测试。服务器接受不代表已收到。\nData: codex-reset.com\n" + SOURCE, self.config, time.time())
                ids = {r[0] for r in self.store.db.execute("SELECT id FROM queue WHERE id>?", (before,))}
            self.sender.drain(self.config, time.time(), only_ids=ids)
        self.test_async(run)

    def time_text(self, seconds):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(float(seconds), ZoneInfo(self.config["timezone"])).strftime("%Y-%m-%d %H:%M:%S") if seconds else "—"

    def refresh(self):
        if self.closing and self.done_event.is_set():
            self.secrets.clear()
            self.store.close()
            self.root.destroy()
            return
        snapshot = self.store.snapshot()
        self.snapshot = snapshot
        meta = snapshot["meta"]
        if self.done_event.is_set() and not self.demo:
            if self.running:
                self.running = False
            self.controls(False)
        if not self.demo:
            state = "监测中" if self.running else ("已停止" if self.done_event.is_set() else "处理中／停止后等待请求结束")
            self.status.set(state + "。" + meta.get("source_warning", ""))
        self.info.set("基线：" + (self.time_text(meta.get("baseline_at")) if meta.get("baseline_at") else "尚未建立")
                      + "   最近检查：" + self.time_text(meta.get("last_fetch"))
                      + "\n下次检查：" + self.time_text(meta.get("next_poll")) + "   提醒时区：" + self.config["timezone"]
                      + "\n来源抓取时间：" + meta.get("source_fetched_at", "—") + "   最新原帖：" + meta.get("source_newest_post_at", "—"))
        batch_selection, queue_selection = self.batch_tree.selection(), self.queue_tree.selection()
        self.batch_tree.delete(*self.batch_tree.get_children())
        for row in snapshot["batches"]:
            self.batch_tree.insert("", "end", iid=row["key"], values=({"global": "全局重置", "banked": "可留存卡", "observation": "第三方观察"}[row["kind"]], STAGES[row["stage"]], self.time_text(row["updated_at"]), "已关注" if row["tracking"] else "停止／基线"))
        self.queue_tree.delete(*self.queue_tree.get_children())
        for row in snapshot["queue"]:
            self.queue_tree.insert("", "end", iid=str(row["id"]), values=(row["channel"].upper(), row["destination"], STATUS[row["status"]], row["tries"], self.time_text(row["due"]) if row["status"] in ("retry", "pending") else "—"))
        for tree, selection in ((self.batch_tree, batch_selection), (self.queue_tree, queue_selection)):
            if selection and tree.exists(selection[0]):
                tree.selection_set(selection[0])
        lines = [self.time_text(row["at"]) + "  [" + row["code"] + "] " + row["message"] for row in snapshot["logs"]]
        self.write_text(self.log_text, "\n\n".join(lines) or "尚无日志。没有启动监测，也没有发送通知。")
        self.root.after(1000, self.refresh)

    def write_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def batch_selected(self, event=None):
        selection = self.batch_tree.selection()
        self.selected_batch = selection[0] if selection else ""
        row = next((r for r in self.snapshot["batches"] if r["key"] == self.selected_batch), None)
        if row:
            self.write_text(self.details, STAGES[row["stage"]] + "\n批次 ID：" + row["key"] + "\n" + row["summary"] + "\n证据：" + row["url"] + "\n公开完成只描述公告，不确认您本人到账。\nData: codex-reset.com\n" + SOURCE)

    def queue_selected(self, event=None):
        selection = self.queue_tree.selection()
        row = next((r for r in self.snapshot["queue"] if selection and str(r["id"]) == selection[0]), None)
        if row:
            self.write_text(self.queue_details, row["subject"] + "\n" + STATUS[row["status"]] + "\n" + row["error"] + "\n\n" + row["body"])

    def track(self, enabled):
        if self.selected_batch:
            if not self.store.set_tracking(self.selected_batch, enabled, time.time()):
                messagebox.showinfo("不能关注", "已公开完成的批次或第三方观察不启动定时提醒。", parent=self.root)

    def open_evidence(self):
        row = next((r for r in self.snapshot["batches"] if r["key"] == self.selected_batch), None)
        if row:
            webbrowser.open(row["url"])

    def retry_selected(self):
        selection = self.queue_tree.selection()
        if selection and messagebox.askyesno("重新排队", "请先核对接收情况。确认丢失的通知可能已经送达，人工重试可能产生重复。\n重新排队后，下一次监测运行才会尝试发送。继续？", parent=self.root):
            self.store.retry(int(selection[0]), time.time())

    def startup(self, enabled):
        if self.demo:
            return
        if enabled:
            if not self.save(quiet=True):
                return
            try:
                validate(self.config, require_contact=True)
            except ValueError as error:
                messagebox.showerror("先完成配置", str(error), parent=self.root)
                return
            if not messagebox.askyesno("主动启用开机启动", "这会在当前 Windows 用户登录后打开本程序并开始本机监测。\n需要的凭据必须已授权保存，否则通知会进入受阻队列。\n电脑睡眠或离线仍可能错过及时提醒。是否设置？", parent=self.root):
                return
        try:
            change_startup(enabled, self.directory)
            self.startup_status.set("已由您启用登录启动并监测。" if enabled else "已移除本软件的开机启动项。")
        except OSError:
            messagebox.showerror("设置失败", "无法修改本软件的当前用户启动项。", parent=self.root)

    def guide(self):
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
        path = base / "中文用户指南.html"
        if path.exists():
            webbrowser.open(path.resolve().as_uri())
        else:
            messagebox.showinfo("用户指南", "请阅读发布包内的 README.md 或 docs/中文用户指南.md。", parent=self.root)

    def close(self):
        if not self.done_event.is_set() and not messagebox.askyesno("停止并退出", "退出会停止本机监测。正在进行的请求可能需等待最多约 40 秒；已经开始的发送不能撤回。\n是否停止并退出？", parent=self.root):
            return
        self.stop_event.set()
        self.running, self.closing = False, True
        self.controls(True)
        self.status.set("正在退出，等待正在进行的请求结束。")

    def populate_demo(self):
        now = time.time()
        config = copy.deepcopy(DEFAULTS)
        config.update(smtp_enabled=True, recipients=["demo@example.invalid"])
        if not self.store.meta("baseline_at"):
            self.store.ingest([], config, now - 3600)
            sample = [Notice("900000000000000001", "900000000000000001", "banked", "announced", now - 600,
                             "离线演示：宣布发放一张可留存重置卡。", SOURCE + "banked-reset"),
                      Notice("900000000000000002", "900000000000000001", "banked", "arriving", now - 300,
                             "离线演示：同一批次发放中，尚未确认完成。", SOURCE + "banked-reset"),
                      Notice("900000000000000003", "900000000000000003", "observation", "observed", now - 100,
                             "离线演示：第三方账户观察，不代表您到账。", SOURCE + "banked-reset")]
            self.store.ingest(sample, config, now)
            Sender(self.store, DemoTransport()).drain(config, now)


def main():
    parser = argparse.ArgumentParser(description="Codex Reset Notification 中文桌面公告监测；普通启动不会自动监测。")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--start-monitor", action="store_true", help="仅供用户选择的启动入口")
    parser.add_argument("--demo", action="store_true", help="隔离离线演示；不使用真实通知或凭据")
    parser.add_argument("--smoke-report", type=Path, help="隐藏窗口进行 GUI 自检；需 --demo 与 --data-dir")
    args = parser.parse_args()
    if args.smoke_report and (args.data_dir is None or args.start_monitor):
        parser.error("--smoke-report requires --data-dir and forbids --start-monitor")
    directory = args.data_dir or (Path(tempfile.mkdtemp(prefix="resetnotice-demo-")) if args.demo else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ResetNotice")
    directory = directory.resolve()
    root = tk.Tk()
    if args.smoke_report:
        root.withdraw()
    mutex = None
    try:
        mutex = InstanceLock(directory)
        app = App(root, directory, args.demo)
        if args.smoke_report:
            root.update_idletasks()
            root.update()
            def descendants(widget):
                return [widget] + [item for child in widget.winfo_children() for item in descendants(child)]
            widgets = descendants(root)
            report = {"ok": True, "frozen": bool(getattr(sys, "frozen", False)), "demo": args.demo,
                      "monitoring": app.running, "tabs": len(app.book.tabs()),
                      "widgets": len(widgets), "batches": len(app.store.snapshot()["batches"]),
                      "queue": len(app.store.snapshot()["queue"]), "tk_version": str(tk.TkVersion),
                      "credentials_backend": "memory only" if args.demo else "Windows own namespace; no credential read/write", "network_requests": 0,
                      "startup_changes": 0}
            args.smoke_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            app.store.close()
            root.destroy()
            return
        if args.start_monitor and not args.demo:
            root.after(300, app.start)
        root.mainloop()
    except (ValueError, OSError) as error:
        if args.smoke_report:
            args.smoke_report.write_text(json.dumps({"ok": False, "error_type": type(error).__name__}), encoding="utf-8")
        else:
            messagebox.showerror("启动失败", str(error) if isinstance(error, ValueError) else "无法访问本机运行目录或系统功能。", parent=root)
        root.destroy()
        raise SystemExit(1)
    finally:
        if mutex:
            mutex.close()


if __name__ == "__main__":
    main()
