from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(tags=["System"])


@router.get("/")
async def health():
    from server import APP_VERSION

    return {"status": "ok", "service": "QuantG API", "version": APP_VERSION}


@router.get("/health")
async def health_check():
    return await health()


@router.get("/version")
async def get_version():
    from server import APP_VERSION, START_TIME, get_file_version, get_git_info

    commit, branch, dirty = get_git_info()
    file_ver = get_file_version()
    uptime_seconds = (datetime.now(timezone.utc) - START_TIME).total_seconds()
    return {
        "backend_version": APP_VERSION,
        "file_version": file_ver,
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "start_time": START_TIME.isoformat(),
        "up_time_seconds": round(uptime_seconds, 2),
    }
