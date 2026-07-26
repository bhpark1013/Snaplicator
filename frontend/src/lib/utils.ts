import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
    return twMerge(clsx(inputs))
}

/** Human-readable binary size: 82128896 → "78.3 MiB". */
export function formatBytes(n?: number | null): string {
    if (n == null || !Number.isFinite(n) || n < 0) return ''
    if (n < 1024) return `${n} B`
    const units = ['KiB', 'MiB', 'GiB', 'TiB']
    let v = n
    let i = -1
    do { v /= 1024; i++ } while (v >= 1024 && i < units.length - 1)
    return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

/**
 * Copy text to the clipboard, with a fallback for insecure contexts.
 * navigator.clipboard is only available over HTTPS or on localhost, so when the
 * admin UI is served over plain HTTP (e.g. http://<host-ip>:3000) it is undefined.
 * In that case fall back to a hidden textarea + execCommand('copy').
 */
export async function copyText(text: string): Promise<boolean> {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text)
            return true
        }
    } catch {
        /* fall through to legacy path */
    }
    try {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.setAttribute('readonly', '')
        ta.style.position = 'fixed'
        ta.style.left = '-9999px'
        ta.style.top = '0'
        document.body.appendChild(ta)
        ta.select()
        ta.setSelectionRange(0, ta.value.length)
        const ok = document.execCommand('copy')
        document.body.removeChild(ta)
        return ok
    } catch {
        return false
    }
}
