import { useTranslation } from "react-i18next";
import { LayoutList, Plus } from "lucide-react";
import { cn } from "@shared/cn";
import type { Task } from "../api";
import { TaskKindIcon } from "./TasksView/schedule";
import { useOrderedMenu } from "./common/menuDrag";

// The Agents menu's look (AgentsNav.tsx): the rail's own purple, white on it.
const row = "flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1 text-left text-sm text-white/85 hover:bg-white/10";
const activeRow = "bg-white/20 font-medium text-white hover:bg-white/20";

/** A task's name as the list shows it: its label, else its name. */
const nameOf = (task: Task) => task.label?.trim() || task.name;

/**
 * The Tasks page's menu (2026-11-20, the person's call: like the Agents, Devices and Workspaces
 * menus), beside the rail: **Tasks**, with a + for a new task when the person may make one
 * (`onCreate`), **All tasks** (their list, `#/tasks`), then each task by name with its kind
 * (manual, scheduled…), each opening it. The tasks are App's, as the list reads them. The tasks
 * can be dragged into an order of the person's own (2026-11-28, `useOrderedMenu`); any not in it
 * follow, by name.
 */
export function TasksNav({
  rows,
  selected,
  onSelect,
  onCreate,
}: {
  /** The person's tasks; null until read. */
  rows: Task[] | null;
  /** The task open, or null for All tasks. */
  selected: string | null;
  onSelect: (task: string | null) => void;
  /** Start a new task; absent when the person may not. */
  onCreate?: () => void;
}) {
  const { t } = useTranslation();
  const byName = rows && [...rows].sort((a, b) => nameOf(a).localeCompare(nameOf(b)));
  const menu = useOrderedMenu("tasks", byName ?? [], (task) => task.id);
  const sorted = byName && menu.items;

  return (
    <nav
      className="sticky top-0 flex h-screen w-60 shrink-0 flex-col gap-0.5 self-start overflow-y-auto border-l border-white/30 bg-[#4b3283] px-2 py-3 text-white"
      aria-label={t("tasks.nav.label")}
    >
      <div className="flex items-center pb-2">
        <h2 className="min-w-0 flex-1 px-2 text-base font-bold text-white">{t("tasks.nav.title")}</h2>
        {onCreate && (
          <button
            type="button"
            className="mr-1 shrink-0 rounded p-1 text-white/80 hover:bg-white/20 hover:text-white"
            onClick={onCreate}
            aria-label={t("tasks.list.add")}
            title={t("tasks.list.add")}
            data-track="Tasks nav new task"
          >
            <Plus size={16} aria-hidden />
          </button>
        )}
      </div>
      <ul className="flex flex-col gap-0.5" data-track-private>
        <li>
          <button type="button" className={cn(row, selected === null && activeRow)} onClick={() => onSelect(null)} aria-current={selected === null ? "page" : undefined}>
            <LayoutList size={14} className="shrink-0 text-white/80" aria-hidden />
            <span className="truncate">{t("tasks.nav.all")}</span>
          </button>
        </li>
        {sorted === null && <li className="px-2 text-xs text-white/70">{t("tasks.list.loading")}</li>}
        {sorted?.length === 0 && <li className="px-2 text-xs text-white/70">{t("tasks.list.empty")}</li>}
        {(sorted ?? []).map((task) => (
          <li key={task.id} className={cn("rounded-md", menu.markOf(task.id))} {...menu.row(task.id)}>
            <button type="button" className={cn(row, selected === task.id && activeRow)} onClick={() => onSelect(task.id)} aria-current={selected === task.id ? "page" : undefined}>
              <span className="min-w-0 flex-1 truncate">{nameOf(task)}</span>
              <TaskKindIcon trigger={task.trigger} size={13} className="shrink-0 text-white/70" />
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
