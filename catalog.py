"""SQLite catalog: which files exist, which discs exist, which files are on which disc."""
import csv
import os
import sqlite3
from datetime import datetime

# 100 GB BDXL triple-layer disc: 48,878,592 sectors x 2048 bytes.
DEFAULTS = {
    "capacity_bytes": "100103356416",
    "reserve_bytes": "1500000000",
    "verify_after_burn": "1",
}

DEFAULT_IGNORE = """# Files and folders matching these patterns are never backed up.
# One pattern per line. * matches anything, ? matches one character.
# Patterns match a file or folder name (not a full path) and ignore upper/lower case.
# Lines starting with "# " (hash and a space) are comments.

# Windows
Thumbs.db
ehthumbs.db
ehthumbs_vista.db
desktop.ini
~$*
*.tmp

# Mac
.DS_Store
._*
.AppleDouble
.AppleDB
.AppleDesktop
.Spotlight-V100
.Trashes
.fseventsd
.TemporaryItems
.DocumentRevisions-V100
.apdisk
.localized

# NAS (Synology / QNAP) thumbnails and recycle bins
@eaDir
#recycle
#snapshot
.@__thumb

# Unfinished downloads
*.crdownload
*.part
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    label TEXT UNIQUE NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    last_scan INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_id, rel_path, size, mtime)
);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE TABLE IF NOT EXISTS discs(
    id INTEGER PRIMARY KEY,
    label TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    burned_at TEXT,
    verified_at TEXT,
    bytes_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL            -- planned | burned | verified | failed
);
CREATE TABLE IF NOT EXISTS disc_files(
    disc_id INTEGER NOT NULL REFERENCES discs(id),
    file_id INTEGER NOT NULL REFERENCES files(id),
    disc_path TEXT NOT NULL,        -- path on the disc, '/' separated
    PRIMARY KEY(disc_id, file_id)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Content (by sha256) counts as "taken" if it is on any disc that isn't failed.
_TAKEN_SHAS = """
    SELECT f2.sha256 FROM disc_files df
    JOIN discs d ON d.id = df.disc_id
    JOIN files f2 ON f2.id = df.file_id
    WHERE d.status != 'failed'
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def default_catalog_path():
    return os.path.join(os.path.expanduser("~"), "MDiscArchive", "catalog.sqlite")


class Catalog:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- settings -------------------------------------------------------
    def get(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else DEFAULTS[key]

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, str(value)))
        self.db.commit()

    def ignore_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), "ignore.txt")

    def ignore_text(self):
        """Contents of ignore.txt, creating it with common patterns on first use."""
        path = self.ignore_path()
        if not os.path.exists(path):
            text = DEFAULT_IGNORE
            # Carry over custom patterns from the old comma-separated "excludes" setting.
            row = self.db.execute("SELECT value FROM settings WHERE key='excludes'").fetchone()
            if row:
                known = {line.strip().lower() for line in text.splitlines()}
                extra = [p.strip() for p in row["value"].split(",") if p.strip() and p.strip().lower() not in known]
                if extra:
                    text += "\n# From earlier settings\n" + "\n".join(extra) + "\n"
            self.save_ignore_text(text)
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def save_ignore_text(self, text):
        with open(self.ignore_path(), "w", encoding="utf-8") as fh:
            fh.write(text)

    def excludes(self):
        lines = (line.strip() for line in self.ignore_text().splitlines())
        return [line for line in lines if line and not line.startswith("# ") and line != "#"]

    def usable_bytes(self):
        return int(self.get("capacity_bytes")) - int(self.get("reserve_bytes"))

    # ---- sources --------------------------------------------------------
    def add_source(self, path):
        path = os.path.abspath(path)
        row = self.db.execute("SELECT id FROM sources WHERE path=?", (path,)).fetchone()
        if row:
            self.db.execute("UPDATE sources SET active=1 WHERE id=?", (row["id"],))
        else:
            base = os.path.basename(path.rstrip("\\/")) or "root"
            label, n = base, 2
            while self.db.execute("SELECT 1 FROM sources WHERE label=?", (label,)).fetchone():
                label, n = f"{base}_{n}", n + 1
            self.db.execute("INSERT INTO sources(path, label) VALUES(?, ?)", (path, label))
        self.db.commit()

    def remove_source(self, source_id):
        # Keep the rows: files from this source may already be on discs.
        self.db.execute("UPDATE sources SET active=0 WHERE id=?", (source_id,))
        self.db.commit()

    def active_sources(self):
        return self.db.execute("SELECT * FROM sources WHERE active=1 ORDER BY label").fetchall()

    # ---- scanning -------------------------------------------------------
    def next_scan_id(self):
        row = self.db.execute("SELECT COALESCE(MAX(last_scan), 0) + 1 AS n FROM files").fetchone()
        return row["n"]

    def touch_file(self, source_id, rel_path, size, mtime, scan_id):
        """Mark an already-known file version as seen. Returns False if unknown."""
        cur = self.db.execute(
            "UPDATE files SET present=1, last_scan=? WHERE source_id=? AND rel_path=? AND size=? AND mtime=?",
            (scan_id, source_id, rel_path, size, mtime),
        )
        return cur.rowcount > 0

    def add_file(self, source_id, rel_path, size, mtime, sha256, scan_id):
        self.db.execute(
            "INSERT INTO files(source_id, rel_path, size, mtime, sha256, first_seen, present, last_scan) "
            "VALUES(?, ?, ?, ?, ?, ?, 1, ?)",
            (source_id, rel_path, size, mtime, sha256, now(), scan_id),
        )

    def finish_source_scan(self, source_id, scan_id):
        """After a complete walk, anything not seen this scan is gone or changed."""
        self.db.execute(
            "UPDATE files SET present = (last_scan = ?) WHERE source_id=?", (scan_id, source_id)
        )
        self.db.commit()

    def commit(self):
        self.db.commit()

    # ---- planning -------------------------------------------------------
    def pending_files(self):
        """Present files whose content isn't on any non-failed disc; one row per sha256."""
        return self.db.execute(f"""
            SELECT f.id, s.label AS source_label, s.path AS source_path, f.rel_path, f.size, f.sha256
            FROM files f JOIN sources s ON s.id = f.source_id
            WHERE f.present = 1 AND s.active = 1
              AND f.sha256 NOT IN ({_TAKEN_SHAS})
              AND f.id = (SELECT MIN(f3.id) FROM files f3 JOIN sources s3 ON s3.id = f3.source_id
                          WHERE f3.sha256 = f.sha256 AND f3.present = 1 AND s3.active = 1)
            ORDER BY s.label, f.rel_path
        """).fetchall()

    def planned_disc(self):
        return self.db.execute("SELECT * FROM discs WHERE status='planned' ORDER BY id LIMIT 1").fetchone()

    def create_disc(self, files, bytes_used):
        n = self.db.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM discs").fetchone()["n"]
        label = f"MD-{n:04d}"
        self.db.execute(
            "INSERT INTO discs(id, label, created_at, bytes_used, status) VALUES(?, ?, ?, ?, 'planned')",
            (n, label, now(), bytes_used),
        )
        self.db.executemany(
            "INSERT INTO disc_files(disc_id, file_id, disc_path) VALUES(?, ?, ?)",
            [(n, f["id"], f"{f['source_label']}/{f['rel_path']}") for f in files],
        )
        self.db.commit()
        return self.disc(n)

    def cancel_plan(self, disc_id):
        self.db.execute("DELETE FROM disc_files WHERE disc_id=? AND "
                        "(SELECT status FROM discs WHERE id=?)='planned'", (disc_id, disc_id))
        self.db.execute("DELETE FROM discs WHERE id=? AND status='planned'", (disc_id,))
        self.db.commit()

    # ---- discs ----------------------------------------------------------
    def disc(self, disc_id):
        return self.db.execute("SELECT * FROM discs WHERE id=?", (disc_id,)).fetchone()

    def discs(self):
        return self.db.execute("""
            SELECT d.*, (SELECT COUNT(*) FROM disc_files df WHERE df.disc_id = d.id) AS file_count
            FROM discs d ORDER BY d.id
        """).fetchall()

    def disc_contents(self, disc_id):
        return self.db.execute("""
            SELECT df.disc_path, f.size, f.mtime, f.sha256, f.rel_path, s.path AS source_path
            FROM disc_files df JOIN files f ON f.id = df.file_id JOIN sources s ON s.id = f.source_id
            WHERE df.disc_id = ? ORDER BY df.disc_path
        """, (disc_id,)).fetchall()

    def set_status(self, disc_id, status):
        col = {"burned": "burned_at", "verified": "verified_at"}.get(status)
        if col:
            self.db.execute(f"UPDATE discs SET status=?, {col}=? WHERE id=?", (status, now(), disc_id))
        else:
            self.db.execute("UPDATE discs SET status=? WHERE id=?", (status, disc_id))
        self.db.commit()

    # ---- reporting ------------------------------------------------------
    def stats(self):
        archived = self.db.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM (
                SELECT f.sha256, MAX(f.size) AS size FROM disc_files df
                JOIN discs d ON d.id = df.disc_id JOIN files f ON f.id = df.file_id
                WHERE d.status IN ('burned', 'verified') GROUP BY f.sha256)
        """).fetchone()
        pending = self.pending_files()
        return {
            "archived_files": archived["n"],
            "archived_bytes": archived["b"],
            "pending_files": len(pending),
            "pending_bytes": sum(p["size"] for p in pending),
            "discs": self.db.execute("SELECT COUNT(*) FROM discs WHERE status IN ('burned','verified')").fetchone()[0],
        }

    def search(self, term, limit=2000):
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return self.db.execute("""
            SELECT s.label || '/' || f.rel_path AS path, f.size, f.present,
                   x.label AS disc_label, x.disc_path, x.status AS disc_status
            FROM files f JOIN sources s ON s.id = f.source_id
            LEFT JOIN (
                SELECT g.sha256 AS sha, d.label, d.status, df.disc_path
                FROM disc_files df JOIN discs d ON d.id = df.disc_id JOIN files g ON g.id = df.file_id
                WHERE d.status IN ('burned', 'verified')
            ) x ON x.sha = f.sha256
            WHERE (s.label || '/' || f.rel_path) LIKE ? ESCAPE '\\'
            ORDER BY path, disc_label LIMIT ?
        """, (like, limit)).fetchall()

    def export_csv(self, out_path, extra_disc_id=None):
        """Write disc_label,path,size,sha256 for burned/verified discs (plus one extra disc, if given)."""
        rows = self.db.execute("""
            SELECT d.label, df.disc_path, f.size, f.sha256, d.status
            FROM disc_files df JOIN discs d ON d.id = df.disc_id JOIN files f ON f.id = df.file_id
            WHERE d.status IN ('burned', 'verified') OR d.id = ?
            ORDER BY d.id, df.disc_path
        """, (extra_disc_id or -1,))
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["disc_label", "path_on_disc", "size_bytes", "sha256", "status"])
            for r in rows:
                w.writerow([r["label"], r["disc_path"], r["size"], r["sha256"],
                            "burned" if r["status"] == "planned" else r["status"]])

    def snapshot(self, out_path, burning_disc_id=None):
        """Copy the catalog to a standalone file; mark the disc being burned as burned in the copy."""
        if os.path.exists(out_path):
            os.remove(out_path)
        dst = sqlite3.connect(out_path)
        self.db.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        if burning_disc_id is not None:
            dst.execute("UPDATE discs SET status='burned', burned_at=? WHERE id=?", (now(), burning_disc_id))
        dst.commit()
        dst.close()
