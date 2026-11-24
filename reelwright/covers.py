"""Cover frames: pick the most clickable frame of a *rendered* clip (so crop, zoom and captions are already in it).

Score = sharpness + good exposure + colourfulness, with a bonus for a visible face (bigger = better).
Used as the YouTube custom thumbnail and the TikTok cover frame, and shown in the editor.
"""
import subprocess
from pathlib import Path

import cv2
import numpy as np

from . import media


def frame_score(img, faces: int = 0, face_area: float = 0.0) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharp = min(float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 600.0, 1.0)
    exposure = 1.0 - min(abs(float(gray.mean()) / 255 - 0.5) / 0.5, 1.0)
    b, g, r = (c.astype(np.float32) for c in cv2.split(img))
    rg, yb = r - g, 0.5 * (r + g) - b  # Hasler-Süsstrunk colourfulness
    colorful = min((np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())) / 110.0, 1.0)
    face = (0.15 + min(face_area * 3, 0.25)) if faces else 0.0
    return round(0.4 * sharp + 0.25 * exposure + 0.2 * float(colorful) + face, 4)


def pick(video, samples: int = 24, faces: bool = True) -> float | None:
    """Seconds into the video of the best-looking frame (skipping the first/last 8%: fades and title cards)."""
    cap = cv2.VideoCapture(str(video))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / fps
        if dur <= 0:
            return None
        detector = None
        best, best_t = -1.0, None
        for i in range(samples):
            t = dur * (0.08 + 0.84 * (i + 0.5) / samples)
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            scale = 480 / max(frame.shape[:2])
            small = cv2.resize(frame, None, fx=scale, fy=scale)
            n, area = 0, 0.0
            if faces:
                try:
                    detector = detector or media.face_detector(small.shape[1], small.shape[0])
                    _, found = detector.detect(small)
                    if found is not None and len(found):
                        n = len(found)
                        area = max(float(f[2] * f[3]) for f in found) / (small.shape[0] * small.shape[1])
                except Exception:
                    faces = False  # no model / offline: score without faces
            s = frame_score(small, n, area)
            if s > best:
                best, best_t = s, t
        return round(best_t, 2) if best_t is not None else None
    finally:
        cap.release()


def make(video, out, samples: int = 24, faces: bool = True) -> float | None:
    """Write the best frame of `video` to `out` (JPEG); returns its time in seconds, or None if nothing usable."""
    t = pick(video, samples, faces)
    if t is None:
        return None
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([media.FFMPEG, "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)],
                       capture_output=True, creationflags=media.NO_WINDOW)
    return t if r.returncode == 0 and Path(out).exists() else None
