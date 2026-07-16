import { NavLink } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { useAppStore } from '@/store'
import { APP_VERSION } from '@/config/version'

const navItems = [
  { to: '/', label: '联赛赛程', icon: ScheduleIcon },
  { to: '/quant-demo', label: '量化模拟', icon: HubIcon },
]

export default function Sidebar() {
  const { sidebarCollapsed, toggleSidebar } = useAppStore()

  return (
    <aside
      className={`fixed left-0 top-0 h-full bg-surface-dark border-r border-surface-border z-50 flex flex-col transition-[width] duration-300 ${
        sidebarCollapsed ? 'w-[60px]' : 'w-[220px]'
      }`}
    >
      {/* Logo */}
      <div className="h-14 flex items-center border-b border-surface-border px-4">
        <div className="flex items-center gap-2.5 flex-shrink-0">
          <div className="w-7 h-7 rounded-md bg-field-600 flex items-center justify-center flex-shrink-0">
            <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <AnimatePresence>
            {!sidebarCollapsed && (
              <motion.div
                initial={{ opacity: 0, width: 0 }}
                animate={{ opacity: 1, width: 'auto' }}
                exit={{ opacity: 0, width: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <h1 className="text-sm font-semibold tracking-tight text-ink-primary whitespace-nowrap">
                  哨响<span className="text-field-400">AI</span>
                </h1>
                <p className="text-[10px] text-ink-disabled tracking-wider whitespace-nowrap">预测系统 v{APP_VERSION}</p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-3 px-2 space-y-0.5">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            className={({ isActive }) =>
              `flex items-center gap-2.5 px-3 py-2.5 rounded-md text-sm font-medium transition-colors duration-150 ${
                isActive
                  ? 'text-field-400 bg-field-500/8 border border-field-500/15'
                  : 'text-ink-muted hover:text-ink-primary hover:bg-white/[0.04]'
              } ${sidebarCollapsed ? 'justify-center' : ''}`
            }
          >
            <item.icon />
            <AnimatePresence>
              {!sidebarCollapsed && (
                <motion.span
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.15 }}
                  className="whitespace-nowrap"
                >
                  {item.label}
                </motion.span>
              )}
            </AnimatePresence>
          </NavLink>
        ))}
      </nav>

      {/* Collapse toggle */}
      <div className="p-2 border-t border-surface-border">
        <button
          onClick={toggleSidebar}
          className="w-full flex items-center justify-center p-2 rounded-md text-ink-muted hover:text-ink-primary hover:bg-white/[0.04] transition-colors"
        >
          <svg
            className={`w-4 h-4 transition-transform duration-300 ${sidebarCollapsed ? '' : 'rotate-180'}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M11 19l-7-7 7-7m8 14l-7-7 7-7" />
          </svg>
        </button>
      </div>
    </aside>
  )
}


function ScheduleIcon() {
  return (
    <svg className="w-4 h-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
      <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5" />
    </svg>
  )
}

function HubIcon() {
  return (
    <svg className="w-4 h-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25A2.25 2.25 0 018.25 10.5H6A2.25 2.25 0 013.75 8.25V6zm10.5 0A2.25 2.25 0 0116.5 3.75H18.75A2.25 2.25 0 0121 6v2.25A2.25 2.25 0 0118.75 10.5H16.5A2.25 2.25 0 0114.25 8.25V6zM3.75 16.5A2.25 2.25 0 016 14.25h2.25A2.25 2.25 0 0110.5 16.5V18.75A2.25 2.25 0 018.25 21H6A2.25 2.25 0 013.75 18.75V16.5zm10.5 0A2.25 2.25 0 0116.5 14.25H18.75A2.25 2.25 0 0121 16.5V18.75A2.25 2.25 0 0118.75 21H16.5A2.25 2.25 0 0114.25 18.75V16.5z" />
    </svg>
  )
}
