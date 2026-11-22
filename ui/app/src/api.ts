import { cloudHost } from "./cloud/host";
import { cloudFetch } from "./cloud/data";
import { CloudError } from "./cloud/errors";

/** Who made or last changed something (apps/cloud/people.py): their user id, and their name
 * and email as Wysebee Cloud knows them ("" when it does not). */
export interface Person {
  id: string;
  name: string;
  email: string;
}

export interface AgentDetail {
  // Its id (a UUID): what its page, skills and tools are reached by — stable across renames,
  // unlike agent_name, its "@" handle.
  id: string;
  agent_name: string;
  config: Record<string, unknown>;
  // Effective system prompt (stored field, or the bundled default).
  system_prompt: string;
  // Who created this agent ("" when unset). The email is the identity every
  // permission check uses; creator_name is the display name for the UI.
  creator: string;
  creator_name: string;
  updated_at: string | null;
  /** Who made it and who changed it last. */
  created_by?: Person | null;
  updated_by?: Person | null;
  /** "cloud": where it lives (Wysebee Cloud). */
  location?: ItemLocation;
}

export type UserRole = "AGENT_ADMIN" | "AGENT_EDITOR" | "AGENT_READER";

// May the caller create new agents/tools? Both editors and admins can.
export const canCreateConfig = (role: UserRole | null): boolean =>
  role === "AGENT_ADMIN" || role === "AGENT_EDITOR";

// May the caller edit an existing agent/tool owned by `creator`? Admins may
// edit anything; editors only what they created (case-insensitive email match —
// an empty/absent creator never matches, so unowned items are admin-only).
// Mirrors the backend `assert_can_edit_created` gate (the authoritative check).
export const canEditCreated = (
  role: UserRole | null,
  email: string,
  creator: string | null | undefined,
): boolean =>
  role === "AGENT_ADMIN" ||
  (role === "AGENT_EDITOR" &&
    !!creator &&
    !!email &&
    creator.trim().toLowerCase() === email.trim().toLowerCase());

export interface UserProfile {
  user_id: string;
  email: string | null;
  user_role: UserRole;
  auth_provider: Record<string, unknown>;
  additional_attributes: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export class UnauthorizedError extends Error {
  constructor() {
    super("Unauthorized");
  }
}

export class ConflictError extends Error {
  /**
   * The server's `detail` when it is an object rather than a sentence.
   *
   * Most 409s say one thing and say it in a string. A few have something a form can
   * act on — a conflict that names the things standing in the way — and that is what
   * this carries, so the caller can render a dialog instead of a paragraph.
   */
  readonly detail?: Record<string, unknown>;

  constructor(message: string, detail?: Record<string, unknown>) {
    super(message);
    this.detail = detail;
  }
}

export class NotFoundError extends Error {
  constructor(message: string) {
    super(message);
  }
}

// The admin API's paths, answered in the browser by the in-browser gateway over
// wysebee-cloud (cloud/router.ts, cloud/routes.ts), which signs its own requests to the
// cloud. The screens call it exactly as the desktop's call its gateway.
async function authedFetch(path: string, init?: RequestInit): Promise<Response> {
  const method = (init?.method ?? "GET").toUpperCase();
  const body = typeof init?.body === "string" ? init.body : undefined;
  const res = await cloudHost.request(method, path, body);

  // A 403 means the caller is known but not permitted (e.g. not AGENT_ADMIN);
  // surface its detail rather than treating it as an auth failure.
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  return res;
}

/**
 * A failed response, read once: the sentence to show, and the structured detail when
 * there is one.
 *
 * FastAPI's `detail` is usually a string. A route with something a form can act on
 * sends an object instead — `{message, …}` naming what stands in the way — and this used to fall straight through to `return body`, so the user was
 * shown the raw JSON of the whole error response. Reading `message` out of it is what
 * makes any such route display properly, not just that one.
 */
async function readError(
  res: Response,
): Promise<{ message: string; detail?: Record<string, unknown> }> {
  const body = await res.text();
  try {
    const parsed = JSON.parse(body);
    const detail = parsed?.detail;
    if (typeof detail === "string") return { message: detail };
    if (detail && typeof detail === "object") {
      const message =
        typeof detail.message === "string" ? detail.message : `HTTP ${res.status}`;
      return { message, detail: detail as Record<string, unknown> };
    }
  } catch {
    /* not JSON — fall through to the raw body */
  }
  return { message: body || `HTTP ${res.status}` };
}

async function readErrorDetail(res: Response): Promise<string> {
  return (await readError(res)).message;
}

/** A 409 as a ConflictError, carrying whatever structure the server sent. */
async function conflict(res: Response): Promise<ConflictError> {
  const { message, detail } = await readError(res);
  return new ConflictError(message, detail);
}

async function authedGet<T>(path: string): Promise<T> {
  const res = await authedFetch(path);
  if (res.status === 404) {
    throw new NotFoundError(await readErrorDetail(res));
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await readErrorDetail(res)}`);
  }
  return res.json() as Promise<T>;
}

async function authedPost<T>(path: string, body: unknown): Promise<T> {
  const res = await authedFetch(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (res.status === 404) {
    throw new NotFoundError(await readErrorDetail(res));
  }
  if (res.status === 409) {
    throw await conflict(res);
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await readErrorDetail(res)}`);
  }
  return res.json() as Promise<T>;
}

async function authedPut<T>(path: string, body: unknown): Promise<T> {
  const res = await authedFetch(path, {
    method: "PUT",
    body: JSON.stringify(body),
  });
  if (res.status === 404) {
    throw new NotFoundError(await readErrorDetail(res));
  }
  if (res.status === 409) {
    throw await conflict(res);
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await readErrorDetail(res)}`);
  }
  return res.json() as Promise<T>;
}

async function authedDelete(path: string): Promise<void> {
  const res = await authedFetch(path, { method: "DELETE" });
  if (res.status === 404) {
    throw new NotFoundError(await readErrorDetail(res));
  }
  // A delete can be refused as well as fail, and the caller may turn a refusal into
  // a dialog naming what stands in the way.
  if (res.status === 409) {
    throw await conflict(res);
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await readErrorDetail(res)}`);
  }
}

export function fetchMe(): Promise<UserProfile> {
  return authedGet<UserProfile>("/api/v1/config/users/me");
}

// --- Tasks ---------------------------------------------------------------------------

/** How a run ended — or "running". "interrupted": the app quit in the middle of it. */
export type TaskRunStatus = "running" | "succeeded" | "failed" | "stopped" | "interrupted";

/**
 * When a task runs (config/task_schedule.py on the gateway):
 * - manual: only when Start is pressed;
 * - once: a timer — `at` is an ISO instant;
 * - recurring: `repeat` "minutes" / "hours" with `every`, or "daily" / "weekly" with
 *   `time` ("HH:MM" in `tz`) and, weekly, `days` (1=Mon … 7=Sun).
 */
export interface TaskTrigger {
  type: "manual" | "once" | "recurring";
  at?: string;
  repeat?: "minutes" | "hours" | "daily" | "weekly";
  every?: number;
  time?: string;
  days?: number[];
  /** The IANA zone `time` is in (Wysebee Cloud runs it); UTC when none. */
  tz?: string;
}

/** One of a task's steps: an agent's tool Wysebee Cloud runs when the task is due. */
export interface TaskStep {
  /** Its agent, by id (shown by its handle now). */
  agent_id: string | null;
  tool: string;
  arguments?: Record<string, unknown>;
}

/** One run. `messages` (the whole turn) is only on a single run's read. */
export interface TaskRun {
  id: string;
  task_id: string;
  status: TaskRunStatus;
  /** What started it: Start, or the task's schedule. */
  trigger: "manual" | "schedule";
  /** The agent and "provider:model" that ran it — either may have changed since. */
  agent: string | null;
  model: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** The agent's final answer. */
  output: string | null;
  error: string | null;
  /** The folder its recording was saved in (Settings → System → Record agent sessions). */
  recording?: string | null;
  messages?: ChatMessage[] | null;
}

/**
 * A plan, carried out in the browser — set up like an agent (label, browser) but
 * run, not chatted with. The model is not chosen here: Wysebee picks it. Keyed by `id`;
 * `name` is unique and renameable.
 */
export interface Task {
  id: string;
  name: string;
  /** The plan: the one message a run sends. Shown as "Plan"; stored as `prompt`. */
  prompt: string;
  description: string;
  label: string;
  /** On unless turned off; `browser` is the paired browser it drives ("" = none yet). */
  browser_access: boolean;
  browser: string;
  browser_devtools: boolean;
  trigger: TaskTrigger;
  /** What Wysebee Cloud runs when it is due, in order; none for a reminder. */
  steps?: TaskStep[];
  /** When the schedule next starts it (UTC ISO); null for manual, or a timer that ran. */
  next_run_at: string | null;
  creator: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** Who made it and who changed it last. */
  created_by?: Person | null;
  updated_by?: Person | null;
  /** The newest run; null before the first Start. */
  last_run: TaskRun | null;
  /** "cloud": where it lives (Wysebee Cloud). */
  location?: ItemLocation;
}

/** What a create or an update sends. An update changes only the fields present. */
export interface TaskInput {
  name?: string;
  prompt?: string;
  description?: string;
  label?: string;
  browser_access?: boolean;
  browser?: string;
  browser_devtools?: boolean;
  /** Manual when never set. Leave it out to keep the stored one (and its next run). */
  trigger?: TaskTrigger;
}

const taskPath = (id: string) => `/api/v1/tasks/${encodeURIComponent(id)}`;

export function fetchTasks(): Promise<{ rows: Task[] }> {
  return authedGet<{ rows: Task[] }>("/api/v1/tasks");
}

export function createTask(input: TaskInput & { name: string }): Promise<Task> {
  return authedPost<Task>("/api/v1/tasks", input);
}

export function updateTask(id: string, input: TaskInput): Promise<Task> {
  return authedPut<Task>(taskPath(id), input);
}

export function deleteTask(id: string): Promise<void> {
  return authedDelete(taskPath(id));
}

export interface ChatToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

/** One message of a chat, in the provider-neutral shape the gateway speaks. */
export type ChatMessage = (
  | { role: "user"; content: string }
  | {
      role: "assistant";
      content: string;
      tool_calls: ChatToolCall[];
      /** Provider output the gateway replays next turn. Opaque: pass it back as is. */
      raw?: Record<string, unknown>;
    }
  | {
      role: "tool";
      tool_call_id: string;
      name: string;
      content: string;
      is_error: boolean;
    }
) & {
  /** When it was added (ISO): Home's transcript groups and times by it. */
  ts?: string;
};

/** Fired on `window` when something changed that the page did not make itself — a move
 * to the cloud, a sign-in or sign-out — so lists it already loaded (agents,
 * tasks) are read again. After the person's agents or tasks changed — in another tab, or by
 * Wysebee Cloud itself — with `detail: {remote: true, changes: []}` (cloud/events.ts). */
export const APP_CHANGED = "wysebee:app-changed";

/** `APP_CHANGED`'s detail when the change came from Wysebee Cloud's change feed. */
export interface RemoteChangeDetail {
  remote: true;
  /** What changed; empty when anything may have — always, since the feed names nothing. */
  changes: { kind: string; item_id: string; name: string; deleted: boolean }[];
  /** Most likely this page's own write echoed back (cloud/ownWrites.ts): the lists are read
   * again, but no open editor is warned or reloaded. */
  own?: boolean;
}

/** Where an agent or task lives: always Wysebee Cloud now (lists and details carry it). */
export type ItemLocation = "local" | "cloud";

// --- Wysebee and Home (cloud/routes.ts answers these from Wysebee Cloud) ---------------

/** An agent in the list: Wysebee, or one of the person's own. */
export interface AgentSummary {
  id: string;
  agent_name: string;
  updated_at: string | null;
  label?: string | null;
  avatar?: string | null;
  /** Made by pairing the Wysebee Agent browser extension: it works in that browser. */
  browser?: boolean;
  /** Made by pairing Wysebee Agent for Mac: it works on that Mac. */
  mac?: boolean;
  /** While paired, the device's agent type key (`paired_kind`: `wysebee-windows`, …). */
  device?: string | null;
}

export interface AgentListResponse {
  rows: AgentSummary[];
}

export function fetchAgents(): Promise<AgentListResponse> {
  return authedGet<AgentListResponse>("/agent/agents");
}

/** Add one of the person's own agents: `name` its "@" handle, `label` what it is called. */
export function createAgent(name: string, label: string): Promise<AgentDetail> {
  return authedPost<AgentDetail>("/agent/agents", { name, label });
}

export function deleteAgent(agentId: string): Promise<void> {
  return authedDelete(`/agent/agents/${encodeURIComponent(agentId)}`);
}

export function fetchAgentDetail(agentId: string): Promise<AgentDetail> {
  return authedGet<AgentDetail>(`/agent/agents/${encodeURIComponent(agentId)}`);
}

/** Save an agent's settings — and, with `name`, give it a new "@" handle. */
export function saveAgentConfig(agentId: string, config: Record<string, unknown>, name?: string): Promise<AgentDetail> {
  return authedPut<AgentDetail>(`/agent/agents/${encodeURIComponent(agentId)}`, { config, ...(name !== undefined ? { name } : {}) });
}

/** A skill of the library (Wysebee Cloud's apps/cloud/skills.py): its id — which agents link
 * to it by, so it can be renamed — its name, its description (its SKILL.md's), its files and
 * the folders made that hold no file yet. */
export interface WebSkill {
  id: string;
  name: string;
  description: string;
  /** Each file's path in the skill, its id in the person's skill file store, its size. */
  files: { path: string; id?: string; size: number }[];
  folders?: string[];
  updated_at: string | null;
  /** Who made it and who changed it last. */
  created_by?: Person | null;
  updated_by?: Person | null;
}

/** The skills library. */
export function fetchSkills(): Promise<{ skills: WebSkill[] }> {
  return authedGet<{ skills: WebSkill[] }>("/api/v1/skills");
}

const skillUrl = (id: string) => `/api/v1/skills/${encodeURIComponent(id)}`;

export function fetchSkill(id: string): Promise<WebSkill> {
  return authedGet<WebSkill>(skillUrl(id));
}

/** A skill the person's AI app asks to give an agent (apps/cloud/skill_changes.py), and why. */
export interface SkillRequest {
  skill_id: string;
  name: string;
  description: string;
  reason: string;
  created_at: string;
}

const skillRequests = (agentId: string) => `/api/v1/agents/${encodeURIComponent(agentId)}/skill-requests`;

export function fetchSkillRequests(agentId: string): Promise<{ requests: SkillRequest[] }> {
  return authedGet<{ requests: SkillRequest[] }>(skillRequests(agentId));
}

/** The person's approval: the skill given to the agent, after its others. */
export function giveRequestedSkill(agentId: string, skillId: string): Promise<{ skills: WebSkill[] }> {
  return authedPost<{ skills: WebSkill[] }>(`${skillRequests(agentId)}/${encodeURIComponent(skillId)}/give`, {});
}

export function declineSkillRequest(agentId: string, skillId: string): Promise<void> {
  return authedDelete(`${skillRequests(agentId)}/${encodeURIComponent(skillId)}`);
}

/** A change to a skill proposed by the person's AI app (apps/cloud/skill_changes.py): not in
 * the skill until the person applies it. `new`: the skill has no such file (or folder) yet. */
export interface SkillChange {
  id: string;
  path: string;
  folder: boolean;
  size: number;
  new: boolean;
  created_at: string;
}

export function fetchSkillChanges(id: string): Promise<{ changes: SkillChange[] }> {
  return authedGet<{ changes: SkillChange[] }>(`${skillUrl(id)}/changes`);
}

/** A proposed change with its text, and the file's text now (null: it has none). */
export function fetchSkillChange(id: string, change: string): Promise<SkillChange & { content: string | null; current: string | null }> {
  return authedGet(`${skillUrl(id)}/changes/${encodeURIComponent(change)}`);
}

/** The person's approval: the change saved into the skill. */
export function applySkillChange(id: string, change: string): Promise<WebSkill> {
  return authedPost<WebSkill>(`${skillUrl(id)}/changes/${encodeURIComponent(change)}/apply`, {});
}

export function discardSkillChange(id: string, change: string): Promise<void> {
  return authedDelete(`${skillUrl(id)}/changes/${encodeURIComponent(change)}`);
}

/** One of the person's agents that has a skill, as the Skills page names it. */
export interface SkillAgent {
  id: string;
  name: string;
  label: string;
}

/** The person's agents that have this skill, by name (`GET /api/v1/skills/{id}/agents`). */
export function fetchSkillAgents(id: string): Promise<{ agents: SkillAgent[] }> {
  return authedGet<{ agents: SkillAgent[] }>(`${skillUrl(id)}/agents`);
}

/** Start a skill by its name: Wysebee Cloud writes it a starter SKILL.md. */
export function createSkill(name: string): Promise<WebSkill> {
  return authedPost<WebSkill>("/api/v1/skills", { name });
}

/** Rename a skill (its SKILL.md's `name` with it): the agents that have it keep it. */
export function renameSkill(id: string, name: string): Promise<WebSkill> {
  return authedPut<WebSkill>(skillUrl(id), { name });
}

/** Delete a skill from Wysebee Cloud, with its files; every agent that had it loses it. */
export function deleteSkill(id: string): Promise<void> {
  return authedDelete(skillUrl(id));
}

/** A text file of a skill; a 422 (an Error) when it is not text. */
export async function fetchSkillFile(id: string, path: string): Promise<{ path: string; content: string; size: number }> {
  return authedGet(`${skillUrl(id)}/files/${workspacePath(path)}`);
}

/** Write a text file of a skill (made when new, its folders with it): the skill. */
export async function saveSkillFile(id: string, path: string, content: string): Promise<WebSkill> {
  return authedPut<WebSkill>(`${skillUrl(id)}/files/${workspacePath(path)}`, { content });
}

/** Upload a file from the computer into a skill, as it is (any kind): the skill. Sent
 * straight to Wysebee Cloud, its bytes as they are. */
export async function uploadSkillFile(id: string, path: string, file: Blob): Promise<WebSkill> {
  try {
    const r = await cloudFetch(`${skillUrl(id)}/files/${workspacePath(path)}`, {
      method: "PUT",
      body: file,
      headers: { "Content-Type": "application/octet-stream" },
    });
    return (await r.json()) as WebSkill;
  } catch (e) {
    throw e instanceof CloudError ? new Error(e.message) : e;
  }
}

/** A stored skill file's bytes (`GET /api/v1/skill-files/{id}`), for a download. */
export async function downloadSkillFile(fileId: string): Promise<Blob> {
  try {
    return await (await cloudFetch(`/api/v1/skill-files/${encodeURIComponent(fileId)}`, { headers: { Accept: "application/octet-stream" } })).blob();
  } catch (e) {
    throw e instanceof CloudError ? new Error(e.message) : e;
  }
}

export async function deleteSkillFile(id: string, path: string): Promise<void> {
  return authedDelete(`${skillUrl(id)}/files/${workspacePath(path)}`);
}

export async function createSkillFolder(id: string, path: string): Promise<WebSkill> {
  return authedPost<WebSkill>(`${skillUrl(id)}/folders/${workspacePath(path)}`, {});
}

export async function deleteSkillFolder(id: string, path: string): Promise<void> {
  return authedDelete(`${skillUrl(id)}/folders/${workspacePath(path)}`);
}

/** The skills an agent has (by the agent's id), in its order. */
export function fetchAgentSkills(agentId: string): Promise<{ skills: WebSkill[] }> {
  return authedGet<{ skills: WebSkill[] }>(`/api/v1/agents/${encodeURIComponent(agentId)}/skills`);
}

/** Give an agent these skills (by id), in this order, saved at once. */
export function setAgentSkills(agentId: string, skillIds: string[]): Promise<{ skills: WebSkill[] }> {
  return authedPut<{ skills: WebSkill[] }>(`/api/v1/agents/${encodeURIComponent(agentId)}/skills`, { skill_ids: skillIds });
}



/** A browser paired with the Wysebee Agent extension (`/api/v1/browser-links`, the cloud's
 * apps/cloud/browser_links.py): the agent made for it — the only one that drives it — and
 * whether the extension is reaching the cloud now. */
/** An agent type as a link names it. */
export interface AgentTypeRef {
  id: string;
  key: string;
  name: string;
}

/** A switch an agent type's agents may have (its agent type pack's, apps/cloud/agent_packs.py):
 * the setting set to true when on, its English name and what it does, and the tool kind that says
 * which agents may have it (null: any agent of the type, by its link). */
export interface AgentTypeSetting {
  field: string;
  label: string;
  tool_kind: string | null;
  description: string;
}

/** An agent type as `GET /api/v1/agent-types` lists it: which kinds of device can pair, the link
 * kind and the setting holding its link, the tools only its agents may have (its row's
 * `tool_kinds`) and the switches they may have. */
export interface AgentType extends AgentTypeRef {
  description: string;
  enabled: boolean;
  kind: string;
  link_field: string;
  tool_kinds: string[];
  settings: AgentTypeSetting[];
}

/** The agent types, enabled first: their names, links, tools and switches. */
export function fetchAgentTypes(): Promise<{ types: AgentType[] }> {
  return authedGet<{ types: AgentType[] }>("/api/v1/agent-types");
}

export interface BrowserLink {
  id: string;
  /** Its agent's "@" handle, and what the person called it. */
  agent: string;
  label: string;
  browser: string;
  extension_version: string;
  status: "connected" | "offline";
  /** The person paused it in the extension: the agent is refused until they resume. */
  paused: boolean;
  last_seen: string | null;
  created_at: string;
  /** What is paired: a browser's extension (also when absent), or **Wysebee Agent for Mac**
   * (wysebee-agent-osx's docs/protocol.md), whose `browser` is the Mac's name and
   * `extension_version` that app's version. Kept for older readers: new code reads
   * `agent_type`. An `osx` link may be a Windows PC (its type says so); `android` is a phone
   * paired with Wysebee Agent for Android. */
  kind?: "browser" | "osx" | "android";
  /** Its agent type (2026-11-13, later: a table in the cloud) — null when its type is no
   * longer one; absent from a cloud before types. */
  agent_type?: AgentTypeRef | null;
  /** A Mac's: the agent's programs the person allowed there, as it last said. */
  approved?: string[];
  /** A Mac's: the agent's skills the person allowed there (their scripts run on that Mac),
   * as it last said; `[]` for a browser. */
  approved_skills?: string[];
  /** A Mac's: the folders the person picked there for the agent (Local Folder), as it last
   * said; `[]` for a browser. */
  folders?: { id: string; name: string; path: string; mode: "read" | "write"; description: string }[];
}

export function fetchBrowserLinks(): Promise<{ links: BrowserLink[] }> {
  return authedGet<{ links: BrowserLink[] }>("/api/v1/browser-links");
}

/** A pairing or an unpairing the cloud refused: its `code` (`code_unknown`, `name_taken`,
 * `too_many`, `invalid_name`, `reserved`, `too_many_tries`; "" when it sent none) and its
 * own sentence. */
/** The agent a `name_taken` refusal names, and whether the device may be reconnected to it. */
export interface TakenName {
  agent: { name: string; label: string };
  reconnect: boolean;
}

export class BrowserLinkError extends Error {
  constructor(
    readonly code: string,
    message: string,
    /** For `name_taken`: whose the name is (2026-11-13). */
    readonly taken?: TakenName,
  ) {
    super(message);
  }
}

async function browserLinkError(res: Response): Promise<BrowserLinkError> {
  const { message, detail } = await readError(res);
  const agent = detail?.agent as { name?: unknown; label?: unknown } | undefined;
  const taken =
    agent && typeof agent.name === "string" && typeof agent.label === "string"
      ? { agent: { name: agent.name, label: agent.label }, reconnect: detail?.reconnect === true }
      : undefined;
  return new BrowserLinkError(typeof detail?.code === "string" ? detail.code : "", message, taken);
}

/** Pair the browser (or Mac) whose app shows `code`: a new agent is made for it — under
 * `name` when the person chose one, or the existing agent of that name is given it with
 * `reconnect` (2026-11-13, when the cloud said it may be). */
export async function claimBrowserLink(code: string, choice: { name?: string; reconnect?: boolean } = {}): Promise<{ link: BrowserLink; agent: { name: string; label: string } }> {
  const res = await authedFetch("/api/v1/browser-links/claim", { method: "POST", body: JSON.stringify({ code, ...choice }) });
  if (!res.ok) throw await browserLinkError(res);
  return res.json() as Promise<{ link: BrowserLink; agent: { name: string; label: string } }>;
}

/** A pairing offered here for a device to scan as a QR code (2026-11-17): one use, for
 * `expires_in` seconds. */
export interface PairingOffer {
  id: string;
  token: string;
  expires_in: number;
  /** This cloud's public address, which the QR code names: the device pairs there. */
  cloud?: string;
}

/** How an offer stands: waiting for a device, paired (with the agent made), or run out. */
export interface PairingOfferState {
  state: "waiting" | "paired" | "expired";
  agent?: { name: string; label: string };
}

/** Ask for a pairing a device can scan: the token goes in the QR code. */
export async function offerPairing(): Promise<PairingOffer> {
  const res = await authedFetch("/api/v1/browser-links/offers", { method: "POST", body: "{}" });
  if (!res.ok) throw await browserLinkError(res);
  return res.json() as Promise<PairingOffer>;
}

export async function fetchPairingOffer(id: string): Promise<PairingOfferState> {
  const res = await authedFetch(`/api/v1/browser-links/offers/${encodeURIComponent(id)}`);
  if (!res.ok) throw await browserLinkError(res);
  return res.json() as Promise<PairingOfferState>;
}

/** What a pairing QR code says: the offer's token, and the cloud it was made by — the device
 * pairs with that cloud (2026-11-22). */
export function pairingQrText(token: string, from: string): string {
  return `wysebee-agent:pair?offer=${encodeURIComponent(token)}&from=${encodeURIComponent(from)}`;
}

/** Unpair a browser: its agent stays, without it. */
export async function unpairBrowserLink(id: string): Promise<void> {
  const res = await authedFetch(`/api/v1/browser-links/${encodeURIComponent(id)}/unpair`, { method: "POST" });
  if (!res.ok) throw await browserLinkError(res);
}

/** One of the person's notifications, sent by their AI app through Wysebee Assistant
 * (apps/cloud/notifications.py): its body is Markdown. */
export interface ActivityNotification {
  id: string;
  title: string;
  body: string;
  /** The agent it is from, by handle; null when it names none. */
  agent: string | null;
  created_at: string;
  read: boolean;
}

/** Fired on `window` with `detail: {unread}` when the person's unread count changed
 * elsewhere (cloud/events.ts) or here (Activity read them). */
export const ACTIVITY_CHANGED = "wysebee:activity-changed";

/** Their notifications, newest first, and how many are unread. */
export function fetchNotifications(limit = 100): Promise<{ notifications: ActivityNotification[]; unread: number }> {
  return authedGet(`/api/v1/notifications?limit=${limit}`);
}

export function fetchUnreadNotifications(): Promise<{ unread: number }> {
  return authedGet("/api/v1/notifications/unread");
}

/** Every one is seen now. */
export function markNotificationsRead(): Promise<{ read: number }> {
  return authedPost("/api/v1/notifications/read", {});
}

/** Delete one. */
export function deleteNotification(id: string): Promise<void> {
  return authedDelete(`/api/v1/notifications/${encodeURIComponent(id)}`);
}

/** The person's profile: the name they chose ("" when none) and their picture. */
export interface Profile {
  display_name: string;
  avatar: string | null;
}

/** The person's file storage, as Wysebee Cloud measures it: the bytes (`used`) and number
 * (`files`) of every file it keeps for them in blob storage, against the storage quota
 * (`limit`), and per kind (`parts`: `assets` — pictures, `skill_files`). */
export interface Storage {
  used: number;
  limit: number;
  files: number;
  parts?: { kind: string; bytes: number; files: number }[];
}

export function fetchStorage(): Promise<Storage> {
  return authedGet<Storage>("/api/v1/account/storage");
}

/** A payment for the person's subscription — one invoice of the payment provider's.
 * `amount` is in the currency's smallest unit (cents). */
export interface Purchase {
  id: string;
  created_at: string;
  month: string;
  amount: number;
  currency: string;
  status: "paid" | "failed";
}

/** The person's subscription, as the payment provider last said: `active` while it still
 * gives its plan. */
export interface Subscription {
  status: string;
  active: boolean;
  plan: PlanId | null;
  cancel_at_period_end: boolean;
  current_period_end: string | null;
}

/** A plan's id, as the server's billing extension names it. */
export type PlanId = string;

/** What a plan gives: devices paired (`devices`) and online at once (`online`), relay traffic
 * a calendar month (`relay_bytes`) and file storage (`storage`), in bytes. */
export interface PlanLimits {
  devices: number;
  online: number;
  relay_bytes: number;
  storage: number;
}

/** A plan: its name as the server gives it, its monthly price in the currency's smallest unit,
 * what it gives, the person's own payment link for it (null for a plan not set up, and for
 * every plan while they are subscribed — they switch in the portal), and the free trial that
 * link starts with: its days, and whether it asks for a card (null or absent: none). */
export interface Plan extends PlanLimits {
  id: PlanId;
  name: string;
  amount: number;
  currency: string;
  buy_url: string | null;
  trial?: { days: number; card: boolean } | null;
}

/** What the Billing tab shows: the plans, the one the person is on (null: none), the payment
 * provider's customer portal (null unless they have a subscription), their subscription and their
 * payments, newest first. */
export interface Billing {
  plans: Plan[];
  current: PlanId | null;
  manage_url: string | null;
  subscription: Subscription | null;
  purchases: Purchase[];
}

/** The person's plan and how much of it they use (`GET /plan`, served by the server's billing
 * extension where it has one): `allowed` is whether they may use Wysebee Cloud at all. `limits` is null without a plan; `usage` is this calendar month's (UTC). */
export interface PlanStatus {
  plan: PlanId | null;
  paid: boolean;
  required: boolean;
  allowed: boolean;
  billing_url: string;
  limits: PlanLimits | null;
  usage: { devices: number; online: number; relay_bytes: number; month: string; resets_at: string };
}

export function fetchPlan(): Promise<PlanStatus> {
  return authedGet<PlanStatus>("/api/v1/account/plan");
}

export function fetchBilling(): Promise<Billing> {
  return authedGet<Billing>("/api/v1/account/billing");
}

export function fetchProfile(): Promise<Profile> {
  return authedGet("/api/v1/profile");
}

/** Change what is given: a name ("" clears it), a picture (null removes it). */
export function saveProfile(change: { display_name?: string; avatar?: string | null }): Promise<Profile> {
  return authedPut("/api/v1/profile", change);
}

/** The app's look, kept with Wysebee's settings in the cloud (theme.ts). */
export function fetchAppearance(): Promise<{ theme: string }> {
  return authedGet("/api/v1/config/appearance");
}

export function saveAppearance(theme: string): Promise<{ theme: string }> {
  return authedPut("/api/v1/config/appearance", { theme });
}

/** A reminder Wysebee set, as the desktop lists it. */
export interface Reminder {
  id: string;
  text: string;
  /** UTC, ISO. */
  due_at: string;
  created_at: string | null;
  delivered_at: string | null;
}

/** Reminders still to come, soonest first. */
export function fetchReminders(): Promise<{ reminders: Reminder[] }> {
  return authedGet("/api/v1/reminders");
}

/** Cancel one still to come. */
export function cancelReminder(id: string): Promise<void> {
  return authedDelete(`/api/v1/reminders/${encodeURIComponent(id)}`);
}

export function fetchTaskRuns(id: string, limit = 20): Promise<{ rows: TaskRun[] }> {
  return authedGet<{ rows: TaskRun[] }>(`${taskPath(id)}/runs?limit=${limit}`);
}

export function fetchTaskRun(id: string, runId: string): Promise<TaskRun> {
  return authedGet<TaskRun>(`${taskPath(id)}/runs/${encodeURIComponent(runId)}`);
}

/** The site tools (sites.rs on the desktop): each one tool over the person's own links. */
export const SITE_KINDS = ["gmail", "calendar", "travel", "news", "website"] as const;
export type SiteKind = (typeof SITE_KINDS)[number];

/** One link of a site tool: `address` the link, `description` what it is for. */
export interface SiteLink {
  description: string;
  address: string;
}


/** What a tool is (apps/cloud/user_tools.py): a database connection, a SQL Query tool over one,
 * a site link, or a Mac's program. A connection is not a tool the AI sees (2026-11-17): it is
 * set on the agent's General tab, and the AI reaches it through SQL Query tools. */
export type ToolKind = "postgres" | "snowflake" | "sql" | SiteKind | "program";

/** A database connection's kinds. */
export const CONNECTION_KINDS = ["postgres", "snowflake"] as const;
export type ConnectionKind = (typeof CONNECTION_KINDS)[number];
export const isConnection = (t: Pick<Tool, "kind">): boolean => (CONNECTION_KINDS as readonly string[]).includes(t.kind);

/** One of the person's tools (2026-11-15): kept in their account in Wysebee Cloud with an id —
 * which agents link to it by, so it can be renamed — given to agents, each tool to as many as
 * they like. A connection's password, token or key is write-only: `has_secret` alone. */
export interface Tool {
  id: string;
  kind: ToolKind;
  name: string;
  description: string;
  config: Record<string, unknown>;
  has_secret: boolean;
  created_at: string;
  updated_at: string;
  /** Who made it and who changed it last. */
  created_by?: Person | null;
  updated_by?: Person | null;
}

/** A tool as it is saved: `secret` left out keeps the stored one, `""` removes it. A site
 * link's name is its address (`config.address`). */
export interface ToolIn {
  kind?: ToolKind;
  /** A new tool's id, when the page chose it. */
  id?: string;
  name: string;
  description: string;
  config: Record<string, unknown>;
  secret?: string;
}

export function fetchTools(): Promise<{ tools: Tool[] }> {
  return authedGet<{ tools: Tool[] }>("/api/v1/tools");
}

export function createTool(tool: ToolIn): Promise<Tool> {
  return authedPost<Tool>("/api/v1/tools", tool);
}

/** Change a tool, or rename it: every agent that has it keeps it. */
export function updateTool(id: string, tool: ToolIn): Promise<Tool> {
  return authedPut<Tool>(`/api/v1/tools/${encodeURIComponent(id)}`, tool);
}

export type TestResult = { ok: true; version: string; error: null } | { ok: false; version: null; error: string };

/** Try a connection with what the dialog holds (the secret typed, else the stored one of
 * `tool_id`); nothing saved. */
export function testTool(tool: ToolIn & { tool_id?: string }): Promise<TestResult> {
  return authedPost<TestResult>("/api/v1/tools/test", tool);
}

/** A SQL Query tool's input parameter (apps/cloud/agent_chat/sql_tools.py). */
export const SQL_PARAM_TYPES = ["string", "integer", "number", "boolean", "date"] as const;
export type SqlParamType = (typeof SQL_PARAM_TYPES)[number];
export interface SqlParam {
  name: string;
  type: SqlParamType;
  description: string;
  required: boolean;
  /** Its value when the AI gives none; "" none. */
  default: string;
  /** Only filter when set: it sits in [[ ]] in the SQL, a part left out when it has no value. */
  optional: boolean;
}

/** A SQL Query tool as its editor reads and saves it. */
export interface SqlTool {
  id: string;
  name: string;
  description: string;
  connectionId: string;
  params: SqlParam[];
  sql: string;
  /** Drafted by the person's AI app (apps/cloud/tool_drafts.py): the agent cannot use it until
   * the person creates it, which a save from here does. */
  draft?: boolean;
  /** A change the person's AI app proposed (`config.pending`): the tool as it would be. */
  pending?: SqlTool;
}

export function sqlToolOf(t: Tool): SqlTool {
  const c = t.config as { connection_id?: string; params?: Array<Partial<SqlParam> & { default?: unknown }>; sql?: string };
  const params = (c.params ?? []).map((p) => ({
    name: p.name ?? "",
    type: p.type ?? "string",
    description: p.description ?? "",
    required: p.required === true,
    default: p.default === null || p.default === undefined ? "" : String(p.default),
    optional: p.optional === true,
  }));
  const p = t.config.pending as { name?: string; description?: string; config?: Record<string, unknown> } | undefined;
  const pending = p && typeof p === "object" ? { pending: sqlToolOf({ ...t, name: p.name ?? t.name, description: p.description ?? t.description, config: p.config ?? {} }) } : {};
  return { id: t.id, name: t.name, description: t.description, connectionId: c.connection_id ?? "", params, sql: c.sql ?? "", ...(t.config.draft === true ? { draft: true } : {}), ...pending };
}

export function sqlToolIn(s: SqlTool, fresh = false): ToolIn {
  const params = s.params.map((p) => ({ name: p.name.trim(), type: p.type, description: p.description.trim(), required: p.required && !p.optional, default: p.default.trim() === "" ? null : p.default.trim(), optional: p.optional }));
  return { kind: "sql", ...(fresh ? { id: s.id } : {}), name: s.name.trim(), description: s.description.trim(), config: { connection_id: s.connectionId, params, sql: s.sql } };
}

/** A SQL Query tool on a connection, with the agents that have it. */
export interface ToolUser {
  id: string;
  name: string;
  draft: boolean;
  agents: { id: string; name: string; label: string }[];
}

/** A connection's SQL Query tools: while it has any, it cannot be deleted. */
export function fetchToolUsers(id: string): Promise<{ tools: ToolUser[] }> {
  return authedGet<{ tools: ToolUser[] }>(`/api/v1/tools/${encodeURIComponent(id)}/used-by`);
}

/** Delete one of the person's tools: every agent that has it loses it. */
export function deleteTool(id: string): Promise<void> {
  return authedDelete(`/api/v1/tools/${encodeURIComponent(id)}`);
}

export type ValidateResult = { ok: true; error: null } | { ok: false; error: string };

/** Check a SQL Query tool — its template, and its statement by its database, never run. */
export function validateSql(tool: ToolIn): Promise<ValidateResult> {
  return authedPost<ValidateResult>("/api/v1/tools/validate-sql", { name: tool.name, description: tool.description, config: tool.config });
}

/** The tools an agent has (by the agent's id), by kind and in its order. */
export function fetchAgentTools(agentId: string): Promise<{ tools: Tool[] }> {
  return authedGet<{ tools: Tool[] }>(`/api/v1/agents/${encodeURIComponent(agentId)}/tools`);
}

/** Give an agent these tools of a kind (by id), in this order — replacing its others of that kind. */
export function setAgentTools(agentId: string, kind: ToolKind, toolIds: string[]): Promise<{ tools: Tool[] }> {
  return authedPut<{ tools: Tool[] }>(`/api/v1/agents/${encodeURIComponent(agentId)}/tools`, { kind, tool_ids: toolIds });
}

/** A PostgreSQL connection, as its dialog and row read it. Its password is write-only: only
 * whether one is stored. */
export interface CloudPostgres {
  id: string;
  name: string;
  description: string;
  host: string;
  port: number;
  database: string;
  user: string;
  ssl: "verify" | "require";
  changes: boolean;
  has_password: boolean;
  updated_at: string;
}

/** A connection as the dialog saves or tests it: `password` left out keeps the stored one,
 * `""` removes it. */
export type CloudPostgresIn = Omit<CloudPostgres, "id" | "name" | "has_password" | "updated_at"> & { password?: string };

/** A connection's tool as its dialog and row read it: its config is as Wysebee Cloud checked
 * and kept it (apps/cloud/user_tools.py), every field there. */
export function postgresOf(t: Tool): CloudPostgres {
  const c = t.config as Pick<CloudPostgres, "host" | "port" | "database" | "user" | "ssl" | "changes">;
  return { id: t.id, name: t.name, description: t.description, ...c, has_password: t.has_secret, updated_at: t.updated_at };
}

export function postgresIn(name: string, c: CloudPostgresIn): ToolIn {
  const { description, password, ...config } = c;
  return { kind: "postgres", name, description, config, ...(password !== undefined ? { secret: password } : {}) };
}

/** A Snowflake connection, as its dialog and row read it. Its access token or private key is
 * write-only: only whether one is stored. */
export interface CloudSnowflake {
  id: string;
  name: string;
  description: string;
  /** The account identifier, like `myorg-myaccount`. */
  account: string;
  database: string;
  warehouse: string;
  schema: string;
  role: string;
  /** Needed with a key pair (its JWT names the user). */
  user: string;
  auth: "token" | "key";
  changes: boolean;
  has_secret: boolean;
  updated_at: string;
}

/** A connection as the dialog saves or tests it: `secret` left out keeps the stored one. */
export type CloudSnowflakeIn = Omit<CloudSnowflake, "id" | "name" | "has_secret" | "updated_at"> & { secret?: string };

export function snowflakeOf(t: Tool): CloudSnowflake {
  const c = t.config as Pick<CloudSnowflake, "account" | "database" | "warehouse" | "schema" | "role" | "user" | "auth" | "changes">;
  return { id: t.id, name: t.name, description: t.description, ...c, has_secret: t.has_secret, updated_at: t.updated_at };
}

export function snowflakeIn(name: string, c: CloudSnowflakeIn): ToolIn {
  const { description, secret, ...config } = c;
  return { kind: "snowflake", name, description, config, ...(secret !== undefined ? { secret } : {}) };
}

/** An agent's Connect AI app: its MCP address in Wysebee Cloud, how many tools and skills an
 * AI app gets, and why it cannot act now (its browser is closed), if so. */
/** A tool an AI app gets: its name and the first line of what it does. */
export interface McpTool {
  name: string;
  description: string;
}

/** An agent's Connect AI app: Wysebee Assistant's address, whether the agent is on for AI
 * apps, and its tools and skills. */
export interface McpStatus {
  url: string;
  exposed: boolean;
  tools: McpTool[];
  skills: number;
  offline: string | null;
}

export function fetchMcpStatus(agent: string): Promise<McpStatus> {
  return authedGet<McpStatus>(`/api/v1/cloud-mcp-status?agent=${encodeURIComponent(agent)}`);
}

/** Turn one of the person's agents, Wysebee among them, on or off for AI apps. */
export function setMcpAgentExposed(agent: string, exposed: boolean): Promise<{ agent: string; exposed: boolean }> {
  return authedPut<{ agent: string; exposed: boolean }>(`/api/v1/cloud-mcp-agents/${encodeURIComponent(agent)}`, { exposed });
}

/** The person's "Send usage analytics" choice, which follows them. */
export function fetchUsageAnalytics(): Promise<{ telemetry_enabled: boolean }> {
  return authedGet<{ telemetry_enabled: boolean }>("/api/v1/system/settings");
}

export function setUsageAnalytics(on: boolean): Promise<{ telemetry_enabled: boolean }> {
  return authedPut<{ telemetry_enabled: boolean }>("/api/v1/system/settings", { telemetry_enabled: on });
}


// --- Workspaces (2026-11-14): the person's documentation, each a folder of text files in
// Wysebee Cloud's `user-workspaces` container (crates/wysebee-backend/src/workspaces.rs).

export interface Workspace {
  id: string;
  name: string;
  /** Where the workspace's folder is kept in Wysebee Cloud's blob storage. */
  resource_url: string;
  created_at?: string | null;
  updated_at?: string | null;
  /** Who made it and who changed it (or one of its files) last. */
  created_by?: Person | null;
  updated_by?: Person | null;
}

export interface WorkspaceFile {
  path: string;
  size: number;
  updated_at?: string | null;
}

/** A workspace with its folder structure, as the cloud's container lists it. */
export interface WorkspaceDetail extends Workspace {
  files: WorkspaceFile[];
  folders: string[];
}

/** Each segment of a path inside a workspace or a skill, encoded; the slashes kept.
 *
 * A segment `.` or `..` is refused here, before any request: the browser would resolve it
 * (even written `%2E%2E`) and send the request to another address — `folders/../x` became
 * `POST /workspaces/{id}/x` and answered "Method Not Allowed". Wysebee Cloud refuses such a
 * name too (apps/cloud/workspaces.py, valid_path); this says why in the same words. */
export function workspacePath(path: string): string {
  const segments = path.split("/");
  if (segments.some((s) => s === "." || s === "..")) {
    throw new Error(`'${path}' cannot be a path: ${PATH_RULE}`);
  }
  return segments.map(encodeURIComponent).join("/");
}

/** What a file's or folder's name may be, as Wysebee Cloud says it. */
export const PATH_RULE = "A path is up to ten folders and a name, each of letters, digits, spaces, '-', '_' or '.', starting with a letter or digit.";

const workspaceUrl = (id: string) => `/api/v1/workspaces/${encodeURIComponent(id)}`;

/** A rich HTML page the person's AI app made from a workspace document (apps/cloud/workspace_pages.py). */
export interface WorkspacePage {
  id: string;
  workspace_id: string;
  workspace_name: string;
  source_path: string;
  title: string;
  /** Where it is shown from: its own address, the address its permission, sandboxed there. */
  view_url: string;
  updated_at: string;
}

export function fetchPage(id: string): Promise<WorkspacePage> {
  return authedGet<WorkspacePage>(`/api/v1/pages/${encodeURIComponent(id)}`);
}

/** A document's page, if the AI app made one. */
export function fetchDocumentPage(workspaceId: string, path: string): Promise<{ page: { id: string; title: string; updated_at: string } | null }> {
  return authedGet(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/page?path=${encodeURIComponent(path)}`);
}

export function fetchWorkspaces(): Promise<{ workspaces: Workspace[] }> {
  return authedGet<{ workspaces: Workspace[] }>("/api/v1/workspaces");
}

export function createWorkspace(name: string): Promise<Workspace> {
  return authedPost<Workspace>("/api/v1/workspaces", { name });
}

export function fetchWorkspace(id: string): Promise<WorkspaceDetail> {
  return authedGet<WorkspaceDetail>(workspaceUrl(id));
}

/** Rename a workspace: its files stay where they are (apps/cloud/workspaces.py). */
export function renameWorkspace(id: string, name: string): Promise<Workspace> {
  return authedPut<Workspace>(workspaceUrl(id), { name });
}

export function deleteWorkspace(id: string): Promise<void> {
  return authedDelete(workspaceUrl(id));
}

export async function fetchWorkspaceFile(id: string, path: string): Promise<{ path: string; content: string; size: number }> {
  return authedGet(`${workspaceUrl(id)}/files/${workspacePath(path)}`);
}

export async function saveWorkspaceFile(id: string, path: string, content: string): Promise<{ path: string; size: number }> {
  return authedPut(`${workspaceUrl(id)}/files/${workspacePath(path)}`, { content });
}

export async function deleteWorkspaceFile(id: string, path: string): Promise<void> {
  return authedDelete(`${workspaceUrl(id)}/files/${workspacePath(path)}`);
}

export async function createWorkspaceFolder(id: string, path: string): Promise<{ path: string }> {
  return authedPost(`${workspaceUrl(id)}/folders/${workspacePath(path)}`, {});
}

export async function deleteWorkspaceFolder(id: string, path: string): Promise<void> {
  return authedDelete(`${workspaceUrl(id)}/folders/${workspacePath(path)}`);
}

// --- Wysebee Assistant (2026-11-14): the person's AI app writes the page open in the editor,
// through Wysebee Assistant's /assistant/mcp (apps/cloud/agent_mcp.py, apps/cloud/workspace_assistant.py).

export interface AssistantState {
  on: boolean;
  /** The address the person adds to their AI app. */
  url: string;
  path?: string;
  revision?: number;
  /** Who wrote the text last. */
  writer?: "person" | "ai";
  /** The text, when it is new to the caller. */
  content?: string | null;
  /** The AI app, as it named itself when it connected. */
  ai_client?: string | null;
  ai_seen_at?: string | null;
  /** Off: the person's AI app made a skill and opened its file there (`create_skill`). */
  opened?: { skill_id: string; path: string } | null;
}

/** What the open page belongs to: a workspace, or (2026-11-17) a skill. */
export type PageKind = "workspaces" | "skills";

const assistantUrl = (id: string, kind: PageKind) => `/api/v1/${kind}/${encodeURIComponent(id)}/assistant`;

/** Switch it on for this page, with the text the editor holds. */
export function startAssistant(id: string, path: string, content: string, kind: PageKind = "workspaces"): Promise<AssistantState> {
  return authedPost<AssistantState>(assistantUrl(id, kind), { path, content });
}

/** The state, held up to `wait` seconds until the AI app writes or connects. */
export function pollAssistant(id: string, path: string, after: number, wait: number, kind: PageKind = "workspaces"): Promise<AssistantState> {
  const q = new URLSearchParams({ path, after: String(after), wait: String(wait) });
  return authedGet<AssistantState>(`${assistantUrl(id, kind)}?${q}`);
}

/** The person's own edits; a ConflictError (its detail, the state) when the AI app wrote since. */
export function sendAssistantEdit(id: string, path: string, content: string, revision: number, kind: PageKind = "workspaces"): Promise<{ revision: number }> {
  return authedPut<{ revision: number }>(assistantUrl(id, kind), { path, content, revision });
}

export function stopAssistant(id: string, kind: PageKind = "workspaces"): Promise<void> {
  return authedDelete(assistantUrl(id, kind));
}
