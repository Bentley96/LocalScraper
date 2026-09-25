#!/usr/bin/env python3
"""Site Collector — a desktop window around collect.py.

Pick a job folder, paste the websites, press Start. Everything is saved in the
job folder (sites.txt, sites/<domain>/..., report.csv, collect.log), exactly as
the command-line tool does, so a job folder can be committed to its own repo.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import collect

APP_NAME = "Site Collector"
SYSTEM = platform.system()

STATUS_LABELS = {
    "ok": "OK",
    "not_collected": "Not collected yet",
    "collecting": "Collecting…",
    "maintenance": "Maintenance / coming soon",
    "password_protected": "Password protected",
    "redirect_offsite": "Redirects to another domain",
    "blocked": "Blocked by firewall",
    "suspended": "Hosting suspended",
    "http_error": "HTTP error",
    "dns_error": "Domain not found (DNS)",
    "ssl_error": "SSL certificate problem",
    "timeout": "Timed out",
    "connection_error": "Could not connect",
    "redirect_loop": "Redirect loop",
    "error": "Unexpected error",
}
FAILED_STATUSES = {"dns_error", "ssl_error", "timeout", "connection_error", "redirect_loop", "error"}
MODES = {
    "new": "Collect new sites only (skip ones already collected)",
    "retry": "Retry sites that had problems",
    "all": "Re-collect every site",
}


# --------------------------------------------------------------------------- helpers


def config_path() -> Path:
    if SYSTEM == "Darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif SYSTEM == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "site-collector"
    return base / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_path(path: Path) -> None:
    """Open a file or folder with the system's default app (Finder, Explorer...)."""
    if SYSTEM == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif SYSTEM == "Windows":
        os.startfile(str(path))  # noqa: S606 - opening a local path the user asked for
    else:
        subprocess.Popen(["xdg-open", str(path)])


def friendly_notes(warnings: str) -> str:
    """'images_failed: 1 of 20 images…' -> '1 of 20 images…' (the status column already says the rest)."""
    notes = []
    for w in warnings.split(" | "):
        if not w or w.startswith("insecure"):
            continue
        text = w.split(": ", 1)[1] if ": " in w else w
        notes.append(text.replace("try --render", "try Render in Chrome"))
    return "; ".join(notes)


class QueueLogHandler(logging.Handler):
    def __init__(self, events: queue.Queue):
        super().__init__(logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(("log", self.format(record)))


def self_test(out_file: str | None, require_browser: bool) -> int:
    """Used by the build to check the packaged app has everything it needs."""
    lines = [f"{APP_NAME} self-test", f"python {sys.version.split()[0]} on {platform.platform()}"]
    ok = True
    try:
        import importlib

        import requests
        for module in ("bs4", "lxml.etree", "playwright.sync_api"):
            importlib.import_module(module)
        from playwright._impl._driver import compute_driver_executable
        driver = compute_driver_executable()
        driver_path = Path(driver[0] if isinstance(driver, tuple) else driver)
        lines.append(f"playwright driver: {driver_path} exists={driver_path.exists()}")
        ok = driver_path.exists()
        lines.append(f"CA bundle: {requests.certs.where()} exists={Path(requests.certs.where()).exists()}")
    except Exception:  # noqa: BLE001
        ok = False
        lines.append(traceback.format_exc())
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = collect.launch_browser(p)
            page = browser.new_page()
            page.set_content("<h1>render ok</h1>")
            lines.append(f"browser: {browser.browser_type.name} {browser.version} -> {page.inner_text('h1')}")
            browser.close()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"browser: not available ({exc})")
        ok = ok and not require_browser
    lines.append("RESULT: " + ("OK" if ok else "FAILED"))
    text = "\n".join(lines) + "\n"
    if out_file:
        Path(out_file).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if ok else 1


# --------------------------------------------------------------------------- the window


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.job_dir: Path | None = None
        self.rows: dict[str, dict] = {}

        root.title(APP_NAME)
        root.minsize(900, 620)
        root.geometry(self.settings.get("geometry", "1100x760"))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        style = ttk.Style()
        if SYSTEM == "Linux":
            style.theme_use("clam")
        style.configure("Heading.TLabel", font=("TkDefaultFont", 13, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Big.TButton", padding=(18, 6))

        self.job_var = tk.StringVar()
        self.mode_var = tk.StringVar(value=self.settings.get("mode", "new"))
        self.test_var = tk.BooleanVar(value=self.settings.get("test_run", True))
        self.test_count_var = tk.IntVar(value=self.settings.get("test_count", 3))
        self.render_var = tk.BooleanVar(value=self.settings.get("render", False))
        self.progress_var = tk.StringVar(value="Choose a job folder to get started.")
        self.summary_var = tk.StringVar()
        self.count_var = tk.StringVar(value="0 websites")

        self._build()
        job = self.settings.get("job_folder")
        if job and Path(job).is_dir():
            self.load_job(Path(job))
        self.update_buttons()
        root.after(100, self.poll_events)

    # ---- layout

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # Job folder
        job = ttk.Frame(outer)
        job.grid(row=0, column=0, sticky="ew")
        job.columnconfigure(1, weight=1)
        ttk.Label(job, text="Job folder", style="Heading.TLabel").grid(row=0, column=0, padx=(0, 10))
        ttk.Entry(job, textvariable=self.job_var, state="readonly").grid(row=0, column=1, sticky="ew")
        self.choose_btn = ttk.Button(job, text="Choose…", command=self.choose_job)
        self.choose_btn.grid(row=0, column=2, padx=(8, 0))
        self.open_job_btn = ttk.Button(job, text="Open in " + ("Finder" if SYSTEM == "Darwin" else "file browser"),
                                       command=lambda: self.job_dir and open_path(self.job_dir))
        self.open_job_btn.grid(row=0, column=3, padx=(8, 0))
        ttk.Label(job, style="Hint.TLabel",
                  text="Each job gets its own folder. The website list, collected sites and report are saved there."
                  ).grid(row=1, column=1, columnspan=3, sticky="w", pady=(4, 0))

        # Websites + options
        top = ttk.Frame(outer)
        top.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        top.columnconfigure(0, weight=1)

        sites = ttk.LabelFrame(top, text="Websites — one per line", padding=8)
        sites.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        sites.columnconfigure(0, weight=1)
        self.sites_text = tk.Text(sites, height=9, width=40, wrap="none", undo=True, font=("TkFixedFont", 11),
                                  relief="solid", borderwidth=1)
        self.sites_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(sites, orient="vertical", command=self.sites_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.sites_text.configure(yscrollcommand=scroll.set)
        self.sites_text.bind("<<Modified>>", self.on_sites_modified)
        self.sites_text.bind("<FocusOut>", lambda e: None if self.running else self.refresh_table())
        row = ttk.Frame(sites)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(row, textvariable=self.count_var, style="Hint.TLabel").pack(side="left")
        self.import_btn = ttk.Button(row, text="Import list…", command=self.import_list)
        self.import_btn.pack(side="right")

        opts = ttk.LabelFrame(top, text="Options", padding=8)
        opts.grid(row=0, column=1, sticky="nsew")
        self.option_widgets = []
        for i, (key, label) in enumerate(MODES.items()):
            rb = ttk.Radiobutton(opts, text=label, value=key, variable=self.mode_var)
            rb.grid(row=i, column=0, columnspan=3, sticky="w")
            self.option_widgets.append(rb)
        ttk.Separator(opts).grid(row=3, column=0, columnspan=3, sticky="ew", pady=6)
        test_row = ttk.Frame(opts)
        test_row.grid(row=4, column=0, columnspan=3, sticky="w")
        cb = ttk.Checkbutton(test_row, text="Test run — only the first", variable=self.test_var)
        cb.pack(side="left")
        sb = ttk.Spinbox(test_row, from_=1, to=999, width=4, textvariable=self.test_count_var)
        sb.pack(side="left", padx=4)
        ttk.Label(test_row, text="sites").pack(side="left")
        cb2 = ttk.Checkbutton(opts, text="Render pages in Chrome (slower — for sites with missing content)",
                              variable=self.render_var)
        cb2.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.option_widgets += [cb, sb, cb2]

        buttons = ttk.Frame(opts)
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.start_btn = ttk.Button(buttons, text="▶  Start", style="Big.TButton", command=self.start_clicked)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="■  Stop", command=self.stop_clicked)
        self.stop_btn.pack(side="left", padx=8)

        # Results / log
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        results = ttk.Frame(self.tabs, padding=6)
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        self.tabs.add(results, text="Results")

        columns = ("site", "status", "sections", "images", "failed", "forms", "notes")
        self.table = ttk.Treeview(results, columns=columns, show="headings", selectmode="extended")
        headings = {"site": ("Website", 210), "status": ("Status", 205), "sections": ("Sections", 78),
                    "images": ("Images", 80), "failed": ("Failed", 60), "forms": ("Forms", 60),
                    "notes": ("Notes", 360)}
        for col, (text, width) in headings.items():
            self.table.heading(col, text=text)
            anchor = "center" if col in ("sections", "images", "failed", "forms") else "w"
            self.table.column(col, width=width, anchor=anchor, stretch=col == "notes")
        self.table.grid(row=0, column=0, sticky="nsew")
        tscroll = ttk.Scrollbar(results, orient="vertical", command=self.table.yview)
        tscroll.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=tscroll.set)
        self.table.tag_configure("ok", foreground="#1a7f37")
        self.table.tag_configure("warn", foreground="#b35900")
        self.table.tag_configure("bad", foreground="#c62828")
        self.table.tag_configure("todo", foreground="#777777")
        self.table.tag_configure("busy", foreground="#0b57d0")
        self.table.bind("<Double-1>", lambda e: self.open_selected_folder())
        self.table.bind("<<TreeviewSelect>>", lambda e: self.update_buttons())
        self.table.bind("<Button-3>" if SYSTEM != "Darwin" else "<Button-2>", self.show_menu)
        if SYSTEM == "Darwin":
            self.table.bind("<Control-Button-1>", self.show_menu)

        actions = ttk.Frame(results)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.row_buttons = [
            ttk.Button(actions, text="Open folder", command=self.open_selected_folder),
            ttk.Button(actions, text="View saved page", command=self.open_saved_page),
            ttk.Button(actions, text="Open live site", command=self.open_live_site),
            ttk.Button(actions, text="Collect again", command=lambda: self.run_selected(render=False)),
            ttk.Button(actions, text="Render in Chrome", command=lambda: self.run_selected(render=True)),
        ]
        for b in self.row_buttons:
            b.pack(side="left", padx=(0, 6))
        self.report_btn = ttk.Button(actions, text="Open report.csv", command=self.open_report)
        self.report_btn.pack(side="right")

        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="Open folder", command=self.open_selected_folder)
        self.menu.add_command(label="View saved page", command=self.open_saved_page)
        self.menu.add_command(label="Open live site", command=self.open_live_site)
        self.menu.add_separator()
        self.menu.add_command(label="Collect again", command=lambda: self.run_selected(render=False))
        self.menu.add_command(label="Render in Chrome", command=lambda: self.run_selected(render=True))

        log_frame = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(log_frame, text="Log")
        self.log_text = ScrolledText(log_frame, height=10, state="disabled", font=("TkFixedFont", 10))
        self.log_text.pack(fill="both", expand=True)

        # Progress
        bottom = ttk.Frame(outer)
        bottom.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(bottom, textvariable=self.progress_var).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(bottom, textvariable=self.summary_var, style="Hint.TLabel").grid(row=1, column=1, sticky="e", pady=(4, 0))

    # ---- job folder & website list

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def choose_job(self) -> None:
        start = self.job_dir or Path.home() / "Documents"
        folder = filedialog.askdirectory(title="Choose or create a job folder", initialdir=str(start), mustexist=False)
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            self.load_job(path)

    def load_job(self, path: Path) -> None:
        self.job_dir = path
        self.job_var.set(str(path))
        self.settings["job_folder"] = str(path)
        save_settings(self.settings)
        sites_file = path / "sites.txt"
        text = sites_file.read_text(encoding="utf-8") if sites_file.exists() else ""
        self.sites_text.delete("1.0", "end")
        self.sites_text.insert("1.0", text)
        self.sites_text.edit_modified(True)
        self.refresh_table()
        self.progress_var.set("Ready." if text.strip() else "Add the websites to collect, then press Start.")
        self.update_buttons()

    def import_list(self) -> None:
        name = filedialog.askopenfilename(title="Import a list of websites",
                                          filetypes=[("Text or CSV", "*.txt *.csv"), ("All files", "*")])
        if not name:
            return
        raw = Path(name).read_text(encoding="utf-8", errors="replace")
        lines = [cell.strip() for line in raw.splitlines() for cell in line.split(",")[:1]]
        found = [collect.normalize_domain(l) for l in lines if "." in l and not l.startswith("#")]
        existing = self.domains_in_box()
        added = [d for d in collect.unique(found) if d not in existing]
        if added:
            current = self.sites_text.get("1.0", "end").rstrip("\n")
            self.sites_text.insert("end", ("\n" if current else "") + "\n".join(added) + "\n")
        messagebox.showinfo(APP_NAME, f"Added {len(added)} website(s).")

    def domains_in_box(self) -> list[str]:
        domains = []
        for line in self.sites_text.get("1.0", "end").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                d = collect.normalize_domain(line)
                if d and d not in domains:
                    domains.append(d)
        return domains

    def on_sites_modified(self, _event=None) -> None:
        if self.sites_text.edit_modified():
            n = len(self.domains_in_box())
            self.count_var.set(f"{n} website{'s' if n != 1 else ''}")
            self.sites_text.edit_modified(False)

    def save_sites(self) -> list[str]:
        assert self.job_dir is not None
        text = self.sites_text.get("1.0", "end").rstrip() + "\n"
        (self.job_dir / "sites.txt").write_text(text, encoding="utf-8")
        return collect.read_sites(self.job_dir / "sites.txt")

    # ---- results table

    def refresh_table(self) -> None:
        if not self.job_dir:
            return
        rows = collect.build_report_rows(self.job_dir / "sites", self.domains_in_box())
        self.table.delete(*self.table.get_children())
        self.rows = {}
        for row in rows:
            self.set_row(row)
        self.update_summary()

    def set_row(self, row: dict) -> None:
        domain, status = row["domain"], row["status"] or "not_collected"
        self.rows[domain] = row
        if status == "ok":
            tag = "ok"
        elif status == "not_collected":
            tag = "todo"
        elif status == "collecting":
            tag = "busy"
        elif status in FAILED_STATUSES:
            tag = "bad"
        else:
            tag = "warn"
        collected = status not in ("not_collected", "collecting")
        notes = friendly_notes(row.get("warnings") or "")
        values = (
            domain,
            STATUS_LABELS.get(status, status),
            row.get("sections", "") if collected else "",
            f"{row.get('images_downloaded', 0)} / {row.get('images_found', 0)}" if collected else "",
            row.get("images_failed", "") if collected and row.get("images_failed") else "",
            row.get("forms", "") if collected else "",
            notes,
        )
        if self.table.exists(domain):
            self.table.item(domain, values=values, tags=(tag,))
        else:
            self.table.insert("", "end", iid=domain, values=values, tags=(tag,))

    def update_summary(self) -> None:
        rows = list(self.rows.values())
        done = [r for r in rows if r["status"] not in ("not_collected", "collecting")]
        ok = sum(r["status"] == "ok" for r in rows)
        attention = len(done) - ok
        self.summary_var.set(
            f"{len(rows)} sites · {len(done)} collected · {ok} OK" + (f" · {attention} need attention" if attention else "")
        )

    def selected_domains(self) -> list[str]:
        return list(self.table.selection())

    def show_menu(self, event) -> None:
        row = self.table.identify_row(event.y)
        if row:
            if row not in self.table.selection():
                self.table.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def site_dir(self, domain: str) -> Path:
        assert self.job_dir is not None
        return self.job_dir / "sites" / collect.dir_name(domain)

    def open_selected_folder(self) -> None:
        for domain in self.selected_domains()[:5]:
            path = self.site_dir(domain)
            if path.exists():
                open_path(path)

    def open_saved_page(self) -> None:
        for domain in self.selected_domains()[:5]:
            folder = self.site_dir(domain)
            page = folder / "rendered.html" if (folder / "rendered.html").exists() else folder / "index.html"
            if page.exists():
                webbrowser.open(page.resolve().as_uri())

    def open_live_site(self) -> None:
        for domain in self.selected_domains()[:5]:
            webbrowser.open(f"https://{domain}/")

    def open_report(self) -> None:
        if self.job_dir and (self.job_dir / "report.csv").exists():
            open_path(self.job_dir / "report.csv")

    # ---- running

    def update_buttons(self) -> None:
        busy = self.running
        has_job = self.job_dir is not None
        has_selection = bool(self.table.selection())
        self.start_btn.configure(state="normal" if has_job and not busy else "disabled")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        for w in (self.choose_btn, self.import_btn, *self.option_widgets):
            w.configure(state="disabled" if busy else "normal")
        self.sites_text.configure(state="disabled" if busy else "normal")
        self.open_job_btn.configure(state="normal" if has_job else "disabled")
        self.report_btn.configure(state="normal" if has_job and (self.job_dir / "report.csv").exists() else "disabled")
        for i, b in enumerate(self.row_buttons):
            enabled = has_selection and (i < 3 or not busy)
            b.configure(state="normal" if enabled else "disabled")
        for i in (4, 5):
            self.menu.entryconfigure(i, state="disabled" if busy else "normal")

    def start_clicked(self) -> None:
        if not self.job_dir:
            return
        domains = self.save_sites()
        if not domains:
            messagebox.showwarning(APP_NAME, "Add at least one website (one per line) first.")
            return
        selected = domains
        if self.test_var.get():
            try:
                selected = domains[: max(1, int(self.test_count_var.get()))]
            except (tk.TclError, ValueError):
                selected = domains[:3]
        mode = self.mode_var.get()
        opts = collect.Options(force=mode == "all", retry_failed=mode == "retry", render=self.render_var.get())
        if opts.render and len(selected) > 10 and not messagebox.askyesno(
            APP_NAME, f"Rendering all {len(selected)} sites in Chrome will be slow and re-collect every one.\n\n"
                      "Usually you only render the few sites that look incomplete (select them in the table and "
                      "press “Render in Chrome”).\n\nRender all of them anyway?"):
            return
        self.start_run(selected, domains, opts)

    def run_selected(self, render: bool) -> None:
        if self.running or not self.job_dir:
            return
        chosen = self.selected_domains()
        if not chosen:
            return
        domains = self.save_sites()
        self.start_run(chosen, collect.unique(domains + chosen), collect.Options(force=True, render=render))

    def start_run(self, selected: list[str], all_domains: list[str], opts: collect.Options) -> None:
        assert self.job_dir is not None
        self.refresh_table()
        self.persist_options()
        self.stop_event.clear()
        collect.setup_logging(self.job_dir / "collect.log", console=False)
        collect.log.addHandler(QueueLogHandler(self.events))
        self.progress.configure(maximum=len(selected), value=0)
        self.progress_var.set(f"Starting — {len(selected)} site(s)…")
        self.append_log(f"\n=== Run started: {len(selected)} site(s) ===")
        job = self.job_dir

        def work():
            try:
                summary = collect.run_collection(
                    selected, all_domains, job / "sites", job / "report.csv", opts,
                    on_start=lambda i, n, d: self.events.put(("start", i, n, d)),
                    on_done=lambda i, n, d, m, skipped: self.events.put(("done", i, n, d, skipped)),
                    stop=self.stop_event,
                )
                self.events.put(("finished", summary))
            except Exception:  # noqa: BLE001
                self.events.put(("crashed", traceback.format_exc()))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.update_buttons()

    def stop_clicked(self) -> None:
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.progress_var.set("Stopping after the current site…")

    def poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.append_log(event[1])
                elif kind == "start":
                    _, i, n, domain = event
                    self.progress_var.set(f"Collecting {i} of {n}: {domain}")
                    self.set_row({**self.rows.get(domain, {"domain": domain}), "domain": domain, "status": "collecting"})
                    if self.table.exists(domain):
                        self.table.see(domain)
                elif kind == "done":
                    _, i, n, domain, skipped = event
                    self.progress.configure(value=i)
                    row = collect.build_report_rows(self.job_dir / "sites", [domain], include_extras=False)[0]
                    self.set_row(row)
                    self.update_summary()
                elif kind == "finished":
                    self.run_finished(event[1])
                elif kind == "crashed":
                    self.append_log(event[1])
                    self.progress_var.set("Something went wrong — see the Log tab.")
                    self.update_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def run_finished(self, summary: collect.RunSummary) -> None:
        self.worker = None
        self.refresh_table()
        problems = [r for r in summary.rows if r["status"] not in ("ok", "not_collected")]
        low = [r["domain"] for r in summary.rows if "low_content" in (r["warnings"] or "")]
        verb = "Stopped" if summary.stopped else "Finished"
        msg = f"{verb}: collected {summary.collected} site(s)"
        if summary.skipped:
            msg += f", skipped {summary.skipped} already collected"
        self.progress_var.set(msg + ".")
        self.update_buttons()
        details = [msg + "."]
        if problems:
            details.append(f"{len(problems)} site(s) need attention — they're shown in orange/red in the table.")
        if low:
            details.append("These look like they may be missing content. Select them and press "
                           "“Render in Chrome”:\n  " + "\n  ".join(low[:10]))
        self.root.bell()
        messagebox.showinfo(APP_NAME, "\n\n".join(details))

    def append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def persist_options(self) -> None:
        self.settings.update(mode=self.mode_var.get(), test_run=self.test_var.get(), render=self.render_var.get())
        try:
            self.settings["test_count"] = int(self.test_count_var.get())
        except (tk.TclError, ValueError):
            pass
        save_settings(self.settings)

    def on_close(self) -> None:
        if self.running and not messagebox.askyesno(APP_NAME, "A collection is running. Stop it and quit?"):
            return
        self.stop_event.set()
        self.settings["geometry"] = self.root.geometry()
        self.persist_options()
        self.root.destroy()


def main() -> int:
    if "--self-test" in sys.argv:
        i = sys.argv.index("--self-test")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("--") else None
        return self_test(out, "--require-browser" in sys.argv)
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
