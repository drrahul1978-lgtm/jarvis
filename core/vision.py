"""Let Jarvis look through a camera and say what is there.

Object detection runs locally with YOLO — no image ever leaves the machine,
which matters rather a lot for a camera pointed at your home.

The camera is opened for the moment a look takes and released immediately
afterwards. Holding it open would keep the webcam light on permanently and stop
every other program from using it, and a camera that is quietly always-on is not
a thing to build by accident.
"""

import threading
import time

from . import config

# COCO's 80 classes, which is what the stock model knows. Grouped so Jarvis can
# talk about them naturally rather than reciting labels.
PEOPLE = {"person"}
DEVICES = {
    "cell phone", "laptop", "keyboard", "mouse", "remote", "tv", "monitor",
    "microwave", "oven", "toaster", "refrigerator", "clock", "hair drier",
}
_model = None
_model_lock = threading.Lock()
_last_seen: dict[str, float] = {}


class VisionUnavailable(Exception):
    """The camera or detector is not installed."""


class VisionError(Exception):
    """Something went wrong looking."""


def _load():
    """Load the detector once and keep it. Cold start costs seconds."""
    global _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise VisionUnavailable(
                "Seeing needs the vision extras. Install them with:  "
                "pip install -r requirements-vision.txt"
            ) from exc

        name = config.VISION_MODEL
        try:
            _model = YOLO(name)
        except Exception as exc:  # noqa: BLE001
            raise VisionError(f"Could not load the detector {name!r}: {exc}") from exc
        return _model


def _open_camera(index: int | None = None):
    try:
        import cv2
    except ImportError as exc:
        raise VisionUnavailable(
            "Seeing needs OpenCV. Install it with:  "
            "pip install -r requirements-vision.txt"
        ) from exc

    index = config.CAMERA_INDEX if index is None else index
    # DirectShow avoids a slow enumeration path on Windows; ignored elsewhere.
    backend = getattr(cv2, "CAP_DSHOW", 0) if hasattr(cv2, "CAP_DSHOW") else 0
    camera = cv2.VideoCapture(index, backend) if backend else cv2.VideoCapture(index)
    if not camera.isOpened():
        camera.release()
        raise VisionError(
            f"Camera {index} would not open. Another program may be using it. "
            "Set JARVIS_CAMERA to a different number if you have more than one."
        )
    return camera


_frame_cache = None
_frame_at = 0.0
FRAME_TTL = 1.5


def grab(index: int | None = None, fresh: bool = False):
    """Take a single frame, then let the camera go.

    Frames are reused for a second and a half. That is partly speed, but mostly
    correctness: opening and releasing a webcam repeatedly in quick succession
    returns dark, empty frames on Windows, so two questions asked moments apart
    would disagree about what is in the room. They should see the same view.
    """
    global _frame_cache, _frame_at

    if not fresh and _frame_cache is not None and (time.time() - _frame_at) < FRAME_TTL:
        return _frame_cache

    camera = _open_camera(index)
    try:
        # A webcam's first frames are dark while exposure settles, and some
        # return nothing at all until they have been read from a few times.
        frame = None
        for _ in range(config.CAMERA_WARMUP):
            ok, candidate = camera.read()
            if ok and candidate is not None and candidate.any():
                frame = candidate
            time.sleep(0.04)
        if frame is None:
            raise VisionError("The camera opened but produced no image.")

        _frame_cache, _frame_at = frame, time.time()
        return frame
    finally:
        camera.release()


def detect(frame=None, threshold: float | None = None) -> list[dict]:
    """Return what is in the frame, most confident first."""
    model = _load()
    if frame is None:
        frame = grab()
    threshold = config.VISION_CONFIDENCE if threshold is None else threshold

    try:
        result = model(frame, verbose=False)[0]
    except Exception as exc:  # noqa: BLE001
        raise VisionError(f"Detection failed: {exc}") from exc

    found = []
    for box in result.boxes:
        confidence = float(box.conf[0])
        if confidence < threshold:
            continue
        found.append({
            "name": model.names[int(box.cls[0])],
            "confidence": confidence,
        })
    found.sort(key=lambda item: -item["confidence"])

    now = time.time()
    for item in found:
        _last_seen[item["name"]] = now
    return found


def _phrase(counts: dict[str, int]) -> str:
    """Turn {'person': 2, 'keyboard': 1} into 'two people and a keyboard'."""
    words = {1: "a", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
    parts = []
    for name, count in counts.items():
        if count == 1:
            article = "an" if name[0] in "aeiou" else "a"
            parts.append(f"{article} {name}")
        else:
            plural = "people" if name == "person" else f"{name}s"
            parts.append(f"{words.get(count, count)} {plural}")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def look(index: int | None = None) -> str:
    """Describe what the camera can see, in a sentence."""
    frame = grab(index)
    found = detect(frame)
    if not found:
        return "The camera is working, but I cannot make out anything I recognise."

    counts: dict[str, int] = {}
    for item in found:
        counts[item["name"]] = counts.get(item["name"], 0) + 1

    detail = ", ".join(
        f"{item['name']} ({item['confidence']:.0%})" for item in found[:8]
    )
    return f"I can see {_phrase(counts)}.\nDetected: {detail}"


def find(what: str, index: int | None = None) -> str:
    """Answer whether a particular thing is visible."""
    wanted = what.strip().lower().rstrip("?")
    found = detect(grab(index))
    names = [item["name"] for item in found]

    matches = [item for item in found
               if wanted in item["name"] or item["name"] in wanted]
    if matches:
        best = max(item["confidence"] for item in matches)
        count = len(matches)
        how_many = "one" if count == 1 else str(count)
        return f"Yes — {how_many} {wanted} visible ({best:.0%} confident)."

    model = _load()
    if not any(wanted in label for label in model.names.values()):
        return (
            f"I cannot recognise {wanted!r} — I only know {len(model.names)} "
            "everyday object types, not arbitrary things."
        )
    seen = ", ".join(sorted(set(names))) or "nothing"
    return f"No {wanted} in view. I can see: {seen}."


def known_objects() -> str:
    model = _load()
    labels = sorted(model.names.values())
    return f"I can recognise {len(labels)} object types: " + ", ".join(labels)


def warm() -> None:
    """Load the detector ahead of time, so the first look is not the slow one."""
    try:
        _load()
    except (VisionUnavailable, VisionError):
        pass


def cameras() -> str:
    """Which camera indexes actually produce an image."""
    try:
        import cv2
    except ImportError as exc:
        raise VisionUnavailable("OpenCV is not installed.") from exc

    working = []
    for index in range(4):
        backend = getattr(cv2, "CAP_DSHOW", 0)
        camera = cv2.VideoCapture(index, backend) if backend else cv2.VideoCapture(index)
        try:
            if camera.isOpened():
                ok, frame = camera.read()
                if ok and frame is not None:
                    working.append(f"  [{index}] {frame.shape[1]}x{frame.shape[0]}")
        finally:
            camera.release()
    if not working:
        return "No working cameras found."
    return "Cameras:\n" + "\n".join(working)
