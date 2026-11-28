import { cloudFetch } from "./data";
import { errorResponse, json } from "./http";

/**
 * Paths Wysebee Cloud answers itself, the same ones the screens call, so they go straight
 * up, signed as the person, as they came — a path's encoded slashes and its query kept:
 *
 * - `/api/v1/workspaces…` (2026-11-14): the person's documentation, each a folder of text files
 *   in the `user-workspaces` container (apps/cloud/workspaces.py);
 * - `/api/v1/tasks…` (2026-11-16): the person's tasks, kept and checked there
 *   (apps/cloud/tasks.py), when each is next due among them;
 * - `/api/v1/skills…` (2026-11-17): the person's skills, each a folder of files kept in their
 *   skill file store (apps/cloud/skills.py, skill_folders.py), and which agents have which
 *   (`/api/v1/agents/{id}/skills`), and the skills the person's AI app asks to give an agent
 *   (`/api/v1/agents/{id}/skill-requests…`, apps/cloud/skill_changes.py);
 * - `/api/v1/pages…` (2026-11-17): the rich HTML pages the person's AI app made from their
 *   workspace documents (apps/cloud/workspace_pages.py);
 * - `/api/v1/menu-orders…` (2026-11-28): the order the person dragged a menu's items into
 *   (apps/cloud/menu_orders.py).
 */
export const FORWARDED = ["/api/v1/workspaces", "/api/v1/tasks", "/api/v1/skills", "/api/v1/pages", "/api/v1/menu-orders"];

// A task's runs are read here, from the person's data (home.ts), not sent up.
const TASK_RUNS = /^\/api\/v1\/tasks\/[^/]+\/runs(\/|$)/;
// An agent's skills, by its id, and the skills asked for it.
const AGENT_SKILLS = /^\/api\/v1\/agents\/[^/]+\/(skills|skill-requests(\/[^/]+(\/give)?)?)$/;

/** Whether a path is one of those (a list, or anything under it) — but a task's runs. */
export function isForwarded(pathname: string): boolean {
  if (AGENT_SKILLS.test(pathname)) return true;
  return !TASK_RUNS.test(pathname) && FORWARDED.some((prefix) => pathname === prefix || pathname.startsWith(prefix + "/"));
}

/** The request, sent to Wysebee Cloud as it came; its answer, or its refusal. */
export async function forward(method: string, pathname: string, rawBody?: string): Promise<Response> {
  try {
    const r = await cloudFetch(pathname, { method, ...(rawBody !== undefined ? { body: rawBody } : {}) });
    return r.status === 204 ? json(null, 204) : json(await r.json(), r.status);
  } catch (e) {
    return errorResponse(e);
  }
}
