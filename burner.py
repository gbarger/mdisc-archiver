"""Prepare a planned disc, burn it through Windows IMAPI2 (burn_windows.ps1), and verify it."""
import json
import os
import shutil
import subprocess
import sys
import time

from scanner import sha256_file

HERE = os.path.dirname(os.path.abspath(__file__))
PS1 = os.path.join(HERE, "burn_windows.ps1")
IS_WINDOWS = sys.platform == "win32"

DISC_README = """This disc was written by M-Disc Archiver.

Your files are stored as ordinary files and folders, so any computer can read them.

The _index folder holds the catalog of every disc burned so far, including this one:
  catalog.sqlite  - full catalog (open it with M-Disc Archiver or any SQLite viewer)
  catalog.csv     - the same list as a spreadsheet: disc_label, path_on_disc, size, sha256
To find a file, search catalog.csv for its name. The disc_label column tells you
which numbered disc it is on.
"""


def work_dir(cat, disc):
    return os.path.join(os.path.dirname(os.path.abspath(cat.path)), "work", disc["label"])


def prepare(cat, disc, log=print):
    """Check that the source files are unchanged, then write manifest.tsv and the _index folder.
    Returns (manifest_path, index_dir)."""
    contents = cat.disc_contents(disc["id"])
    problems = []
    for r in contents:
        src = os.path.join(r["source_path"], *r["rel_path"].split("/"))
        try:
            st = os.stat(src)
            if st.st_size != r["size"] or st.st_mtime_ns // 1_000_000_000 != r["mtime"]:
                problems.append(f"changed: {src}")
        except OSError:
            problems.append(f"missing: {src}")
    if problems:
        for p in problems[:20]:
            log("  " + p)
        raise RuntimeError(f"{len(problems)} planned file(s) changed or went missing since the plan was made. "
                           "Click 'Cancel plan', run Scan again, then plan a new disc.")

    wd = work_dir(cat, disc)
    index_dir = os.path.join(wd, "_index")
    os.makedirs(index_dir, exist_ok=True)
    cat.snapshot(os.path.join(index_dir, "catalog.sqlite"), burning_disc_id=disc["id"])
    cat.export_csv(os.path.join(index_dir, "catalog.csv"), extra_disc_id=disc["id"])
    with open(os.path.join(index_dir, "DISC_LABEL.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"{disc['label']}\n{len(contents)} files\n")
    with open(os.path.join(index_dir, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(DISC_README)

    manifest = os.path.join(wd, "manifest.tsv")
    with open(manifest, "w", encoding="utf-8") as fh:
        for r in contents:
            src = os.path.join(r["source_path"], *r["rel_path"].split("/"))
            fh.write(r["disc_path"].replace("/", "\\") + "\t" + src + "\n")
    log(f"Prepared {disc['label']}: {len(contents)} files, index written to {index_dir}")
    return manifest, index_dir


def run_ps(args, on_event):
    """Run burn_windows.ps1 and pass each JSON line it prints to on_event(dict)."""
    if not IS_WINDOWS:
        raise RuntimeError("Burning needs Windows (it uses the built-in IMAPI2 burning service).")
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", PS1] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    error = None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip().lstrip("\ufeff")
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            ev = {"type": "log", "msg": line}
        if ev.get("type") == "error":
            error = ev.get("msg")
        on_event(ev)
    proc.wait()
    if error or proc.returncode != 0:
        raise RuntimeError(error or f"PowerShell exited with code {proc.returncode}")


def list_drives():
    drives = []
    run_ps(["-Action", "list"], lambda ev: ev.get("type") == "drive" and drives.append(ev))
    return drives


def burn(manifest, index_dir, label, drive_id, on_event):
    run_ps(["-Action", "burn", "-Drive", drive_id, "-Manifest", manifest,
            "-IndexDir", index_dir, "-Label", label], on_event)


def make_iso(manifest, index_dir, label, out_path, on_event):
    run_ps(["-Action", "iso", "-Manifest", manifest, "-IndexDir", index_dir,
            "-Label", label, "-Out", out_path], on_event)


def eject_and_reload(drive_id, on_event):
    run_ps(["-Action", "eject", "-Drive", drive_id], on_event)


def export_folder(cat, disc, index_dir, dest, log=print, cancel=None):
    """Copy the disc's files into a folder, for burning with another program (e.g. ImgBurn)."""
    root = os.path.join(dest, disc["label"])
    contents = cat.disc_contents(disc["id"])
    for i, r in enumerate(contents, 1):
        if cancel and cancel.is_set():
            raise InterruptedError("cancelled")
        src = os.path.join(r["source_path"], *r["rel_path"].split("/"))
        out = os.path.join(root, *r["disc_path"].split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(src, out)
        if i % 200 == 0:
            log(f"  copied {i}/{len(contents)}")
    shutil.copytree(index_dir, os.path.join(root, "_index"), dirs_exist_ok=True)
    return root


def wait_for_disc(volume, label, timeout=180):
    """Wait until the disc with this label is readable at volume (e.g. 'E:\\')."""
    marker = os.path.join(volume, "_index", "DISC_LABEL.txt")
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(marker, encoding="utf-8") as fh:
                if fh.readline().strip() == label:
                    return True
        except OSError:
            pass
        time.sleep(3)
    return False


def verify(cat, disc, volume, log=print, progress=None, cancel=None):
    """Hash every file on the disc and compare with the catalog. Returns a list of problems."""
    marker = os.path.join(volume, "_index", "DISC_LABEL.txt")
    try:
        with open(marker, encoding="utf-8") as fh:
            found = fh.readline().strip()
    except OSError:
        return [f"No M-Disc Archiver index found at {volume}. Is the right disc inserted?"]
    if found != disc["label"]:
        return [f"The disc in the drive is {found}, not {disc['label']}."]
    contents = cat.disc_contents(disc["id"])
    problems = []
    for i, r in enumerate(contents, 1):
        path = os.path.join(volume, *r["disc_path"].split("/"))
        try:
            if sha256_file(path, cancel) != r["sha256"]:
                problems.append(f"checksum mismatch: {r['disc_path']}")
        except InterruptedError:
            raise
        except OSError as e:
            problems.append(f"unreadable: {r['disc_path']} ({e})")
        if progress:
            progress(i * 100 // max(len(contents), 1))
    log(f"Verified {len(contents)} files on {disc['label']}: {len(problems)} problem(s)")
    return problems
