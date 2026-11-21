"""tkinter UI for M-Disc Archiver."""
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import burner
import planner
import scanner
from catalog import Catalog


def gb(n):
    return f"{n / 1e9:,.1f} GB"


class App:
    def __init__(self, root, catalog_path):
        self.root = root
        self.catalog_path = catalog_path
        self.cat = Catalog(catalog_path)          # used only on the UI thread
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.drives = []

        root.title("M-Disc Archiver")
        root.geometry("980x680")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Burner:").pack(side="left")
        self.drive_var = tk.StringVar()
        self.drive_box = ttk.Combobox(top, textvariable=self.drive_var, state="readonly", width=55)
        self.drive_box.pack(side="left", padx=6)
        ttk.Button(top, text="Refresh drives", command=self.refresh_drives).pack(side="left")
        self.stats_var = tk.StringVar()
        ttk.Label(top, textvariable=self.stats_var, foreground="#444").pack(side="right")

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=10)
        self._build_archive(nb)
        self._build_search(nb)
        self._build_discs(nb)
        self._build_sources(nb)
        self._build_settings(nb)

        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, padding=(10, 4, 10, 0)).pack(fill="x")
        bottom = ttk.Frame(root, padding=(10, 6))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.refresh_all()
        if burner.IS_WINDOWS:
            self.refresh_drives()
        else:
            self.log("Burning is only available on Windows. Scanning, planning and search still work.")
        root.after(100, self.poll)

    # ---------------------------------------------------------------- tabs
    def _build_archive(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Archive")

        steps = ttk.Frame(f)
        steps.pack(fill="x")
        self.btn_scan = ttk.Button(steps, text="1. Scan for new files", command=self.do_scan)
        self.btn_plan = ttk.Button(steps, text="2. Plan next disc", command=self.do_plan)
        self.btn_burn = ttk.Button(steps, text="3. Burn disc", command=self.do_burn)
        self.btn_verify = ttk.Button(steps, text="4. Verify disc", command=self.do_verify_planned)
        for b in (self.btn_scan, self.btn_plan, self.btn_burn, self.btn_verify):
            b.pack(side="left", padx=(0, 6))

        card = ttk.LabelFrame(f, text="Next disc", padding=10)
        card.pack(fill="x", pady=10)
        self.disc_label_var = tk.StringVar(value="—")
        ttk.Label(card, textvariable=self.disc_label_var, font=("Segoe UI", 32, "bold")).pack(side="left", padx=(0, 20))
        info = ttk.Frame(card)
        info.pack(side="left", fill="x", expand=True)
        self.disc_info_var = tk.StringVar(value="No disc planned.")
        ttk.Label(info, textvariable=self.disc_info_var, justify="left").pack(anchor="w")
        alt = ttk.Frame(info)
        alt.pack(anchor="w", pady=(8, 0))
        self.btn_cancel_plan = ttk.Button(alt, text="Cancel plan", command=self.do_cancel_plan)
        self.btn_iso = ttk.Button(alt, text="Save ISO instead…", command=self.do_iso)
        self.btn_folder = ttk.Button(alt, text="Export folder instead…", command=self.do_folder)
        self.btn_mark = ttk.Button(alt, text="Mark burned (burned elsewhere)", command=self.do_mark_burned)
        for b in (self.btn_cancel_plan, self.btn_iso, self.btn_folder, self.btn_mark):
            b.pack(side="left", padx=(0, 6))

        self.log_text = tk.Text(f, height=14, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _build_search(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Search")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.search_var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.search_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _: self.do_search())
        ttk.Button(row, text="Search", command=self.do_search).pack(side="left", padx=6)
        cols = ("path", "size", "disc", "on_disc_as")
        self.search_tree = ttk.Treeview(f, columns=cols, show="headings")
        for c, w, t in zip(cols, (430, 90, 90, 330), ("File", "Size", "Disc", "Path on disc")):
            self.search_tree.heading(c, text=t)
            self.search_tree.column(c, width=w, anchor="w")
        self.search_tree.pack(fill="both", expand=True, pady=(8, 0))

    def _build_discs(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Discs")
        cols = ("label", "status", "files", "size", "created", "burned", "verified")
        self.disc_tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, (90, 80, 70, 90, 150, 150, 150)):
            self.disc_tree.heading(c, text=c.capitalize())
            self.disc_tree.column(c, width=w, anchor="w")
        self.disc_tree.pack(fill="both", expand=True)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Verify selected disc", command=self.do_verify_selected).pack(side="left")
        ttk.Button(row, text="Export catalog.csv", command=self.do_export_csv).pack(side="left", padx=6)

    def _build_sources(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Sources")
        ttk.Label(f, text="Folders to back up (photos, videos, ...):").pack(anchor="w")
        self.src_list = tk.Listbox(f, height=10)
        self.src_list.pack(fill="both", expand=True, pady=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Add folder…", command=self.do_add_source).pack(side="left")
        ttk.Button(row, text="Remove selected", command=self.do_remove_source).pack(side="left", padx=6)

    def _build_settings(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Settings")
        self.set_vars = {}
        fields = [
            ("capacity_bytes", "Disc capacity (bytes). 100 GB BDXL = 100103356416"),
            ("reserve_bytes", "Space kept free for the index and filesystem (bytes)"),
            ("excludes", "Skip files/folders matching (comma separated)"),
        ]
        for i, (key, text) in enumerate(fields):
            ttk.Label(f, text=text).grid(row=i, column=0, sticky="w", pady=4)
            v = tk.StringVar(value=self.cat.get(key))
            ttk.Entry(f, textvariable=v, width=50).grid(row=i, column=1, sticky="w", padx=8)
            self.set_vars[key] = v
        self.verify_var = tk.BooleanVar(value=self.cat.get("verify_after_burn") == "1")
        ttk.Checkbutton(f, text="Verify every file after burning", variable=self.verify_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(f, text="Save settings", command=self.do_save_settings).grid(row=4, column=0, sticky="w", pady=8)
        ttk.Label(f, text=f"Catalog: {self.catalog_path}").grid(row=5, column=0, columnspan=2, sticky="w", pady=(16, 0))
        ttk.Button(f, text="Open catalog folder", command=self.open_catalog_folder).grid(row=6, column=0, sticky="w")
        ttk.Label(f, text="Keep a copy of this folder in another backup too (cloud, external drive).",
                  foreground="#666").grid(row=7, column=0, columnspan=2, sticky="w")

    # ------------------------------------------------------------- helpers
    def log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def selected_drive(self):
        i = self.drive_box.current()
        if i < 0 or i >= len(self.drives):
            messagebox.showwarning("No burner", "Choose a burner drive at the top of the window first.")
            return None
        return self.drives[i]

    def refresh_all(self):
        s = self.cat.stats()
        usable = self.cat.usable_bytes()
        need = -(-s["pending_bytes"] // usable) if s["pending_bytes"] else 0
        self.stats_var.set(f"On disc: {s['archived_files']:,} files ({gb(s['archived_bytes'])}) on {s['discs']} discs   |   "
                           f"Not yet backed up: {s['pending_files']:,} files ({gb(s['pending_bytes'])}, ~{need} disc(s))")
        d = self.cat.planned_disc()
        if d:
            self.disc_label_var.set(d["label"])
            n = len(self.cat.disc_contents(d["id"]))
            self.disc_info_var.set(f"{n:,} files, {gb(d['bytes_used'])}.\nWrite \"{d['label']}\" on the disc after burning.")
        else:
            self.disc_label_var.set("—")
            self.disc_info_var.set("No disc planned. Scan, then click 'Plan next disc'.")
        self.disc_tree.delete(*self.disc_tree.get_children())
        for r in self.cat.discs():
            self.disc_tree.insert("", "end", iid=str(r["id"]), values=(
                r["label"], r["status"], r["file_count"], gb(r["bytes_used"]),
                r["created_at"] or "", r["burned_at"] or "", r["verified_at"] or ""))
        self.src_list.delete(0, "end")
        self.sources = self.cat.active_sources()
        for s in self.sources:
            self.src_list.insert("end", f"{s['label']}   —   {s['path']}")
        self.update_buttons()

    def update_buttons(self):
        planned = self.cat.planned_disc() is not None
        win = burner.IS_WINDOWS

        def st(b, ok):
            b.configure(state="normal" if ok and not self.busy else "disabled")
        st(self.btn_scan, True)
        st(self.btn_plan, not planned)
        st(self.btn_burn, planned and win)
        st(self.btn_iso, planned and win)
        st(self.btn_folder, planned)
        st(self.btn_mark, planned)
        st(self.btn_cancel_plan, planned)
        st(self.btn_verify, win)
        self.cancel_btn.configure(state="normal" if self.busy else "disabled")

    # --------------------------------------------------- background jobs
    def run_job(self, title, fn):
        """Run fn(cat, emit) on a worker thread with its own DB connection."""
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        self.progress.configure(mode="determinate", value=0)
        self.update_buttons()
        self.log(f"--- {title} ---")

        def worker():
            cat = Catalog(self.catalog_path)
            try:
                fn(cat, lambda kind, payload=None: self.events.put((kind, payload)))
                self.events.put(("done", None))
            except Exception as e:  # report every failure in the UI
                self.events.put(("error", str(e)))
            finally:
                cat.close()
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "progress":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=payload)
                elif kind == "status":
                    text, pct = payload
                    self.status_var.set(text)
                    if pct is None:
                        if str(self.progress["mode"]) != "indeterminate":
                            self.progress.configure(mode="indeterminate")
                            self.progress.start(15)
                    else:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=pct)
                elif kind == "spin":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(15)
                elif kind == "info":
                    messagebox.showinfo("M-Disc Archiver", payload)
                elif kind == "drives":
                    self.drives = payload
                    self.drive_box["values"] = [f"{d.get('volume') or '?'}  {d.get('name', '')}" for d in payload]
                    if payload and self.drive_box.current() < 0:
                        self.drive_box.current(0)
                elif kind in ("done", "error"):
                    self.busy = False
                    self.status_var.set("")
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0 if kind == "error" else 100)
                    if kind == "error":
                        self.log("ERROR: " + payload)
                        messagebox.showerror("M-Disc Archiver", payload)
                    self.refresh_all()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    @staticmethod
    def ps_event_handler(emit):
        def handle(ev):
            t = ev.get("type")
            if t == "progress":
                emit("progress", ev.get("pct", 0))
            elif t == "stage":
                emit("progress", ev["done"] * 100 // max(ev["total"], 1))
            elif t == "phase":
                emit("log", ev.get("msg", ""))
                if ev.get("spin"):
                    emit("spin")
            elif t in ("log", "error"):
                emit("log", ev.get("msg", ""))
        return handle

    # ------------------------------------------------------------ actions
    def refresh_drives(self):
        def job(cat, emit):
            drives = burner.list_drives()
            emit("drives", drives)
            emit("log", f"Found {len(drives)} burner(s).")
        self.run_job("Looking for burners", job)

    def do_scan(self):
        if not self.cat.active_sources():
            messagebox.showinfo("No sources", "Add your photo/video folders on the Sources tab first.")
            return

        def job(cat, emit):
            emit("spin")
            new, errors = scanner.scan(cat, log=lambda m: emit("log", m), cancel=self.cancel,
                                       progress=lambda text, pct: emit("status", (text, pct)))
            emit("log", f"Scan done: {new} new or changed file(s), {errors} unreadable.")
        self.run_job("Scanning", job)

    def do_plan(self):
        pending = self.cat.pending_files()
        if not pending:
            messagebox.showinfo("Nothing to do", "Everything is already backed up. Run Scan to look for new files.")
            return
        chosen, used, oversized = planner.plan(pending, self.cat.usable_bytes())
        for f in oversized:
            self.log(f"Too big for one disc, skipped: {f['source_label']}/{f['rel_path']} ({gb(f['size'])})")
        if not chosen:
            messagebox.showwarning("Nothing fits", "No pending file fits on a disc.")
            return
        d = self.cat.create_disc(chosen, used)
        self.log(f"Planned {d['label']}: {len(chosen):,} files, {gb(used)}. "
                 f"{len(pending) - len(chosen) - len(oversized):,} files left for later discs.")
        self.refresh_all()

    def do_cancel_plan(self):
        d = self.cat.planned_disc()
        if d and messagebox.askyesno("Cancel plan", f"Discard the plan for {d['label']}? (Nothing has been burned.)"):
            self.cat.cancel_plan(d["id"])
            self.refresh_all()

    def do_burn(self):
        d = self.cat.planned_disc()
        drive = self.selected_drive()
        if not d or not drive:
            return
        if not messagebox.askokcancel("Burn", f"Insert a blank disc in {drive.get('volume')} and click OK to burn {d['label']}."):
            return
        verify_after = self.cat.get("verify_after_burn") == "1"

        def job(cat, emit):
            log = lambda m: emit("log", m)
            manifest, index_dir = burner.prepare(cat, d, log)
            burner.burn(manifest, index_dir, d["label"], drive["id"], self.ps_event_handler(emit))
            cat.set_status(d["id"], "burned")
            cat.export_csv(os.path.join(os.path.dirname(cat.path), "catalog.csv"))
            log(f"{d['label']} burned. Write \"{d['label']}\" on the disc.")
            if verify_after:
                self._verify_job(cat, emit, cat.disc(d["id"]), drive, reload=True)
        self.run_job(f"Burning {d['label']}", job)

    def _verify_job(self, cat, emit, d, drive, reload=False):
        log = lambda m: emit("log", m)
        volume = drive.get("volume")
        if not volume:
            raise RuntimeError("The drive has no drive letter, so the disc can't be read back.")
        if reload:
            log("Ejecting and reloading the disc so Windows reads it fresh...")
            try:
                burner.eject_and_reload(drive["id"], self.ps_event_handler(emit))
            except RuntimeError as e:
                log(f"Eject/reload failed: {e}")
            emit("spin")
            if not burner.wait_for_disc(volume, d["label"]):
                emit("info", f"Couldn't read {d['label']} yet. Reinsert the disc, then click "
                             "'Verify selected disc' on the Discs tab.")
                return
        emit("progress", 0)
        problems = burner.verify(cat, d, volume, log, progress=lambda p: emit("progress", p), cancel=self.cancel)
        if problems:
            for p in problems[:50]:
                log("  " + p)
            cat.set_status(d["id"], "failed")
            emit("info", f"{d['label']} FAILED verification ({len(problems)} problem(s)). Its files will go "
                         "on a future disc. Mark this disc as bad.")
        else:
            cat.set_status(d["id"], "verified")
            cat.export_csv(os.path.join(os.path.dirname(cat.path), "catalog.csv"))
            emit("info", f"{d['label']} verified: every file matches.")

    def do_verify_planned(self):
        # "Verify" on the Archive tab: the most recent burned-but-unverified disc.
        rows = [r for r in self.cat.discs() if r["status"] == "burned"]
        if not rows:
            messagebox.showinfo("Verify", "No burned, unverified disc. Use the Discs tab to re-verify any disc.")
            return
        self._start_verify(rows[-1])

    def do_verify_selected(self):
        sel = self.disc_tree.selection()
        if not sel:
            messagebox.showinfo("Verify", "Select a disc in the list first.")
            return
        d = self.cat.disc(int(sel[0]))
        if d["status"] == "planned":
            messagebox.showinfo("Verify", "That disc hasn't been burned yet.")
            return
        self._start_verify(d)

    def _start_verify(self, d):
        drive = self.selected_drive()
        if not drive:
            return
        if not messagebox.askokcancel("Verify", f"Insert disc {d['label']} in {drive.get('volume')} and click OK."):
            return
        self.run_job(f"Verifying {d['label']}", lambda cat, emit: self._verify_job(cat, emit, d, drive))

    def do_iso(self):
        d = self.cat.planned_disc()
        out = filedialog.asksaveasfilename(defaultextension=".iso", initialfile=f"{d['label']}.iso",
                                           filetypes=[("Disc image", "*.iso")])
        if not out:
            return

        def job(cat, emit):
            manifest, index_dir = burner.prepare(cat, d, lambda m: emit("log", m))
            burner.make_iso(manifest, index_dir, d["label"], out, self.ps_event_handler(emit))
            emit("info", f"Saved {out}.\nBurn it with ImgBurn or Windows Disc Image Burner, then click "
                         "'Mark burned' and verify the disc.")
        self.run_job(f"Saving ISO for {d['label']}", job)

    def do_folder(self):
        d = self.cat.planned_disc()
        dest = filedialog.askdirectory(title="Folder to copy the disc's files into (needs up to 100 GB free)")
        if not dest:
            return

        def job(cat, emit):
            log = lambda m: emit("log", m)
            _, index_dir = burner.prepare(cat, d, log)
            emit("spin")
            out = burner.export_folder(cat, d, index_dir, dest, log, self.cancel)
            emit("info", f"Copied to {out}.\nBurn the CONTENTS of that folder as a UDF disc labelled {d['label']} "
                         "(e.g. ImgBurn 'Write files/folders to disc'), then click 'Mark burned' and verify.")
        self.run_job(f"Exporting {d['label']}", job)

    def do_mark_burned(self):
        d = self.cat.planned_disc()
        if d and messagebox.askyesno("Mark burned", f"Did you burn {d['label']} with another program?"):
            self.cat.set_status(d["id"], "burned")
            self.cat.export_csv(os.path.join(os.path.dirname(self.cat.path), "catalog.csv"))
            self.log(f"{d['label']} marked as burned. Click 'Verify disc' to check it.")
            self.refresh_all()

    def do_search(self):
        self.search_tree.delete(*self.search_tree.get_children())
        term = self.search_var.get().strip()
        if not term:
            return
        for r in self.cat.search(term):
            name = r["path"] + ("" if r["present"] else "   (old version)")
            self.search_tree.insert("", "end", values=(
                name, gb(r["size"]) if r["size"] >= 1e8 else f"{r['size'] / 1e6:,.1f} MB",
                r["disc_label"] or "not on disc yet", r["disc_path"] or ""))

    def do_export_csv(self):
        out = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="catalog.csv")
        if out:
            self.cat.export_csv(out)
            self.log(f"Exported {out}")

    def do_add_source(self):
        path = filedialog.askdirectory(title="Choose a folder to back up")
        if path:
            self.cat.add_source(path)
            self.refresh_all()

    def do_remove_source(self):
        sel = self.src_list.curselection()
        if sel and messagebox.askyesno("Remove", "Stop backing up this folder? Discs already burned are kept in the catalog."):
            self.cat.remove_source(self.sources[sel[0]]["id"])
            self.refresh_all()

    def do_save_settings(self):
        try:
            for key in ("capacity_bytes", "reserve_bytes"):
                int(self.set_vars[key].get())
        except ValueError:
            messagebox.showerror("Settings", "Capacity and reserve must be whole numbers of bytes.")
            return
        for key, v in self.set_vars.items():
            self.cat.set(key, v.get().strip())
        self.cat.set("verify_after_burn", "1" if self.verify_var.get() else "0")
        self.log("Settings saved.")
        self.refresh_all()

    def open_catalog_folder(self):
        folder = os.path.dirname(os.path.abspath(self.catalog_path))
        if burner.IS_WINDOWS:
            os.startfile(folder)
        else:
            subprocess.run(["open", folder])

    def on_close(self):
        if self.busy and not messagebox.askyesno("Quit", "A job is running. Quit anyway? (Never quit during a burn.)"):
            return
        self.root.destroy()


def main(catalog_path):
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, catalog_path)
    root.mainloop()
