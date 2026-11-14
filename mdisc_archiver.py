"""M-Disc Archiver: back up a growing photo/video library to numbered 100 GB M-Discs.

Usage:  pythonw mdisc_archiver.py [--catalog PATH]
"""
import argparse

from catalog import default_catalog_path


def main():
    ap = argparse.ArgumentParser(description="M-Disc Archiver")
    ap.add_argument("--catalog", default=default_catalog_path(),
                    help="catalog database file (default: %(default)s)")
    args = ap.parse_args()
    import gui
    gui.main(args.catalog)


if __name__ == "__main__":
    main()
