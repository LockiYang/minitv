"""Video processing callbacks."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.content.upload_service import UploadRejected, apply_callback

router = APIRouter(tags=["public-flow"])


@router.post("/bunny/callback")
async def bunny_callback(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Bunny-Signature", "")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="json required") from exc
    # Accept our local shape ({"video_id", "status"}) and Bunny's native
    # webhook shape ({"VideoGuid", "Status": 0..5}) so a real Stream webhook
    # can point at this endpoint once the API is publicly reachable.
    video_id = payload.get("video_id") or payload.get("VideoGuid")
    if not video_id:
        raise HTTPException(status_code=400, detail="video id required")
    raw_status = payload.get("status", payload.get("Status", "finished"))
    if isinstance(raw_status, int):
        if raw_status < 4:  # created / uploaded / processing / transcoding
            return {"ignored": True, "status": raw_status}
        outcome = "failed" if raw_status >= 5 else "finished"
    else:
        outcome = "failed" if str(raw_status).lower() in {"failed", "error"} else "finished"
    try:
        return apply_callback(
            db,
            body=body,
            signature=signature,
            video_id=video_id,
            outcome=outcome,
        )
    except UploadRejected as exc:
        code = 401 if exc.reason == "bad signature" else 400
        raise HTTPException(status_code=code, detail=exc.reason) from exc
