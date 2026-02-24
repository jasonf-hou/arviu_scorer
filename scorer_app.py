#!/usr/bin/env python3
"""
Web-based Scoring App for Inter-Rater Reliability

Scorers view composite images (aligned participant drawing overlaid on reference)
and mark paired points: (1) the participant's drawn mark, (2) the corresponding
reference target. Each pair captures the localization error distance.

Deployment:
  Local:   python3 scorer_app.py
  Package: pip install pyinstaller && pyinstaller --onefile scorer_app.py
  Cloud:   deploy to Render/Railway with requirements.txt
"""

import os
import json
import csv
from pathlib import Path
from datetime import datetime

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, send_file, Response
)
import zipfile
import io

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "arviu_scorer_dev_key")

# Paths — resolve relative to this script so it works from any CWD
BASE_DIR = Path(__file__).resolve().parent
ALIGNED_DIR = BASE_DIR / "aligned_output"
SCORER_DATA_DIR = BASE_DIR / "scorer_data"
SCORER_DATA_DIR.mkdir(exist_ok=True)

# Reference images
REFERENCE_AR = BASE_DIR / "reference_png.png"       # for 2D/AR, AR-VIU
REFERENCE_SCREEN = BASE_DIR / "reference_screen.png"  # for 2D/Screen, 3D/Screen
AR_SYSTEMS = {"2D/AR", "AR-VIU"}

# Pixel-to-mm (same constants as icc_analysis.py)
BOX_W_INCHES = 5.84
BOX_H_INCHES = 6.31
IMG_W_PX = 1110
IMG_H_PX = 1215
PX_PER_INCH = ((IMG_W_PX / BOX_W_INCHES) + (IMG_H_PX / BOX_H_INCHES)) / 2
MM_PER_PX = 25.4 / PX_PER_INCH

# CSV header for scorer output
CSV_HEADER = [
    "image_code", "measurement_id",
    "point1_x", "point1_y",   # participant's drawn mark
    "point2_x", "point2_y",   # reference target
    "distance_px", "distance_mm",
    "timestamp",
]


def load_manifest():
    manifest_path = ALIGNED_DIR / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return {}


def get_image_list():
    manifest = load_manifest()
    return sorted(manifest.keys())


def get_scorer_progress(scorer_id):
    """Return set of image codes already scored."""
    scores_path = SCORER_DATA_DIR / scorer_id / "scores.csv"
    scored = set()
    if scores_path.exists():
        with open(scores_path) as f:
            for row in csv.DictReader(f):
                scored.add(row["image_code"])
    return scored


def save_measurements(scorer_id, image_code, measurements):
    """Save paired-point measurements for one image."""
    scorer_dir = SCORER_DATA_DIR / scorer_id
    scorer_dir.mkdir(exist_ok=True)
    scores_path = scorer_dir / "scores.csv"

    file_exists = scores_path.exists()
    with open(scores_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        for m in measurements:
            dx = m["p1_x"] - m["p2_x"]
            dy = m["p1_y"] - m["p2_y"]
            dist_px = (dx**2 + dy**2) ** 0.5
            dist_mm = dist_px * MM_PER_PX
            writer.writerow([
                image_code, m["id"],
                m["p1_x"], m["p1_y"],
                m["p2_x"], m["p2_y"],
                f"{dist_px:.2f}", f"{dist_mm:.2f}",
                datetime.now().isoformat(),
            ])


# ── Routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    scorer_id = request.form.get("scorer_id", "").strip()
    if not scorer_id:
        return redirect(url_for("index"))
    session["scorer_id"] = scorer_id
    return redirect(url_for("score_next"))


@app.route("/score")
def score_next():
    scorer_id = session.get("scorer_id")
    if not scorer_id:
        return redirect(url_for("index"))

    all_codes = get_image_list()
    scored = get_scorer_progress(scorer_id)
    remaining = [c for c in all_codes if c not in scored]

    if not remaining:
        return render_template("done.html", total=len(all_codes), scorer_id=scorer_id)

    code = remaining[0]
    manifest = load_manifest()
    system = manifest.get(code, {}).get("system", "")
    ref_type = "ar" if system in AR_SYSTEMS else "screen"
    return render_template(
        "score.html",
        image_code=code,
        image_url=f"/image/{code}",
        reference_url=f"/reference/{ref_type}",
        progress_done=len(scored),
        progress_total=len(all_codes),
        scorer_id=scorer_id,
        mm_per_px=MM_PER_PX,
    )


@app.route("/image/<code>")
def serve_image(code):
    img_path = ALIGNED_DIR / f"{code}.png"
    if img_path.exists():
        return send_file(img_path, mimetype="image/png")
    return "Not found", 404


@app.route("/reference/<ref_type>")
def serve_reference(ref_type):
    if ref_type == "ar" and REFERENCE_AR.exists():
        return send_file(REFERENCE_AR, mimetype="image/png")
    if ref_type == "screen" and REFERENCE_SCREEN.exists():
        return send_file(REFERENCE_SCREEN, mimetype="image/png")
    return "Not found", 404


@app.route("/submit", methods=["POST"])
def submit_scores():
    scorer_id = session.get("scorer_id")
    if not scorer_id:
        return jsonify({"error": "No scorer session"}), 400

    data = request.get_json()
    image_code = data.get("image_code")
    measurements = data.get("measurements", [])

    if not image_code:
        return jsonify({"error": "No image code"}), 400

    save_measurements(scorer_id, image_code, measurements)
    return jsonify({"status": "ok", "next": url_for("score_next")})


@app.route("/progress")
def progress():
    scorer_id = session.get("scorer_id")
    if not scorer_id:
        return redirect(url_for("index"))
    all_codes = get_image_list()
    scored = get_scorer_progress(scorer_id)
    return jsonify({
        "scorer_id": scorer_id,
        "total": len(all_codes),
        "scored": len(scored),
        "remaining": len(all_codes) - len(scored),
    })


@app.route("/admin/download")
def download_all_scores():
    """Download all scorer data as a zip file (for the researcher)."""
    if not SCORER_DATA_DIR.exists():
        return "No data yet", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for scorer_dir in sorted(SCORER_DATA_DIR.iterdir()):
            if not scorer_dir.is_dir():
                continue
            scores_path = scorer_dir / "scores.csv"
            if scores_path.exists():
                zf.write(scores_path, f"{scorer_dir.name}/scores.csv")

        # Include manifest for de-blinding
        manifest_path = ALIGNED_DIR / "manifest.json"
        if manifest_path.exists():
            zf.write(manifest_path, "manifest.json")

    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=scorer_data.zip"},
    )


@app.route("/admin/status")
def admin_status():
    """Overview of all scorers' progress."""
    all_codes = get_image_list()
    total = len(all_codes)
    scorers = []
    if SCORER_DATA_DIR.exists():
        for d in sorted(SCORER_DATA_DIR.iterdir()):
            if d.is_dir() and (d / "scores.csv").exists():
                scored = get_scorer_progress(d.name)
                scorers.append({
                    "id": d.name,
                    "scored": len(scored),
                    "total": total,
                    "pct": round(len(scored) / total * 100) if total else 0,
                })
    return jsonify({"total_images": total, "scorers": scorers})


if __name__ == "__main__":
    manifest = load_manifest()
    n = len(manifest)
    print(f"Aligned images: {ALIGNED_DIR}  ({n} images)")
    print(f"Scorer data:    {SCORER_DATA_DIR}")
    print(f"Conversion:     {MM_PER_PX:.4f} mm/px")
    print(f"\nOpen http://localhost:5050 in your browser")
    app.run(host="0.0.0.0", port=5050, debug=True)
