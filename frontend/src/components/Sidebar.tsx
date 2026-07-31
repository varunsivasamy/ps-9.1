import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  ClipboardList,
  Database,
  Hexagon,
  SlidersHorizontal,
  User,
} from "lucide-react";

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  activePage: string;
  onNavigate: (page: string) => void;
}

const NAV = [
  { id: "query", label: "Query Agent", icon: Database },
  { id: "audit", label: "Audit Trail", icon: ClipboardList },
  { id: "calibration", label: "Calibration", icon: SlidersHorizontal },
];

const STATUS = [
  { label: "Agent",    ok: true  },
  { label: "Database", ok: true  },
  { label: "LLM API",  ok: true  },
];

export function Sidebar({ collapsed, onToggle: _onToggle, activePage, onNavigate }: SidebarProps) {
  return (
    <motion.aside
      animate={{ width: collapsed ? 64 : 220 }}
      transition={{ duration: 0.22, ease: [0.22, 0.61, 0.36, 1] }}
      className="flex flex-col bg-sidebar-bg text-white shrink-0 overflow-hidden
                 border-r border-sidebar-border relative select-none h-screen sticky top-0"
    >
      {/* Logo */}
      <div className="flex items-center gap-3 px-4 py-5 border-b border-sidebar-border shrink-0">
        <Hexagon className="text-brand w-7 h-7 shrink-0" strokeWidth={1.5} />
        <AnimatePresence>
          {!collapsed && (
            <motion.span
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -8 }}
              transition={{ duration: 0.18 }}
              className="font-bold text-sm tracking-tight whitespace-nowrap"
            >
              Risk Console
            </motion.span>
          )}
        </AnimatePresence>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2 py-3 flex flex-col gap-0.5 overflow-y-auto">
        {NAV.map(({ id, label, icon: Icon }) => {
          const active = activePage === id;
          return (
            <button
              key={id}
              type="button"
              onClick={() => onNavigate(id)}
              title={collapsed ? label : undefined}
              className={`flex items-center gap-3 w-full rounded-lg px-3 py-2.5 text-sm font-medium
                          transition-colors duration-150 group
                          ${active
                            ? "bg-sidebar-active text-white"
                            : "text-gray-400 hover:bg-sidebar-hover hover:text-white"}`}
            >
              <Icon className="w-4.5 h-4.5 shrink-0" size={18} />
              <AnimatePresence>
                {!collapsed && (
                  <motion.span
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.15 }}
                    className="whitespace-nowrap"
                  >
                    {label}
                  </motion.span>
                )}
              </AnimatePresence>
            </button>
          );
        })}
      </nav>

      {/* System status */}
      <div className="px-3 pb-3 border-t border-sidebar-border pt-3 shrink-0">
        <AnimatePresence>
          {!collapsed && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="mb-2"
            >
              <p className="text-[10px] font-bold uppercase tracking-widest text-gray-500 px-1 mb-2">
                System Status
              </p>
              {STATUS.map(({ label, ok }) => (
                <div key={label} className="flex items-center justify-between px-1 py-1">
                  <span className="text-xs text-gray-400">{label}</span>
                  <span className={`flex items-center gap-1 text-xs font-semibold
                    ${ok ? "text-emerald-400" : "text-red-400"}`}>
                    <Activity size={11} />
                    {ok ? "Online" : "Offline"}
                  </span>
                </div>
              ))}
            </motion.div>
          )}
        </AnimatePresence>

        {/* Admin profile */}
        <div className={`flex items-center gap-2.5 rounded-lg px-2 py-2 mt-1
                         hover:bg-sidebar-hover cursor-pointer transition-colors
                         ${collapsed ? "justify-center" : ""}`}>
          <div className="w-7 h-7 rounded-full bg-brand flex items-center justify-center shrink-0">
            <User size={14} className="text-white" />
          </div>
          <AnimatePresence>
            {!collapsed && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="min-w-0"
              >
                <p className="text-xs font-semibold text-white truncate">Admin User</p>
                <p className="text-[10px] text-gray-500 truncate">admin@console.ai</p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* Collapse toggle removed */}
    </motion.aside>
  );
}
