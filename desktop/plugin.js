/**
 * Hermes Sync — desktop plugin. Statusbar chip + dialog + auto-backup.
 *
 * A chip on the right side of the status bar ("Sync ✓/·"); clicking opens a
 * dialog with login/backup/restore controls, an auto-backup toggle and a
 * "ask before auto-restore" toggle. Backend:
 * ~/.hermes/plugins/hermes-sync/ (plugin_api.py).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import {
  atom,
  Button,
  cn,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  SegmentedControl,
  Switch,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'hermes-sync'

const DEFAULT_CFG = {
  autoBackup: { enabled: false, intervalHours: 6 },
  askRestore: true // ask before the automatic first restore on a new machine
}

// Module-level state shared by the chip label and the dialog.
const $status = atom(null) // { authenticated, os, needs_restore, last_backup, last_restore }
const $busy = atom(null) // label string while an operation runs, or null
const $msg = atom(null) // { kind: 'ok'|'err', text }
const $cfg = atom(DEFAULT_CFG)
const $pendingRestore = atom(false) // chip → dialog: show the restore confirm

const SETTINGS_KEY = 'settings'
const LEGACY_AUTOBACKUP_KEY = 'autoBackup'
const CHECK_MS = 5 * 60 * 1000 // lightweight check every 5 min
const INTERVAL_OPTIONS = [
  { id: '1', label: '1h' },
  { id: '3', label: '3h' },
  { id: '6', label: '6h' },
  { id: '12', label: '12h' },
  { id: '24', label: '24h' }
]

function fmt(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

// --------------------------------------------------------------------------- //
// business logic (module scope — components only read atoms)
// --------------------------------------------------------------------------- //

async function loadSettings(ctx) {
  try {
    let cfg = await ctx.storage.get(SETTINGS_KEY)
    if (!cfg) {
      // Legacy key (autoBackup only) from earlier versions.
      const legacy = await ctx.storage.get(LEGACY_AUTOBACKUP_KEY)
      if (legacy) cfg = { autoBackup: legacy }
    }
    if (cfg) $cfg.set({ ...DEFAULT_CFG, ...cfg, autoBackup: { ...DEFAULT_CFG.autoBackup, ...(cfg.autoBackup || {}) } })
  } catch (_) {}
}

function saveSettings(ctx, next) {
  $cfg.set(next)
  ctx.storage.set(SETTINGS_KEY, next).catch(() => {})
  ctx.storage.remove(LEGACY_AUTOBACKUP_KEY).catch(() => {})
}

async function refreshStatus(ctx) {
  try {
    const s = await ctx.rest('/status', { timeoutMs: 20000 })
    $status.set(s)
    return s
  } catch (e) {
    $msg.set({ kind: 'err', text: 'Falha ao carregar status: ' + String((e && e.message) || e) })
    return null
  }
}

async function run(ctx, label, fn, silent = false) {
  if ($busy.get()) return
  $busy.set(label)
  if (!silent) $msg.set(null)
  try {
    const res = await fn()
    if (!silent) {
      if (res && res.ok === false) {
        $msg.set({ kind: 'err', text: res.error || 'Erro' })
      } else if (res && res.restored) {
        $msg.set({
          kind: 'ok',
          text:
            'Mesclado: ' +
            res.restored.join(', ') +
            (res.restart_required ? ' — reinicie o gateway para aplicar.' : '')
        })
      } else if (res && res.ok === true) {
        $msg.set({ kind: 'ok', text: 'Backup concluído.' })
      }
    }
    await refreshStatus(ctx)
  } catch (e) {
    if (!silent) $msg.set({ kind: 'err', text: String((e && e.message) || e) })
  } finally {
    $busy.set(null)
  }
}

async function doLogin(ctx, setAuthUrl) {
  await run(ctx, 'Login…', async () => {
    const res = await ctx.rest('/login', { method: 'POST', timeoutMs: 20000 })
    if (res.authenticated) {
      $msg.set({ kind: 'ok', text: 'Já autenticado no Google.' })
      setAuthUrl(null)
    } else {
      setAuthUrl(res.auth_url)
      if (typeof window !== 'undefined') {
        try {
          window.open(res.auth_url, '_blank')
        } catch (_) {}
      }
    }
    return res
  })
}

async function doCompleteLogin(ctx, code, setCode, setAuthUrl) {
  await run(ctx, 'Concluindo login…', async () => {
    if (!code.trim()) {
      $msg.set({ kind: 'err', text: 'Cole a URL de redirecionamento do navegador.' })
      return { ok: false }
    }
    const res = await ctx.rest('/login/complete', { method: 'POST', body: { code: code.trim() }, timeoutMs: 20000 })
    setCode('')
    setAuthUrl(null)
    if (res.authenticated) $msg.set({ kind: 'ok', text: 'Login Google concluído.' })
    else $msg.set({ kind: 'err', text: res.error || 'Falha no login.' })
    return res
  })
}

function doBackup(ctx) {
  return run(ctx, 'Fazendo backup…', () => ctx.rest('/backup', { method: 'POST', timeoutMs: 300000 }))
}

function doRestore(ctx, setConfirmRestore) {
  return run(ctx, 'Restaurando…', async () => {
    const res = await ctx.rest('/restore', { method: 'POST', timeoutMs: 300000 })
    setConfirmRestore(false)
    return res
  })
}

/** Lightweight periodic check: backup only when the configured interval has
 *  elapsed since the last backup. Runs while the desktop app is open. */
async function checkAutoBackup(ctx) {
  const cfg = $cfg.get()
  if (!cfg.autoBackup || !cfg.autoBackup.enabled || $busy.get()) return
  const s = $status.get()
  if (!s || !s.authenticated) return
  const intervalMs = (cfg.autoBackup.intervalHours || 6) * 3600 * 1000
  const last = s.last_backup || 0
  if (Date.now() / 1000 - last < intervalMs) return
  await run(
    ctx,
    'Backup automático…',
    () => ctx.rest('/backup', { method: 'POST', timeoutMs: 300000 }),
    true // silent — no dialog message spam for background backups
  )
}

// --------------------------------------------------------------------------- //
// dialog
// --------------------------------------------------------------------------- //

function SyncDialog({ ctx, open, onOpenChange }) {
  const status = useValue($status)
  const busy = useValue($busy)
  const msg = useValue($msg)
  const cfg = useValue($cfg)

  const [authUrl, setAuthUrl] = useState(null)
  const [code, setCode] = useState('')
  const [confirmRestore, setConfirmRestore] = useState(false)

  const authed = status && status.authenticated
  const btn = 'w-full'

  // When the chip asks us to restore (auto-restore with askRestore enabled),
  // surface the confirm prompt instead of running silently.
  useEffect(() => {
    if (open && $pendingRestore.get()) {
      $pendingRestore.set(false)
      setConfirmRestore(true)
    }
  }, [open])

  const setCfg = (next) => saveSettings(ctx, next)

  return jsx(Dialog, {
    open,
    onOpenChange,
    children: jsx(DialogContent, {
      className: 'max-w-xl',
      children: jsxs('div', {
        className: 'flex flex-col gap-3',
        children: [
          jsx(DialogHeader, {
            children: jsx(DialogTitle, { children: 'Hermes Sync' })
          }),
          jsx(DialogDescription, {
            children:
              'Sincroniza config, plugins, skills, memórias e chaves entre máquinas via Google Drive (skills e memórias por sistema operacional).'
          }),

          // Auth status
          jsx('div', {
            className: cn(
              'rounded-md border px-2.5 py-2 text-[0.8125rem]',
              authed
                ? 'border-(--ui-stroke-success) text-(--ui-text-success)'
                : 'border-(--ui-stroke-warning) text-(--ui-text-warning)'
            ),
            children: authed
              ? status && status.email
                ? `✓ Logado como: ${status.email}`
                : '✓ Google autenticado'
              : 'Google: não autenticado'
          }),
          jsx('div', {
            className: 'text-(--ui-text-quaternary) text-[0.75rem]',
            children: `SO detectado: ${status ? status.os : '—'}`
          }),

          // Auth flow
          authUrl
            ? jsxs('div', {
                className: 'flex flex-col gap-2',
                children: [
                  jsx('div', {
                    className: 'text-(--ui-text-tertiary) text-[0.75rem]',
                    children:
                      'Abra a URL, autorize e cole aqui a URL de redirecionamento (o navegador vai falhar em localhost — copie o endereço inteiro):'
                  }),
                  jsx('textarea', {
                    className:
                      'w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 text-[0.75rem] text-foreground',
                    rows: 3,
                    placeholder: 'http://localhost:1/?code=...',
                    value: code,
                    onInput: e => setCode(e.target.value)
                  }),
                  jsx(Button, { className: btn, onClick: () => doCompleteLogin(ctx, code, setCode, setAuthUrl), children: 'Concluir login' })
                ]
              })
            : jsx(Button, {
                className: btn,
                onClick: () => doLogin(ctx, setAuthUrl),
                disabled: !!busy,
                children: authed ? 'Conectar novamente' : 'Login com Google'
              }),

          // Actions
          jsx(Button, {
            className: btn,
            onClick: () => doBackup(ctx),
            disabled: !!busy || !authed,
            children: 'Backup (subir para o Drive)'
          }),

          confirmRestore
            ? jsxs('div', {
                className: cn(
                  'flex flex-col gap-2 rounded-md border border-(--ui-stroke-warning) p-2.5'
                ),
                children: [
                  jsx('div', {
                    className: 'text-[0.75rem]',
                    children: 'Mesclar com o que já existe nesta máquina? (faz um snapshot antes — reversível)'
                  }),
                  jsxs('div', {
                    className: 'flex gap-2',
                    children: [
                      jsx(Button, { className: 'flex-1', onClick: () => doRestore(ctx, setConfirmRestore), disabled: !!busy, children: 'Sim, mesclar' }),
                      jsx(Button, { className: 'flex-1', onClick: () => setConfirmRestore(false), children: 'Cancelar' })
                    ]
                  })
                ]
              })
            : jsx(Button, {
                className: btn,
                onClick: () => setConfirmRestore(true),
                disabled: !!busy || !authed,
                children: 'Restaurar (mesclar do Drive)'
              }),

          // Settings: auto-backup + ask-before-restore
          jsxs('div', {
            className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2.5',
            children: [
              jsxs('div', {
                className: 'flex items-center justify-between gap-2',
                children: [
                  jsx('div', { className: 'text-[0.8125rem]', children: 'Backup automático' }),
                  jsx(Switch, {
                    checked: !!(cfg.autoBackup && cfg.autoBackup.enabled),
                    onCheckedChange: v =>
                      setCfg({ ...cfg, autoBackup: { ...cfg.autoBackup, enabled: !!v } })
                  })
                ]
              }),
              cfg.autoBackup && cfg.autoBackup.enabled
                ? jsxs('div', {
                    className: 'flex flex-col gap-1.5',
                    children: [
                      jsx('div', {
                        className: 'text-(--ui-text-quaternary) text-[0.6875rem]',
                        children: 'Intervalo'
                      }),
                      jsx(SegmentedControl, {
                        options: INTERVAL_OPTIONS,
                        value: String(cfg.autoBackup.intervalHours || 6),
                        onChange: id =>
                          setCfg({ ...cfg, autoBackup: { ...cfg.autoBackup, intervalHours: Number(id) } })
                      })
                    ]
                  })
                : null,
              jsx('div', {
                className: 'text-(--ui-text-quaternary) text-[0.6875rem]',
                children: cfg.autoBackup && cfg.autoBackup.enabled
                  ? `O estado sobe automaticamente a cada ${cfg.autoBackup.intervalHours}h enquanto o app estiver aberto.`
                  : 'Roda enquanto o app estiver aberto.'
              }),
              jsxs('div', {
                className: 'mt-1 flex items-center justify-between gap-2 border-t border-(--ui-stroke-secondary) pt-2',
                children: [
                  jsx('div', {
                    className: 'text-[0.8125rem]',
                    children: 'Perguntar antes de restaurar automaticamente'
                  }),
                  jsx(Switch, {
                    checked: !!cfg.askRestore,
                    onCheckedChange: v => setCfg({ ...cfg, askRestore: !!v })
                  })
                ]
              })
            ]
          }),

          busy
            ? jsx('div', { className: 'text-(--ui-text-tertiary) text-[0.75rem]', children: `⏳ ${busy}` })
            : null,

          msg
            ? jsx('div', {
                className: cn(
                  'text-[0.75rem]',
                  msg.kind === 'ok' ? 'text-(--ui-text-success)' : 'text-(--ui-text-warning)'
                ),
                children: msg.text
              })
            : null,

          jsxs('div', {
            className: 'border-t border-(--ui-stroke-secondary) pt-2 text-[0.6875rem] text-(--ui-text-quaternary)',
            children: [
              jsx('div', { children: `Último backup: ${fmt(status && status.last_backup)}` }),
              jsx('div', { children: `Última mesclagem: ${fmt(status && status.last_restore)}` })
            ]
          })
        ]
      })
    })
  })
}

// --------------------------------------------------------------------------- //
// statusbar chip
// --------------------------------------------------------------------------- //

function Chip({ ctx }) {
  const [open, setOpen] = useState(false)
  const status = useValue($status)
  const authed = status && status.authenticated

  // Initial load: settings, status, first-restore, auto-backup loop.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      await loadSettings(ctx)

      const s = await refreshStatus(ctx)
      if (cancelled) return

      // First time on this machine and already authenticated: offer restore.
      if (s && s.authenticated && s.needs_restore) {
        const cfg = $cfg.get()
        if (cfg.askRestore) {
          // Ask first: open the dialog with the confirm prompt ready.
          $pendingRestore.set(true)
          setOpen(true)
        } else {
          await run(ctx, 'Restaurando (primeira vez)…', async () => {
            const res = await ctx.rest('/restore', { method: 'POST', timeoutMs: 300000 })
            if (res && res.ok) $msg.set({ kind: 'ok', text: 'Sincronização inicial concluída.' })
            return res
          })
        }
      }
    })()

    // Lightweight periodic check that fires backups when the interval elapses.
    const timer = setInterval(() => checkAutoBackup(ctx), CHECK_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  return jsxs('div', {
    className: 'inline-flex h-full items-center',
    children: [
      jsx('button', {
        className: cn(
          'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
          'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
        ),
        type: 'button',
        title: authed
          ? `Hermes Sync — ${status && status.email ? status.email : 'Google autenticado'}`
          : 'Hermes Sync — fazer login',
        onClick: () => setOpen(true),
        children: jsxs('span', {
          className: 'inline-flex items-center gap-1',
          children: [
            jsx('span', { className: 'opacity-70', children: 'Sync' }),
            jsx('span', { children: authed ? '✓' : '·' })
          ]
        })
      }),
      jsx(SyncDialog, { ctx, open, onOpenChange: setOpen })
    ]
  })
}

export default {
  id: ID, // must match the folder name
  name: 'Hermes Sync',
  register(ctx) {
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 110,
      render: () => jsx(Chip, { ctx })
    })
  }
}
