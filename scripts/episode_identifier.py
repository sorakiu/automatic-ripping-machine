#!/usr/bin/env python3
"""episode_identifier.py — Identify TV episodes from ripped MKV files via STT + subtitle matching.

Usage
-----
Watch mode (run as a service):
    episode_identifier.py watch

Process a single file:
    episode_identifier.py process /path/to/file.mkv [--show "Show Name"]

Pre-fetch subtitles for a show before ripping:
    episode_identifier.py prefetch "Renegade" --seasons 1 2 3 [--year 1992]

Configuration (environment variables)
--------------------------------------
WATCH_DIR               Directory to watch (default: /home/arm/media/completed/tv)
OUTPUT_DIR              Output root (default: /home/arm/media/renamed/tv)
CACHE_DIR               Subtitle cache root (default: /home/arm/cache/episode-id)
WHISPER_MODEL           tiny / base / small / medium / large (default: base)
CONFIDENCE_THRESHOLD    Match threshold 0-100 (default: 65)
SUBTITLE_PROVIDERS      Comma-separated subliminal providers (default: podnapisi,opensubtitles)

Subtitle cache layout
----------------------
$CACHE_DIR/subtitles/<ShowName>/
    S01E01 - PilotTitle.en.srt
    S01E02 - SecondEp.en.srt
    ...
    _index.json

Drop manually-downloaded .en.srt files into the show cache directory and they
will be picked up automatically without any API calls.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import inotify_simple
import whisper
from babelfish import Language
from rapidfuzz import fuzz
from subliminal import download_best_subtitles
from subliminal import region as subliminal_region
from subliminal.video import Episode as SubliminalEpisode

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger(__name__)

_SRT_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")
_SRT_INDEX_RE = re.compile(r"^\d+$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    watch_dir: Path
    output_dir: Path
    cache_dir: Path
    whisper_model: str = "base"
    confidence_threshold: float = 65.0
    audio_offsets: list[int] = field(default_factory=lambda: [300, 480, 720])
    audio_duration: int = 60
    subtitle_providers: list[str] = field(default_factory=lambda: ["podnapisi", "opensubtitles"])
    stability_seconds: int = 10


def config_from_env() -> Config:
    return Config(
        watch_dir=Path(os.environ.get("WATCH_DIR", "/home/arm/media/completed/tv")),
        output_dir=Path(os.environ.get("OUTPUT_DIR", "/home/arm/media/renamed/tv")),
        cache_dir=Path(os.environ.get("CACHE_DIR", "/home/arm/cache/episode-id")),
        whisper_model=os.environ.get("WHISPER_MODEL", "base"),
        confidence_threshold=float(os.environ.get("CONFIDENCE_THRESHOLD", "65")),
        subtitle_providers=os.environ.get(
            "SUBTITLE_PROVIDERS", "podnapisi,opensubtitles"
        ).split(","),
    )


# ---------------------------------------------------------------------------
# SRT utilities
# ---------------------------------------------------------------------------


def parse_srt(srt_text: str) -> str:
    """Strip SRT formatting, returning plain dialogue as a single string."""
    lines = []
    for raw in srt_text.splitlines():
        line = raw.strip()
        if not line or _SRT_TIMESTAMP_RE.match(line) or _SRT_INDEX_RE.match(line):
            continue
        lines.append(_HTML_TAG_RE.sub("", line))
    return " ".join(lines)


def safe_name(name: str) -> str:
    return _UNSAFE_CHARS_RE.sub("_", name).strip()


# ---------------------------------------------------------------------------
# Subtitle cache
# ---------------------------------------------------------------------------


def _cache_dir_for_show(show_name: str, config: Config) -> Path:
    return config.cache_dir / "subtitles" / safe_name(show_name)


def _index_path(show_name: str, config: Config) -> Path:
    return _cache_dir_for_show(show_name, config) / "_index.json"


def _load_index(show_name: str, config: Config) -> dict:
    p = _index_path(show_name, config)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_index(show_name: str, index: dict, config: Config) -> None:
    p = _index_path(show_name, config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index, indent=2))


def list_cached_srts(show_name: str, config: Config) -> list[tuple[str, str, Path]]:
    """List cached SRT files for a show.

    Returns:
        Sorted list of (episode_id, episode_title, srt_path). episode_id is "S01E05".
    """
    d = _cache_dir_for_show(show_name, config)
    if not d.exists():
        return []
    out = []
    for srt_file in sorted(d.glob("*.en.srt")):
        stem = srt_file.stem.removesuffix(".en")
        m = re.match(r"(S\d+E\d+)\s*-\s*(.*)", stem, re.IGNORECASE)
        ep_id = m.group(1).upper() if m else stem
        title = m.group(2).strip() if m else ""
        out.append((ep_id, title, srt_file))
    return out


def _init_subliminal_cache(config: Config) -> None:
    if not subliminal_region.is_configured:
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        dbm_path = str(config.cache_dir / "subliminal.cache.dbm")
        subliminal_region.configure("dogpile.cache.dbm", arguments={"filename": dbm_path})


def _fetch_season(
    show_name: str, season: int, year: Optional[int], index: dict, config: Config
) -> int:
    """Download subtitles for all episodes in a season via subliminal.

    Tries episodes 1-99 and stops after 3 consecutive misses (end of season).

    Returns:
        Number of newly cached subtitle files.
    """
    cached_episodes = {v["episode"] for v in index.values() if isinstance(v, dict)}
    downloaded = 0
    consecutive_misses = 0

    for ep_num in range(1, 100):
        episode_id = f"S{season:02d}E{ep_num:02d}"
        if episode_id in cached_episodes:
            consecutive_misses = 0
            continue

        video = SubliminalEpisode(
            name=f"{show_name} {episode_id}.mkv",
            series=show_name,
            season=season,
            episodes={ep_num},
            year=year,
        )
        results = download_best_subtitles(
            [video],
            {Language("eng")},
            only_one=True,
            providers=config.subtitle_providers,
        )
        subs = results.get(video, [])

        if not subs:
            consecutive_misses += 1
            log.debug("No subtitle found for %s (miss %d)", episode_id, consecutive_misses)
            if consecutive_misses >= 3:
                log.info("Season %d appears complete after %s", season, episode_id)
                break
            continue

        consecutive_misses = 0
        sub = subs[0]
        srt_name = f"{episode_id}.en.srt"
        srt_path = _cache_dir_for_show(show_name, config) / srt_name
        content = sub.content or (sub.text.encode("utf-8") if sub.text else None)
        if not content:
            log.warning("Empty subtitle content for %s, skipping", episode_id)
            continue

        srt_path.write_bytes(content)
        index[episode_id] = {"episode": episode_id, "title": "", "file": srt_name}
        _save_index(show_name, index, config)
        downloaded += 1
        log.info("Cached %s via %s", episode_id, sub.provider_name)
        time.sleep(1)

    return downloaded


def fetch_subtitles_for_show(
    show_name: str,
    config: Config,
    seasons: Optional[list[int]] = None,
    year: Optional[int] = None,
) -> int:
    """Download and cache English subtitles for a show via subliminal.

    Args:
        show_name: Series name, also used as the local cache directory name.
        config: Runtime configuration.
        seasons: Season numbers to fetch. Auto-discovers seasons if None.
        year: Series premiere year, helps disambiguate shows with the same name.

    Returns:
        Number of newly cached subtitle files.
    """
    _init_subliminal_cache(config)
    _cache_dir_for_show(show_name, config).mkdir(parents=True, exist_ok=True)
    index = _load_index(show_name, config)

    search_seasons = seasons if seasons else range(1, 50)
    downloaded = 0

    for season in search_seasons:
        log.info("Fetching subtitles for '%s' season %d", show_name, season)
        n = _fetch_season(show_name, int(season), year, index, config)
        downloaded += n
        if n == 0 and not seasons:
            log.info("No subtitles found for season %d — stopping", season)
            break

    return downloaded


# ---------------------------------------------------------------------------
# Audio extraction + Whisper
# ---------------------------------------------------------------------------


def load_whisper_model(model_name: str) -> Any:
    """Load Whisper model, preferring CUDA when available."""
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    log.info("Loading Whisper '%s' on %s", model_name, device)
    return whisper.load_model(model_name, device=device)


def extract_audio_clip(mkv_path: Path, offset: int, duration: int, out_path: Path) -> bool:
    """Extract a 16kHz mono WAV clip from an MKV at the given offset.

    Returns:
        False if the offset is past the end of the file or ffmpeg fails.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(offset),
            "-i", str(mkv_path),
            "-t", str(duration),
            "-vn",
            "-map", "0:a:0",
            "-ar", "16000",
            "-ac", "1",
            str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not out_path.exists():
        log.debug("ffmpeg failed at offset %ds: %s", offset, result.stderr[-300:])
        return False
    return out_path.stat().st_size > 1024


def _transcribe_clip(audio_path: Path, model: Any) -> str:
    result = model.transcribe(str(audio_path), language="en", fp16=False)
    return result.get("text", "").strip()


def transcribe_all_offsets(mkv_path: Path, config: Config, model: Any) -> list[str]:
    """Return Whisper transcripts from each configured audio offset that yields content."""
    transcripts = []
    with tempfile.TemporaryDirectory() as tmp:
        for offset in config.audio_offsets:
            clip = Path(tmp) / f"clip_{offset}.wav"
            if not extract_audio_clip(mkv_path, offset, config.audio_duration, clip):
                log.debug("Skipping offset %ds (past end or extraction failed)", offset)
                continue
            text = _transcribe_clip(clip, model)
            if text:
                log.info("Transcribed %ds offset: %d chars", offset, len(text))
                transcripts.append(text)
    return transcripts


# ---------------------------------------------------------------------------
# Episode matching
# ---------------------------------------------------------------------------


def match_episode(
    transcript: str, show_name: str, config: Config
) -> Optional[tuple[str, str, float]]:
    """Fuzzy-match a Whisper transcript against cached SRTs for a show.

    Args:
        transcript: Whisper output text.
        show_name: Show name used to locate the subtitle cache.
        config: Runtime configuration.

    Returns:
        (episode_id, title, score) if best match exceeds the threshold, else None.
    """
    srts = list_cached_srts(show_name, config)
    if not srts:
        log.warning("No cached subtitles for '%s'", show_name)
        return None

    best_score = 0.0
    best: Optional[tuple[str, str]] = None
    needle = transcript.lower()

    for episode_id, title, srt_path in srts:
        try:
            srt_text = parse_srt(
                srt_path.read_text(encoding="utf-8", errors="replace")
            ).lower()
        except OSError as exc:
            log.warning("Cannot read %s: %s", srt_path, exc)
            continue
        score = float(fuzz.partial_ratio(needle, srt_text))
        if score > best_score:
            best_score, best = score, (episode_id, title)

    if best and best_score >= config.confidence_threshold:
        return best[0], best[1], best_score

    log.info("Best match %.1f%% below threshold %.1f%%", best_score, config.confidence_threshold)
    return None


def best_match_across_offsets(
    transcripts: list[str], show_name: str, config: Config
) -> Optional[tuple[str, str, float]]:
    """Run match_episode for each transcript; return the highest-scoring result."""
    best: Optional[tuple[str, str, float]] = None
    for transcript in transcripts:
        result = match_episode(transcript, show_name, config)
        if result and (best is None or result[2] > best[2]):
            best = result
        if best and best[2] >= 90:
            break  # early exit on highly confident match
    return best


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


def wait_for_stability(path: Path, wait_seconds: int) -> None:
    """Block until the file size is unchanged for wait_seconds."""
    prev = -1
    while True:
        size = path.stat().st_size
        if size == prev:
            return
        prev = size
        time.sleep(wait_seconds)


def move_matched(
    mkv_path: Path, show_name: str, episode_id: str, title: str, config: Config
) -> Path:
    out_name = (
        f"{show_name} - {episode_id} - {safe_name(title)}.mkv"
        if title
        else f"{show_name} - {episode_id}.mkv"
    )
    dest_dir = config.output_dir / safe_name(show_name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / out_name
    shutil.move(str(mkv_path), dest)
    log.info("Matched  → %s", dest)
    return dest


def move_unmatched(mkv_path: Path, show_name: str, config: Config) -> Path:
    dest_dir = config.output_dir / safe_name(show_name) / "unrecognized"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / mkv_path.name
    shutil.move(str(mkv_path), dest)
    log.warning("Unmatched → %s", dest)
    return dest


# ---------------------------------------------------------------------------
# Per-file pipeline
# ---------------------------------------------------------------------------


def process_file(mkv_path: Path, show_name: str, config: Config, model: Any) -> None:
    """Full pipeline: wait for stability → fetch subtitles → transcribe → match → move.

    Args:
        mkv_path: Path to the MKV file to process.
        show_name: TV show name, used to locate the subtitle cache.
        config: Runtime configuration.
        model: Loaded Whisper model.
    """
    log.info("Processing '%s'  (show: %s)", mkv_path.name, show_name)
    wait_for_stability(mkv_path, config.stability_seconds)

    if not list_cached_srts(show_name, config) and config.opensubtitles_api_key:
        log.info("No cached subtitles for '%s' — fetching from OpenSubtitles", show_name)
        fetch_subtitles_for_show(show_name, config)

    if not list_cached_srts(show_name, config):
        log.error("No subtitles available for '%s' — moving to unrecognized", show_name)
        move_unmatched(mkv_path, show_name, config)
        return

    transcripts = transcribe_all_offsets(mkv_path, config, model)
    if not transcripts:
        log.error("No transcript produced for %s", mkv_path.name)
        move_unmatched(mkv_path, show_name, config)
        return

    result = best_match_across_offsets(transcripts, show_name, config)
    if result:
        episode_id, title, score = result
        log.info("Matched %s as %s (%.1f%%)", mkv_path.name, episode_id, score)
        move_matched(mkv_path, show_name, episode_id, title, config)
    else:
        move_unmatched(mkv_path, show_name, config)


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


@dataclass
class _WatchState:
    inotify: inotify_simple.INotify
    flags: Any
    wd_map: dict[int, Path]
    parent_wd: int


def _setup_watches(watch_dir: Path) -> _WatchState:
    inotify = inotify_simple.INotify()
    flags = inotify_simple.flags
    wd_map: dict[int, Path] = {}

    parent_wd = inotify.add_watch(str(watch_dir), flags.CREATE | flags.MOVED_TO)
    wd_map[parent_wd] = watch_dir

    for entry in watch_dir.iterdir():
        if entry.is_dir():
            wd = inotify.add_watch(str(entry), flags.MOVED_TO | flags.CLOSE_WRITE)
            wd_map[wd] = entry

    return _WatchState(inotify=inotify, flags=flags, wd_map=wd_map, parent_wd=parent_wd)


def _handle_event(event: Any, state: _WatchState, config: Config, model: Any) -> None:
    flags = state.flags
    mask = event.mask
    name = event.name

    is_dir = bool(mask & flags.ISDIR)
    is_arrival = bool(mask & (flags.MOVED_TO | flags.CLOSE_WRITE))

    if event.wd == state.parent_wd and is_dir and bool(mask & (flags.CREATE | flags.MOVED_TO)):
        new_dir = config.watch_dir / name
        wd = state.inotify.add_watch(str(new_dir), flags.MOVED_TO | flags.CLOSE_WRITE)
        state.wd_map[wd] = new_dir
        log.info("Watching new show directory: %s", new_dir)
        return

    if not is_arrival or is_dir or not name.lower().endswith(".mkv"):
        return

    parent_dir = state.wd_map.get(event.wd)
    if parent_dir is None:
        return

    process_file(parent_dir / name, parent_dir.name, config, model)


def watch_loop(config: Config, model: Any) -> None:
    state = _setup_watches(config.watch_dir)
    log.info(
        "Watching %s  (%d existing show dirs)",
        config.watch_dir,
        len(state.wd_map) - 1,
    )
    while True:
        for event in state.inotify.read():
            _handle_event(event, state, config, model)


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def cmd_watch(args: argparse.Namespace, config: Config) -> None:
    if not config.watch_dir.exists():
        sys.exit(f"Watch directory does not exist: {config.watch_dir}")
    model = load_whisper_model(config.whisper_model)
    try:
        watch_loop(config, model)
    except KeyboardInterrupt:
        log.info("Shutting down")


def cmd_process(args: argparse.Namespace, config: Config) -> None:
    mkv = Path(args.file).resolve()
    if not mkv.exists():
        sys.exit(f"File not found: {mkv}")
    model = load_whisper_model(config.whisper_model)
    show_name = args.show or mkv.parent.name
    process_file(mkv, show_name, config, model)


def cmd_prefetch(args: argparse.Namespace, config: Config) -> None:
    seasons = [int(s) for s in args.seasons] if args.seasons else None
    year = int(args.year) if args.year else None
    count = fetch_subtitles_for_show(args.show, config, seasons, year)
    print(f"Downloaded {count} subtitle file(s) for '{args.show}'")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _apply_cli_overrides(args: argparse.Namespace, config: Config) -> None:
    if args.watch_dir:
        config.watch_dir = Path(args.watch_dir)
    if args.output_dir:
        config.output_dir = Path(args.output_dir)
    if args.cache_dir:
        config.cache_dir = Path(args.cache_dir)
    if args.whisper_model:
        config.whisper_model = args.whisper_model
    if args.confidence is not None:
        config.confidence_threshold = args.confidence


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Identify TV episodes from ripped MKV files via speech-to-text + subtitle matching"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--watch-dir", help="Directory to watch for new MKV files")
    parser.add_argument("--output-dir", help="Root directory for renamed output files")
    parser.add_argument("--cache-dir", help="Root directory for the subtitle cache")
    parser.add_argument(
        "--whisper-model",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper model size (default: base)",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        help="Match confidence threshold 0-100 (default: 65)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("watch", help="Watch for new MKV files and process them").set_defaults(
        func=cmd_watch
    )

    p_proc = sub.add_parser("process", help="Process a single MKV file")
    p_proc.add_argument("file", help="Path to the MKV file")
    p_proc.add_argument("--show", help="Show name override (default: parent directory name)")
    p_proc.set_defaults(func=cmd_process)

    p_fetch = sub.add_parser("prefetch", help="Pre-download subtitles for a show")
    p_fetch.add_argument("show", help="Show name as it appears on OpenSubtitles")
    p_fetch.add_argument(
        "--seasons",
        nargs="+",
        metavar="N",
        help="Season numbers to fetch (default: all seasons)",
    )
    p_fetch.add_argument(
        "--year",
        metavar="YYYY",
        help="Series premiere year to disambiguate shows with the same name (e.g. 1992)",
    )
    p_fetch.set_defaults(func=cmd_prefetch)

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = config_from_env()
    _apply_cli_overrides(args, config)
    args.func(args, config)


if __name__ == "__main__":
    main()
