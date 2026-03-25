#!/usr/bin/env python3
"""Flask server for TEPS Listening Player — static file serving + MP3 processing API."""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

try:
    from pydub import AudioSegment
    from pydub.silence import split_on_silence
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
TMP_DIR = PROJECT_ROOT / "tmp"
MANIFEST_PATH = PROJECT_ROOT / "manifest.json"
INDEX_HTML_PATH = PROJECT_ROOT / "index.html"

MANIFEST_START = "// === MANIFEST (auto-updated by split.py, or edit manually for file:// use) ==="
MANIFEST_END = "// === END MANIFEST ==="

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(PROJECT_ROOT), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB upload limit


# ── Helpers (shared with split.py) ───────────────────────────────────────────
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


def upsert_manifest_files(manifest: dict, name: str, output_dir: Path, files: list) -> dict:
    """Upsert a test entry with a full files array (not just questionCount)."""
    rel_path = output_dir.relative_to(PROJECT_ROOT).as_posix()
    entry = {
        "name": name,
        "displayName": make_display_name(name),
        "path": rel_path,
        "files": files,
    }
    tests = manifest.get("tests", [])
    for i, t in enumerate(tests):
        if t["name"] == name:
            tests[i] = entry
            break
    else:
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
    indented_lines = [
        "        " + line if line.strip() else line
        for line in manifest_json.splitlines()
    ]
    indented_json = "\n".join(indented_lines)
    replacement = (
        MANIFEST_START + "\n"
        + "        const EMBEDDED_MANIFEST = "
        + indented_json.strip() + ";\n"
        + "        " + MANIFEST_END
    )
    new_html, n = pattern.subn(replacement, html)
    if n:
        with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(new_html)


# ── Static routes ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file(str(INDEX_HTML_PATH))


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "pydub": PYDUB_AVAILABLE})


# Serve temp segment files for in-browser preview
@app.route("/tmp/<session_id>/raw/<filename>")
def serve_tmp(session_id, filename):
    # Sanitize to prevent path traversal
    if ".." in session_id or ".." in filename:
        return jsonify({"error": "invalid path"}), 400
    path = TMP_DIR / session_id / "raw" / filename
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(path), mimetype="audio/mpeg")


# ── Upload & split ────────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    if not PYDUB_AVAILABLE:
        return jsonify({"error": "pydub is not installed on the server."}), 500

    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".mp3"):
        return jsonify({"error": "Only MP3 files are supported."}), 400

    # Derive test name from filename
    stem = Path(f.filename).stem.lower()
    # Remove spaces/special chars
    test_name = re.sub(r"[^a-z0-9_]", "", stem.replace(" ", "_"))
    if not test_name:
        test_name = "test"

    # Create session temp dir
    session_id = str(uuid.uuid4())
    session_dir = TMP_DIR / session_id
    raw_dir = session_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded file
    original_path = session_dir / "original.mp3"
    f.save(str(original_path))

    # Load and split
    try:
        audio = AudioSegment.from_mp3(str(original_path))
    except FileNotFoundError:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": "ffmpeg not found. Install ffmpeg and add it to PATH."}), 500
    except Exception as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": f"Failed to load audio: {str(e)}"}), 500

    try:
        segments = split_on_silence(
            audio,
            min_silence_len=3000,
            silence_thresh=-40,
            keep_silence=300,
        )
    except Exception as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": f"Splitting failed: {str(e)}"}), 500

    # Filter short segments
    segments = [s for s in segments if len(s) >= 500]

    if not segments:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": "No segments found. Try adjusting silence parameters."}), 400

    # Export raw segments
    width = max(3, len(str(len(segments))))
    seg_meta = []
    for i, seg in enumerate(segments):
        filename = f"{str(i + 1).zfill(width)}.mp3"
        seg.export(str(raw_dir / filename), format="mp3")
        seg_meta.append({
            "index": i,
            "filename": filename,
            "duration_ms": len(seg),
        })

    # Save session metadata
    meta = {
        "test_name": test_name,
        "original_filename": f.filename,
        "segments": seg_meta,
    }
    with open(session_dir / "meta.json", "w", encoding="utf-8") as mf:
        json.dump(meta, mf, indent=2)

    return jsonify({
        "session_id": session_id,
        "test_name": test_name,
        "segments": seg_meta,
    })


# ── Session query ─────────────────────────────────────────────────────────────
@app.route("/api/session/<session_id>")
def get_session(session_id):
    if ".." in session_id:
        return jsonify({"error": "invalid"}), 400
    meta_path = TMP_DIR / session_id / "meta.json"
    if not meta_path.exists():
        return jsonify({"error": "Session not found."}), 404
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    return jsonify({
        "session_id": session_id,
        "test_name": meta["test_name"],
        "segments": meta["segments"],
    })


# ── Finalize ──────────────────────────────────────────────────────────────────
@app.route("/api/finalize", methods=["POST"])
def finalize():
    if not PYDUB_AVAILABLE:
        return jsonify({"error": "pydub not available"}), 500

    data = request.get_json()
    session_id = data.get("session_id", "")
    test_name = data.get("test_name", "").strip().lower()
    rows = data.get("segments", [])

    if not test_name or not rows:
        return jsonify({"error": "Missing test_name or segments."}), 400
    if ".." in session_id:
        return jsonify({"error": "invalid"}), 400

    session_dir = TMP_DIR / session_id
    raw_dir = session_dir / "raw"
    if not raw_dir.exists():
        return jsonify({"error": "Session not found or expired."}), 404

    # Load raw segment filenames from meta
    meta_path = session_dir / "meta.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    raw_files = {seg["index"]: seg["filename"] for seg in meta["segments"]}

    # Create output directory
    output_dir = PROJECT_ROOT / "script" / test_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clear existing mp3 files
    for old in output_dir.glob("*.mp3"):
        old.unlink()

    manifest_files = []
    for row in rows:
        indices = row.get("indices", [])
        label = row.get("label", "")
        if not indices or not label:
            continue

        filename = label.replace("-", "_") + ".mp3"
        out_path = output_dir / filename

        if len(indices) == 1:
            # Single segment: copy file
            src = raw_dir / raw_files[indices[0]]
            shutil.copy2(str(src), str(out_path))
        else:
            # Multiple segments: concatenate
            combined = AudioSegment.empty()
            for idx in indices:
                seg_path = raw_dir / raw_files[idx]
                combined += AudioSegment.from_mp3(str(seg_path))
            combined.export(str(out_path), format="mp3")

        manifest_files.append({"file": filename, "label": label})

    # Update manifest.json
    manifest = load_manifest()
    manifest = upsert_manifest_files(manifest, test_name, output_dir, manifest_files)
    save_manifest(manifest)

    # Update embedded manifest in index.html
    update_index_html(manifest)

    # Clean up session temp files
    shutil.rmtree(session_dir, ignore_errors=True)

    return jsonify({"ok": True, "test_name": test_name, "file_count": len(manifest_files)})


# ── Cancel session ────────────────────────────────────────────────────────────
@app.route("/api/session/<session_id>", methods=["DELETE"])
def cancel_session(session_id):
    if ".." in session_id:
        return jsonify({"error": "invalid"}), 400
    session_dir = TMP_DIR / session_id
    shutil.rmtree(session_dir, ignore_errors=True)
    return jsonify({"ok": True})


# ── Git push ──────────────────────────────────────────────────────────────────
@app.route("/api/git-push", methods=["POST"])
def git_push():
    data = request.get_json() or {}
    test_name = data.get("test_name", "").strip()

    # Check if git is available
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"],
                       cwd=str(PROJECT_ROOT), check=True,
                       capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return jsonify({"error": "Git not available in this environment."}), 400

    # Configure git identity if not set (needed on Render)
    try:
        subprocess.run(["git", "config", "user.email"], cwd=str(PROJECT_ROOT),
                       check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(["git", "config", "user.email", "server@teps-player"],
                       cwd=str(PROJECT_ROOT))
        subprocess.run(["git", "config", "user.name", "TEPS Server"],
                       cwd=str(PROJECT_ROOT))

    # Stage files
    files_to_add = ["manifest.json", "index.html"]
    if test_name:
        files_to_add.append(f"script/{test_name}/")

    result = subprocess.run(
        ["git", "add"] + files_to_add,
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({"error": result.stderr}), 500

    # Check if there's anything to commit
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(PROJECT_ROOT), capture_output=True
    )
    if status.returncode == 0:
        return jsonify({"ok": True, "message": "Nothing to commit."})

    commit_msg = f"Add {test_name}" if test_name else "Update manifest"
    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({"error": result.stderr}), 500

    # Push (use GITHUB_TOKEN env var if available for Render)
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        # Get remote URL and inject token
        remote_url_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True
        )
        remote_url = remote_url_result.stdout.strip()
        if "github.com" in remote_url and "https://" in remote_url:
            authed_url = remote_url.replace(
                "https://", f"https://{token}@"
            )
            push_result = subprocess.run(
                ["git", "push", authed_url, "HEAD:main"],
                cwd=str(PROJECT_ROOT),
                capture_output=True, text=True
            )
        else:
            push_result = subprocess.run(
                ["git", "push"],
                cwd=str(PROJECT_ROOT),
                capture_output=True, text=True
            )
    else:
        push_result = subprocess.run(
            ["git", "push"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True
        )

    if push_result.returncode != 0:
        return jsonify({"error": push_result.stderr or push_result.stdout}), 500

    return jsonify({"ok": True, "message": "Pushed to GitHub successfully."})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()

    TMP_DIR.mkdir(exist_ok=True)
    print(f"Starting TEPS server on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
