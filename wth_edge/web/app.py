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
from PIL import Image, ImageDraw

from ..camera import FrameBus
from ..config import Roi, save_roi_overrides
from ..recording import RecordingError

BOUNDARY = "wthframe"
DETECT_COLOUR = (239, 68, 68)
ROI_COLOURS = [(33, 209, 159), (250, 204, 21), (96, 165, 250), (244, 114, 182)]


def encode_jpeg(image, quality: int, rois: dict | None = None, det: dict | None = None) -> bytes:
    """JPEG of a frame; with ``rois`` draws the boxes, with ``det`` what the detectors see."""
    img = Image.fromarray(image)
    if rois or det:
        img = img.convert("RGB")
        d = ImageDraw.Draw(img)
        W, H = img.width, img.height
        for i, (name, roi) in enumerate((rois or {}).items()):
            box = roi.to_pixels(W, H)
            colour = ROI_COLOURS[i % len(ROI_COLOURS)]
            d.rectangle(box, outline=colour, width=2)
            d.text((box[0] + 4, box[1] + 2), name, fill=colour)
        if det:
            bridge, boat = det.get("bridge") or {}, det.get("boat") or {}
            if line := bridge.get("line"):
                d.line((line[0] * W, line[1] * H, line[2] * W, line[3] * H), fill=DETECT_COLOUR, width=3)
                if bridge.get("angle_deg") is not None:
                    d.text((line[0] * W, line[1] * H - 12), f"{bridge['angle_deg']:.0f}° {bridge.get('position', '')}", fill=DETECT_COLOUR)
            if box := boat.get("box"):
                px = (box[0] * W, box[1] * H, box[2] * W, box[3] * H)
                d.rectangle(px, outline=DETECT_COLOUR, width=3)
                label = f"boat {boat.get('confidence', 0):.2f}" + (f" {boat['direction']}" if boat.get("direction") else "")
                d.text((px[0] + 3, max(0, px[1] - 12)), label, fill=DETECT_COLOUR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def create_app(config, bus: FrameBus, get_state: Callable[[], dict[str, Any]], recorder=None, store=None) -> FastAPI:
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
        return JSONResponse(st)

    @app.get("/snapshot.jpg", dependencies=[Depends(auth)])
    def snapshot(overlay: int = 1) -> Response:
        frame = bus.latest()
        if frame is None:
            raise HTTPException(503, "no camera frame yet")
        return Response(encode_jpeg(frame.image, wcfg.jpeg_quality, rois if overlay else None,
                                    get_state() if overlay else None), media_type="image/jpeg")

    @app.get("/stream.mjpg", dependencies=[Depends(auth)])
    async def stream(request: Request, overlay: int = 1) -> StreamingResponse:
        period = 1.0 / max(wcfg.preview_fps, 0.5)

        async def frames():
            last_seq = -1
            while not await request.is_disconnected():
                t0 = time.monotonic()
                frame = bus.latest()
                if frame is not None and frame.seq != last_seq:
                    last_seq = frame.seq
                    jpg = await asyncio.to_thread(encode_jpeg, frame.image, wcfg.jpeg_quality,
                                                  rois if overlay else None, get_state() if overlay else None)
                    yield (
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode()
                        + jpg
                        + b"\r\n"
                    )
                await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

        return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

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
