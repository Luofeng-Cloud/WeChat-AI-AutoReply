"""RapidOCR wrapper with lazy, diagnostics-friendly initialisation.

The 1.x scripts constructed the OCR engine at import time:

    ocr_engine = init_ocr()

That call loads ~15 MB of ONNX graphs and builds several ONNX Runtime sessions.
Consequences: the GUI process paid the cost even though it never runs OCR, unit tests
could not import the engine module at all, and a missing model file produced an opaque
traceback before any logging existed.

Initialisation is now lazy and returns a structured status the UI can display.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from . import logutil, paths

DET_MODEL = "ch_PP-OCRv4_det_infer.onnx"
CLS_MODEL = "ch_ppocr_mobile_v2.0_cls_infer.onnx"
REC_MODEL = "ch_PP-OCRv4_rec_infer.onnx"


class OcrUnavailable(RuntimeError):
    """Raised when RapidOCR cannot be imported or initialised."""


def box_center(box):
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def box_bounds(box):
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return (min(xs), min(ys), max(xs), max(ys))


def normalize_result(raw):
    """Turn RapidOCR output into a stable [(box, text, score)] list."""
    normalized = []
    for item in raw or ():
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        text = str(text or "").strip()
        if not text:
            continue
        try:
            points = [[float(p[0]), float(p[1])] for p in box]
        except (TypeError, IndexError, ValueError):
            continue
        if len(points) < 2:
            continue
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 0.0
        normalized.append((points, text, confidence))
    return normalized


class OcrEngine:
    """Lazy singleton around RapidOCR, thread safe.

    One ONNX Runtime session set is reused for every call; creating a set per scan would
    allocate hundreds of megabytes per second.
    """

    def __init__(self, models_dir=None, warmup: bool = True):
        self._lock = threading.RLock()
        self._engine = None
        self._models_dir = Path(models_dir) if models_dir else None
        self._warmup = warmup
        self._error = None
        self._ready_at = None
        self._inference_count = 0
        self._total_seconds = 0.0

    # -- lifecycle --------------------------------------------------------
    @property
    def ready(self):
        return self._engine is not None

    @property
    def error(self):
        return self._error

    @property
    def stats(self):
        with self._lock:
            average = (self._total_seconds / self._inference_count) if self._inference_count else 0.0
            return {
                "ready": self.ready,
                "error": self._error,
                "inferences": self._inference_count,
                "averageSeconds": round(average, 4),
                "modelsDir": str(self._resolve_models_dir() or ""),
                "bundledModels": bool(self._bundled_model_paths()),
            }

    def _resolve_models_dir(self):
        if self._models_dir and self._models_dir.is_dir():
            return self._models_dir
        candidate = paths.models_dir()
        return candidate if candidate.is_dir() else None

    def _bundled_model_paths(self):
        directory = self._resolve_models_dir()
        if not directory:
            return None
        det = directory / DET_MODEL
        rec = directory / REC_MODEL
        if det.is_file() and rec.is_file():
            cls = directory / CLS_MODEL
            return {
                "det": str(det),
                "rec": str(rec),
                "cls": str(cls) if cls.is_file() else None,
            }
        return None

    def load(self, force: bool = False):
        """Build the RapidOCR engine. Returns self; raises OcrUnavailable on failure."""
        with self._lock:
            if self._engine is not None and not force:
                return self
            if force:
                self._engine = None
            try:
                from rapidocr_onnxruntime import RapidOCR
            except Exception as exc:  # pragma: no cover - environment dependent
                self._error = "rapidocr_onnxruntime not installed: %s" % exc
                raise OcrUnavailable(self._error)

            started = time.time()
            try:
                bundled = self._bundled_model_paths()
                if bundled:
                    kwargs = {
                        "det_model_path": bundled["det"],
                        "rec_model_path": bundled["rec"],
                    }
                    if bundled["cls"]:
                        kwargs["cls_model_path"] = bundled["cls"]
                    self._engine = RapidOCR(**kwargs)
                else:
                    # Falls back to the models shipped inside the wheel.
                    self._engine = RapidOCR()
            except Exception as exc:
                self._error = "model load failed: %s" % exc
                self._engine = None
                raise OcrUnavailable(self._error)

            self._error = None
            self._ready_at = time.time()
            logutil.log("[OCR] RapidOCR ready in %.2fs (%s models)" % (
                self._ready_at - started,
                "bundled" if bundled else "default",
            ))
        if self._warmup:
            self._warm_up()
        return self

    def _warm_up(self):
        """Run one inference on a tiny image so the first real scan is not slow."""
        try:
            from PIL import Image
            import numpy as np

            canvas = Image.new("RGB", (64, 32), (255, 255, 255))
            self.infer(np.asarray(canvas))
        except Exception:
            pass

    def close(self):
        with self._lock:
            self._engine = None

    # -- inference --------------------------------------------------------
    def __call__(self, image):
        return self.infer(image)

    def infer(self, image):
        """Run detection + recognition. Returns a list of (box, text, score).

        Every failure mode maps onto an empty result plus a log line, because a single
        bad frame must not take the daemon down.
        """
        if image is None:
            return []
        try:
            self.load()
        except OcrUnavailable:
            return []

        import numpy as np

        with self._lock:
            engine = self._engine
        if engine is None:
            return []

        array = image if isinstance(image, np.ndarray) else np.asarray(image)
        started = time.time()
        try:
            raw, _elapsed = engine(array)
        except Exception as exc:
            logutil.get_bus().warn("[OCR inference failed] %s" % exc)
            return []
        finally:
            with self._lock:
                self._inference_count += 1
                self._total_seconds += time.time() - started

        return normalize_result(raw)

    def infer_downscaled(self, image, max_width: int = 1000):
        """OCR a wide image at reduced resolution and map boxes back to source pixels.

        The 1.x engine downscaled to 1000 px wide "to cut OCR from 5.5 s to 0.8 s" and
        then divided box coordinates by the ratio. That is correct, but the ratio was
        recomputed in three places with three slightly different conditions
        (W > 1100, W > 1100, and a target width of 1000), so a 1050 px window was
        sometimes scaled and sometimes not. One helper, one rule.
        """
        if image is None:
            return [], 1.0
        from PIL import Image

        width, _height = image.size
        if width <= 0 or width <= max_width:
            return self.infer(image), 1.0

        ratio = max_width / float(width)
        scaled = image.resize((max_width, max(1, int(image.size[1] * ratio))), Image.Resampling.BILINEAR)
        remapped = []
        for box, text, score in self.infer(scaled):
            remapped.append(([[point[0] / ratio, point[1] / ratio] for point in box], text, score))
        return remapped, ratio


_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine(models_dir=None):
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = OcrEngine(models_dir=models_dir)
        return _ENGINE


def reset_engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            _ENGINE.close()
        _ENGINE = None
