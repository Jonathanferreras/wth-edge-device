"""Local control panel served by ``wth-edge run`` (LAN / Tailscale only).

This first version shows the live camera view (with the site's ROIs drawn on), the
device state and telemetry. Recording controls and the clip browser come next.

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

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from PIL import Image, ImageDraw

from ..camera import FrameBus

BOUNDARY = "wthframe"
ROI_COLOURS = [(33, 209, 159), (250, 204, 21), (96, 165, 250), (244, 114, 182)]


def encode_jpeg(image, quality: int, rois: dict | None = None) -> bytes:
    img = Image.fromarray(image)
    if rois:
        img = img.convert("RGB")
        d = ImageDraw.Draw(img)
        for i, (name, roi) in enumerate(rois.items()):
            box = roi.to_pixels(img.width, img.height)
            colour = ROI_COLOURS[i % len(ROI_COLOURS)]
            d.rectangle(box, outline=colour, width=2)
            d.text((box[0] + 4, box[1] + 2), name, fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def create_app(config, bus: FrameBus, get_state: Callable[[], dict[str, Any]]) -> FastAPI:
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
        st["rois"] = {k: vars(v) for k, v in rois.items()}
        return JSONResponse(st)

    @app.get("/snapshot.jpg", dependencies=[Depends(auth)])
    def snapshot(overlay: int = 1) -> Response:
        frame = bus.latest()
        if frame is None:
            raise HTTPException(503, "no camera frame yet")
        return Response(encode_jpeg(frame.image, wcfg.jpeg_quality, rois if overlay else None), media_type="image/jpeg")

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
                    jpg = await asyncio.to_thread(encode_jpeg, frame.image, wcfg.jpeg_quality, rois if overlay else None)
                    yield (
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode()
                        + jpg
                        + b"\r\n"
                    )
                await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

        return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

    return app
