"""Local control panel served by ``wth-edge run`` (LAN / Tailscale only).

Live camera view with the site's ROIs drawn on (and editable), device state and
telemetry, recording controls, and a browser for recorded clips.

Auth: HTTP Basic, user ``wth``, password from ``WTH_WEB_PASSWORD`` (install.sh generates
one into /etc/wth/secrets.env). With no password set the panel is open, and says so.
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets
import time
from importlib import resources
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import numpy as np
from PIL import Image, ImageDraw

from ..camera import FrameBus
from ..config import Roi, save_roi_overrides
from ..recording import RecordingError

BOUNDARY = "wthframe"
DETECT_COLOUR = (239, 68, 68)
CAR_MOVING_COLOUR = (245, 245, 247)
CAR_STILL_COLOUR = (255, 159, 10)
ROI_COLOURS = [(33, 209, 159), (250, 204, 21), (96, 165, 250), (244, 114, 182), (167, 139, 250), (251, 146, 60), (45, 212, 191)]


ROI_ZOOM_MAX_WIDTH = 1280


def crop_roi(frame, roi: Roi) -> np.ndarray:
    """The ROI cut out of a frame, from the full-resolution image when there is one.

    For a YUV420 hires frame only the cropped planes are converted, so a zoomed view
    costs about as much as the box is big, not the whole sensor frame.
    """
    hires = frame.hires
    if hires is None:
        x1, y1, x2, y2 = roi.to_pixels(frame.image.shape[1], frame.image.shape[0])
        return frame.image[y1:max(y2, y1 + 1), x1:max(x2, x1 + 1)]
    w, h = hires.size
    x1, y1, x2, y2 = roi.to_pixels(w, h)
    if hires.kind == "yuv420" and hires.data.shape[1] == w and h % 2 == 0 and w % 2 == 0:
        # I420 wants an even width and a height that's a multiple of 4.
        x1, y1 = x1 & ~1, y1 & ~3
        cw, ch = max(2, (x2 - x1) & ~1), max(4, (y2 - y1) & ~3)
        cw, ch = min(cw, w - x1), min(ch, h - y1)
        if cw >= 2 and ch >= 4 and cw % 2 == 0 and ch % 4 == 0:
            import cv2

            d = hires.data
            y = d[y1:y1 + ch, x1:x1 + cw]
            u = d[h:h + h // 4].reshape(h // 2, w // 2)[y1 // 2:(y1 + ch) // 2, x1 // 2:(x1 + cw) // 2]
            v = d[h + h // 4:h + h // 2].reshape(h // 2, w // 2)[y1 // 2:(y1 + ch) // 2, x1 // 2:(x1 + cw) // 2]
            i420 = np.concatenate([y.reshape(-1), u.reshape(-1), v.reshape(-1)]).reshape(ch * 3 // 2, cw)
            return cv2.cvtColor(i420, cv2.COLOR_YUV2RGB_I420)
    return hires.rgb()[y1:max(y2, y1 + 1), x1:max(x2, x1 + 1)]


def detections(det: dict | None) -> list[tuple[tuple, tuple, int, str | None]]:
    """What the detectors found, as (frame-fraction box, colour, line width, label)."""
    if not det:
        return []
    out = []
    for v in det.get("vehicles") or []:
        moving = v.get("moving")
        out.append((tuple(v["box"]), CAR_MOVING_COLOUR if moving else CAR_STILL_COLOUR, 2, None if moving else "stopped"))
    boat = det.get("boat") or {}
    if box := boat.get("box"):
        label = f"boat {boat.get('confidence', 0):.2f}" + (f" {boat['direction']}" if boat.get("direction") else "")
        out.append((tuple(box), DETECT_COLOUR, 3, label))
    return out


def encode_roi_jpeg(frame, roi: Roi, quality: int, det: dict | None = None) -> bytes:
    """The ROI zoomed in; with ``det`` the cars and boats inside it are boxed."""
    img = Image.fromarray(np.ascontiguousarray(crop_roi(frame, roi)))
    if img.width > ROI_ZOOM_MAX_WIDTH:
        img = img.resize((ROI_ZOOM_MAX_WIDTH, max(1, round(img.height * ROI_ZOOM_MAX_WIDTH / img.width))))
    found = detections(det)
    if found:
        img = img.convert("RGB")
        d = ImageDraw.Draw(img)
        W, H = img.width, img.height
        for box, colour, width, label in found:
            # Frame fractions to crop pixels, kept to the boxes that overlap this ROI.
            x1, y1 = (box[0] - roi.x) / roi.w * W, (box[1] - roi.y) / roi.h * H
            x2, y2 = (box[2] - roi.x) / roi.w * W, (box[3] - roi.y) / roi.h * H
            x1, x2 = sorted((x1, x2))
            y1, y2 = sorted((y1, y2))
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W - 1, x2), min(H - 1, y2)
            if x2 <= x1 or y2 <= y1:
                continue  # outside this ROI, or only touching its edge
            d.rectangle((x1, y1, x2, y2), outline=colour, width=width * 2)
            if label:
                d.text((max(0, x1) + 4, max(0, y1) + 3), label, fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def encode_jpeg(image, quality: int, rois: dict | None = None, det: dict | None = None,
                only: str | None = None, boxes: bool = True) -> bytes:
    """JPEG of a frame; with ``rois`` and ``boxes`` draws the ROIs (just ``only`` when given),
    with ``det`` what the detectors see: leaf lines, lane levels, cars and boats."""
    img = Image.fromarray(image)
    if (rois and boxes) or det:
        img = img.convert("RGB")
        d = ImageDraw.Draw(img)
        W, H = img.width, img.height
        for i, (name, roi) in enumerate((rois or {}).items()):
            if not boxes or (only and name != only):
                continue
            box = roi.to_pixels(W, H)
            colour = ROI_COLOURS[i % len(ROI_COLOURS)]
            d.rectangle(box, outline=colour, width=2)
            d.text((box[0] + 4, box[1] + 2), name, fill=colour)
        if det:
            for leaf in (det.get("bridge") or {}, det.get("far_leaf") or {}):
                if line := leaf.get("line"):
                    d.line((line[0] * W, line[1] * H, line[2] * W, line[3] * H), fill=DETECT_COLOUR, width=3)
                    if leaf.get("angle_deg") is not None:
                        d.text((line[0] * W, line[1] * H - 12), f"{leaf['angle_deg']:.0f}° {leaf.get('position', '')}", fill=DETECT_COLOUR)
            for name, lane in (det.get("traffic") or {}).items():
                if rois and name in rois and lane.get("level"):
                    box = rois[name].to_pixels(W, H)
                    d.text((box[0] + 4, box[3] - 12), f"{lane['level']} {lane.get('flow') or 0:.3f}", fill=DETECT_COLOUR)
            if (opening := det.get("opening")) and opening.get("phase"):
                d.text((6, H - 14), f"{opening['phase']} · {opening.get('position', '')}", fill=DETECT_COLOUR)
            for box, colour, width, label in detections(det):
                px = (box[0] * W, box[1] * H, box[2] * W, box[3] * H)
                d.rectangle(px, outline=colour, width=width)
                if label and colour == DETECT_COLOUR:  # car labels would cover the lane at this size
                    d.text((px[0] + 3, max(0, px[1] - 12)), label, fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def create_app(config, bus: FrameBus, get_state: Callable[[], dict[str, Any]], recorder=None, store=None,
               tuner=None) -> FastAPI:
    app = FastAPI(title="Watch The Hutch device", docs_url=None, redoc_url=None, openapi_url=None)
    password = os.environ.get("WTH_WEB_PASSWORD", "")
    basic = HTTPBasic(auto_error=False)
    wcfg = config.web
    rois = config.site.rois

    def auth(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not password:
            return
        if creds is None or not (
            secrets.compare_digest(creds.username.encode(), b"wth")
            and secrets.compare_digest(creds.password.encode(), password.encode())
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": 'Basic realm="wth"'})

    @app.get("/healthz")
    def healthz() -> dict:
        # Unauthenticated on purpose: used by `wth-edge doctor` and monitoring.
        st = get_state()
        return {"status": st.get("status"), "camera_ok": st.get("camera", {}).get("ok")}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def index() -> str:
        return resources.files("wth_edge.web").joinpath("static/index.html").read_text()

    @app.get("/api/state", dependencies=[Depends(auth)])
    def api_state() -> JSONResponse:
        st = dict(get_state())
        st["auth_enabled"] = bool(password)
        st["temperature_unit"] = config.ui.temperature_unit.upper()
        st["rois"] = {k: vars(v) for k, v in rois.items()}
        st["site"] = {"bridge_lat": config.site.bridge_lat, "bridge_lon": config.site.bridge_lon,
                      "approach_sides": list(config.site.approach_sides), "ais_enabled": config.ais.enabled}
        return JSONResponse(st)

    @app.get("/snapshot.jpg", dependencies=[Depends(auth)])
    def snapshot(overlay: int = 1, only: str | None = None, boxes: int | None = None, det: int | None = None) -> Response:
        """``overlay`` turns both layers on or off; ``boxes`` (the ROIs) and ``det`` (what the
        detectors found) override it one at a time."""
        frame = bus.latest()
        if frame is None:
            raise HTTPException(503, "no camera frame yet")
        b, dt = overlay if boxes is None else boxes, overlay if det is None else det
        return Response(encode_jpeg(frame.image, wcfg.jpeg_quality, rois, get_state() if dt else None, only, bool(b)),
                        media_type="image/jpeg")

    def need_roi(name: str) -> Roi:
        if name not in rois:
            raise HTTPException(404, f"unknown ROI {name!r}")
        return rois[name]

    @app.get("/stream.mjpg", dependencies=[Depends(auth)])
    async def stream(request: Request, overlay: int = 1, only: str | None = None, roi: str | None = None,
                     boxes: int | None = None, det: int | None = None) -> StreamingResponse:
        """The live view; ``roi`` streams that box zoomed in instead, ``only`` draws just that box.
        ``boxes`` and ``det`` work as on the snapshot."""
        show_boxes, show_det = bool(overlay if boxes is None else boxes), bool(overlay if det is None else det)
        if roi is not None:
            need_roi(roi)
        period = 1.0 / max(wcfg.preview_fps, 0.5)

        async def frames():
            last_seq = -1
            while not await request.is_disconnected():
                t0 = time.monotonic()
                frame = bus.latest()
                if frame is not None and frame.seq != last_seq:
                    last_seq = frame.seq
                    if roi is not None:
                        jpg = await asyncio.to_thread(encode_roi_jpeg, frame, rois[roi], wcfg.jpeg_quality,
                                                      get_state() if show_det else None)
                    else:
                        jpg = await asyncio.to_thread(encode_jpeg, frame.image, wcfg.jpeg_quality, rois,
                                                      get_state() if show_det else None, only, show_boxes)
                    yield (
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode()
                        + jpg
                        + b"\r\n"
                    )
                await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

        return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

    @app.get("/roi/{name}.jpg", dependencies=[Depends(auth)])
    def roi_snapshot(name: str, det: int = 0) -> Response:
        box = need_roi(name)
        frame = bus.latest()
        if frame is None:
            raise HTTPException(503, "no camera frame yet")
        return Response(encode_roi_jpeg(frame, box, wcfg.jpeg_quality, get_state() if det else None),
                        media_type="image/jpeg")

    # ---- recording ----
    def need_recorder():
        if recorder is None:
            raise HTTPException(404, "recording is not set up")
        return recorder

    @app.get("/api/recording", dependencies=[Depends(auth)])
    def recording_status() -> dict:
        return need_recorder().status()

    @app.post("/api/recording/start", dependencies=[Depends(auth)])
    def recording_start(body: dict = Body(default={})) -> dict:
        rec = need_recorder()
        label = body.get("label") or None
        if label is not None and label not in config.recording.labels:
            raise HTTPException(400, f"label must be one of {list(config.recording.labels)}")
        try:
            return rec.start("manual", label=label, note=str(body.get("note", ""))[:500])
        except RecordingError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/recording/stop", dependencies=[Depends(auth)])
    def recording_stop() -> dict:
        meta = need_recorder().stop(by_user=True)
        if meta is None:
            raise HTTPException(409, "not recording")
        return meta

    # ---- clips ----
    def need_store():
        if store is None:
            raise HTTPException(404, "no clip store")
        return store

    def clip_file(clip_id: str, kind: str):
        try:
            path = need_store().paths(clip_id)[kind]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, "no such clip")
        return path

    @app.get("/api/clips", dependencies=[Depends(auth)])
    def clips(limit: int = 200) -> dict:
        st = need_store()
        active = recorder.status()["clip_id"] if recorder else None
        items = st.list(limit)
        for m in items:
            m["recording"] = m["id"] == active
        return {"clips": items}

    @app.get("/clips/{clip_id}.mp4", dependencies=[Depends(auth)])
    def clip_video(clip_id: str, download: int = 0) -> FileResponse:
        path = clip_file(clip_id, "video")
        return FileResponse(path, media_type="video/mp4", filename=path.name if download else None)

    @app.get("/clips/{clip_id}.jpg", dependencies=[Depends(auth)])
    def clip_thumb(clip_id: str) -> FileResponse:
        return FileResponse(clip_file(clip_id, "thumb"), media_type="image/jpeg")

    @app.get("/clips/{clip_id}.json", dependencies=[Depends(auth)])
    def clip_meta(clip_id: str) -> FileResponse:
        return FileResponse(clip_file(clip_id, "meta"), media_type="application/json")

    @app.patch("/api/clips/{clip_id}", dependencies=[Depends(auth)])
    def clip_update(clip_id: str, body: dict = Body(...)) -> dict:
        allowed = {k: body[k] for k in ("keep", "label", "note") if k in body}
        if "keep" in allowed:
            allowed["keep"] = bool(allowed["keep"])
        if allowed.get("label") not in (None, *config.recording.labels):
            raise HTTPException(400, "unknown label")
        meta = need_store().update(clip_id, **allowed)
        if meta is None:
            raise HTTPException(404, "no such clip")
        return meta

    @app.delete("/api/clips/{clip_id}", dependencies=[Depends(auth)])
    def clip_delete(clip_id: str) -> dict:
        if recorder and recorder.status()["clip_id"] == clip_id:
            raise HTTPException(409, "clip is still recording")
        if not need_store().delete(clip_id):
            raise HTTPException(404, "no such clip")
        return {"deleted": clip_id}

    # ---- self-tuning ----
    def need_tuner():
        if tuner is None:
            raise HTTPException(404, "self-tuning is not set up")
        return tuner

    @app.get("/api/tune", dependencies=[Depends(auth)])
    def tune_status() -> dict:
        t = need_tuner()
        return {**t.status(), "reports": t.reports()[:20]}

    @app.post("/api/tune/start", dependencies=[Depends(auth)])
    def tune_start(body: dict = Body(default={})) -> dict:
        from ..tune.session import TuneError

        minutes = body.get("minutes")
        try:
            minutes = float(minutes) if minutes not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "minutes must be a number") from exc
        try:
            return need_tuner().start(minutes)
        except TuneError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/tune/stop", dependencies=[Depends(auth)])
    def tune_stop() -> dict:
        return need_tuner().stop()

    @app.post("/api/tune/reset", dependencies=[Depends(auth)])
    def tune_reset() -> dict:
        t = need_tuner()
        if t.status().get("active"):
            raise HTTPException(409, "self-tuning is running; turn it off first")
        return {"undone": t.reset_tuning()}

    @app.get("/api/tune/reports/{rid}", dependencies=[Depends(auth)])
    def tune_report(rid: str) -> dict:
        r = need_tuner().report(rid)
        if r is None:
            raise HTTPException(404, "no such report")
        return r

    @app.get("/tune/{rid}/{name}", dependencies=[Depends(auth)])
    def tune_frame(rid: str, name: str) -> FileResponse:
        path = need_tuner().frame_path(rid, name)
        if path is None:
            raise HTTPException(404, "no such frame")
        return FileResponse(path, media_type="image/jpeg")

    # ---- ROIs ----
    @app.put("/api/rois", dependencies=[Depends(auth)])
    def rois_update(body: dict = Body(...)) -> dict:
        new = {}
        for name, r in body.items():
            if name not in rois:
                raise HTTPException(400, f"unknown ROI {name!r}")
            try:
                x, y, w, h = (float(r[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(400, f"ROI {name!r} needs numeric x, y, w, h") from exc
            if not (0 <= x < 1 and 0 <= y < 1 and 0.01 <= w <= 1 - x + 1e-6 and 0.01 <= h <= 1 - y + 1e-6):
                raise HTTPException(400, f"ROI {name!r} must lie inside the frame")
            new[name] = Roi(round(x, 4), round(y, 4), round(w, 4), round(h, 4))
        rois.update(new)  # live: the stream and detectors read this dict
        path = save_roi_overrides(config, rois)
        return {"saved_to": str(path), "rois": {k: vars(v) for k, v in rois.items()}}

    return app
