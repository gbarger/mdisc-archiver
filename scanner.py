"""Walk source folders and record new or changed files in the catalog."""
import fnmatch
import hashlib
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information"}
HASH_WORKERS = 4        # parallel reads hide per-file latency (antivirus, network shares)
REPORT_EVERY = 0.25     # seconds between progress updates
COMMIT_EVERY = 2.0      # seconds between commits while hashing
# Windows: file content lives in the cloud (OneDrive "online-only"); reading it downloads it first.
CLOUD_ONLY = 0x00400000 | 0x00040000 | 0x00001000   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE


def sha256_file(path, cancel=None, on_chunk=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError("cancelled")
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            if on_chunk:
                on_chunk(len(chunk))
    return h.hexdigest()


def _excluded(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _walk(top, patterns, cancel, on_error):
    """Yield (rel_path, DirEntry) for files under top. DirEntry.stat() is free on Windows."""
    stack = [("", top)]
    while stack:
        if cancel and cancel.is_set():
            return
        rel_dir, path = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            on_error(f"  Could not read folder {path}: {e}")
            continue
        subdirs = []
        for e in entries:
            if _excluded(e.name, patterns):
                continue
            rel = rel_dir + e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIRS:
                        subdirs.append((rel + "/", e.path))
                elif e.is_file():
                    yield rel, e
            except OSError as err:
                on_error(f"  Could not read {e.path}: {err}")
        stack.extend(reversed(subdirs))


def _fmt_time(sec):
    if sec >= 3600:
        return f"{sec / 3600:.1f} h"
    return f"{sec / 60:.0f} min" if sec >= 60 else f"{sec:.0f} s"


def scan(cat, log=print, cancel=None, progress=None):
    """Scan every active source. Returns (new_files, errors).

    progress(text, pct) is called a few times per second; pct is None while the total is unknown.
    """
    progress = progress or (lambda text, pct: None)
    patterns = cat.excludes()
    scan_id = cat.next_scan_id()
    errors = 0

    def error(msg):
        nonlocal errors
        errors += 1
        log(msg)

    # Phase 1: list every file and mark already-known versions as seen. No file contents are read.
    sources, todo = [], []          # todo: (source_id, rel, full_path, size, mtime)
    checked, cloud_only, last = 0, 0, 0.0
    for src in cat.active_sources():
        if not os.path.isdir(src["path"]):
            log(f"Source not found, skipped: {src['path']}")
            continue
        log(f"Listing {src['path']} ...")
        sources.append(src)
        for rel, entry in _walk(src["path"], patterns, cancel, error):
            try:
                st = entry.stat()
            except OSError as e:
                error(f"  Could not read {entry.path}: {e}")
                continue
            mtime = st.st_mtime_ns // 1_000_000_000
            if not cat.touch_file(src["id"], rel, st.st_size, mtime, scan_id):
                todo.append((src["id"], rel, entry.path, st.st_size, mtime))
                if getattr(st, "st_file_attributes", 0) & CLOUD_ONLY:
                    cloud_only += 1
            checked += 1
            if time.monotonic() - last > REPORT_EVERY:
                last = time.monotonic()
                progress(f"Listing files: {checked:,} found, {len(todo):,} new or changed", None)
    cat.commit()
    if cancel and cancel.is_set():
        log("Scan cancelled. Run Scan again to finish.")
        return 0, errors

    total_bytes = sum(t[3] for t in todo)
    log(f"  {checked:,} files checked, {len(todo):,} new or changed ({total_bytes / 1e9:,.1f} GB) to fingerprint.")
    if cloud_only:
        log(f"  Note: {cloud_only:,} of them are online-only cloud files (e.g. OneDrive). Reading them downloads "
            "them first, which is slow and uses local disk space. Set the folder to 'Always keep on this device' "
            "to download them in advance.")

    # Phase 2: hash new files in parallel; only this thread touches the database.
    new_count, done_bytes = 0, 0
    lock = threading.Lock()

    def add_bytes(n):
        nonlocal done_bytes
        with lock:
            done_bytes += n

    def hash_one(t):
        return t, sha256_file(t[2], cancel, add_bytes)

    def report(now):
        rate = done_bytes / max(now - started, 0.001)
        left = f", about {_fmt_time((total_bytes - done_bytes) / rate)} left" if rate > 0 else ""
        progress(f"Fingerprinting: {new_count:,} of {len(todo):,} files, "
                 f"{done_bytes / 1e9:,.1f} of {total_bytes / 1e9:,.1f} GB, {rate / 1e6:,.0f} MB/s{left}",
                 100 * done_bytes / total_bytes if total_bytes else 100)

    complete = True
    started = last = last_commit = time.monotonic()
    pending_iter = iter(todo)
    running = set()
    with ThreadPoolExecutor(HASH_WORKERS) as pool:
        while True:
            while len(running) < HASH_WORKERS * 2 and not (cancel and cancel.is_set()):
                t = next(pending_iter, None)
                if t is None:
                    break
                running.add(pool.submit(hash_one, t))
            if not running:
                break
            finished, running = wait(running, timeout=REPORT_EVERY, return_when=FIRST_COMPLETED)
            for fut in finished:
                try:
                    (source_id, rel, _, size, mtime), sha = fut.result()
                except InterruptedError:
                    complete = False
                    continue
                except OSError as e:
                    error(f"  Could not read {e.filename or ''}: {e}")
                    continue
                cat.add_file(source_id, rel, size, mtime, sha, scan_id)
                new_count += 1
            now = time.monotonic()
            if now - last_commit > COMMIT_EVERY:
                cat.commit()
                last_commit = now
            if now - last > REPORT_EVERY:
                last = now
                report(now)
    report(time.monotonic())
    cat.commit()
    if not complete or (cancel and cancel.is_set()):
        log("Scan cancelled. New files found so far are saved; run Scan again to finish.")
        return new_count, errors
    for src in sources:
        cat.finish_source_scan(src["id"], scan_id)
    return new_count, errors
