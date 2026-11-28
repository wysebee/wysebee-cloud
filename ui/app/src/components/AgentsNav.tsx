import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { LayoutList, Plus } from "lucide-react";
import { cn } from "@shared/cn";
import { agentNamed, loadAgents, useAgents } from "../agents";
import { AgentAvatar } from "./common/AgentAvatar";
import { agentDevice, DeviceIcon } from "./common/DeviceIcon";
import { presenceOf, useLinks } from "../presence";
import { AddAgentDialog } from "./AddAgentDialog";
import { useOrderedMenu } from "./common/menuDrag";

// The rail's own purple (Sidebar.tsx), white on it, a light line between the two.
const row = "flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1 text-left text-sm text-white/85 hover:bg-white/10";
const activeRow = "bg-white/20 font-medium text-white hover:bg-white/20";

/**
 * The Agents page's menu (Home's until 2026-11-19), beside the rail — a lighter shade of the rail's purple: **Agents**,
 * **All agents** first (their list, `#/agents`, 2026-11-20 — like the Devices menu), then a row per
 * agent — Wysebee first, then the person's own — each opening that agent's settings, with a +
 * (and "Add an agent") that adds one (AddAgentDialog). There is no chat (2026-11-14, the
 * person's call): an agent's tools are reached from an AI app, through Wysebee Assistant. The
 * agents can be dragged into an order of the person's own (2026-11-28, `useOrderedMenu`); any
 * not in it follow, as listed.
 */
export function AgentsNav({
  selected,
  onSelect,
}: {
  /** The agent shown: its id (or, from an older address, its handle); null for All agents. */
  selected: string | null;
  /** Open an agent's settings, or (null) the list of them. */
  onSelect: (agent: string | null) => void;
}) {
  const { t } = useTranslation();
  const agents = useAgents();
  const links = useLinks();
  const [addingAgent, setAddingAgent] = useState(false);
  const menu = useOrderedMenu("agents", agents.items ?? [], (agent) => agent.id);
  const shown = selected === null ? null : agentNamed(agents.items ?? [], selected)?.id;

  // Read now, and again when the window comes back: an agent added elsewhere shows without
  // a reload.
  useEffect(() => {
    void loadAgents();
    const onFocus = () => void loadAgents();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  return (
    <nav
      className="sticky top-0 flex h-screen w-60 shrink-0 flex-col gap-0.5 self-start overflow-y-auto border-l border-white/30 bg-[#4b3283] px-2 py-3 text-white"
      aria-label={t("home.nav.label")}
    >
      <div className="group flex items-center pb-2">
        <h2 className="min-w-0 flex-1 px-2 text-base font-bold text-white">{t("home.nav.title")}</h2>
        <button
          type="button"
          className="mr-1 shrink-0 rounded p-1 text-white/80 opacity-0 hover:bg-white/20 hover:text-white focus:opacity-100 group-hover:opacity-100"
          onClick={() => setAddingAgent(true)}
          aria-label={t("home.nav.addAgent")}
          title={t("home.nav.addAgent")}
          data-track="Agents add agent"
        >
          <Plus size={16} aria-hidden />
        </button>
      </div>

      <ul className="flex flex-col gap-0.5" data-track-private>
        <li>
          <button type="button" className={cn(row, selected === null && activeRow)} onClick={() => onSelect(null)} aria-current={selected === null ? "page" : undefined}>
            <LayoutList size={14} className="shrink-0 text-white/80" aria-hidden />
            <span className="truncate">{t("home.nav.all")}</span>
          </button>
        </li>
        {agents.error && <li className="px-2 text-xs text-red-100">{agents.error}</li>}
        {menu.items.map((agent) => {
          const device = agentDevice(agent.browser, agent.device);
          return (
            <li key={agent.id} className={cn("rounded-md", menu.markOf(agent.id))} {...menu.row(agent.id)}>
              <button
                type="button"
                className={cn(row, "text-base", shown === agent.id && activeRow)}
                onClick={() => onSelect(agent.id)}
                aria-current={shown === agent.id ? "page" : undefined}
              >
                <AgentAvatar avatar={agent.avatar} label={agent.label} size={24} status={presenceOf(agent, links)} />
                <span className="truncate">{agent.label}</span>
                {/* The device it lives on, named on hover. */}
                {device && <DeviceIcon name={device.name} kind={device.kind} size={14} tone="text-white/70" />}
              </button>
            </li>
          );
        })}
        <li>
          <button type="button" className={cn(row, "text-white/70")} onClick={() => setAddingAgent(true)} data-track="Agents add agent row">
            <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-white/15">
              <Plus size={12} aria-hidden />
            </span>
            {t("home.nav.addAgentRow")}
          </button>
        </li>
      </ul>
      {addingAgent && (
        <AddAgentDialog
          onClose={() => setAddingAgent(false)}
          onAdded={(id) => {
            setAddingAgent(false);
            onSelect(id);
          }}
        />
      )}
    </nav>
  );
}
