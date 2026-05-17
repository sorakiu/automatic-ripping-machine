#!/usr/bin/env python3
"""
tv_organizer.py — Organize ARM-ripped TV disc files into Plex/Jellyfin layout.

ARM rips each track to a flat directory. This script sorts those files and
moves them into the standard structure media servers expect:

  <dest>/<Show Name>/Season <N>/S<SS>E<EE>.mkv

Run once per disc, using --start-ep to offset episode numbers for disc 2+.

Examples
--------
# Disc 1 of Renegade season 1 (episodes 1-4):
  tv_organizer.py --show "Renegade" --season 1 --start-ep 1 \\
      --source /home/arm/media/completed/tv/Renegade \\
      --dest /mnt/media/tv

# Disc 2 (episodes 5-8), preview first:
  tv_organizer.py --show "Renegade" --season 1 --start-ep 5 \\
      --source /home/arm/media/completed/tv/Renegade \\
      --dest /mnt/media/tv --dry-run
"""
import argparse
import os
import shutil
import sys


def collect_files(source_dir, ext):
    """Return sorted list of files with the given extension in source_dir."""
    try:
        entries = os.listdir(source_dir)
    except OSError as e:
        print(f"Error reading source directory: {e}", file=sys.stderr)
        sys.exit(1)
    files = sorted(
        f for f in entries
        if f.lower().endswith(f".{ext.lstrip('.')}")
        and os.path.isfile(os.path.join(source_dir, f))
    )
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Move ARM-ripped TV tracks into Plex/Jellyfin Season/Episode layout",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--show", required=True,
                        help="Show name as it should appear in the media library (e.g. 'Renegade')")
    parser.add_argument("--season", type=int, required=True,
                        help="Season number")
    parser.add_argument("--start-ep", type=int, default=1,
                        help="Episode number for the first file on this disc (default: 1)")
    parser.add_argument("--source", required=True,
                        help="Directory containing ARM-ripped files for this disc")
    parser.add_argument("--dest", required=True,
                        help="TV root in your media library (e.g. /mnt/media/tv)")
    parser.add_argument("--ext", default="mkv",
                        help="File extension to process (default: mkv)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned moves without touching any files")
    args = parser.parse_args()

    source = os.path.abspath(args.source)
    season_dir = os.path.join(os.path.abspath(args.dest), args.show, f"Season {args.season:02d}")

    files = collect_files(source, args.ext)
    if not files:
        print(f"No .{args.ext} files found in {source}", file=sys.stderr)
        sys.exit(1)

    print(f"Show    : {args.show}")
    print(f"Season  : {args.season:02d}")
    print(f"Episodes: {args.start_ep} – {args.start_ep + len(files) - 1}")
    print(f"Dest    : {season_dir}")
    print(f"{'--- DRY RUN ---' if args.dry_run else ''}")
    print()

    if not args.dry_run:
        os.makedirs(season_dir, exist_ok=True)

    errors = 0
    for i, filename in enumerate(files):
        ep_num = args.start_ep + i
        new_name = f"S{args.season:02d}E{ep_num:02d}.{args.ext}"
        src_path = os.path.join(source, filename)
        dst_path = os.path.join(season_dir, new_name)

        prefix = "[DRY RUN] " if args.dry_run else ""
        print(f"  {prefix}{filename}  →  {new_name}")

        if args.dry_run:
            continue

        if os.path.exists(dst_path):
            print(f"    WARNING: {dst_path} already exists — skipping", file=sys.stderr)
            errors += 1
            continue

        try:
            shutil.move(src_path, dst_path)
        except OSError as e:
            print(f"    ERROR moving file: {e}", file=sys.stderr)
            errors += 1

    print()
    if args.dry_run:
        print("Dry run complete. Re-run without --dry-run to move files.")
    elif errors:
        print(f"Done with {errors} error(s). Check output above.")
        sys.exit(1)
    else:
        print("Done.")


if __name__ == "__main__":
    main()
