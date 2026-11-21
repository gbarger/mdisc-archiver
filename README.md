# M-Disc Archiver
This application is designed to write 100 GB M-Discs on Windows. It writes new files to the M-Disc discs. Only changed files are written again. A SQLite catalog is used to store all files on all discs. Every single M-Disc always has a copy of this catalog.

## Why?
This is one tool you can use to help you with the 3-2-1 backpu strategy: https://www.backblaze.com/blog/the-3-2-1-backup-strategy/

## Setup (Windows)
1. Install Python 3 from https://www.python.org/downloads/windows/ (the standard installer includes the Tk window toolkit) or chocolatey, or something similar.
2. Copy the `mdisc-archiver` folder anywhere, for example `C:\Tools\mdisc-archiver`.
3. Double-click `run.bat`.

Burning uses Windows' built-in IMAPI2 burning service through `burn_windows.ps1`, so no burning software is needed.

## Workflow
1. **Sources tab:** add your folders.
2. **Archive tab → 1. Scan:** finds new or changed files and fingerprints them (SHA-256). The first scan of a big library takes a while. Later scans only read new files.
3. **2. Plan next disc:** fills the next disc (`MD-0001`, `MD-0002`, …) with files that aren't backed up yet.
4. **3. Burn disc:** insert a blank disc and confirm. Burning 100 GB can take an hour or more.
5. **4. Verify:** by default this runs automatically after a burn. It reads every file back and checks its checksum.
6. Write the disc number (e.g. `MD-0001`) on the disc with a disc-safe marker.

Repeat whenever you add files. **Search tab** tells you which disc holds a file, with no disc inserted.

## What's on each disc
```
Photos\2024\...           your files, exactly as they are in your folders
_index\catalog.sqlite     catalog of every disc burned so far, including this one
_index\catalog.csv        the same list as a spreadsheet
_index\DISC_LABEL.txt     the disc number
_index\README.txt
```
Any disc can tell you where every file is. The newest disc has the most complete index.

## Good to know
- **Catalog location:** `%USERPROFILE%\MDiscArchive\catalog.sqlite`. Back up this folder somewhere else too (cloud, external drive). You can use a different file with `pyw mdisc_archiver.py --catalog D:\path\catalog.sqlite`.
- **Moved or renamed files** aren't burned again, because files are matched by content. Search shows both the current path and the path on the disc.
- **Edited files** are burned again as a new version. The old version stays listed on its original disc.
- **Failed verification:** the disc is marked `failed` and its files go on the next disc. Throw away or mark the bad disc.
- **Ignored files:** Settings tab → **Edit ignore list…** opens `ignore.txt` (next to the catalog), one pattern per line. It comes preloaded with common Windows, Mac and NAS clutter (`Thumbs.db`, `desktop.ini`, `.DS_Store`, `._*`, `@eaDir`, …). Matching ignores upper/lower case. Files that were already scanned but now match the list are dropped from "not yet backed up" on the next full scan.

## First-time test (recommended)
IMAPI2 support for 100 GB BDXL media varies by drive, so test before archiving your whole library:
1. Use `--catalog C:\temp\test.sqlite` and add a small test folder.
2. Scan → Plan → **Save ISO instead…** and open the ISO in Explorer to check the files.
3. Burn one real disc and let it verify.

## If Windows burning doesn't work with your drive
- **Save ISO instead…:** writes a disc image. Burn it with ImgBurn or Windows Disc Image Burner.
- **Export folder instead…:** copies the disc's files (up to 100 GB) into a folder. Burn that folder's *contents* as a UDF disc with the label shown (e.g. ImgBurn → "Write files/folders to disc").

After either one, click **Mark burned**, insert the disc, and click **Verify disc**.

## Development
`python -m unittest tests/test_core.py`
