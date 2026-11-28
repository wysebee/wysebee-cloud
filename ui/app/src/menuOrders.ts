import { useEffect, useSyncExternalStore } from "react";
import { fetchMenuOrders, putMenuOrder } from "./api";

/**
 * The order a person dragged a menu's items into (2026-11-28), for any menu beside the rail:
 * Workspaces now, Tasks and Agents later. Each menu keeps lists of item keys by scope — the list
 * the items are in (apps/cloud/menu_orders.py). `useMenuOrders(menu)` reads them once;
 * `saveMenuOrder` keeps one at once and on the server; `ordered` puts a list in its order.
 */

type Orders = Record<string, string[]>;

let menus: Record<string, Orders | undefined> = {};
const listeners = new Set<() => void>();
const reading: Record<string, Promise<void> | undefined> = {};

function set(menu: string, orders: Orders): void {
  menus = { ...menus, [menu]: orders };
  listeners.forEach((l) => l());
}

/** Read a menu's orders (once; again with `again`). A menu that cannot be read keeps its items by name. */
export function loadMenuOrders(menu: string, again = false): Promise<void> {
  if (reading[menu] && !again) return reading[menu]!;
  reading[menu] = fetchMenuOrders(menu)
    .then((r) => set(menu, r.orders))
    .catch(() => set(menu, menus[menu] ?? {}));
  return reading[menu]!;
}

/** A menu's orders by scope; empty until read. */
export function useMenuOrders(menu: string): Orders {
  useEffect(() => {
    void loadMenuOrders(menu);
  }, [menu]);
  return (
    useSyncExternalStore(
      (l) => {
        listeners.add(l);
        return () => listeners.delete(l);
      },
      () => menus[menu],
    ) ?? EMPTY
  );
}
const EMPTY: Orders = {};

/** Keep a list's order: shown at once, saved after (and read again if that fails). */
export async function saveMenuOrder(menu: string, scope: string, keys: string[]): Promise<void> {
  set(menu, { ...(menus[menu] ?? {}), [scope]: keys });
  try {
    await putMenuOrder(menu, scope, keys);
  } catch (e) {
    await loadMenuOrders(menu, true);
    throw e;
  }
}

/** `items` in the order `keys` gives (an item it does not name keeps its place after them). */
export function ordered<T>(items: T[], keyOf: (item: T) => string, keys: string[] | undefined): T[] {
  if (!keys?.length) return items;
  const at = new Map(keys.map((k, i) => [k, i]));
  const rank = (item: T) => at.get(keyOf(item)) ?? keys.length;
  // A stable sort: items with no place stay in the order they came in.
  return items.map((item, i) => ({ item, i })).sort((a, b) => rank(a.item) - rank(b.item) || a.i - b.i).map((x) => x.item);
}

/** `keys` with `key` taken out and put before or after `beside` (at the end when `beside` is not there). */
export function placed(keys: string[], key: string, beside: string, where: "before" | "after"): string[] {
  const rest = keys.filter((k) => k !== key);
  const at = rest.indexOf(beside);
  if (at < 0) return [...rest, key];
  rest.splice(where === "before" ? at : at + 1, 0, key);
  return rest;
}

/** For tests: forget what was read. */
export function resetMenuOrders(): void {
  menus = {};
  for (const k of Object.keys(reading)) delete reading[k];
}
