# Hermes Sync — desktop plugin

Sincroniza a instalação do Hermes Agent entre máquinas via **Google Drive**:
configurações, plugins, skills e chaves (`.env`). Instale numa máquina nova,
faça login com o Google e tudo é restaurado — depois use os botões de backup
e restore para manter tudo em dia.

```
┌─────────────────┐   backup (push)   ┌─────────────────────┐
│  Máquina A/B/C  │ ────────────────► │  Google Drive        │
│  (Windows/Linux │ ◄──────────────── │  pasta "Hermes-Sync" │
│   /macOS)       │  restore (pull)   └─────────────────────┘
└─────────────────┘
```

## O que é sincronizado

| Conteúdo | Destino no Drive |
|---|---|
| `config.yaml` (configurações) | `config.yaml` |
| `.env` (chaves/segredos) | `env` |
| `google_client_secret.json` (necessário pro login OAuth) | `google_client_secret.json` |
| `~/.hermes/plugins/` (plugins backend) | `plugins.tar.gz` |
| `~/.hermes/desktop-plugins/` (plugins desktop) | `desktop-plugins.tar.gz` |
| `~/.hermes/skills/` (skills) | `skills.tar.gz` |

## Requisitos

- Hermes Agent **desktop app** (`hermes desktop`)
- A skill **google-workspace** instalada (fornece o OAuth do Google)
- Uma conta Google com a **Google Drive API** habilitada no projeto do
  client OAuth (uma única vez — veja abaixo)

## Primeira vez (uma única vez)

1. Habilite a **Google Drive API** no projeto do seu OAuth client:
   https://console.cloud.google.com/apis/library/drive.googleapis.com
2. Garanta o `google_client_secret.json` em `~/.hermes/` (o client OAuth
   "Desktop app" do Google Cloud Console). Este é o único arquivo que você
   copia manualmente numa máquina nova — o resto o plugin baixa sozinho.

## Instalação

### 1. Backend

```bash
hermes plugins install ElissonSvensson/hermes-sync
```

Responda `Enable now?` com **sim** (ou rode com `--enable`).

### 2. Frontend (o painel)

**Linux / macOS:**

```bash
mkdir -p ~/.hermes/desktop-plugins/hermes-sync
cp desktop/plugin.js ~/.hermes/desktop-plugins/hermes-sync/plugin.js
```

**Windows (PowerShell):**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.hermes\desktop-plugins\hermes-sync" | Out-Null
Copy-Item desktop\plugin.js "$env:USERPROFILE\.hermes\desktop-plugins\hermes-sync\plugin.js"
```

### 3. Ative

- **Reinicie o gateway** (pill do gateway na statusbar → Restart) para montar
  o backend.
- Abra o painel **Hermes Sync** (se não aparecer: **⌘K** / Ctrl+K →
  **Reload desktop plugins**).

## Uso

1. **Login com Google** → autorize no navegador (na primeira vez numa máquina
   nova, o restore roda automaticamente após o login).
2. **Backup** → sobe o estado atual para o Drive.
3. **Restaurar** → baixa e aplica. Antes de sobrescrever, **faz backup local**
   em `~/.hermes/.hermes-sync-backup/` (reversível). Se um download falhar,
   o restore reverte automaticamente.

## Configuração por sistema operacional

Por padrão, todas as máquinas compartilham o mesmo `config.yaml`. Se uma
máquina (ou um SO) precisar de uma configuração diferente, adicione no Drive
(na pasta `Hermes-Sync`) um arquivo `config.<os>.yaml` — ele vence o
`config.yaml` naquele SO. O mesmo vale para `env.<os>`:

| SO | Arquivo de override |
|---|---|
| Linux | `config.linux.yaml` / `env.linux` |
| Windows | `config.windows.yaml` / `env.windows` |
| macOS | `config.macos.yaml` / `env.macos` |

## Segurança

- As rotas do backend só escutam em `localhost` e exigem o token de sessão do
  Hermes (auth padrão do gateway).
- O `.env` é enviado **como está** para o seu Google Drive privado — a conta
  Google é o limite de confiança: quem tiver a conta, tem as chaves. Use
  2FA na conta Google. (Criptografia do `.env` com senha pode ser adicionada
  no futuro.)

## Estrutura

```
hermes-sync/
├── plugin.yaml          # manifesto para `hermes plugins install`
├── dashboard/
│   ├── manifest.json    # manifesto do web server (campo "api")
│   └── plugin_api.py    # backend Python (FastAPI + Google Drive)
└── desktop/
    └── plugin.js        # painel do desktop (plugin desktop)
```

## Licença

MIT — veja [LICENSE](LICENSE).
