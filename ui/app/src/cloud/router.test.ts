import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  accessToken: vi.fn(async () => "tok"),
  reauthenticate: vi.fn(async () => {}),
  currentProfile: vi.fn(async (): Promise<{ sub: string; name: string; email: string } | null> => null),
  signOut: vi.fn(async () => {}),
}));
vi.mock("./auth", () => auth);

import { dispatch } from "./router";

async function call(method: string, path: string, body?: string) {
  const r = await dispatch(method, path, body);
  const text = await r.text();
  return { status: r.status, body: text ? JSON.parse(text) : null };
}

const ann = { sub: "auth0|1", name: "Ann", email: "a@x.io" };

beforeEach(() => {
  auth.currentProfile.mockReset();
  auth.currentProfile.mockResolvedValue(null);
});

describe("dispatch", () => {
  it("passes workspaces straight through to Wysebee Cloud, a file's path and all", async () => {
    const seen: [string, string, unknown][] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push([init.method ?? "GET", url, init.body]);
        if (init.method === "DELETE") return new Response(null, { status: 204 });
        if (url.endsWith("/taken")) return new Response(JSON.stringify({ detail: "Taken." }), { status: 409 });
        return new Response(JSON.stringify({ ok: true }), { status: init.method === "POST" ? 201 : 200 });
      }),
    );
    expect(await call("GET", "/api/v1/workspaces")).toEqual({ status: 200, body: { ok: true } });
    expect(await call("POST", "/api/v1/workspaces", '{"name":"Docs"}')).toEqual({ status: 201, body: { ok: true } });
    expect(await call("PUT", "/api/v1/workspaces/p1/files/guide/My%20Setup.md", '{"content":"x"}')).toEqual({ status: 200, body: { ok: true } });
    expect(await call("DELETE", "/api/v1/workspaces/p1/folders/guide")).toEqual({ status: 204, body: null });
    expect(await call("GET", "/api/v1/workspaces/taken")).toEqual({ status: 409, body: { detail: "Taken." } });
    expect(await call("GET", "/api/v1/workspaces/p1/assistant?path=README.md&after=2&wait=20")).toEqual({ status: 200, body: { ok: true } });
    expect(seen.map(([m, u]) => `${m} ${u}`)).toEqual([
      "GET /api/v1/workspaces",
      "POST /api/v1/workspaces",
      "PUT /api/v1/workspaces/p1/files/guide/My%20Setup.md",
      "DELETE /api/v1/workspaces/p1/folders/guide",
      "GET /api/v1/workspaces/taken",
      "GET /api/v1/workspaces/p1/assistant?path=README.md&after=2&wait=20",
    ]);
    expect(seen[2][2]).toBe('{"content":"x"}');
    // Not a workspaces path: the usual routes.
    expect((await call("GET", "/api/v1/workspacesx")).status).toBe(501);
  });

  it("passes menu orders straight through to Wysebee Cloud", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push(`${init.method ?? "GET"} ${url} ${init.body ?? ""}`.trim());
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }),
    );
    expect(await call("GET", "/api/v1/menu-orders/workspaces")).toEqual({ status: 200, body: { ok: true } });
    expect(await call("PUT", "/api/v1/menu-orders/workspaces", '{"scope":"","keys":["w1"]}')).toEqual({ status: 200, body: { ok: true } });
    expect(seen).toEqual(["GET /api/v1/menu-orders/workspaces", 'PUT /api/v1/menu-orders/workspaces {"scope":"","keys":["w1"]}']);
  });

  it("refuses a body that is not JSON, and what the web cannot do", async () => {
    expect(await call("PUT", "/agent/agents/a1", "{nope")).toEqual({
      status: 422,
      body: { detail: "The request body is not JSON." },
    });
    const r = await call("POST", "/api/v1/config/browser-probe/install");
    expect(r.status).toBe(501);
    expect(r.body.detail).toMatch(/Not available on the web/);
    // A known path with another method is not a match either.
    expect((await call("DELETE", "/api/v1/config/users/me")).status).toBe(501);
  });

  it("passes tasks straight through to Wysebee Cloud too, its refusals and all", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push(`${init.method ?? "GET"} ${url} ${init.body ?? ""}`.trim());
        if (url.endsWith("/taken")) return new Response(JSON.stringify({ detail: "A task named 'taken' already exists." }), { status: 409 });
        return new Response(JSON.stringify({ rows: [] }), { status: 200 });
      }),
    );
    expect(await call("GET", "/api/v1/tasks")).toEqual({ status: 200, body: { rows: [] } });
    expect(await call("PUT", "/api/v1/tasks/taken", '{"name":"taken"}')).toEqual({ status: 409, body: { detail: "A task named 'taken' already exists." } });
    expect(seen).toEqual(["GET /api/v1/tasks", 'PUT /api/v1/tasks/taken {"name":"taken"}']);
    // A task's runs are not: they are read from the person's data here.
    expect((await call("GET", "/api/v1/tasks/t1/runsx")).status).toBe(409 - 409 + 200);
  });

  it("passes skills and an agent's skills straight through to Wysebee Cloud", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push(`${init.method ?? "GET"} ${url}`);
        return new Response(JSON.stringify({ skills: [] }), { status: 200 });
      }),
    );
    expect(await call("GET", "/api/v1/skills")).toEqual({ status: 200, body: { skills: [] } });
    expect((await call("GET", "/api/v1/skills/s1/files/scripts/run.py")).status).toBe(200);
    expect(await call("GET", "/api/v1/agents/a1/skills")).toEqual({ status: 200, body: { skills: [] } });
    expect(seen).toEqual(["GET /api/v1/skills", "GET /api/v1/skills/s1/files/scripts/run.py", "GET /api/v1/agents/a1/skills"]);
  });

  it("answers a failing handler as the gateway would", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error("offline"))));
    expect(await call("GET", "/api/v1/tasks")).toEqual({ status: 503, body: { detail: "offline" } });
    expect(await call("GET", "/agent/agents/a1")).toEqual({ status: 503, body: { detail: "offline" } });
  });

  it("decodes path parameters", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        seen.push(url);
        return new Response(JSON.stringify({ detail: "There is no such agent." }), { status: 404 });
      }),
    );
    expect(await call("GET", "/agent/agents/my%20agent")).toEqual({ status: 404, body: { detail: "There is no such agent." } });
    expect(seen).toEqual(["/api/v1/agents/my%20agent"]);
  });
});

describe("who is asking", () => {
  it("is the signed-in person, or nobody", async () => {
    expect((await call("GET", "/api/v1/config/users/me")).body).toMatchObject({ user_id: "", email: null, user_role: "AGENT_ADMIN" });
    auth.currentProfile.mockResolvedValue(ann);
    expect((await call("GET", "/api/v1/config/users/me")).body).toMatchObject({ user_id: "auth0|1", email: "a@x.io" });
  });

  it("is all the web stubs: settings, categories, models and the rest are not asked for here", async () => {
    for (const [m, p] of [
      ["GET", "/api/v1/config/system-settings"],
      ["GET", "/agent/skill_categories"],
      ["GET", "/agent/skills"],
      ["GET", "/api/v1/account"],
      ["GET", "/api/v1/chat/models"],
    ]) {
      expect((await call(m, p)).status).toBe(501);
    }
  });
});
