import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { CheckCircle2, Loader2, XCircle, Info } from 'lucide-react'

import { cn } from '@/lib/utils'

export type ToastType = 'loading' | 'success' | 'error' | 'info'

interface Toast {
    id: number
    type: ToastType
    message: string
}

interface PromiseMessages<T> {
    loading: string
    success: string | ((value: T) => string)
    error?: string | ((err: any) => string)
}

interface ToastContextValue {
    show: (type: ToastType, message: string) => number
    loading: (message: string) => number
    update: (id: number, type: ToastType, message: string) => void
    dismiss: (id: number) => void
    promise: <T>(p: Promise<T>, messages: PromiseMessages<T>) => Promise<T>
}

const ToastContext = createContext<ToastContextValue | null>(null)

const AUTO_DISMISS_MS = 5000

export function ToastProvider({ children }: { children: ReactNode }) {
    const [toasts, setToasts] = useState<Toast[]>([])
    const counter = useRef(0)
    const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map())

    const dismiss = useCallback((id: number) => {
        setToasts((prev) => prev.filter((t) => t.id !== id))
        const timer = timers.current.get(id)
        if (timer) {
            clearTimeout(timer)
            timers.current.delete(id)
        }
    }, [])

    const scheduleDismiss = useCallback((id: number) => {
        const existing = timers.current.get(id)
        if (existing) clearTimeout(existing)
        timers.current.set(id, setTimeout(() => dismiss(id), AUTO_DISMISS_MS))
    }, [dismiss])

    const show = useCallback((type: ToastType, message: string) => {
        const id = ++counter.current
        setToasts((prev) => [...prev, { id, type, message }])
        if (type !== 'loading') scheduleDismiss(id)
        return id
    }, [scheduleDismiss])

    const loading = useCallback((message: string) => show('loading', message), [show])

    const update = useCallback((id: number, type: ToastType, message: string) => {
        setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, type, message } : t)))
        if (type !== 'loading') scheduleDismiss(id)
    }, [scheduleDismiss])

    const promise = useCallback(<T,>(p: Promise<T>, messages: PromiseMessages<T>): Promise<T> => {
        const id = show('loading', messages.loading)
        return p.then(
            (value) => {
                const msg = typeof messages.success === 'function' ? messages.success(value) : messages.success
                update(id, 'success', msg)
                return value
            },
            (err) => {
                const fallback = String(err?.message || err)
                const msg = messages.error
                    ? (typeof messages.error === 'function' ? messages.error(err) : messages.error)
                    : fallback
                update(id, 'error', msg)
                throw err
            },
        )
    }, [show, update])

    const value = useMemo(() => ({ show, loading, update, dismiss, promise }), [show, loading, update, dismiss, promise])

    return (
        <ToastContext.Provider value={value}>
            {children}
            <div className="pointer-events-none fixed right-4 top-4 z-[100] flex w-[340px] max-w-[calc(100vw-2rem)] flex-col gap-2">
                {toasts.map((t) => (
                    <ToastCard key={t.id} toast={t} onClose={() => dismiss(t.id)} />
                ))}
            </div>
        </ToastContext.Provider>
    )
}

function ToastCard({ toast, onClose }: { toast: Toast; onClose: () => void }) {
    const icon = {
        loading: <Loader2 className="size-4 flex-none animate-spin text-info" />,
        success: <CheckCircle2 className="size-4 flex-none text-success" />,
        error: <XCircle className="size-4 flex-none text-destructive" />,
        info: <Info className="size-4 flex-none text-info" />,
    }[toast.type]

    const accent = {
        loading: 'border-l-info',
        success: 'border-l-success',
        error: 'border-l-destructive',
        info: 'border-l-info',
    }[toast.type]

    return (
        <div
            role="status"
            onClick={toast.type === 'loading' ? undefined : onClose}
            className={cn(
                'pointer-events-auto flex items-start gap-2.5 rounded-md border border-border border-l-2 bg-card px-3.5 py-3 text-[13px] text-foreground shadow-lg shadow-black/40',
                accent,
                toast.type !== 'loading' && 'cursor-pointer',
                'animate-toast-in',
            )}
        >
            {icon}
            <span className="min-w-0 flex-1 break-words leading-snug">{toast.message}</span>
        </div>
    )
}

export function useToast(): ToastContextValue {
    const ctx = useContext(ToastContext)
    if (!ctx) throw new Error('useToast must be used within <ToastProvider>')
    return ctx
}
