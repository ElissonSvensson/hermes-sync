# Hermes Sync — desktop plugin

Sincroniza a instalação do Hermes Agent entre máquinas via **Google Drive**.
Instale numa máquina nova, faça login com o Google e o estado é **mesclado**
com o que já existe lá — sem perder nada. Depois use os botões de backup e
restore para manter tudo em dia.

```
┌─────────────────┐   backup (push)   ┌──────────────────────────┐
│  Máquina A/B/C  │ ────────────────► │  Google Drive            │
│  (Windows/Linux │ ◄──────────────── │  pasta "Hermes-Sync"     │
│   /macOS)       │  restore (merge)  │  common/ + <os>/skills   │
└─────────────────┘                   └──────────────────────────┘
```

## O que é sincronizado

| Conteúdo | No Drive |
|---|---|
| `config.yaml` (configurações) | `common/config.yaml` |
| `.env` (chaves/segredos) | `common/env` |
| `google_client_secret.json` (login OAuth) | `common/google_client_secret.json` |
| `plugins/` (plugins backend) | `common/plugins.tar.gz` |
| `desktop-plugins/` (plugins desktop) | `common/desktop-plugins.tar.gz` |
| `skills/` (skills — **por SO**) | `<so>/skills.tar.gz` (linux/windows/macos) |

Config, plugins e chaves são **comuns** a todas as máquinas. As **skills** são
separadas por sistema operacional: cada máquina restaura (e faz backup de)
apenas as skills do seu SO.

```
Hermes-Sync/
├── common/
│   ├── config.yaml
│   ├── env
│   ├── google_client_secret.json
│   ├── plugins.tar.gz
│   └── desktop-plugins.tar.gz
├── linux/    └── skills.tar.gz
├── windows/  └── skills.tar.gz
└── macos/    └── skills.tar.gz
```

## Restore = mesclar, não sobrescrever

No restore, o plugin detecta o SO e **mescla** o que vem do Drive com o que
já existe localmente (em vez de substituir destrutivamente):

- **config.yaml**: merge — chaves do Drive **vencem** em conflito; chaves que
  só existem localmente são **preservadas** (adicionadas no final do arquivo)
- **.env**: merge de variáveis — Drive vence; variáveis locais únicas mantidas
- **plugins/ e skills/**: união — o arquivo do Drive vence se existir nos dois;
  o que só existe localmente é **mantido**
- Antes de mesclar, é feito um **snapshot** (cópia) do estado local em
  `~/.hermes/.hermes-sync-backup/` para reversão; se algo falhar, reverte.

## Requisitos

- Hermes Agent **desktop app** (`hermes desktop`)
- A skill **google-workspace** instalada (fornece o OAuth do Google)
- Uma conta Google com a **Google Drive API** habilitada no projeto do
  client OAuth (uma única vez — veja abaixo)

## Primeira vez (uma única vez)

1. Habilite a **Google Drive API** no projeto do seu OAuth client:
   https://console.cloud.google.com/apis/library/drive.googleapis.com
2. Garanta o `google_client_secret.json` em `~/.hermes/` (o client OAuth
   "Desktop app" do Google Cloud Console). É o único arquivo que você copia
   manualmente numa máquina nova — o resto o plugin baixa e mescla sozinho.

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

1. **Login com Google** → autorize no navegador. Na primeira vez numa máquina
   nova, o restore (mesclagem) roda automaticamente após o login.
2. **Backup** → sobe o estado atual (comum + skills do seu SO) para o Drive.
3. **Restaurar** → baixa e **mescla** com o local (snapshot antes, reversível).
4. **Backup automático** (no diálogo) → ligue o toggle e escolha o intervalo
   (1h–24h). Enquanto o app do desktop estiver aberto, o estado sobe sozinho
   sempre que o intervalo vence — não precisa lembrar de fazer backup.

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
