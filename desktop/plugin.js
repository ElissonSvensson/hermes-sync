/**
 * Hermes Sync — desktop pane for syncing Hermes state across machines via
 * Google Drive. Backend: ~/.hermes/plugins/hermes-sync/ (plugin_api.py).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import { atom, Button, cn, host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'hermes-sync'

const $status = atom(null) // { authenticated, os, needs_restore, last_backup, last_restore }
const $busy = atom(null) // label string while an operation runs, or null

function fmt(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

function Pane({ ctx }) {
  const status = useValue($status)
  const busy = useValue($busy)

  const [authUrl, setAuthUrl] = useState(null)
  const [code, setCode] = useState('')
  const [confirmRestore, setConfirmRestore] = useState(false)
  const [message, setMessage] = useState(null) // { kind: 'ok'|'err', text }

  const authed = status && status.authenticated

  const refreshStatus = async () => {
    try {
      const s = await ctx.rest('/status', { timeoutMs: 20000 })
      $status.set(s)
      return s
    } catch (e) {
      setMessage({ kind: 'err', text: 'Falha ao carregar status: ' + String((e && e.message) || e) })
      return null
    }
  }

  const run = async (label, fn) => {
    if ($busy.get()) return
    $busy.set(label)
    setMessage(null)
    try {
      const res = await fn()
      if (res && res.ok === false) setMessage({ kind: 'err', text: res.error || 'Erro' })
      else if (res && res.restored) {
        setMessage({
          kind: 'ok',
          text:
            'Restaurado: ' +
            res.restored.join(', ') +
            (res.restart_required ? ' — reinicie o gateway para aplicar.' : '')
        })
      } else if (res && res.ok === true) {
        setMessage({ kind: 'ok', text: 'Backup concluído.' })
      }
      await refreshStatus()
    } catch (e) {
      setMessage({ kind: 'err', text: String((e && e.message) || e) })
    } finally {
      $busy.set(null)
    }
  }

  const doLogin = () =>
    run('Login…', async () => {
      const res = await ctx.rest('/login', { method: 'POST', timeoutMs: 20000 })
      if (res.authenticated) {
        setMessage({ kind: 'ok', text: 'Já autenticado no Google.' })
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

  const doCompleteLogin = () =>
    run('Concluindo login…', async () => {
      if (!code.trim()) {
        setMessage({ kind: 'err', text: 'Cole a URL de redirecionamento do navegador.' })
        return { ok: false }
      }
      const res = await ctx.rest('/login/complete', { method: 'POST', body: { code: code.trim() }, timeoutMs: 20000 })
      setCode('')
      setAuthUrl(null)
      if (res.authenticated) setMessage({ kind: 'ok', text: 'Login Google concluído.' })
      else setMessage({ kind: 'err', text: res.error || 'Falha no login.' })
      return res
    })

  const doBackup = () =>
    run('Fazendo backup…', () => ctx.rest('/backup', { method: 'POST', timeoutMs: 300000 }))

  const doRestore = () =>
    run('Restaurando…', async () => {
      const res = await ctx.rest('/restore', { method: 'POST', timeoutMs: 300000 })
      setConfirmRestore(false)
      return res
    })

  // Initial load + auto-restore on first login on this machine.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const s = await refreshStatus()
      if (cancelled || !s || !s.authenticated || !s.needs_restore) return
      // First time on this machine and already authenticated: restore now.
      await run('Restaurando (primeira vez)…', async () => {
        const res = await ctx.rest('/restore', { method: 'POST', timeoutMs: 300000 })
        if (res && res.ok) setMessage({ kind: 'ok', text: 'Sincronização inicial concluída.' })
        return res
      })
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const btn = 'w-full'
  const row = 'flex flex-col gap-2 p-3 text-sm'

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm',
    children: [
      jsx('div', {
        className: 'text-(--ui-text-tertiary)',
        children: 'Sincroniza config, plugins, skills e chaves entre máquinas via Google Drive.'
      }),

      jsx('div', {
        className: cn(
          'rounded-md border px-2.5 py-2 text-[0.8125rem]',
          authed ? 'border-(--ui-stroke-success) text-(--ui-text-success)' : 'border-(--ui-stroke-warning) text-(--ui-text-warning)'
        ),
        children: authed ? '✓ Google autenticado' : 'Google: não autenticado'
      }),

      jsx('div', {
        className: 'text-(--ui-text-quaternary) text-[0.75rem]',
        children: `SO detectado: ${status ? status.os : '—'}`
      }),

      // Auth flow
      authUrl
        ? jsxs('div', {
            className: row,
            children: [
              jsx('div', {
                className: 'text-(--ui-text-tertiary) text-[0.75rem]',
                children:
                  'Abra a URL, autorize e cole aqui a URL de redirecionamento (a página do navegador vai falhar em localhost — copie o endereço inteiro):'
              }),
              jsx('textarea', {
                className:
                  'w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 text-[0.75rem] text-foreground',
                rows: 3,
                placeholder: 'http://localhost:1/?code=...',
                value: code,
                onInput: e => setCode(e.target.value)
              }),
              jsx(Button, { className: btn, onClick: doCompleteLogin, children: 'Concluir login' })
            ]
          })
        : jsx(Button, {
            className: btn,
            onClick: doLogin,
            disabled: !!busy,
            children: authed ? 'Conectar novamente' : 'Login com Google'
          }),

      // Actions
      jsx(Button, {
        className: btn,
        onClick: doBackup,
        disabled: !!busy || !authed,
        children: 'Backup (subir para o Drive)'
      }),

      confirmRestore
        ? jsxs('div', {
            className: cn(row, 'rounded-md border border-(--ui-stroke-warning)'),
            children: [
              jsx('div', { className: 'text-[0.75rem]', children: 'Sobrescrever a config local? Um backup local é feito antes (reversível).' }),
              jsxs('div', { className: 'flex gap-2', children: [
                jsx(Button, { className: 'flex-1', onClick: doRestore, disabled: !!busy, children: 'Sim, restaurar' }),
                jsx(Button, { className: 'flex-1', onClick: () => setConfirmRestore(false), children: 'Cancelar' })
              ] })
            ]
          })
        : jsx(Button, {
            className: btn,
            onClick: () => setConfirmRestore(true),
            disabled: !!busy || !authed,
            children: 'Restaurar (baixar do Drive)'
          }),

      busy
        ? jsx('div', { className: 'text-(--ui-text-tertiary) text-[0.75rem]', children: `⏳ ${busy}` })
        : null,

      message
        ? jsx('div', {
            className: cn('text-[0.75rem]', message.kind === 'ok' ? 'text-(--ui-text-success)' : 'text-(--ui-text-warning)'),
            children: message.text
          })
        : null,

      jsxs('div', {
        className: 'mt-auto border-t border-(--ui-stroke-secondary) pt-2 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: [
          jsx('div', { children: `Último backup: ${fmt(status && status.last_backup)}` }),
          jsx('div', { children: `Última restauração: ${fmt(status && status.last_restore)}` })
        ]
      })
    ]
  })
}

export default {
  id: ID, // must match the folder name
  name: 'Hermes Sync',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Hermes Sync',
      data: { placement: 'right', width: '320px' },
      render: () => jsx(Pane, { ctx })
    })
  }
}
