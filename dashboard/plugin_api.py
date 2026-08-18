"""Hermes Sync — backend.

Syncs a machine's Hermes state (config.yaml, .env secrets, Google OAuth client
secret, plugins, desktop plugins, skills) to a private "Hermes-Sync" folder in
the user's Google Drive, and restores it on another machine.

Mounted at /api/plugins/hermes-sync/ by the Hermes plugin system.

Security notes
--------------
- All API routes go through the dashboard's session-token auth middleware.
- The Google OAuth token lives in `$HERMES_HOME/google_token.json` (managed by
  the google-workspace skill). The Drive API is called with that token directly.
- The `.env` (secrets) is uploaded as-is to the user's own private Drive. The
  account is the trust boundary: anyone with the Google account gets the keys.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

ROOT_FOLDER = "Hermes-Sync"
META_FILE = ".hermes-sync.json"

# Files that get synced as individual Drive files (name on Drive -> local path).
SINGLE_FILES = {
    "config.yaml": "config.yaml",
    "env": ".env",
    "google_client_secret.json": "google_client_secret.json",
}
# Directories that get tar.gz'd and synced as one archive each.
ARCHIVED_DIRS = {
    "plugins.tar.gz": "plugins",
    "desktop-plugins.tar.gz": "desktop-plugins",
    "skills.tar.gz": "skills",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _os_key() -> str:
    s = platform.system().lower()
    return {"darwin": "macos", "windows": "windows"}.get(s, "linux")


def _meta_path() -> Path:
    return _home() / META_FILE


def _load_meta() -> dict:
    p = _meta_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_meta(partial: dict) -> None:
    meta = _load_meta()
    meta.update(partial)
    meta["os"] = _os_key()
    meta["updated_at"] = time.time()
    _meta_path().write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def _token_path() -> Path:
    return _home() / "google_token.json"


def _authenticated() -> bool:
    t = _token_path()
    if not t.is_file():
        return False
    try:
        payload = json.loads(t.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(payload.get("refresh_token") or payload.get("token") or payload.get("access_token"))


def _stored_scopes() -> list[str]:
    try:
        payload = json.loads(_token_path().read_text(encoding="utf-8"))
        return payload.get("scopes") or []
    except Exception:
        return []


def _service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(_token_path()), _stored_scopes())
    return build("drive", "v3", credentials=creds)


# --------------------------------------------------------------------------- #
# Google Workspace OAuth (delegated to the google-workspace skill's setup.py)
# --------------------------------------------------------------------------- #

def _gw_scripts() -> Path:
    return _home() / "skills" / "productivity" / "google-workspace" / "scripts"


def _run_setup(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    script = _gw_scripts() / "setup.py"
    if not script.is_file():
        raise RuntimeError(
            "google-workspace skill not installed — install it before using Hermes Sync "
            "(it provides the OAuth token management)."
        )
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# Drive helpers (upsert by name inside the root folder)
# --------------------------------------------------------------------------- #

def _find_folder(service, name: str, parent_id: str | None = None) -> dict | None:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = service.files().list(q=q, pageSize=10, fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0] if files else None


def _get_or_create_folder(service, name: str) -> str:
    found = _find_folder(service, name)
    if found:
        return found["id"]
    res = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return res["id"]


def _find_file(service, name: str, folder_id: str) -> dict | None:
    q = f"name='{name}' and '{folder_id}' in parents and trashed=false"
    res = service.files().list(q=q, pageSize=10, fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0] if files else None


def _upsert_file(service, local_path: Path, folder_id: str, drive_name: str) -> tuple[str, bool]:
    """Upload local_path as drive_name inside folder_id. Returns (file_id, updated)."""
    from googleapiclient.http import MediaFileUpload

    mime = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=True)
    existing = _find_file(service, drive_name, folder_id)
    if existing:
        service.files().update(fileId=existing["id"], media_body=media, fields="id").execute()
        return existing["id"], True
    res = service.files().create(
        body={"name": drive_name, "parents": [folder_id]},
        media_body=media,
        fields="id",
    ).execute()
    return res["id"], False


def _download_file(service, file_id: str, out_path: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    out_path.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(str(out_path), "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.close()


def _make_tar(src_dir: Path, dst: Path) -> None:
    """Tar.gz src_dir into dst, skipping VCS/bytecode/cache noise."""
    with tarfile.open(dst, "w:gz") as tar:
        for path in sorted(src_dir.rglob("*")):
            rel = path.relative_to(src_dir)
            parts = rel.parts
            if any(p in {".git", "__pycache__", "node_modules"} or p.endswith(".pyc") for p in parts):
                continue
            if path.is_file():
                tar.add(path, arcname=str(rel))
            elif path.is_dir():
                tar.add(path, arcname=str(rel), recursive=False)


def _extract_tar(tar_path: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(dst_dir, filter="data")


# --------------------------------------------------------------------------- #
# sync logic
# --------------------------------------------------------------------------- #

def _backup_local(dir_path: Path, backup_root: Path) -> None:
    """Move the current state aside before a restore overwrites it."""
    if not dir_path.exists():
        return
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_root / f"{dir_path.name}-{stamp}"
    shutil.move(str(dir_path), str(target))


def _list_remote(service, folder_id: str) -> dict[str, str]:
    """Map drive file name -> id for everything in the sync folder."""
    out: dict[str, str] = {}
    page_token = None
    while True:
        res = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            pageSize=100,
            fields="files(id, name), nextPageToken",
            pageToken=page_token,
        ).execute()
        for f in res.get("files", []):
            out[f["name"]] = f["id"]
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return out


def do_backup() -> dict:
    home = _home()
    service = _service()
    folder_id = _get_or_create_folder(service, ROOT_FOLDER)

    report = {"uploaded": [], "updated": [], "skipped": []}

    # Individual files.
    for drive_name, local_name in SINGLE_FILES.items():
        local_path = home / local_name
        if not local_path.is_file():
            report["skipped"].append(drive_name)
            continue
        _, updated = _upsert_file(service, local_path, folder_id, drive_name)
        report["updated" if updated else "uploaded"].append(drive_name)

    # Archived directories.
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for archive_name, dir_name in ARCHIVED_DIRS.items():
            src = home / dir_name
            if not src.is_dir():
                report["skipped"].append(archive_name)
                continue
            tar_path = td_path / archive_name
            _make_tar(src, tar_path)
            _, updated = _upsert_file(service, tar_path, folder_id, archive_name)
            report["updated" if updated else "uploaded"].append(archive_name)

    _save_meta({"last_backup": time.time()})
    return {"ok": True, **report}


def do_restore() -> dict:
    home = _home()
    service = _service()
    folder_id = _get_or_create_folder(service, ROOT_FOLDER)
    remote = _list_remote(service, folder_id)

    os_key = _os_key()
    backup_root = home / ".hermes-sync-backup"
    restored: list[str] = []

    # Determine which config/env files to use (per-OS override wins).
    config_name = f"config.{os_key}.yaml" if f"config.{os_key}.yaml" in remote else "config.yaml"
    env_name = f"env.{os_key}" if f"env.{os_key}" in remote else "env"

    # Back up current state before overwriting (always — restore is destructive).
    for local_name in list(SINGLE_FILES.values()) + list(ARCHIVED_DIRS.values()):
        _backup_local(home / local_name, backup_root)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        # Individual files.
        for drive_name, local_name in SINGLE_FILES.items():
            # config.yaml and env resolve through their per-OS variant.
            target_drive = {
                "config.yaml": config_name,
                "env": env_name,
            }.get(local_name, drive_name)
            if target_drive not in remote:
                continue
            tmp = td_path / local_name
            _download_file(service, remote[target_drive], tmp)
            shutil.copy2(tmp, home / local_name)
            restored.append(local_name)

        # Archived directories.
        for archive_name, dir_name in ARCHIVED_DIRS.items():
            if archive_name not in remote:
                continue
            tmp_tar = td_path / archive_name
            _download_file(service, remote[archive_name], tmp_tar)
            _extract_tar(tmp_tar, home / dir_name)
            restored.append(dir_name + "/")

    _save_meta({"last_restore": time.time()})
    return {
        "ok": True,
        "restored": restored,
        "config_source": config_name,
        "env_source": env_name,
        # config.yaml and .env only take effect after the gateway restarts.
        "restart_required": bool({"config.yaml", ".env"} & set(restored)),
        "backup_location": str(backup_root),
    }


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

class LoginComplete(BaseModel):
    code: str


@router.get("/status")
def status() -> dict:
    meta = _load_meta()
    return {
        "authenticated": _authenticated(),
        "os": _os_key(),
        "needs_restore": "last_restore" not in meta,
        "last_backup": meta.get("last_backup"),
        "last_restore": meta.get("last_restore"),
        "updated_at": meta.get("updated_at"),
    }


@router.post("/login")
def login() -> dict:
    if _authenticated():
        return {"authenticated": True, "auth_url": None}
    # Start the OAuth device/loopback flow via the google-workspace setup script.
    proc = _run_setup("--auth-url", timeout=180)
    url = proc.stdout.strip()
    if proc.returncode != 0 or not url:
        raise RuntimeError(proc.stderr.strip() or "failed to generate auth URL")
    return {"authenticated": False, "auth_url": url}


@router.post("/login/complete")
def login_complete(body: LoginComplete) -> dict:
    proc = _run_setup("--auth-code", body.code, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "auth exchange failed")
    if not _authenticated():
        return {"authenticated": False, "error": "auth exchange did not produce a token"}
    return {"authenticated": True}


@router.post("/backup")
def backup() -> dict:
    if not _authenticated():
        return {"ok": False, "error": "Google not authenticated — log in first"}
    try:
        return do_backup()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@router.post("/restore")
def restore() -> dict:
    if not _authenticated():
        return {"ok": False, "error": "Google not authenticated — log in first"}
    try:
        return do_restore()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
