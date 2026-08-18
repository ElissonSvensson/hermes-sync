# Hermes Sync — desktop plugin

Synchronizes your Hermes Agent installation across machines via **Google Drive**.
Install on a new machine, log in with Google, and the state is **merged** with
what's already there — nothing is lost. Then use the backup and restore buttons
(or auto-backup) to keep everything up to date.

```
┌─────────────────┐   backup (push)   ┌──────────────────────────┐
│  Machine A/B/C  │ ────────────────► │  Google Drive            │
│  (Windows/Linux │ ◄──────────────── │  "Hermes-Sync" folder    │
│   /macOS)       │  restore (merge)  │  common/ + <os>/skills   │
└─────────────────┘                   └──────────────────────────┘
```

## What is synced

| Content | On Drive |
|---|---|
| `config.yaml` (settings) | `common/config.yaml` |
| `.env` (secrets/keys) | `common/env` |
| `google_client_secret.json` (OAuth login) | `common/google_client_secret.json` |
| `plugins/` (backend plugins) | `common/plugins.tar.gz` |
| `desktop-plugins/` (desktop plugins) | `common/desktop-plugins.tar.gz` |
| `skills/` (skills — **per OS**) | `<os>/skills.tar.gz` (linux/windows/macos) |
| `memories/` (memory — **per OS**) | `<os>/memories.tar.gz` (linux/windows/macos) |

Config, plugins and secrets are **common** to every machine. **Skills** and
**memories** are split per operating system: each machine restores (and backs
up) only the ones for its own OS.

```
Hermes-Sync/
├── common/
│   ├── config.yaml
│   ├── env
│   ├── google_client_secret.json
│   ├── plugins.tar.gz
│   └── desktop-plugins.tar.gz
├── linux/    ├── skills.tar.gz
│             └── memories.tar.gz
├── windows/  ├── skills.tar.gz
│             └── memories.tar.gz
└── macos/    ├── skills.tar.gz
              └── memories.tar.gz
```

## Restore = merge, not overwrite

On restore, the plugin detects the OS and **merges** what comes from Drive with
what already exists locally (instead of overwriting destructively):

- **config.yaml**: merged — Drive keys **win** on conflicts; keys that exist
  only locally are **preserved** (appended to the end of the file)
- **.env**: variable merge — Drive wins; unique local variables are kept
- **plugins/ and skills/**: union — the Drive file wins if it exists in both;
  local-only files are **kept**
- Before merging, a **snapshot** (copy) of the local state is taken in
  `~/.hermes/.hermes-sync-backup/` for reversibility; on failure it rolls back.

## Requirements

- Hermes Agent **desktop app** (`hermes desktop`)
- The **google-workspace** skill installed (provides Google OAuth)
- A Google account with the **Google Drive API** enabled for the OAuth
  client's project (one-time — see below)

## First time (one-time)

1. Enable the **Google Drive API** for your OAuth client's project:
   https://console.cloud.google.com/apis/library/drive.googleapis.com
2. Make sure `google_client_secret.json` is in your Hermes home — this is the
   only file you copy manually to a new machine (the plugin downloads and
   merges everything else):
   - **Linux / macOS:** `~/.hermes/google_client_secret.json`
   - **Windows:** `%LOCALAPPDATA%\hermes\google_client_secret.json`

> **Windows note:** on Windows the Hermes home is `%LOCALAPPDATA%\hermes`
> (e.g. `C:\Users\<you>\AppData\Local\hermes`), **not** `%USERPROFILE%\.hermes`.
> Everything below uses the Hermes home; on Windows replace `~/.hermes` with
> `%LOCALAPPDATA%\hermes`.

## Installation

### 1. Backend

```bash
hermes plugins install ElissonSvensson/hermes-sync
```

Answer `Enable now?` with **yes** (or run with `--enable`).

### 2. Frontend (the dialog)

**Linux / macOS:**

```bash
mkdir -p ~/.hermes/desktop-plugins/hermes-sync
cp desktop/plugin.js ~/.hermes/desktop-plugins/hermes-sync/plugin.js
```

**Windows (PowerShell):**

```powershell
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\hermes\desktop-plugins\hermes-sync" | Out-Null
Copy-Item desktop\plugin.js "$env:LOCALAPPDATA\hermes\desktop-plugins\hermes-sync\plugin.js"
```

### 3. Activate

- **Restart the gateway** (gateway pill on the status bar → Restart) to mount
  the backend.
- Open the **Hermes Sync** dialog from the status-bar chip (`Sync`). If it
  doesn't show up: **⌘K** / Ctrl+K → **Reload desktop plugins**.

## Usage

1. **Login with Google** → authorize in the browser. On the first time on a
   new machine, the restore (merge) is offered automatically — if
   **"Ask before restoring automatically"** is on (default), a dialog asks for
   confirmation first; turn it off to restore without asking.
2. **Backup** → uploads the current state (common + your OS's skills and
   memories) to Drive.
3. **Restore** → downloads and **merges** with the local state (snapshot
   first, reversible).
4. **Auto-backup** (in the dialog) → enable the toggle and pick an interval
   (1h–24h). While the desktop app is open, the state uploads automatically
   whenever the interval elapses — no need to remember to back up.

> Note: with auto-backup enabled on **multiple machines at once**, the last
> machine to back up wins for the shared `common/` content (last-write-wins).
> Keep auto-backup on **one machine at a time** for predictable results;
> per-OS skills are isolated and unaffected.

## Security

- Backend routes only listen on `localhost` and require the Hermes session
  token (standard gateway auth).
- The `.env` is uploaded **as-is** to your private Google Drive — your Google
  account is the trust boundary: whoever has the account, has the keys. Use
  2FA on the Google account. (Password-based `.env` encryption could be added
  in the future.)

## Structure

```
hermes-sync/
├── plugin.yaml          # manifest for `hermes plugins install`
├── dashboard/
│   ├── manifest.json    # web-server manifest ("api" field)
│   └── plugin_api.py    # Python backend (FastAPI + Google Drive)
└── desktop/
    └── plugin.js        # desktop dialog + status-bar chip
```

## License

MIT — see [LICENSE](LICENSE).
