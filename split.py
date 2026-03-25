#!/usr/bin/env python3
"""Split MP3 audio files on silence boundaries for TEPS listening tests."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from pydub import AudioSegment
    from pydub.silence import split_on_silence
except ImportError:
    print("Error: pydub is not installed. Run: pip install pydub>=0.25.1")
    sys.exit(1)


PROJECT_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PROJECT_ROOT / "manifest.json"
INDEX_HTML_PATH = PROJECT_ROOT / "index.html"

MANIFEST_START = "// === MANIFEST (auto-updated by split.py, or edit manually for file:// use) ==="
MANIFEST_END = "// === END MANIFEST ==="


def parse_args():
    parser = argparse.ArgumentParser(
        description="Split an MP3 file on silence into numbered segments."
    )
    parser.add_argument("input_file", help="Path to the input MP3 file")
    parser.add_argument(
        "--threshold",
        type=int,
        default=-40,
        help="Silence threshold in dBFS (default: -40)",
    )
    parser.add_argument(
        "--min-silence-len",
        type=int,
        default=2500,
        help="Minimum silence length in ms (default: 2500)",
    )
    parser.add_argument(
        "--keep-silence",
        type=int,
        default=300,
        help="Amount of silence to keep at boundaries in ms (default: 300)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Override output directory (default: derived from filename)",
    )
    return parser.parse_args()


def derive_output_dir(input_file: Path) -> Path:
    stem = input_file.stem.lower()
    return PROJECT_ROOT / "script" / stem


def make_display_name(name: str) -> str:
    spaced = re.sub(r"(\d+)", r" \1", name)
    return spaced.strip().title()


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tests": []}


def save_manifest(manifest: dict):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


def upsert_manifest(manifest: dict, name: str, output_dir: Path, count: int) -> dict:
    rel_path = output_dir.relative_to(PROJECT_ROOT).as_posix()
    entry = {
        "name": name,
        "displayName": make_display_name(name),
        "path": rel_path,
        "questionCount": count,
    }

    tests = manifest.get("tests", [])
    found = False
    for i, t in enumerate(tests):
        if t["name"] == name:
            tests[i] = entry
            found = True
            break
    if not found:
        tests.append(entry)

    tests.sort(key=lambda t: t["name"])
    manifest["tests"] = tests
    return manifest


def update_index_html(manifest: dict):
    if not INDEX_HTML_PATH.exists():
        return

    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    pattern = re.compile(
        re.escape(MANIFEST_START) + r".*?" + re.escape(MANIFEST_END),
        re.DOTALL,
    )

    manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False)
    # Indent the JSON to match surrounding code (8 spaces for inside the script)
    indented_lines = []
    for line in manifest_json.splitlines():
        indented_lines.append("        " + line if line.strip() else line)
    indented_json = "\n".join(indented_lines)

    replacement = (
        MANIFEST_START
        + "\n"
        + "        const EMBEDDED_MANIFEST = "
        + indented_json.strip()
        + ";\n"
        + "        "
        + MANIFEST_END
    )

    new_html, count = pattern.subn(replacement, html)
    if count == 0:
        print("Warning: Could not find manifest markers in index.html")
        return

    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"Updated embedded manifest in index.html")


def main():
    args = parse_args()

    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = derive_output_dir(input_path)

    test_name = input_path.stem.lower()

    print(f"Input:      {input_path}")
    print(f"Output dir: {output_dir}")
    print(f"Test name:  {test_name}")
    print()

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load audio
    print("Loading audio file...")
    try:
        audio = AudioSegment.from_mp3(str(input_path))
    except FileNotFoundError:
        print(
            "Error: ffmpeg/ffprobe not found. Install ffmpeg and ensure it is on your PATH."
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error loading audio: {e}")
        sys.exit(1)

    duration_s = len(audio) / 1000.0
    print(f"Loaded: {duration_s:.1f}s ({len(audio)}ms)")
    print()

    # Split on silence
    print(
        f"Detecting silence (threshold={args.threshold} dBFS, min_len={args.min_silence_len}ms)..."
    )
    segments = split_on_silence(
        audio,
        min_silence_len=args.min_silence_len,
        silence_thresh=args.threshold,
        keep_silence=args.keep_silence,
    )
    print(f"Found {len(segments)} raw segments")

    # Filter short segments
    segments = [s for s in segments if len(s) >= 500]
    print(f"After filtering (<500ms): {len(segments)} segments")
    print()

    if len(segments) == 0:
        print("Warning: No segments found. Try adjusting --threshold or --min-silence-len.")
        sys.exit(1)

    # Clear existing MP3 files in output dir
    existing = list(output_dir.glob("*.mp3"))
    if existing:
        print(f"Clearing {len(existing)} existing file(s) in {output_dir}")
        for f in existing:
            f.unlink()

    # Determine zero-padding width
    width = max(2, len(str(len(segments))))

    # Export segments
    print("Exporting segments:")
    for i, segment in enumerate(segments, start=1):
        filename = f"{str(i).zfill(width)}.mp3"
        filepath = output_dir / filename
        segment.export(str(filepath), format="mp3")
        seg_duration = len(segment) / 1000.0
        print(f"  {filename}  ({seg_duration:.1f}s)")

    print()
    print(f"Exported {len(segments)} segments to {output_dir}")

    # Update manifest.json
    manifest = load_manifest()
    manifest = upsert_manifest(manifest, test_name, output_dir, len(segments))
    save_manifest(manifest)
    print(f"Updated {MANIFEST_PATH}")

    # Update index.html
    update_index_html(manifest)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
