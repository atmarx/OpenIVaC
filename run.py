#!/usr/bin/env python3
"""
OpenIVaC -- Instructional Videos as Code

Video runner -- records all or individual walkthroughs.

Usage:
    # Record all videos (headless, with TTS narration)
    python run.py

    # Record a specific video by number or name fragment
    python run.py 01
    python run.py settings

    # Record all videos for a project prefix
    python run.py myapp

    # Record with visible browser (useful for debugging)
    python run.py --headed

    # Re-narrate existing videos without re-recording
    python run.py --narrate-only
    python run.py --narrate-only myapp

    # Disable TTS for a quick recording pass
    TTS_ENABLED=false python run.py

    # Record at slower pace (for longer narration gaps)
    DEMO_PACE=1.5 python run.py

    # Point at a different instance
    DEMO_BASE_URL=http://staging.example.edu python run.py

Environment variables:
    DEMO_BASE_URL   App URL (default: http://localhost:8000)
    DEMO_USERNAME   Login username (default: admin)
    DEMO_PASSWORD   Login password (default: admin)
    DEMO_PACE       Timing multiplier (default: 1.0)
    TTS_ENABLED     Enable TTS narration (default: true, set false to skip)
    TTS_ENDPOINT    OpenAI-compatible TTS endpoint (default: http://localhost:8100/v1)
    TTS_VOICE       Voice name (default: shimmer)
    TTS_MODEL       TTS model name (default: tts-1)
    TTS_SPEED       TTS speed multiplier (default: 1.0)
"""

import importlib
import sys
from pathlib import Path

from config import add_narration, parse_srt, OUTPUT_DIR

# -- Video registry --------------------------------------------------------
# Auto-discovered from video_*.py files in this directory, sorted by name.
# Each module must export a `script` (VideoScript) and a `run(headless)` function.

VIDEOS = sorted(
    p.stem for p in Path(__file__).parent.glob("video_*.py")
)

# Also discover from scripts/ subdirectory
VIDEOS += sorted(
    p.stem for p in (Path(__file__).parent / "scripts").glob("video_*.py")
    if p.stem not in VIDEOS
)


def run_video(module_name: str, headless: bool = True):
    """Import and run a single video module."""
    print(f"\n{'='*60}")
    print(f"  Recording: {module_name}")
    print(f"{'='*60}")

    # Try scripts/ subdirectory first, then root
    try:
        mod = importlib.import_module(f"scripts.{module_name}")
    except ModuleNotFoundError:
        mod = importlib.import_module(module_name)

    print(f"  Title:    {mod.script.title}")
    print(f"  Audience: {mod.script.target_audience}")
    print(f"  Duration: {mod.script.duration_estimate}")
    print(f"  Scenes:   {len(mod.script.scenes)}")
    print()

    mod.run(headless=headless)

    output_dir = Path(__file__).parent / "output" / mod.script.id
    print(f"\n  Output: {output_dir}/")
    if output_dir.exists():
        for f in sorted(output_dir.iterdir()):
            size = f.stat().st_size
            if size > 1024 * 1024:
                size_str = f"{size / (1024*1024):.1f} MB"
            elif size > 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"
            print(f"    {f.name:40s} {size_str}")


def narrate_only(filter_arg: str = ""):
    """Re-run TTS narration on existing video artifacts without re-recording.

    Walks output directories, finds _subtitled.mp4 + .srt pairs, and
    produces _voiced.mp4 for each.  Skips videos missing either file.
    """
    import config
    # --narrate-only implies TTS is the goal
    config.TTS_ENABLED = True

    dirs = sorted(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else []
    if not dirs:
        print("No output directories found.")
        return

    processed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        if filter_arg and filter_arg not in d.name:
            continue

        video_id = d.name
        subtitled = d / f"{video_id}_subtitled.mp4"
        srt = d / f"{video_id}.srt"

        if not subtitled.exists() or not srt.exists():
            missing = []
            if not subtitled.exists():
                missing.append("_subtitled.mp4")
            if not srt.exists():
                missing.append(".srt")
            print(f"  Skipping {video_id} -- missing {', '.join(missing)}")
            continue

        print(f"\n  Narrating: {video_id}")
        result = add_narration(subtitled, srt)
        if result:
            processed += 1

    print(f"\n  Narrated {processed} video(s).")


def main():
    headless = "--headed" not in sys.argv
    narrate_mode = "--narrate-only" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if narrate_mode:
        narrate_only(args[0] if args else "")
        return

    if args:
        # Run specific video(s) by number, name fragment, or project prefix
        for arg in args:
            matches = [v for v in VIDEOS if arg in v]
            if matches:
                for m in matches:
                    run_video(m, headless=headless)
            else:
                print(f"No video matching '{arg}'.  Available:")
                for v in VIDEOS:
                    print(f"  {v}")
                sys.exit(1)
    else:
        # Run all videos
        print("Recording all demo videos...")
        for v in VIDEOS:
            run_video(v, headless=headless)

    print(f"\n{'='*60}")
    print("  All recordings complete!")
    print(f"  Output directory: {Path(__file__).parent / 'output'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
