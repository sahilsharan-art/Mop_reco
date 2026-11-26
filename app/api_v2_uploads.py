"""The Upload screen's API (2026-11-26): month-wise upload of the files that are not in the ERP, checked once and stored without a second
upload, a history of every file, a month grid of what has arrived, and a comparison with what the numbered folders held.

A file is STAGED first (one upload, one check, nothing stored), then CONFIRMED by token (moved into uploaded_sources/<report>/<month>/ and
read once into per-date statistics in the background). Nothing is read from or written to the numbered folders. A stored file is never edited;
withdrawing it moves it to uploads_archive/ and keeps the history row (rules 4 and 7).
"""
from __future__ import annotations

import json
import logging
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from mop_reco import upload_tools as ut
from mop_reco import uploads as up
from mop_reco.db import Run, UploadedFile

logger = logging.getLogger("mop_reco")

STAGING_DIR_NAME = "uploads_staging"
STAGE_MAX_AGE_S = 6 * 3600
MAX_BYTES = 1024 * 1024 * 1024


class ConfirmStage(BaseModel):
    token: str = Field(min_length=8, max_length=64, pattern=r"^[0-9a-f]+$")


def register(app, *, db, signed_in, cfg, factory, clean, prefix):
    stats_lock = threading.Lock()                     # one statistics read at a time: they are CPU and memory heavy

    # ------------------------------------------------------------------ statistics, in the background
    def read_stats(upload_id: int) -> None:
        with stats_lock:
            with factory() as s:
                row = s.get(UploadedFile, upload_id)
                if row is None or row.status != "stored":
                    return
                try:
                    ut.record_stats(s, row, cfg())
                except Exception:  # noqa: BLE001 - never kills the worker; the upload shows 'not read'
                    logger.exception("statistics of upload %s failed", upload_id)

    def start_stats(upload_id: int) -> None:
        threading.Thread(target=read_stats, args=(upload_id,), daemon=True, name=f"upload-stats-{upload_id}").start()

    def catch_up() -> None:
        """Files stored while the application was down, or whose read was interrupted, are read now."""
        time.sleep(5)
        try:
            with factory() as s:
                ids = [r.id for r in ut.pending_stats(s)]
            for i in ids:
                read_stats(i)
        except Exception:  # noqa: BLE001
            logger.exception("catching up upload statistics failed")

    threading.Thread(target=catch_up, daemon=True, name="upload-stats-catch-up").start()

    # ------------------------------------------------------------------ staging
    def staging_root() -> Path:
        root = Path(cfg().base_dir) / STAGING_DIR_NAME
        root.mkdir(parents=True, exist_ok=True)
        return root

    def sweep(root: Path) -> None:
        now = time.time()
        for d in root.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > STAGE_MAX_AGE_S:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

    def spec_for(s, source_key: str) -> up.SourceSpec:
        allowed = {x.key: x for x in ut.uploadable_specs(s, cfg())}
        spec = allowed.get(source_key)
        if spec is None:
            raise HTTPException(422, "That report is not uploaded here: it is supplied by the ERP, or it is a master edited on the Masters screen.")
        return spec

    @app.get(prefix + "/uploads/sources")
    def sources(u=Depends(signed_in), s=Depends(db)):
        return {"sources": [{"key": x.key, "title": x.display_name, "extensions": list(x.extensions), "month_specific": x.month_specific,
                             "note": x.note} for x in ut.uploadable_specs(s, cfg())],
                "from_erp": sorted(ut.erp_fed_keys())}

    @app.post(prefix + "/uploads/stage")
    async def stage(file: UploadFile = File(...), source_key: str = Form(...), month: str = Form(""), u=Depends(signed_in), s=Depends(db)):
        """ONE upload and ONE check. Nothing is stored; the answer carries a token to confirm with."""
        spec = spec_for(s, source_key)
        if spec.month_specific:
            if not month:
                raise HTTPException(422, "Choose the month this file belongs to.")
            try:
                datetime.strptime(month + "-01", "%Y-%m-%d")
            except ValueError:
                raise HTTPException(422, "The month must look like 2026-11.")
        original = file.filename or "upload"
        suffix = Path(original).suffix.lower()
        if suffix not in spec.extensions:
            raise HTTPException(422, f"{spec.display_name} accepts {', '.join(spec.extensions)} only.")
        root = staging_root()
        sweep(root)
        token = secrets.token_hex(16)
        folder = root / token
        folder.mkdir()
        try:
            temp = folder / ("upload" + suffix)
            size = 0
            with temp.open("wb") as dest:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "The file is larger than 1 GB.")
                    dest.write(chunk)
            preview = up.validate_upload(spec, temp, original, month or None, cfg())
            (folder / "meta.json").write_text(json.dumps({
                "source_key": spec.key, "month": month or None, "original": original, "by": u.display_name, "ok": bool(preview.ok),
                "row_count": preview.row_count, "date_range": list(preview.date_range) if preview.date_range else None,
                "staged_at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
            return clean({"token": token, "ok": preview.ok, "errors": preview.errors, "warnings": preview.warnings, "row_count": preview.row_count,
                          "date_range": preview.date_range, "columns_found": preview.columns_found, "file": original, "size_bytes": size,
                          "rows": clean(preview.first_rows) if preview.first_rows is not None else []})
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            await file.close()

    @app.post(prefix + "/uploads/confirm")
    def confirm(data: ConfirmStage, u=Depends(signed_in), s=Depends(db)):
        """Store a file that was staged and checked. No second upload and no second check; the file is moved into its month folder."""
        folder = staging_root() / data.token
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            raise HTTPException(404, "This file is no longer waiting (staged files are kept for a few hours). Choose it again.")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta["by"] != u.display_name:
            raise HTTPException(403, "This file was staged by someone else.")
        if not meta["ok"]:
            raise HTTPException(422, "The check failed, so nothing is stored.")
        spec = spec_for(s, meta["source_key"])
        temp = next(folder.glob("upload.*"))
        date_range = tuple(meta["date_range"]) if meta.get("date_range") else None
        row = up.store_file(spec, temp, meta["original"], meta["month"], u.display_name, s, cfg(), meta["row_count"], date_range)
        shutil.rmtree(folder, ignore_errors=True)
        start_stats(row.id)
        return {"id": row.id, "stored_as": Path(row.stored_path).name, "folder": str(Path(row.stored_path).parent.name),
                "renamed": bool(getattr(row, "_renamed", False))}

    @app.post(prefix + "/uploads/{upload_id}/reread")
    def reread(upload_id: int, u=Depends(signed_in), s=Depends(db)):
        row = s.get(UploadedFile, upload_id)
        if row is None or row.status != "stored":
            raise HTTPException(404, "No such stored upload.")
        row.stats_at, row.stats_error = None, None
        s.commit()
        start_stats(upload_id)
        return {"ok": True}

    # ------------------------------------------------------------------ the grid, the history, the comparison
    @app.get(prefix + "/uploads/coverage")
    def coverage(u=Depends(signed_in), s=Depends(db)):
        return clean(ut.coverage_grid(s, cfg()))

    @app.get(prefix + "/uploads/history")
    def history(u=Depends(signed_in), s=Depends(db), source_key: str = "", month: str = "", status: str = "",
                offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=200)):
        q = s.query(UploadedFile)
        if source_key:
            q = q.filter(UploadedFile.source_key == source_key)
        if month:
            q = q.filter(UploadedFile.month == month)
        if status in ("stored", "deleted"):
            q = q.filter(UploadedFile.status == status)
        total = q.count()
        rows = q.order_by(UploadedFile.id.desc()).offset(offset).limit(limit).all()
        out = []
        for r in rows:
            read = ("withdrawn" if r.status == "deleted" else "failed" if r.stats_error else "read" if r.stats_at else
                    "reading" if r.source_key in ut.STAT_SOURCES else "not needed")
            out.append({"id": r.id, "uploaded_at": r.uploaded_at, "data": r.report_name, "source_key": r.source_key, "month": r.month,
                        "file": r.original_file_name, "rows": r.row_count, "from": r.date_range_start, "to": r.date_range_end,
                        "size_bytes": r.size_bytes, "uploaded_by": r.uploaded_by, "status": r.status, "stats": read,
                        "stats_error": r.stats_error, "in_use_by_run": up.used_by_run(s, r.stored_path) if r.status == "stored" else None,
                        "withdrawn_by": r.deleted_by, "withdrawn_at": r.deleted_at})
        return clean({"rows": out, "total": total})

    compare_lock = threading.Lock()

    def compare_job() -> None:
        with factory() as s:
            ut.run_exact_comparison(cfg(), s)

    @app.get(prefix + "/uploads/compare")
    def compare(u=Depends(signed_in), s=Depends(db)):
        """`quick`: the per-file statistics against the folder baseline (instant, but overlapping files count twice). `exact`: the last
        full comparison, made with the run's own readers (deduplicated), with its state."""
        return clean({"quick": ut.compare_with_folders(s), "exact": ut.read_comparison()})

    @app.post(prefix + "/uploads/compare/run")
    def compare_run(u=Depends(signed_in), s=Depends(db)):
        """Start the exact comparison in the background (minutes for Paytm). Reads the uploaded files; writes nothing but its own result."""
        if s.query(Run.id).filter_by(status="running").first() is not None:
            raise HTTPException(409, "A reconciliation is running. Compare after it finishes: both read the uploaded files and need the memory.")
        if not ut._load_baseline():
            raise HTTPException(409, "No folder baseline was recorded, so there is nothing to compare with.")
        with compare_lock:
            last = ut.read_comparison()
            if last.get("state") == "running" and last.get("started_at") and                     (datetime.now() - datetime.fromisoformat(last["started_at"])).total_seconds() < 3 * 3600:
                raise HTTPException(409, "A comparison is already running.")
            ut._save(ut.COMPARE_PATH, {"state": "running", "started_at": datetime.now().isoformat(timespec="seconds"),
                                      "progress": ["starting ..."], "sources": []})
            threading.Thread(target=compare_job, daemon=True, name="upload-compare").start()
        return {"ok": True}
