from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, UploadFile

from app.config import MAX_FILE_BYTES
from app.errors import DomainError
from app.modules.m8_backfill.handler import backfill_dashboard, package_detail, submit_backfill
from app.sources import SAMPLES, Source

router = APIRouter(prefix="/api/backfill", tags=["补传水位"])


@router.get("/overview")
def overview(request: Request, sample: Annotated[str, Query()] = "dirty"):
    if sample not in SAMPLES:
        raise DomainError("SAMPLE_UNKNOWN", 404)
    return backfill_dashboard(request.app.state.store, request.app.state.settings.source_dir, sample)


@router.post("/packages", status_code=201)
async def upload_package(
    request: Request,
    sample: Annotated[str, Form()] = "dirty",
    file: UploadFile = File(...),
):
    if sample not in SAMPLES:
        raise DomainError("SAMPLE_UNKNOWN", 404)
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise DomainError("SOURCE_TOO_LARGE", file=file.filename)
    upload = Source(file.filename or "backfill.csv", content)
    result = submit_backfill(request.app.state.store, request.app.state.settings.source_dir, sample, upload)
    return result


@router.get("/packages/{package_id}")
def detail(package_id: str, request: Request):
    return package_detail(request.app.state.store, package_id)
