import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import ttk

from .api import ApiClient, ApiError
from .config import load_config, save_config
from .state import read_runtime, read_worker_status, running_pid, tail_log
from .wallet import caps as compute_caps, caps_usage, read_wallet, roll_periods

POLL_MS = 2000
MODES = ("off", "ondemand", "lend")


def _cli_argv(arguments):
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, "-m", "primal.cli", *arguments]


def _age(value):
    if not value:
        return "never"
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    except (ValueError, TypeError):
        return str(value)
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


class DesktopApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PrimalSpace")
        self.geometry("840x620")
        self.minsize(760, 560)
        self._vars = {}
        self._peers = {}
        self._auto_var = tk.BooleanVar(value=True)
        self._build()
        self._refresh()
        self.after(POLL_MS, self._poll)

    def _var(self, name):
        if name not in self._vars:
            self._vars[name] = tk.StringVar(value="")
        return self._vars[name]

    def _build(self):
        style = ttk.Style(self)
        try:
            style.configure("Title.TLabel", font=("TkDefaultFont", 16, "bold"))
            style.configure("Hint.TLabel", foreground="#666666")
            style.configure("Value.TLabel", font=("TkDefaultFont", 10, "bold"))
        except tk.TclError:
            pass

        header = ttk.Frame(self, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="PrimalSpace", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self._var("device"), style="Hint.TLabel").pack(
            side="left", padx=(12, 0)
        )
        ttk.Label(header, textvariable=self._var("monitor"), style="Hint.TLabel").pack(
            side="right"
        )

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self._notebook = notebook
        notebook.bind("<<NotebookTabChanged>>", self._on_tab)
        self._build_overview(notebook)
        self._build_history(notebook)
        self._build_settings(notebook)
        self._build_messages(notebook)
        self._build_agent(notebook)
        self._build_logs(notebook)

        footer = ttk.Frame(self, padding=(16, 6))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self._var("status"), style="Hint.TLabel").pack(
            side="left"
        )

    def _tab(self, title):
        frame = ttk.Frame(self._notebook, padding=14)
        self._notebook.add(frame, text=title)
        return frame

    def _build_overview(self, notebook):
        frame = self._tab("Overview")
        grid = ttk.Frame(frame)
        grid.pack(fill="x", anchor="n")
        rows = (
            ("Server", "server"),
            ("Device", "device_id"),
            ("Compute mode", "mode"),
            ("Heartbeat", "heartbeat"),
            ("Worker", "worker"),
            ("Wallet", "wallet"),
            ("Caps today", "caps"),
            ("Last task", "task"),
        )
        for index, (label, key) in enumerate(rows):
            ttk.Label(grid, text=label, style="Hint.TLabel").grid(
                row=index, column=0, sticky="w", pady=3
            )
            ttk.Label(grid, textvariable=self._var(key), style="Value.TLabel").grid(
                row=index, column=1, sticky="w", padx=(18, 0), pady=3
            )
        grid.columnconfigure(1, weight=1)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(18, 0))
        buttons = (
            ("Connect", ["connect"]),
            ("Disconnect", ["disconnect"]),
            ("Start worker", ["worker", "start"]),
            ("Stop worker", ["worker", "stop"]),
            ("Pause", ["worker", "pause"]),
            ("Resume", ["worker", "resume"]),
        )
        for index, (label, arguments) in enumerate(buttons):
            ttk.Button(
                actions,
                text=label,
                command=lambda a=arguments: self._run(a),
            ).grid(row=0, column=index, padx=(0, 8), pady=4, sticky="w")

        ttk.Label(
            frame,
            text="Connect registers this machine and starts the heartbeat daemon. "
            "The worker only claims tasks when compute is allowed.",
            style="Hint.TLabel",
            wraplength=700,
        ).pack(fill="x", pady=(18, 0))

    def _build_history(self, notebook):
        frame = self._tab("History")
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Job and task history from the monitor").pack(side="left")
        ttk.Button(toolbar, text="Refresh", command=self._refresh_history).pack(side="right")
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, pady=(10, 0))
        self._history = tk.Text(body, height=18, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(body, command=self._history.yview)
        self._history.configure(yscrollcommand=scroll.set)
        self._history.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_settings(self, notebook):
        frame = self._tab("Settings")
        form = ttk.Frame(frame)
        form.pack(fill="x", anchor="n")

        ttk.Label(form, text="Compute mode").grid(row=0, column=0, sticky="w", pady=4)
        mode = ttk.Combobox(
            form, textvariable=self._var("mode_input"), values=MODES, width=16, state="readonly"
        )
        mode.grid(row=0, column=1, sticky="w", padx=(16, 0))

        ttk.Label(form, text="Auto-lend on start").grid(row=1, column=0, sticky="w", pady=4)
        auto = ttk.Checkbutton(form, variable=self._auto)
        auto.grid(row=1, column=1, sticky="w", padx=(16, 0))

        labels = (
            ("Daily cap (minutes, 0 = unlimited)", "cap_day"),
            ("Weekly cap (minutes)", "cap_week"),
            ("Monthly cap (minutes)", "cap_month"),
            ("Profile name", "profile_name"),
            ("Profile status", "profile_status"),
            ("Profile avatar", "profile_avatar"),
        )
        for offset, (label, key) in enumerate(labels, start=2):
            ttk.Label(form, text=label).grid(row=offset, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=self._var(key), width=34).grid(
                row=offset, column=1, sticky="w", padx=(16, 0)
            )

        ttk.Button(frame, text="Save settings", command=self._save_settings).pack(
            anchor="w", pady=(16, 0)
        )

    def _build_messages(self, notebook):
        frame = self._tab("Messages")
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="To").pack(side="left")
        self._peer_box = ttk.Combobox(
            toolbar, textvariable=self._var("message_to"), width=30, state="readonly"
        )
        self._peer_box.pack(side="left", padx=(8, 8))
        ttk.Button(toolbar, text="Refresh", command=self._refresh_messages).pack(side="right")

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, pady=(10, 0))
        self._messages = tk.Text(body, height=16, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(body, command=self._messages.yview)
        self._messages.configure(yscrollcommand=scroll.set)
        self._messages.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        composer = ttk.Frame(frame)
        composer.pack(fill="x", pady=(10, 0))
        ttk.Entry(composer, textvariable=self._var("message_text")).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(composer, text="Send", command=self._send_message).pack(
            side="left", padx=(8, 0)
        )

    def _build_logs(self, notebook):
        frame = self._tab("Logs")
        self._log = tk.Text(frame, height=18, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(frame, command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        self._log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Button(frame, text="Refresh", command=self._refresh_logs).pack(
            side="bottom", anchor="w", pady=(8, 0)
        )

    def _build_agent(self, notebook):
        frame = self._tab("Agent")
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Button(top, text="Continue with ChatGPT", command=self._agent_login).pack(side="left")
        ttk.Button(top, text="Refresh", command=self._refresh_agent).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Start agent", command=lambda: self._run(["agent", "start"])).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(top, text="Stop agent", command=lambda: self._run(["agent", "stop"])).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(top, text="Run once", command=lambda: self._run(["agent", "run"])).pack(
            side="left", padx=(8, 0)
        )

        form = ttk.Frame(frame)
        form.pack(fill="x", pady=(14, 0))
        ttk.Label(form, text="Agent name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self._var("agent_name"), width=32).grid(
            row=0, column=1, sticky="w", padx=(16, 0)
        )
        ttk.Label(form, text="Model (blank = account default)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self._var("agent_model"), width=32).grid(
            row=1, column=1, sticky="w", padx=(16, 0)
        )
        ttk.Label(form, text="Reply to every group message").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Checkbutton(form, variable=self._agent_reply_all).grid(row=2, column=1, sticky="w", padx=(16, 0))
        ttk.Button(form, text="Save agent settings", command=self._save_agent).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

        status = ttk.Label(frame, textvariable=self._var("agent_status"), style="Hint.TLabel", wraplength=700)
        status.pack(fill="x", pady=(14, 0))

        ttk.Label(frame, text="Group chats", style="Value.TLabel").pack(anchor="w", pady=(16, 0))
        self._groups = tk.Text(frame, height=8, wrap="word", state="disabled")
        self._groups.pack(fill="both", expand=True, pady=(6, 0))

    @property
    def _agent_reply_all(self):
        if not hasattr(self, "_agent_reply_var"):
            self._agent_reply_var = tk.BooleanVar(value=False)
        return self._agent_reply_var

    def _agent_login(self):
        from .chatgpt import login
        from .agent import agent_settings, set_agent_setting

        name = self._var("agent_name").get().strip() or agent_settings()["name"]
        self._var("status").set("waiting for the ChatGPT sign-in in your browser")
        self._log_write(f"Signing in with ChatGPT as '{name}' ...")

        def worker():
            try:
                record = login(agent_name=name, on_url=lambda url: self.after(0, self._log_write, url))
            except Exception as error:
                self.after(0, self._var("status").set, f"sign-in failed: {error}")
                return
            try:
                set_agent_setting("agent_name", name)
            except Exception:
                pass
            self.after(0, self._var("status").set, f"signed in as {record.get('email') or 'ChatGPT account'}")
            self.after(0, self._refresh_agent)

        threading.Thread(target=worker, daemon=True).start()

    def _save_agent(self):
        from .agent import set_agent_setting

        try:
            set_agent_setting("agent_name", self._var("agent_name").get().strip()[:48])
            set_agent_setting("agent_model", self._var("agent_model").get().strip()[:64])
            set_agent_setting("agent_reply_all", bool(self._agent_reply_all.get()))
        except Exception as error:
            self._var("status").set(f"could not save: {error}")
            return
        self._var("status").set("agent settings saved")
        self._refresh_agent()

    def _refresh_agent(self):
        from .agent import agent_settings, read_agent_state
        from .chatgpt import status as chatgpt_status

        settings = agent_settings()
        account = chatgpt_status()
        self._var("agent_name").set(settings["name"])
        self._var("agent_model").set(settings["model"])
        self._agent_reply_all.set(settings["reply_all"])
        if account.get("connected"):
            text = (
                f"ChatGPT connected as {account.get('email') or 'unknown account'}"
                f" · client {str(account.get('client_id'))[:18]}..."
                f" · expires {account.get('expires_at')}"
            )
        else:
            text = "Not signed in with ChatGPT yet."
        state = read_agent_state()
        text += f" · replies sent: {state.get('replies', 0)} · last run: {state.get('last_run') or 'never'}"
        self._var("agent_status").set(text)
        self._load_groups()

    def _load_groups(self):
        try:
            config = load_config()
            if not config.get("device_id") or not config.get("token"):
                self._set_groups("Connect first to load group chats.")
                return
            client = ApiClient(config["server_url"], token=config.get("token"))
            groups = client.get_groups(config["device_id"]).get("groups") or []
        except (ApiError, OSError, ValueError) as error:
            self._set_groups(f"Could not load groups: {error}")
            return
        if not groups:
            self._set_groups("No group chats yet.")
            return
        lines = []
        for group in groups:
            names = ", ".join(
                member.get("name") or member.get("id", "")[:8] for member in group.get("members") or []
            )
            lines.append(f"{group.get('name')}  ·  {names}")
        self._set_groups("\n".join(lines))

    def _set_groups(self, text):
        self._groups.configure(state="normal")
        self._groups.delete("1.0", "end")
        self._groups.insert("end", text + "\n")
        self._groups.configure(state="disabled")

    @property
    def _auto(self):
        return self._auto_var

    def _log_write(self, text):
        self._log.configure(state="normal")
        self._log.insert("end", text.rstrip() + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _on_tab(self, _event=None):
        try:
            current = self._notebook.tab(self._notebook.select(), "text")
        except tk.TclError:
            return
        if current == "Messages":
            self._refresh_messages()
        elif current == "History":
            self._refresh_history()
        elif current == "Agent":
            self._refresh_agent()
        elif current == "Logs":
            self._refresh_logs()
        elif current == "Settings":
            self._load_settings()

    def _refresh(self):
        try:
            config = load_config()
        except RuntimeError as error:
            self._var("status").set(str(error))
            return
        runtime = read_runtime()
        worker = read_worker_status()
        wallet = roll_periods(read_wallet())
        limits = compute_caps(config)
        usage = caps_usage(wallet)
        heartbeat_pid = running_pid("heartbeat")
        worker_pid = running_pid("worker")

        self._var("server").set(config.get("server_url") or "-")
        self._var("device").set(config.get("profile_name") or config.get("device_name") or "unregistered")
        self._var("device_id").set(config.get("device_id") or "not registered")
        self._var("mode").set(config.get("compute_mode") or "off")
        heartbeat = "running" if heartbeat_pid else "stopped"
        last_ok = runtime.get("last_heartbeat_ok")
        if last_ok:
            heartbeat += f" · {_age(last_ok)}"
        if runtime.get("last_error"):
            heartbeat += f" · {runtime['last_error']}"
        self._var("heartbeat").set(heartbeat)
        worker_text = worker.get("status") or ("running" if worker_pid else "stopped")
        current = worker.get("current_task_id")
        if current:
            worker_text += f" · task {current[:8]}"
        if worker.get("last_error"):
            worker_text += f" · {worker['last_error']}"
        self._var("worker").set(worker_text)
        self._var("wallet").set(
            f"{wallet.get('balance', 0.0):.4f} CU · earned {wallet.get('earned_total', 0.0):.4f}"
            f" · sent {wallet.get('spent_total', 0.0):.4f}"
        )
        self._var("caps").set(
            f"{usage['day_used']:.0f}/{limits['day_minutes'] or '∞'} min"
            f" · week {usage['week_used']:.0f}/{limits['week_minutes'] or '∞'}"
            f" · month {usage['month_used']:.0f}/{limits['month_minutes'] or '∞'}"
        )
        last = worker.get("last_result") or {}
        if last:
            task_text = f"{str(last.get('task_id') or '')[:8]} {last.get('status') or ''}".strip()
            if last.get("verified") is not None:
                task_text += f" · verified={last.get('verified')}"
            if last.get("value") is not None:
                task_text += f" · {last.get('value')}"
        else:
            task_text = "-"
        self._var("task").set(task_text)
        self._var("monitor").set(
            "monitor connected" if last_ok else "monitor not connected"
        )

    def _poll(self):
        self._refresh()
        self.after(POLL_MS, self._poll)

    def _load_settings(self):
        config = load_config()
        self._var("mode_input").set(config.get("compute_mode") or "off")
        self._auto.set(bool(config.get("auto_lend_on_start", True)))
        for key, field in (
            ("cap_day", "max_compute_minutes_day"),
            ("cap_week", "max_compute_minutes_week"),
            ("cap_month", "max_compute_minutes_month"),
        ):
            self._var(key).set(str(int(config.get(field) or 0)))
        for key, field in (
            ("profile_name", "profile_name"),
            ("profile_status", "profile_status"),
            ("profile_avatar", "profile_avatar"),
        ):
            self._var(key).set(str(config.get(field) or ""))

    def _save_settings(self):
        try:
            config = load_config()
            config["compute_mode"] = self._var("mode_input").get() or "off"
            config["auto_lend_on_start"] = bool(self._auto.get())
            for key, field in (
                ("cap_day", "max_compute_minutes_day"),
                ("cap_week", "max_compute_minutes_week"),
                ("cap_month", "max_compute_minutes_month"),
            ):
                raw = self._var(key).get().strip() or "0"
                config[field] = max(0, int(raw))
            config["profile_name"] = self._var("profile_name").get().strip()[:48]
            config["profile_status"] = self._var("profile_status").get().strip()[:120]
            config["profile_avatar"] = self._var("profile_avatar").get().strip()[:8]
        except ValueError:
            self._var("status").set("caps must be whole minutes")
            return
        save_config(config)
        self._var("status").set("settings saved")
        self._refresh()

    def _run(self, arguments):
        if self._busy:
            self._var("status").set("a command is already running")
            return
        self._busy = True
        self._var("status").set("running: primal " + " ".join(arguments))
        thread = threading.Thread(target=self._run_worker, args=(arguments,), daemon=True)
        thread.start()

    def _run_worker(self, arguments):
        try:
            completed = subprocess.run(
                _cli_argv(arguments),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode != 0:
                output = output or f"command failed with exit code {completed.returncode}"
        except (OSError, subprocess.SubprocessError) as error:
            output = f"command failed: {error}"
        self.after(0, self._run_done, arguments, output)

    def _run_done(self, arguments, output):
        self._busy = False
        self._log_write("$ primal " + " ".join(arguments))
        if output:
            self._log_write(output)
        self._var("status").set("primal " + " ".join(arguments) + " finished")
        self._refresh()

    def _peers(self, config):
        client = ApiClient(config["server_url"], token=config.get("token"))
        payload = client.get_peers(config["device_id"])
        return payload.get("peers") or []

    def _refresh_history(self):
        config = load_config()
        if not config.get("device_id") or not config.get("token"):
            self._set_history("Device is not registered yet. Connect from the Overview tab.")
            return
        thread = threading.Thread(target=self._load_history, args=(config,), daemon=True)
        thread.start()

    def _load_history(self, config):
        try:
            client = ApiClient(config["server_url"], token=config.get("token"))
            payload = client.get_history(config["device_id"], limit=50)
        except (ApiError, OSError, ValueError) as error:
            self.after(0, self._set_history, f"Could not load history: {error}")
            return
        summary = payload.get("summary") or {}
        lines = [
            f"{summary.get('jobs', 0)} job(s) · {summary.get('completed', 0)} completed · "
            f"{summary.get('failed', 0)} failed · {summary.get('active', 0)} active"
        ]
        jobs = payload.get("jobs") or []
        for job in jobs:
            progress = job.get("progress") or {}
            lines.append(
                f"{job.get('created_at', '')}  {job.get('workload', '')} job "
                f"{str(job.get('id', ''))[:8]}  {job.get('status', '')}  "
                f"{progress.get('completed', 0)}/{progress.get('total', 0)} shards"
            )
            for shard in job.get("shards") or []:
                detail = shard.get("error") or shard.get("value")
                lines.append(
                    f"    shard {int(shard.get('index', 0)) + 1} · {shard.get('status', '')}"
                    + (f" · {detail}" if detail is not None else "")
                )
        standalone = [task for task in payload.get("tasks") or [] if not task.get("job_id")]
        for task in standalone:
            lines.append(
                f"{task.get('created_at', '')}  {task.get('workload', '')} task "
                f"{str(task.get('id', ''))[:8]}  {task.get('status', '')}"
            )
        self.after(0, self._set_history, "\n".join(lines) or "No history yet.")

    def _set_history(self, text):
        self._history.configure(state="normal")
        self._history.delete("1.0", "end")
        self._history.insert("end", text + "\n")
        self._history.configure(state="disabled")

    def _refresh_messages(self):
        config = load_config()
        if not config.get("device_id") or not config.get("token"):
            self._set_messages("Device is not registered yet. Connect from the Overview tab.")
            return
        thread = threading.Thread(target=self._load_messages, args=(config,), daemon=True)
        thread.start()

    def _load_messages(self, config):
        try:
            client = ApiClient(config["server_url"], token=config.get("token"))
            payload = client.get_messages(config["device_id"])
            peers = client.get_peers(config["device_id"]).get("peers") or []
        except (ApiError, OSError, ValueError) as error:
            self.after(0, self._set_messages, f"Could not load messages: {error}")
            return
        self._peers = {"all": None}
        for peer in peers:
            if peer.get("id") != config["device_id"]:
                label = peer.get("profile_name") or peer.get("name") or peer.get("id")
                self._peers[f"{label} ({peer.get('id', '')[:8]})"] = peer.get("id")
        messages = payload.get("messages") or []
        lines = []
        for message in messages[-40:]:
            sender = message.get("from") or "operator"
            target = message.get("to") or "all"
            kind = message.get("kind") or message.get("topic") or "chat"
            credit = message.get("credit")
            suffix = ""
            if isinstance(credit, dict) and credit.get("amount") is not None:
                suffix = f" [{credit.get('kind', 'credit')} {float(credit.get('amount') or 0):.4f} CU]"
            lines.append(f"{message.get('created_at', '')}  {sender} -> {target}  ({kind}){suffix}")
            lines.append(f"    {message.get('body', '')}")
        self.after(0, self._set_messages, "\n".join(lines) or "Inbox is empty.")

    def _set_messages(self, text):
        self._messages.configure(state="normal")
        self._messages.delete("1.0", "end")
        self._messages.insert("end", text + "\n")
        self._messages.configure(state="disabled")
        labels = list(self._peers) or ["all"]
        self._peer_box.configure(values=labels)
        if not self._var("message_to").get():
            self._var("message_to").set(labels[0])

    def _send_message(self):
        text = self._var("message_text").get().strip()
        if not text:
            return
        config = load_config()
        if not config.get("device_id") or not config.get("token"):
            self._var("status").set("connect this device first")
            return
        label = self._var("message_to").get() or "all"
        target = self._peers.get(label)
        thread = threading.Thread(
            target=self._send_worker, args=(config, target, text), daemon=True
        )
        thread.start()

    def _send_worker(self, config, target, text):
        try:
            client = ApiClient(config["server_url"], token=config.get("token"))
            client.send_message(config["device_id"], target, text)
        except (ApiError, OSError, ValueError) as error:
            self.after(0, self._var("status").set, f"send failed: {error}")
            return
        self.after(0, self._var("status").set, "message sent")
        self.after(0, self._var("message_text").set, "")
        self.after(300, self._refresh_messages)

    def _refresh_logs(self):
        lines = []
        for name in ("heartbeat", "worker"):
            lines.append(f"== {name} ==")
            lines.extend(tail_log(name, 40) or ["(no log yet)"])
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.insert("end", "\n".join(lines) + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")


def main():
    try:
        app = DesktopApp()
    except tk.TclError as error:
        print(f"could not open desktop window: {error}", file=sys.stderr)
        return 1
    app.mainloop()
    return 0
