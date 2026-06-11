import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
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
    publisher: ReplicationCheckSide
    subscriber: ReplicationCheckSide
}

interface FsUsageSummary {
    fs_used_bytes?: number | null
    fs_size_bytes?: number | null
    calculated_at?: string | null
}

type CheckStatus = 'checking' | 'ok' | 'mismatch' | 'error'

export function Config() {
    const [copy, setCopy] = useState<CopyProgress | null>(null)
    const [fsUsage, setFsUsage] = useState<FsUsageSummary | null>(null)

    const [check, setCheck] = useState<ReplicationCheckResult | null>(null)
    const [checkLoading, setCheckLoading] = useState(false)
    const [checkError, setCheckError] = useState<string | null>(null)
    const [checkExpanded, setCheckExpanded] = useState(false)
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
        loadCheckSql()
        runCheck()
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
        if (!check.publisher.ok || !check.subscriber.ok) return 'error'
        const pub = String(check.publisher.output || '').trim()
        const sub = String(check.subscriber.output || '').trim()
        return pub === sub ? 'ok' : 'mismatch'
    })()

    const checkBadge = {
        checking: { variant: 'neutral' as const, label: 'Checking…' },
        ok: { variant: 'success' as const, label: 'OK · values match' },
        mismatch: { variant: 'destructive' as const, label: 'Mismatch' },
        error: { variant: 'destructive' as const, label: 'Check failed' },
    }[checkStatus]

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
                    <div className="mt-auto text-xs text-muted-foreground">
                        publisher vs subscriber, auto-checked
                    </div>
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
                    <span className="text-xs text-muted-foreground">— check SQL · publisher/subscriber outputs</span>
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

                        {check && (
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
        </div>
    )
}
