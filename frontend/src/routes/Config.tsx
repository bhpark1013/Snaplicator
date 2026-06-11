import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardTitle } from '@/components/ui/card'
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
    const [copyError, setCopyError] = useState<string | null>(null)
    const [fsUsage, setFsUsage] = useState<FsUsageSummary | null>(null)

    const [check, setCheck] = useState<ReplicationCheckResult | null>(null)
    const [checkLoading, setCheckLoading] = useState(false)
    const [checkError, setCheckError] = useState<string | null>(null)
    const [checkExpanded, setCheckExpanded] = useState(false)

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
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setCopyError(text)
            })
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
        ok: { variant: 'success' as const, label: 'Replication OK · values match' },
        mismatch: { variant: 'destructive' as const, label: 'Mismatch · publisher ≠ subscriber' },
        error: { variant: 'destructive' as const, label: 'Check failed' },
    }[checkStatus]

    return (
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Config</h1>
            </div>

            <div className="mt-4 grid items-stretch gap-4 md:grid-cols-[minmax(320px,1fr)_minmax(240px,320px)]">
                <Card>
                    <CardTitle>Initial Copy</CardTitle>
                    {copy ? (
                        <div className="mt-2 text-[13px]">
                            <div>Initial copy status: <strong className="font-semibold">{copy.status}</strong></div>
                            {copy.total_tables > 0 && (
                                <div className="mt-1">
                                    <div>{copy.finished_tables} / {copy.total_tables} tables ({copy.percent.toFixed(1)}%)</div>
                                    {copy.active && copy.active.length > 0 && (
                                        <ul className="mt-1">
                                            {copy.active.slice(0, 3).map((a, i) => (
                                                <li key={i} className="opacity-80">
                                                    {a.schema}.{a.table}
                                                    {typeof a.percent === 'number' ? ` – ${a.percent.toFixed(1)}%` : ''}
                                                </li>
                                            ))}
                                        </ul>
                                    )}
                                </div>
                            )}
                            {copyError && <p className="text-destructive">{copyError}</p>}
                        </div>
                    ) : (
                        <p className="mt-2 text-[13px] text-muted-foreground">No copy progress available.</p>
                    )}
                </Card>

                <Card>
                    <CardTitle>Btrfs Usage</CardTitle>
                    {fsUsage ? (
                        <div className="mt-2 grid gap-1.5 text-[13px]">
                            <div>
                                <span className="font-semibold">Used:</span>{' '}
                                {`${formatBytes(fsUsage.fs_used_bytes)} / ${formatBytes(fsUsage.fs_size_bytes)}`}
                            </div>
                            {typeof fsUsage.fs_used_bytes === 'number' && typeof fsUsage.fs_size_bytes === 'number' && fsUsage.fs_size_bytes > 0 && (
                                <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-accent">
                                    <div
                                        className="h-full rounded-full bg-info"
                                        style={{ width: `${Math.min(100, (fsUsage.fs_used_bytes / fsUsage.fs_size_bytes) * 100).toFixed(2)}%` }}
                                    />
                                </div>
                            )}
                            {fsUsage.calculated_at && (
                                <div className="text-muted-foreground">
                                    Measured {new Date(fsUsage.calculated_at).toLocaleString()}
                                </div>
                            )}
                        </div>
                    ) : (
                        <p className="mt-2 text-[13px] text-muted-foreground">Usage unavailable.</p>
                    )}
                </Card>
            </div>

            <Card className="mt-4">
                <div className="flex flex-wrap items-center gap-2">
                    <CardTitle>Subscription Status</CardTitle>
                    {subStatus && (
                        <Badge variant={subStatus.status === 'ok' ? 'success' : 'destructive'}>
                            {subStatus.status === 'ok' ? 'DB subscription healthy' : 'subscription down'}
                        </Badge>
                    )}
                    {subLogs && subLogs.has_errors && subStatus?.status === 'ok' && (
                        <Badge variant="warning">resolved (past errors in log)</Badge>
                    )}
                </div>

                {subStatus && subStatus.subscriptions.length > 0 && (
                    <div className="mt-2 grid gap-1 text-[13px]">
                        {subStatus.subscriptions.map((s) => (
                            <div key={s.name} className="flex flex-wrap items-center gap-3">
                                <span className="font-semibold">{s.name}</span>
                                <span>worker: {s.worker_running ? `running (pid ${s.pid})` : 'stopped'}</span>
                                {s.latest_end_time && <span>last sync: {new Date(s.latest_end_time).toLocaleString()}</span>}
                            </div>
                        ))}
                    </div>
                )}

                <div className="my-2 flex flex-wrap items-center gap-2">
                    <Button onClick={() => { loadSubLogs(); loadSubStatus() }} disabled={subLogsLoading}>
                        {subLogsLoading ? 'Loading...' : 'Refresh'}
                    </Button>
                    {subLogs && <span className="text-xs text-muted-foreground">{subLogs.total_matched} matched lines (deduped to {subLogs.lines.length})</span>}
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
            </Card>

            <Card className="mt-4">
                <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setCheckExpanded((v) => !v)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setCheckExpanded((v) => !v) } }}
                    className="flex cursor-pointer select-none items-center gap-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                    <CardTitle>Replication Check</CardTitle>
                    <Badge variant={checkBadge.variant}>{checkBadge.label}</Badge>
                    <Button
                        className="ml-auto"
                        onClick={(e) => { e.stopPropagation(); runCheck() }}
                        disabled={checkLoading}
                    >
                        {checkLoading ? 'Checking...' : 'Re-run'}
                    </Button>
                    <span aria-hidden className="text-xs text-muted-foreground">
                        {checkExpanded ? '▾ collapse' : '▸ details'}
                    </span>
                </div>

                {checkError && <p className="mt-2 text-[13px] text-destructive">{checkError}</p>}

                {checkExpanded && (
                    <div className="mt-3">
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

            <Card className="mt-4">
                <div className="flex items-center gap-2.5">
                    <CardTitle>Manage Tables</CardTitle>
                    <Button asChild className="ml-auto">
                        <Link to="/replication">Open</Link>
                    </Button>
                </div>
                <p className="mt-2 text-[13px] text-muted-foreground">
                    Add or remove tables in the publication / FDW mappings, and refresh the subscription.
                </p>
            </Card>
        </div>
    )
}
