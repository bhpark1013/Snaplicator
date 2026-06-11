import { NavLink, Outlet, useLocation } from 'react-router-dom'

import { cn } from '@/lib/utils'

const navItemClass = (active: boolean) =>
    cn(
        'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        active
            ? 'bg-accent text-foreground'
            : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
    )

export function Layout() {
    const { pathname } = useLocation()

    const clonesActive = pathname === '/' || pathname.startsWith('/clones')
    const configActive = pathname.startsWith('/config') || pathname.startsWith('/replication')

    return (
        <div className="flex min-h-screen">
            <aside className="sticky top-0 flex h-screen w-[210px] flex-none flex-col gap-4 border-r border-border bg-background px-3 py-4">
                <div className="flex items-center gap-2.5 px-2 text-sm font-semibold tracking-tight">
                    <span className="size-[18px] flex-none rounded-[5px] bg-gradient-to-br from-primary to-[#8a93e8] shadow-[inset_0_0_0_1px_rgba(255,255,255,0.15)]" />
                    Snaplicator
                </div>
                <nav className="flex flex-col gap-0.5">
                    <NavLink to="/" className={navItemClass(clonesActive)}>
                        Clones
                    </NavLink>
                    <NavLink to="/snapshots" className={({ isActive }) => navItemClass(isActive)}>
                        Snapshots
                    </NavLink>
                    <NavLink to="/config" className={navItemClass(configActive)}>
                        Config
                    </NavLink>
                </nav>
            </aside>
            <main className="min-w-0 flex-1">
                <Outlet />
            </main>
        </div>
    )
}
