"""Hermes Sync — backend (v2).

Syncs Hermes state across machines via Google Drive, with a per-OS split:

- ``common/``  — config.yaml, .env (env), google_client_secret.json, plugins,
                 desktop-plugins  (shared by every machine)
- ``<os>/``    — skills.tar.gz  (per operating system: linux/windows/macos)

Restore MERGES with whatever already exists locally instead of overwriting
destructively (config/env deep-merged with Drive winning; directories extracted
on top so local-only files survive). A local snapshot is taken first for
reversibility.

Mounted at /api/plugins/hermes-sync/ by the Hermes plugin system.
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

import yaml  # pyyaml — shipped with Hermes

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

ROOT_FOLDER = "Hermes-Sync"
COMMON_FOLDER = "common"
META_FILE = ".hermes-sync.json"

# Shared files (drive name -> local name), stored in common/.
COMMON_FILES = {
    "config.yaml": "config.yaml",
    "env": ".env",
    "google_client_secret.json": "google_client_secret.json",
}
# Shared archived dirs, stored in common/.
COMMON_DIRS = {
    "plugins.tar.gz": "plugins",
    "desktop-plugins.tar.gz": "desktop-plugins",
}
# Per-OS archived dirs, stored in <os>/.
OS_DIRS = {
    "skills.tar.gz": "skills",
    "memories.tar.gz": "memories",
}

LEGACY_ROOT_FILES = set(COMMON_FILES) | set(COMMON_DIRS) | set(OS_DIRS)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _home() -> Path:
    """Resolve HERMES_HOME the same way the core does (platform-aware).

    On Windows the default home is ``%LOCALAPPDATA%\\hermes`` (not
    ``~/.hermes``) — see ``hermes_constants._get_platform_default_hermes_home``.
    """
    try:
        from hermes_constants import get_hermes_home as _core_home
        return _core_home()
    except Exception:
        pass
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


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
# Drive helpers (upsert by name inside a given folder)
# --------------------------------------------------------------------------- #

def _find_folder(service, name: str, parent_id: str | None = None) -> dict | None:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = service.files().list(q=q, pageSize=10, fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0] if files else None


def _get_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    found = _find_folder(service, name, parent_id)
    if found:
        return found["id"]
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    res = service.files().create(body=body, fields="id").execute()
    return res["id"]


def _find_file(service, name: str, folder_id: str) -> dict | None:
    q = f"name='{name}' and '{folder_id}' in parents and trashed=false"
    res = service.files().list(q=q, pageSize=10, fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0] if files else None


def _list_remote(service, folder_id: str) -> dict[str, str]:
    """Map drive file name -> id for everything inside a folder."""
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


def _upsert_file(service, local_path: Path, folder_id: str, drive_name: str) -> tuple[str, bool]:
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


def _trash_file(service, file_id: str) -> None:
    service.files().update(fileId=file_id, body={"trashed": True}).execute()


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
    with tarfile.open(dst, "w:gz") as tar:
        for path in sorted(src_dir.rglob("*")):
            rel = path.relative_to(src_dir)
            parts = rel.parts
            if any(
                p in {".git", "__pycache__", "node_modules"}
                or p.endswith(".pyc")
                or p.endswith(".lock")
                for p in parts
            ):
                continue
            if path.is_file():
                tar.add(path, arcname=str(rel))
            elif path.is_dir():
                tar.add(path, arcname=str(rel), recursive=False)


def _extract_tar(tar_path: Path, dst_dir: Path) -> None:
    """Extract on top of dst_dir (merge: files in the tar overwrite, local-only
    files are preserved)."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(dst_dir, filter="data")


# --------------------------------------------------------------------------- #
# merge logic (Drive wins; local-only keys/files are preserved)
# --------------------------------------------------------------------------- #

def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


def _merge_env(drive_text: str, local_text: str) -> str:
    merged = {**_parse_env(local_text), **_parse_env(drive_text)}  # Drive wins
    return "\n".join(f"{k}={v}" for k, v in merged.items()) + "\n"


def _merge_yaml(drive_text: str, local_text: str) -> str:
    """Drive config wins; top-level keys that exist only locally are appended
    (preserving the Drive file's comments verbatim)."""
    try:
        drive = yaml.safe_load(drive_text) or {}
    except Exception:
        drive = {}
    try:
        local = yaml.safe_load(local_text) or {}
    except Exception:
        local = {}
    if not isinstance(drive, dict) or not isinstance(local, dict):
        return drive_text

    additions = {k: v for k, v in local.items() if k not in drive}
    if not additions:
        return drive_text

    block = yaml.safe_dump(additions, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return drive_text.rstrip() + "\n\n# --- local-only keys (preserved from this machine) ---\n" + block


def _snapshot_local(home: Path, backup_root: Path) -> Path:
    """Copy the current local state into a timestamped backup folder (copy, not
    move — the originals stay put because restore merges)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = backup_root / stamp
    snap.mkdir(parents=True, exist_ok=True)
    for local_name in list(COMMON_FILES.values()) + list(COMMON_DIRS.values()) + list(OS_DIRS.values()):
        src = home / local_name
        if src.is_file():
            shutil.copy2(src, snap / local_name)
        elif src.is_dir():
            shutil.copytree(src, snap / local_name, dirs_exist_ok=True)
    return snap


def _restore_snapshot(home: Path, snap: Path) -> None:
    for item in snap.iterdir():
        dst = home / item.name
        if item.is_file():
            shutil.copy2(item, dst)
        elif item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)


# --------------------------------------------------------------------------- #
# sync logic
# --------------------------------------------------------------------------- #

def do_backup() -> dict:
    home = _home()
    service = _service()
    root_id = _get_or_create_folder(service, ROOT_FOLDER)
    common_id = _get_or_create_folder(service, COMMON_FOLDER, root_id)
    os_id = _get_or_create_folder(service, _os_key(), root_id)

    report = {"uploaded": [], "updated": [], "skipped": []}

    for drive_name, local_name in COMMON_FILES.items():
        local_path = home / local_name
        if not local_path.is_file():
            report["skipped"].append(f"{COMMON_FOLDER}/{drive_name}")
            continue
        _, updated = _upsert_file(service, local_path, common_id, drive_name)
        report["updated" if updated else "uploaded"].append(f"{COMMON_FOLDER}/{drive_name}")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for archive_name, dir_name in COMMON_DIRS.items():
            src = home / dir_name
            if not src.is_dir():
                report["skipped"].append(f"{COMMON_FOLDER}/{archive_name}")
                continue
            tar_path = td_path / archive_name
            _make_tar(src, tar_path)
            _, updated = _upsert_file(service, tar_path, common_id, archive_name)
            report["updated" if updated else "uploaded"].append(f"{COMMON_FOLDER}/{archive_name}")

        for archive_name, dir_name in OS_DIRS.items():
            src = home / dir_name
            if not src.is_dir():
                report["skipped"].append(f"{_os_key()}/{archive_name}")
                continue
            tar_path = td_path / archive_name
            _make_tar(src, tar_path)
            _, updated = _upsert_file(service, tar_path, os_id, archive_name)
            report["updated" if updated else "uploaded"].append(f"{_os_key()}/{archive_name}")

    # Migrate legacy: trash the old flat files at the root (now duplicated into
    # common/<os>), keeping the folder structure clean.
    for name, fid in _list_remote(service, root_id).items():
        if name in LEGACY_ROOT_FILES:
            _trash_file(service, fid)

    _save_meta({"last_backup": time.time()})
    return {"ok": True, **report}


def do_restore() -> dict:
    home = _home()
    service = _service()
    root_id = _get_or_create_folder(service, ROOT_FOLDER)
    common_id = _get_or_create_folder(service, COMMON_FOLDER, root_id)
    os_id = _get_or_create_folder(service, _os_key(), root_id)

    common_remote = _list_remote(service, common_id)
    os_remote = _list_remote(service, os_id)

    backup_root = home / ".hermes-sync-backup"
    snap = _snapshot_local(home, backup_root)
    restored: list[str] = []

    try:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)

            # Individual files — merge where it makes sense.
            if "config.yaml" in common_remote:
                tmp = td_path / "config.yaml"
                _download_file(service, common_remote["config.yaml"], tmp)
                drive_text = tmp.read_text(encoding="utf-8")
                local_path = home / "config.yaml"
                if local_path.is_file():
                    merged = _merge_yaml(drive_text, local_path.read_text(encoding="utf-8"))
                    local_path.write_text(merged, encoding="utf-8")
                else:
                    local_path.write_text(drive_text, encoding="utf-8")
                restored.append("config.yaml")

            if "env" in common_remote:
                tmp = td_path / "env"
                _download_file(service, common_remote["env"], tmp)
                drive_text = tmp.read_text(encoding="utf-8")
                local_path = home / ".env"
                if local_path.is_file():
                    merged = _merge_env(drive_text, local_path.read_text(encoding="utf-8"))
                    local_path.write_text(merged, encoding="utf-8")
                else:
                    local_path.write_text(drive_text, encoding="utf-8")
                restored.append(".env")

            if "google_client_secret.json" in common_remote:
                tmp = td_path / "google_client_secret.json"
                _download_file(service, common_remote["google_client_secret.json"], tmp)
                shutil.copy2(tmp, home / "google_client_secret.json")
                restored.append("google_client_secret.json")

            # Archived dirs — extract on top (merge).
            for archive_name, dir_name in {**COMMON_DIRS, **OS_DIRS}.items():
                remote_map = os_remote if archive_name in OS_DIRS else common_remote
                if archive_name not in remote_map:
                    continue
                tmp_tar = td_path / archive_name
                _download_file(service, remote_map[archive_name], tmp_tar)
                _extract_tar(tmp_tar, home / dir_name)
                restored.append(dir_name + "/")
    except Exception:
        _restore_snapshot(home, snap)
        raise

    _save_meta({"last_restore": time.time()})
    return {
        "ok": True,
        "restored": restored,
        "merged": True,
        # config.yaml and .env only take effect after the gateway restarts.
        "restart_required": bool({"config.yaml", ".env"} & set(restored)),
        "snapshot_location": str(snap),
    }


def _account_email(service) -> str | None:
    """Resolve the authenticated account's email via the Drive about endpoint
    (works with the existing drive scope, no extra OAuth scopes needed)."""
    try:
        res = service.about().get(fields="user(emailAddress, displayName)").execute()
        user = res.get("user") or {}
        return user.get("emailAddress") or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

class LoginComplete(BaseModel):
    code: str


@router.get("/status")
def status() -> dict:
    meta = _load_meta()
    # Resolve the account email with a 1h cache so the cheap /status polling
    # (auto-backup checker) doesn't hit the Drive API every time.
    email = meta.get("account_email")
    if not email or time.time() - (meta.get("account_email_at") or 0) > 3600:
        if _authenticated():
            try:
                email = _account_email(_service())
                _save_meta({"account_email": email, "account_email_at": time.time()})
            except Exception:
                pass
    return {
        "authenticated": _authenticated(),
        "email": email,
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
