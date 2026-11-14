"""Walk source folders and record new or changed files in the catalog."""
import fnmatch
import hashlib
import os

SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information"}


def sha256_file(path, cancel=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError("cancelled")
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _excluded(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def scan(cat, log=print, cancel=None):
    """Scan every active source. Returns (new_files, errors)."""
    patterns = cat.excludes()
    scan_id = cat.next_scan_id()
    new_count, errors = 0, 0
    for src in cat.active_sources():
        if not os.path.isdir(src["path"]):
            log(f"Source not found, skipped: {src['path']}")
            continue
        log(f"Scanning {src['path']} ...")
        seen = 0
        complete = True
        for root, dirs, files in os.walk(src["path"]):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not _excluded(d, patterns))
            for name in sorted(files):
                if cancel and cancel.is_set():
                    complete = False
                    break
                if _excluded(name, patterns):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src["path"]).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                    mtime = st.st_mtime_ns // 1_000_000_000
                    if not cat.touch_file(src["id"], rel, st.st_size, mtime, scan_id):
                        sha = sha256_file(full, cancel)
                        cat.add_file(src["id"], rel, st.st_size, mtime, sha, scan_id)
                        new_count += 1
                        if new_count % 100 == 0:
                            cat.commit()
                            log(f"  {new_count} new files hashed...")
                except InterruptedError:
                    complete = False
                    break
                except OSError as e:
                    errors += 1
                    log(f"  Could not read {full}: {e}")
                seen += 1
            if not complete:
                break
        cat.commit()
        if not complete:
            log("Scan cancelled. New files found so far are saved; run Scan again to finish.")
            return new_count, errors
        cat.finish_source_scan(src["id"], scan_id)
        log(f"  {seen} files checked in {src['label']}")
    return new_count, errors
