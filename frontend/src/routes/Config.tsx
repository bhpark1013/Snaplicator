import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, RefreshCw, Upload } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

interface CopyProgress {
    status: 'idle' | 'copying' | 'complete'
    total_tables: number
    finished_tables: number
    percent: number
    active?: { schema: string; table: string; bytes_processed?: number; bytes_total?: number; percent?: number | null }[] | null
    details?: { state: string; schema: string; table: string }[] | null
}

interface ReplicationCheckSide {
    ok: boolean
    output?: string | null
    error?: string | null
}

interface ReplicationCheckResult {
    sql?: string | null
    // False when no check query has been written yet — in which case nothing
    // ran, and neither side's result means anything.
    configured?: boolean
    publisher: ReplicationCheckSide
    subscriber: ReplicationCheckSide
}

interface FsUsageSummary {
    fs_used_bytes?: number | null
    fs_size_bytes?: number | null
    calculated_at?: string | null
}

type CheckStatus = 'checking' | 'unconfigured' | 'ok' | 'mismatch' | 'error'

export function Config() {
    const [copy, setCopy] = useState<CopyProgress | null>(null)
    const [fsUsage, setFsUsage] = useState<FsUsageSummary | null>(null)

    const [check, setCheck] = useState<ReplicationCheckResult | null>(null)
    const [checkLoading, setCheckLoading] = useState(false)
    const [checkError, setCheckError] = useState<string | null>(null)
    const [checkExpanded, setCheckExpanded] = useState(false)

    // What the replica could not reproduce. Two questions with one answer:
    // whether it is a copy of the primary, or only of the parts that worked.
    const [ext, setExt] = useState<{
        source: { name: string; version: string }[]
        missing_not_installed: { name: string; version: string; available_version?: string }[]
        missing_not_available: { name: string; version: string }[]
        ok: boolean
    } | null>(null)
    const [schemaErrors, setSchemaErrors] = useState<{ recorded: boolean; count: number; errors: string[] } | null>(null)
    const [fidelityExpanded, setFidelityExpanded] = useState(false)

    const [anon, setAnon] = useState<{ configured: boolean; path: string; sql: string } | null>(null)
    const [anonExpanded, setAnonExpanded] = useState(false)
    const [anonPending, setAnonPending] = useState<{ name: string; sql: string; lines: number } | null>(null)
    const [anonDragging, setAnonDragging] = useState(false)
    const [anonSaving, setAnonSaving] = useState(false)
    const [anonError, setAnonError] = useState<string | null>(null)
    const [anonMsg, setAnonMsg] = useState<string | null>(null)
    const [logsExpanded, setLogsExpanded] = useState(false)

    const [editSql, setEditSql] = useState<string>('')
    const [sqlLoading, setSqlLoading] = useState(false)
    const [savingSql, setSavingSql] = useState(false)
    const [sqlMsg, setSqlMsg] = useState<string | null>(null)
    const [sqlErr, setSqlErr] = useState<string | null>(null)
    const [sqlLocked, setSqlLocked] = useState(true)
    const [sqlPersisted, setSqlPersisted] = useState(false)

    const [subLogs, setSubLogs] = useState<{ lines: string[]; error_count: number; has_errors: boolean; total_matched: number; container_name: string; filters: { include: string[]; exclude: string[]; tail: number } } | null>(null)
    const [subLogsLoading, setSubLogsLoading] = useState(false)
    const [subLogsError, setSubLogsError] = useState<string | null>(null)

    const [subStatus, setSubStatus] = useState<{ status: string; subscriptions: Array<{ name: string; pid: number | null; worker_running: boolean; received_lsn: string | null; latest_end_lsn: string | null; latest_end_time: string | null }> } | null>(null)

    const [notif, setNotif] = useState<{ configured: boolean; enabled: boolean; webhook_url_masked: string } | null>(null)
    const [notifExpanded, setNotifExpanded] = useState(false)
    const [notifUrl, setNotifUrl] = useState('')
    const [notifSaving, setNotifSaving] = useState(false)
    const [notifTesting, setNotifTesting] = useState(false)
    const [notifMsg, setNotifMsg] = useState<string | null>(null)
    const [notifErr, setNotifErr] = useState<string | null>(null)

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const formatBytes = (n?: number | null) => {
        if (n == null || isNaN(n)) return '-'
        const units = ['B', 'KB', 'MB', 'GB', 'TB']
        let v = n
        let i = 0
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024
            i++
        }
        return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
    }

    const loadSubStatus = () => {
        fetch(`${base}/replication/subscription-status`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data) => setSubStatus(data))
            .catch(() => setSubStatus(null))
    }

    const loadNotif = () => {
        fetch(`${base}/notifications`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data) => setNotif(data))
            .catch(() => setNotif(null))
    }

    const saveNotif = async (payload: { webhook_url?: string; enabled?: boolean }) => {
        setNotifSaving(true)
        setNotifMsg(null)
        setNotifErr(null)
        try {
            const r = await fetch(`${base}/notifications`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            })
            if (!r.ok) throw new Error(`${r.status}`)
            setNotif(await r.json())
            setNotifUrl('')
            setNotifMsg('Saved.')
        } catch (e) {
            setNotifErr(String(e))
        } finally {
            setNotifSaving(false)
        }
    }

    const testNotif = async () => {
        setNotifTesting(true)
        setNotifMsg(null)
        setNotifErr(null)
        try {
            const r = await fetch(`${base}/notifications/test`, { method: 'POST' })
            const data = await r.json()
            if (data.ok) setNotifMsg('Test message sent — check the Slack channel.')
            else setNotifErr(`Send failed: ${data.error || data.skipped || `status ${data.status ?? 'unknown'}`}`)
        } catch (e) {
            setNotifErr(String(e))
        } finally {
            setNotifTesting(false)
        }
    }

    const loadSubLogs = () => {
        setSubLogsLoading(true)
        setSubLogsError(null)
        fetch(`${base}/replication/logs?tail=500`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data) => setSubLogs(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setSubLogsError(text)
            })
            .finally(() => setSubLogsLoading(false))
    }

    const loadFsUsage = () => {
        fetch(`${base}/clones/usage/fs`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: FsUsageSummary) => setFsUsage(data))
            .catch(() => setFsUsage(null))
    }

    const loadCopy = () => {
        fetch(`${base}/replication/copy-progress`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: CopyProgress) => setCopy(data))
            .catch(() => { /* banner simply stays hidden */ })
    }

    const runCheck = () => {
        setCheckLoading(true)
        setCheckError(null)
        fetch(`${base}/replication/check`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: ReplicationCheckResult) => setCheck(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setCheckError(text)
            })
            .finally(() => setCheckLoading(false))
    }

    const loadFidelity = () => {
        fetch(`${base}/replication/extensions`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d) setExt(d) })
            .catch(() => {})
        fetch(`${base}/replication/schema-errors`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d) setSchemaErrors(d) })
            .catch(() => {})
    }

    const loadAnon = () => {
        fetch(`${base}/clones/anonymize-sql`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d) setAnon({ configured: !!d.configured, path: d.path || '', sql: d.sql || '' }) })
            .catch(() => {})
    }

    const readAnonFile = (f: File) => {
        setAnonError(null)
        setAnonMsg(null)
        if (f.size > 1_000_000) {
            setAnonError(`${f.name} is ${(f.size / 1_000_000).toFixed(1)} MB — that is not an anonymization script.`)
            return
        }
        const r = new FileReader()
        r.onerror = () => setAnonError(`Could not read ${f.name}`)
        r.onload = () => {
            const text = String(r.result || '')
            if (!text.trim()) {
                setAnonError(`${f.name} is empty`)
                return
            }
            setAnonPending({ name: f.name, sql: text, lines: text.trimEnd().split('\n').length })
        }
        r.readAsText(f)
    }

    // Saved only when asked. Replacing this script changes what every future
    // clone exposes, so dropping a file in cannot be the same gesture as
    // putting it into effect.
    const saveAnon = async () => {
        if (!anonPending) return
        setAnonSaving(true)
        setAnonError(null)
        setAnonMsg(null)
        try {
            const r = await fetch(`${base}/clones/anonymize-sql`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sql: anonPending.sql }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            setAnonMsg(`Saved from ${anonPending.name} — applies to clones made after this.`)
            setAnonPending(null)
            loadAnon()
        } catch (e: any) {
            setAnonError(String(e?.message || e))
        } finally {
            setAnonSaving(false)
        }
    }

    const loadCheckSql = () => {
        setSqlLoading(true)
        setSqlErr(null)
        fetch(`${base}/replication/check-sql`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: { sql: string; persisted?: boolean }) => { setEditSql(data.sql ?? ''); setSqlPersisted(!!data.persisted); setSqlLocked(true) })
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setSqlErr(text)
            })
            .finally(() => setSqlLoading(false))
    }

    const saveCheckSql = async () => {
        setSavingSql(true)
        setSqlErr(null)
        setSqlMsg(null)
        try {
            const r = await fetch(`${base}/replication/check-sql`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sql: editSql }),
            })
            if (!r.ok) {
                let detail = `${r.status}`
                try { const j = await r.json(); detail = j.detail || detail } catch { /* ignore */ }
                setSqlErr(String(detail))
                return
            }
            setSqlMsg('Saved. Read-only validated.')
            setSqlPersisted(true)
            setSqlLocked(true)
            runCheck()
        } catch (e) {
            setSqlErr(String(e))
        } finally {
            setSavingSql(false)
        }
    }

    useEffect(() => {
        loadCopy()
        loadFsUsage()
        loadSubLogs()
        loadSubStatus()
        loadNotif()
        loadCheckSql()
        runCheck()
        loadAnon()
        loadFidelity()
        // eslint-disable-next-line react-hooks-exhaustive-deps
    }, [])

    // Auto refresh subscription status and logs every 30 seconds
    useEffect(() => {
        const id = setInterval(() => {
            loadSubLogs()
            loadSubStatus()
        }, 30000)
        return () => clearInterval(id)
    }, [])

    // Auto refresh copy progress every 5 seconds
    useEffect(() => {
        const id = setInterval(() => {
            loadCopy()
        }, 5000)
        return () => clearInterval(id)
    }, [])

    const checkStatus: CheckStatus = (() => {
        if (checkLoading && !check) return 'checking'
        if (checkError) return 'error'
        if (!check) return 'checking'
        // Asked before answered: with no query there is nothing to have gone
        // wrong, and the two sides' fields are empty rather than failing.
        if (check.configured === false) return 'unconfigured'
        if (!check.publisher.ok || !check.subscriber.ok) return 'error'
        const pub = String(check.publisher.output || '').trim()
        const sub = String(check.subscriber.output || '').trim()
        return pub === sub ? 'ok' : 'mismatch'
    })()

    const checkBadge = {
        checking: { variant: 'neutral' as const, label: 'Checking…' },
        unconfigured: { variant: 'neutral' as const, label: 'Not set up' },
        ok: { variant: 'success' as const, label: 'OK · values match' },
        mismatch: { variant: 'destructive' as const, label: 'Mismatch' },
        error: { variant: 'destructive' as const, label: 'Check failed' },
    }[checkStatus]

    const unconfigured = checkStatus === 'unconfigured'

    const missingExt = (ext?.missing_not_installed.length || 0) + (ext?.missing_not_available.length || 0)

    const anonLineCount = anon?.sql ? anon.sql.trimEnd().split('\n').length : 0

    const lastSync = subStatus?.subscriptions?.find((s) => s.latest_end_time)?.latest_end_time
    const copyInProgress = !!copy && copy.status !== 'complete' && copy.total_tables > 0

    const usagePercent =
        typeof fsUsage?.fs_used_bytes === 'number' && typeof fsUsage?.fs_size_bytes === 'number' && fsUsage.fs_size_bytes > 0
            ? Math.min(100, (fsUsage.fs_used_bytes / fsUsage.fs_size_bytes) * 100)
            : null

    const statLabel = 'text-[11px] font-semibold uppercase tracking-wide text-muted-foreground'

    return (
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Config</h1>
                <Button asChild>
                    <Link to="/replication">Manage Tables →</Link>
                </Button>
            </div>

            {/* ── Health strip: three equal status blocks ── */}
            <div className="mt-4 grid gap-3 md:grid-cols-3">
                <Card className="flex flex-col gap-2">
                    <div className={statLabel}>Subscription</div>
                    <div className="flex flex-wrap items-center gap-1.5">
                        {subStatus ? (
                            <Badge variant={subStatus.status === 'ok' ? 'success' : 'destructive'}>
                                {subStatus.status === 'ok' ? 'healthy' : 'down'}
                            </Badge>
                        ) : (
                            <Badge variant="neutral">loading…</Badge>
                        )}
                    </div>
                    <div className="mt-auto text-xs text-muted-foreground">
                        {lastSync ? `last sync ${new Date(lastSync).toLocaleString()}` : '—'}
                    </div>
                </Card>

                <Card className="flex flex-col gap-2">
                    <div className="flex items-center justify-between">
                        <div className={statLabel}>Replication</div>
                        <Button
                            size="icon"
                            variant="ghost"
                            className="-m-1 h-6 w-6"
                            onClick={runCheck}
                            disabled={checkLoading}
                            title="Re-run check"
                        >
                            <RefreshCw className={`size-3.5 ${checkLoading ? 'animate-spin' : ''}`} />
                        </Button>
                    </div>
                    <div>
                        <Badge variant={checkBadge.variant}>{checkBadge.label}</Badge>
                    </div>
                    {unconfigured ? (
                        <button
                            onClick={() => { setCheckExpanded(true); setSqlErr(null); setSqlMsg(null); setSqlLocked(false) }}
                            className="mt-auto text-left text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
                        >
                            no check query yet — write one
                        </button>
                    ) : (
                        <div className="mt-auto text-xs text-muted-foreground">
                            publisher vs subscriber, auto-checked
                        </div>
                    )}
                </Card>

                <Card className="flex flex-col gap-2">
                    <div className={statLabel}>Storage</div>
                    <div className="text-[13px]">
                        <span className="font-semibold">{formatBytes(fsUsage?.fs_used_bytes)}</span>
                        <span className="text-muted-foreground"> / {formatBytes(fsUsage?.fs_size_bytes)}</span>
                    </div>
                    {usagePercent !== null && (
                        <div className="h-1.5 overflow-hidden rounded-full bg-accent">
                            <div
                                className={`h-full rounded-full ${usagePercent > 85 ? 'bg-destructive' : usagePercent > 70 ? 'bg-warning' : 'bg-info'}`}
                                style={{ width: `${usagePercent.toFixed(2)}%` }}
                            />
                        </div>
                    )}
                    <div className="mt-auto text-xs text-muted-foreground">
                        {fsUsage?.calculated_at ? `measured ${new Date(fsUsage.calculated_at).toLocaleString()}` : '—'}
                    </div>
                </Card>
            </div>

            {/* ── Initial copy banner: only while a copy is in progress ── */}
            {copyInProgress && (
                <Card className="mt-3 border-info/35 bg-info/10">
                    <div className="flex items-center gap-3">
                        <RefreshCw className="size-4 animate-spin text-info" />
                        <div className="text-[13px]">
                            <span className="font-semibold">Initial copy in progress</span>
                            {' — '}
                            {copy!.finished_tables} / {copy!.total_tables} tables ({copy!.percent.toFixed(1)}%)
                        </div>
                    </div>
                    {copy!.active && copy!.active.length > 0 && (
                        <ul className="ml-7 mt-1.5 text-xs text-muted-foreground">
                            {copy!.active.slice(0, 3).map((a, i) => (
                                <li key={i}>
                                    {a.schema}.{a.table}
                                    {typeof a.percent === 'number' ? ` – ${a.percent.toFixed(1)}%` : ''}
                                </li>
                            ))}
                        </ul>
                    )}
                </Card>
            )}

            {/* ── Collapsible detail: Replication Check ── */}
            <Card className="mt-3">
                <button
                    onClick={() => setCheckExpanded((v) => !v)}
                    className="flex w-full items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                    {checkExpanded ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
                    <span className="text-[13px] font-semibold tracking-tight">Replication Check</span>
                    <span className="text-xs text-muted-foreground">
                        {unconfigured ? '— no query written yet' : '— check SQL · publisher/subscriber outputs'}
                    </span>
                </button>

                {checkError && <p className="mt-2 text-[13px] text-destructive">{checkError}</p>}

                {checkExpanded && (
                    <div className="mt-3 pl-6">
                        <div className="mb-1 text-xs text-muted-foreground">
                            Read-only check SQL (publisher vs subscriber). Only SELECT / WITH / SHOW / EXPLAIN — writes are rejected on save and blocked at execution inside a READ ONLY transaction.{sqlPersisted ? '' : ' (showing default — not saved yet)'}
                        </div>
                        <Textarea
                            value={editSql}
                            onChange={(e) => setEditSql(e.target.value)}
                            readOnly={sqlLocked}
                            spellCheck={false}
                            placeholder="select count(*) from your_table;"
                            className={`min-h-28 ${sqlLocked ? 'opacity-60' : ''}`}
                        />
                        <div className="mt-1.5 flex flex-wrap items-center gap-2">
                            {sqlLocked ? (
                                <Button onClick={() => { setSqlErr(null); setSqlMsg(null); setSqlLocked(false) }}>Edit SQL</Button>
                            ) : (
                                <>
                                    <Button onClick={saveCheckSql} disabled={savingSql}>
                                        {savingSql ? 'Saving...' : 'Save'}
                                    </Button>
                                    <Button onClick={() => { loadCheckSql(); setSqlLocked(true); setSqlErr(null); setSqlMsg(null) }} disabled={savingSql}>
                                        Cancel
                                    </Button>
                                </>
                            )}
                            <Button onClick={loadCheckSql} disabled={sqlLoading || !sqlLocked}>
                                {sqlLoading ? 'Loading...' : 'Reload'}
                            </Button>
                        </div>
                        {sqlErr && <p className="mt-1.5 whitespace-pre-wrap text-[13px] text-destructive">{sqlErr}</p>}
                        {sqlMsg && <p className="mt-1.5 text-[13px] text-success">{sqlMsg}</p>}

                        {check && unconfigured && (
                            <p className="mt-3 text-[13px] leading-relaxed text-muted-foreground">
                                Nothing has been run. The check compares one query's result on the primary
                                against the same query on the replica, and which query that is depends on
                                what you replicate — so it ships as a template and waits for yours.
                            </p>
                        )}

                        {check && !unconfigured && (
                            <div className="mt-3 flex flex-col gap-3">
                                {typeof check.sql === 'string' && (
                                    <div>
                                        <div className="mb-1 text-[13px] font-semibold">SQL</div>
                                        <pre className="whitespace-pre-wrap break-words rounded-md border border-border bg-secondary p-3 font-mono text-xs leading-relaxed text-zinc-300">
                                            {check.sql.trim()}
                                        </pre>
                                    </div>
                                )}
                                <div className="flex gap-6">
                                    <div>
                                        <div className="text-[13px] font-semibold">Publisher</div>
                                        <pre className={`mt-1 whitespace-pre-wrap break-words rounded-md border border-border bg-secondary p-3 font-mono text-xs leading-relaxed ${check.publisher.ok ? 'text-zinc-300' : 'text-destructive'}`}>
                                            {check.publisher.ok ? String(check.publisher.output || '').trim() : String(check.publisher.error || 'Error')}
                                        </pre>
                                    </div>
                                    <div>
                                        <div className="text-[13px] font-semibold">Subscriber</div>
                                        <pre className={`mt-1 whitespace-pre-wrap break-words rounded-md border border-border bg-secondary p-3 font-mono text-xs leading-relaxed ${check.subscriber.ok ? 'text-zinc-300' : 'text-destructive'}`}>
                                            {check.subscriber.ok ? String(check.subscriber.output || '').trim() : String(check.subscriber.error || 'Error')}
                                        </pre>
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </Card>

            {/* ── Collapsible detail: Replica fidelity ──
                The schema apply runs with ON_ERROR_STOP=0, so what it could
                not build is not a failure anyone is shown — it is a line in a
                log. A replica missing five indexes reads exactly like a
                complete one until someone measures a query. Both halves of
                that question live here. */}
            <Card className="mt-3">
                <button
                    onClick={() => setFidelityExpanded((v) => !v)}
                    className="flex w-full items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                    {fidelityExpanded ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
                    <span className="text-[13px] font-semibold tracking-tight">Replica fidelity</span>
                    <span className="text-xs text-muted-foreground">
                        {ext === null && schemaErrors === null
                            ? ''
                            : missingExt > 0 || (schemaErrors?.count || 0) > 0
                                ? `— ${[
                                    missingExt > 0 && `${missingExt} extension${missingExt === 1 ? '' : 's'} missing`,
                                    (schemaErrors?.count || 0) > 0 && `${schemaErrors!.count} object${schemaErrors!.count === 1 ? '' : 's'} not created`,
                                ].filter(Boolean).join(' · ')}`
                                : '— extensions match, nothing failed to build'}
                    </span>
                    {(missingExt > 0 || (schemaErrors?.count || 0) > 0) && (
                        <Badge variant="warning" className="ml-auto">Incomplete</Badge>
                    )}
                </button>

                {fidelityExpanded && (
                    <div className="mt-3 space-y-4 pl-6">
                        <div>
                            <div className="mb-1.5 text-[13px] font-semibold">Extensions</div>
                            {ext === null ? (
                                <p className="text-xs text-muted-foreground">Loading…</p>
                            ) : ext.ok ? (
                                <p className="text-xs text-success">
                                    All {ext.source.length} extension{ext.source.length === 1 ? '' : 's'} the primary
                                    uses are installed here.
                                </p>
                            ) : (
                                <div className="space-y-2">
                                    {/* Two causes, two fixes. Not installed is
                                        one SQL statement; not available cannot
                                        be fixed by SQL at all. */}
                                    {ext.missing_not_available.length > 0 && (
                                        <div className="rounded-md border border-destructive/30 bg-destructive/[0.07] p-2.5">
                                            <div className="mb-1 text-xs font-medium text-destructive">
                                                Not in this image — no SQL can fix it
                                            </div>
                                            <div className="font-mono text-xs">
                                                {ext.missing_not_available.map((e) => `${e.name} ${e.version}`).join(', ')}
                                            </div>
                                            <p className="mt-1.5 text-xs text-muted-foreground">
                                                Rebuild the replica on a POSTGRES_IMAGE that carries them. Anything
                                                typed by or indexed with these was skipped when the schema was cloned.
                                            </p>
                                        </div>
                                    )}
                                    {ext.missing_not_installed.length > 0 && (
                                        <div className="rounded-md border border-warning/30 bg-warning/[0.07] p-2.5">
                                            <div className="mb-1 text-xs font-medium text-warning">
                                                Present in the image, never created
                                            </div>
                                            <div className="font-mono text-xs">
                                                {ext.missing_not_installed.map((e) => `${e.name} ${e.version}`).join(', ')}
                                            </div>
                                            <pre className="mt-1.5 overflow-x-auto rounded bg-secondary p-2 font-mono text-[11px]">
                                                {ext.missing_not_installed.map((e) => `CREATE EXTENSION ${e.name};`).join('\n')}
                                            </pre>
                                            <p className="mt-1.5 text-xs text-muted-foreground">
                                                Indexes and objects that needed them were skipped and must be
                                                recreated afterwards.
                                            </p>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>

                        <div>
                            <div className="mb-1.5 text-[13px] font-semibold">Objects the schema clone could not create</div>
                            {schemaErrors === null ? (
                                <p className="text-xs text-muted-foreground">Loading…</p>
                            ) : !schemaErrors.recorded ? (
                                <p className="text-xs text-muted-foreground">
                                    Not recorded — this replica was built before failures were kept. Absence of
                                    evidence, not evidence of a complete clone.
                                </p>
                            ) : schemaErrors.count === 0 ? (
                                <p className="text-xs text-success">Nothing failed.</p>
                            ) : (
                                <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-secondary p-3 font-mono text-[11px] leading-relaxed text-zinc-300">
                                    {schemaErrors.errors.join('\n')}
                                </pre>
                            )}
                        </div>
                    </div>
                )}
            </Card>

            {/* ── Collapsible detail: Anonymization ──
                Reached from the clone dialog when it is missing, and from here
                the rest of the time: a script that decides what every clone
                exposes is not something you should have to be about to make a
                clone in order to see. */}
            <Card className="mt-3">
                <button
                    onClick={() => setAnonExpanded((v) => !v)}
                    className="flex w-full items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                    {anonExpanded ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
                    <span className="text-[13px] font-semibold tracking-tight">Anonymization</span>
                    <span className="text-xs text-muted-foreground">
                        {anon === null
                            ? ''
                            : anon.configured
                                ? `— runs on every new clone · ${anonLineCount} lines`
                                : '— none, clones expose production data'}
                    </span>
                    {anon && !anon.configured && <Badge variant="warning" className="ml-auto">Not set up</Badge>}
                </button>

                {anonExpanded && (
                    <div className="mt-3 pl-6">
                        <p className="mb-2 text-xs leading-relaxed text-muted-foreground">
                            Run inside each clone before anyone can connect to it. With no script a clone is
                            production data with a port on it — readable by anyone holding its connection
                            string, and carried into every snapshot taken from it.
                        </p>
                        {anon?.path && (
                            <p className="mb-2 font-mono text-[11px] text-muted-foreground">{anon.path}</p>
                        )}

                        <label
                            onDragOver={(e) => { e.preventDefault(); setAnonDragging(true) }}
                            onDragLeave={() => setAnonDragging(false)}
                            onDrop={(e) => {
                                e.preventDefault()
                                setAnonDragging(false)
                                const f = e.dataTransfer.files?.[0]
                                if (f) readAnonFile(f)
                            }}
                            className={`flex cursor-pointer items-center justify-center gap-2 rounded-md border border-dashed px-4 py-4 text-center text-[13px] transition-colors ${
                                anonDragging ? 'border-primary bg-primary/[0.06]' : 'border-border-strong hover:bg-white/[0.03]'
                            }`}
                        >
                            <input
                                type="file"
                                accept=".sql,text/plain"
                                className="hidden"
                                onChange={(e) => {
                                    const f = e.target.files?.[0]
                                    if (f) readAnonFile(f)
                                    e.target.value = ''
                                }}
                            />
                            <Upload className="size-4 text-muted-foreground" />
                            {anonPending
                                ? <span className="font-mono">{anonPending.name}</span>
                                : <span>{anon?.configured ? 'Replace with a .sql file' : 'Choose a .sql file'}, or drop one here</span>}
                        </label>

                        {anonPending && (
                            <div className="mt-2 flex flex-wrap items-center gap-2">
                                <span className="text-xs text-muted-foreground">
                                    {anonPending.lines} line{anonPending.lines === 1 ? '' : 's'} — not saved yet
                                </span>
                                <Button
                                    size="sm"
                                    variant="primary"
                                    className="ml-auto"
                                    disabled={anonSaving}
                                    onClick={saveAnon}
                                >
                                    {anonSaving ? 'Saving…' : anon?.configured ? 'Replace script' : 'Save script'}
                                </Button>
                                <Button size="sm" disabled={anonSaving} onClick={() => setAnonPending(null)}>
                                    Discard
                                </Button>
                            </div>
                        )}

                        {anonError && <p className="mt-2 text-xs text-destructive">{anonError}</p>}
                        {anonMsg && <p className="mt-2 text-xs text-success">{anonMsg}</p>}

                        {(anonPending?.sql || anon?.sql) && (
                            <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-secondary p-3 font-mono text-xs leading-relaxed text-zinc-300">
                                {anonPending?.sql || anon?.sql}
                            </pre>
                        )}
                    </div>
                )}
            </Card>

            {/* ── Collapsible detail: Subscription Logs ── */}
            <Card className="mt-3">
                <button
                    onClick={() => setLogsExpanded((v) => !v)}
                    className="flex w-full items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                    {logsExpanded ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
                    <span className="text-[13px] font-semibold tracking-tight">Subscription Logs</span>
                    <span className="text-xs text-muted-foreground">
                        {subLogs ? `— ${subLogs.total_matched} matched lines (deduped to ${subLogs.lines.length})` : ''}
                    </span>
                </button>

                {logsExpanded && (
                    <div className="mt-3 pl-6">
                        <div className="mb-2 flex flex-wrap items-center gap-3">
                            <Button onClick={() => { loadSubLogs(); loadSubStatus() }} disabled={subLogsLoading}>
                                {subLogsLoading ? 'Loading...' : 'Refresh'}
                            </Button>
                            {subStatus && subStatus.subscriptions.length > 0 && (
                                <span className="text-xs text-muted-foreground">
                                    {subStatus.subscriptions.map((s) =>
                                        `${s.name}: worker ${s.worker_running ? `running (pid ${s.pid})` : 'stopped'}`,
                                    ).join(' · ')}
                                </span>
                            )}
                        </div>
                        {subLogsError && <p className="text-[13px] text-destructive">{subLogsError}</p>}
                        {subLogs && subLogs.lines.length > 0 && (
                            <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-secondary p-3 font-mono text-xs leading-normal text-zinc-300">
                                {subLogs.lines.map((line, i) => {
                                    const isError = /\b(ERROR|FATAL)\b/.test(line)
                                    return (
                                        <div key={i} className={isError ? 'font-semibold text-destructive' : undefined}>
                                            {line}
                                        </div>
                                    )
                                })}
                            </pre>
                        )}
                        {subLogs && subLogs.lines.length === 0 && (
                            <p className="text-[13px] text-muted-foreground">No replication-related log lines found.</p>
                        )}
                        {subLogs?.filters && (
                            <div className="mt-2 text-[11px] leading-relaxed text-muted-foreground/70">
                                <div>include: {subLogs.filters.include.map((f: string) => `"${f}"`).join(', ')}</div>
                                <div>exclude: {subLogs.filters.exclude.map((f: string) => `"${f}"`).join(', ')}</div>
                                <div>source: docker logs --tail {subLogs.filters.tail} {subLogs.container_name}</div>
                            </div>
                        )}
                    </div>
                )}
            </Card>

            {/* ── Collapsible detail: Slack Notifications ── */}
            <Card className="mt-3">
                <button
                    onClick={() => setNotifExpanded((v) => !v)}
                    className="flex w-full items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                    {notifExpanded ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
                    <span className="text-[13px] font-semibold tracking-tight">Slack Notifications</span>
                    <span className="text-xs text-muted-foreground">— alert on auto-sync errors via incoming webhook</span>
                    {notif && (
                        <Badge variant={notif.configured && notif.enabled ? 'success' : 'neutral'}>
                            {notif.configured ? (notif.enabled ? 'on' : 'off') : 'not configured'}
                        </Badge>
                    )}
                </button>

                {notifExpanded && (
                    <div className="mt-3 flex flex-col gap-2 pl-6">
                        <div className="text-xs text-muted-foreground">
                            Paste a Slack incoming-webhook URL. Error events from the auto-sync loop are posted to its channel (5-min cooldown per event kind).
                            {notif?.configured ? ` Current: ${notif.webhook_url_masked}` : ''}
                        </div>
                        <div className="flex flex-wrap items-center gap-2">
                            <Input
                                type="password"
                                value={notifUrl}
                                onChange={(e) => setNotifUrl(e.target.value)}
                                placeholder={notif?.configured ? 'Replace webhook URL…' : 'https://hooks.slack.com/services/…'}
                                className="w-96 font-mono text-xs"
                                spellCheck={false}
                            />
                            <Button onClick={() => saveNotif({ webhook_url: notifUrl, enabled: true })} disabled={notifSaving || !notifUrl.trim()}>
                                {notifSaving ? 'Saving...' : 'Save'}
                            </Button>
                            {notif?.configured && (
                                <>
                                    <Button onClick={() => saveNotif({ enabled: !notif.enabled })} disabled={notifSaving}>
                                        {notif.enabled ? 'Disable' : 'Enable'}
                                    </Button>
                                    <Button onClick={testNotif} disabled={notifTesting}>
                                        {notifTesting ? 'Sending...' : 'Send test'}
                                    </Button>
                                    <Button onClick={() => saveNotif({ webhook_url: '', enabled: false })} disabled={notifSaving}>
                                        Remove
                                    </Button>
                                </>
                            )}
                        </div>
                        {notifErr && <p className="text-[13px] text-destructive">{notifErr}</p>}
                        {notifMsg && <p className="text-[13px] text-success">{notifMsg}</p>}
                    </div>
                )}
            </Card>
        </div>
    )
}
