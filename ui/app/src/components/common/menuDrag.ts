import { useState, type DragEvent } from "react";
import { toast } from "sonner";
import { ordered, placed, saveMenuOrder, useMenuOrders } from "../../menuOrders";
import { reasonOf } from "../../workspaces";

/**
 * Dragging in the menus beside the rail (2026-11-28): any row can be picked up and dropped on
 * another, before it, after it or into it — whichever the menu `accepts` for that pair, by where
 * the pointer is on the row (its top quarter before, its bottom quarter after, its middle into;
 * a row taking only before and after splits at its half). Shared by every menu: it knows rows
 * by their key and what is dragged as the menu's own value, so Workspaces moves files and
 * folders with it while Tasks and Agents only reorder (`useOrderedMenu`).
 */

export type DropWhere = "before" | "after" | "into";

export interface MenuDragOptions<D, T> {
  /** Where `drag` may be dropped on `target`; none, and it cannot be dropped there. */
  accepts: (drag: D, target: T) => DropWhere[];
  onDrop: (drag: D, target: T, where: DropWhere) => void;
}

/** The line or the highlight a row shows while something can be dropped on it. */
export const dropMark: Record<DropWhere, string> = {
  before: "shadow-[inset_0_2px_0_0_rgba(255,255,255,0.9)]",
  after: "shadow-[inset_0_-2px_0_0_rgba(255,255,255,0.9)]",
  into: "bg-white/25 ring-1 ring-inset ring-white/70",
};

function whereOn(e: DragEvent<HTMLElement>, allowed: DropWhere[]): DropWhere {
  const box = e.currentTarget.getBoundingClientRect();
  const at = box.height ? (e.clientY - box.top) / box.height : 0.5;
  // A row takes into alone, or before and after (with into or not).
  if (allowed.includes("into") && ((at >= 0.25 && at <= 0.75) || allowed.length === 1)) return "into";
  return at < 0.5 ? "before" : "after";
}

export function useMenuDrag<D, T>({ accepts, onDrop }: MenuDragOptions<D, T>) {
  const [dragging, setDragging] = useState<{ key: string; value: D } | null>(null);
  const [over, setOver] = useState<{ key: string; where: DropWhere } | null>(null);
  const end = () => {
    setDragging(null);
    setOver(null);
  };

  /** Props for a row that can be picked up. */
  const source = (key: string, value: D) => ({
    draggable: true,
    onDragStart: (e: DragEvent<HTMLElement>) => {
      e.dataTransfer?.setData("text/plain", key);
      if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
      setDragging({ key, value });
    },
    onDragEnd: end,
  });

  /** Props for a row something can be dropped on. */
  const target = (key: string, value: T) => ({
    onDragOver: (e: DragEvent<HTMLElement>) => {
      const allowed = dragging && dragging.key !== key ? accepts(dragging.value, value) : [];
      if (!allowed.length) return;
      e.preventDefault();
      const where = whereOn(e, allowed);
      if (over?.key !== key || over.where !== where) setOver({ key, where });
    },
    onDragLeave: (e: DragEvent<HTMLElement>) => {
      if (over?.key === key && !e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(null);
    },
    onDrop: (e: DragEvent<HTMLElement>) => {
      e.preventDefault();
      if (dragging && over?.key === key) onDrop(dragging.value, value, over.where);
      end();
    },
  });

  /** The mark for a row while something is over it, else "". */
  const markOf = (key: string) => (over?.key === key ? dropMark[over.where] : "");

  return { source, target, markOf, dragging: dragging?.value ?? null };
}

/**
 * A flat menu's dragging (Tasks, Agents; 2026-11-28): its `items` in the order the person
 * dragged them into under `menu` (scope ""), any not in it after, as they came; `row(key)` gives
 * a row's props — picked up, dropped on before or after another (which keeps the new order) —
 * and `markOf(key)` its line while something is over it.
 */
export function useOrderedMenu<T>(menu: string, items: T[], keyOf: (item: T) => string) {
  const orders = useMenuOrders(menu);
  const shown = ordered(items, keyOf, orders[""]);
  const drag = useMenuDrag<string, string>({
    accepts: () => ["before", "after"],
    onDrop: (key, beside, where) =>
      void saveMenuOrder(menu, "", placed(shown.map(keyOf), key, beside, where as "before" | "after")).catch((e) => toast.error(reasonOf(e))),
  });
  const row = (key: string) => ({ ...drag.source(key, key), ...drag.target(key, key) });
  return { items: shown, row, markOf: drag.markOf };
}
