import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { mockFetch, reply, type FakeRequest } from "../../../test/fetch";
import { resetAgents } from "../agents";
import { resetPresence } from "../presence";
import { resetMenuOrders } from "../menuOrders";
import { dragOnto, useDragEvents } from "../../../test/drag";
import { AgentsNav } from "./AgentsNav";
import { useFetchTransport } from "../../../test/transport";

useFetchTransport();
useDragEvents();

beforeEach(() => {
  resetMenuOrders();
  resetAgents();
  resetPresence();
});

function setup(selected: string | null = "wysebee", routes: Record<string, unknown> = {}) {
  const f = mockFetch({
    "GET /agent/agents": { rows: [{ id: "w", agent_name: "wysebee", label: "Wysebee", updated_at: null }, { id: "a1", agent_name: "aide", label: "Aide", avatar: "🧭", updated_at: null }] },
    ...routes,
  });
  const onSelect = vi.fn();
  const utils = render(<AgentsNav selected={selected} onSelect={onSelect} />);
  return { f, onSelect, ...utils };
}

describe("AgentsNav", () => {
  it("lists the agents under Agents, marks the open one, and opens another", async () => {
    const { onSelect } = setup("aide");
    const aide = await screen.findByRole("button", { name: /Aide$/ });
    expect(aide.getAttribute("aria-current")).toBe("page");
    expect(screen.getByText("Agents")).toBeTruthy();
    // No Hub, topics or direct messages.
    expect(screen.queryByText("Hub")).toBeNull();
    expect(screen.queryByText("Topics")).toBeNull();
    expect(screen.getByRole("list").hasAttribute("data-track-private")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: /Wysebee$/ }));
    expect(onSelect).toHaveBeenLastCalledWith("w");
  });

  it("has All agents first, marked on the list, opening it", async () => {
    const { onSelect } = setup(null);
    const all = screen.getByRole("button", { name: "All agents" });
    expect(all.getAttribute("aria-current")).toBe("page");
    expect((await screen.findByRole("button", { name: /Wysebee$/ })).getAttribute("aria-current")).toBeNull();
    expect(screen.getAllByRole("button").map((b) => b.textContent)[1]).toBe("All agents");
    await userEvent.click(screen.getByRole("button", { name: /Aide$/ }));
    expect(onSelect).toHaveBeenLastCalledWith("a1");
    await userEvent.click(all);
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("shows a browser agent's browser beside its name, and its presence; none for an agent in Wysebee Cloud", async () => {
    setup("wysebee", {
      "GET /agent/agents": {
        rows: [
          { id: "w", agent_name: "wysebee", label: "Wysebee", updated_at: null },
          { id: "s1", agent_name: "scout", label: "Scout", browser: true, updated_at: null },
          { id: "s2", agent_name: "sky", label: "Sky", updated_at: null },
        ],
      },
      "GET /api/v1/browser-links": { links: [{ id: "l1", agent: "scout", status: "connected", paused: false }] },
    });
    const browser = await screen.findByRole("img", { name: "Browser extension" });
    expect(browser.getAttribute("data-device-kind")).toBe("chrome");
    expect(browser.previousElementSibling?.textContent).toBe("Scout");
    for (const name of [/Sky$/, /Wysebee$/]) expect(screen.getByRole("button", { name }).querySelector("[data-device-kind]")).toBeNull();
  });

  it("reads the agents again when the window comes back, for one added elsewhere", async () => {
    const { f } = setup();
    await screen.findByRole("button", { name: /Aide$/ });
    const before = f.to("GET /agent/agents").length;
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(f.to("GET /agent/agents").length).toBeGreaterThan(before));
  });

  it("says why the agents could not be read", async () => {
    setup("wysebee", { "GET /agent/agents": reply(500, { detail: "cloud down" }) });
    expect(await screen.findByText("HTTP 500: cloud down")).toBeTruthy();
  });

  it("adds an agent and opens its settings", async () => {
    const { f, onSelect } = setup("wysebee", {
      "POST /agent/agents": (r: FakeRequest) => ({ id: "new-id", agent_name: (r.body as { name: string }).name, config: { label: (r.body as { label: string }).label }, system_prompt: "", creator: "", updated_at: null }),
    });
    await screen.findByRole("button", { name: /Aide$/ });
    // The last row adds one…
    const addButtons = screen.getAllByRole("button", { name: "Add an agent" });
    await userEvent.click(addButtons[addButtons.length - 1]);
    const dialog = screen.getByRole("dialog", { name: "Add an agent" });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "Travel Planner");
    expect((within(dialog).getByRole("textbox", { name: "@ name" }) as HTMLInputElement).value).toBe("travel-planner");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add agent" }));
    await waitFor(() => expect(onSelect).toHaveBeenLastCalledWith("new-id"));
    expect(f.to("POST /agent/agents")[0].body).toEqual({ name: "travel-planner", label: "Travel Planner" });
    expect(await screen.findByRole("button", { name: /Travel Planner$/ })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    // The header's + opens it too; Escape closes it.
    await userEvent.click(screen.getAllByRole("button", { name: "Add an agent" })[0]);
    expect(screen.getByRole("dialog")).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  describe("dragging (2026-11-28)", () => {
    const three = { rows: [{ id: "w", agent_name: "wysebee", label: "Wysebee", updated_at: null }, { id: "a1", agent_name: "aide", label: "Aide", updated_at: null }, { id: "a2", agent_name: "bee", label: "Bee", updated_at: null }] };
    const names = () => screen.getAllByRole("listitem").map((li) => li.textContent).filter((n) => ["Wysebee", "Aide", "Bee"].includes(n!));
    const rowOf = (name: RegExp) => screen.getByRole("button", { name }).parentElement!;

    it("shows the agents in the order they were dragged into, any others after as listed", async () => {
      setup(null, { "GET /agent/agents": three, "GET /api/v1/menu-orders/agents": { orders: { "": ["a2", "w"] } } });
      await screen.findByRole("button", { name: /Aide$/ });
      await waitFor(() => expect(names()).toEqual(["Bee", "Wysebee", "Aide"]));
    });

    it("reorders the agents by dropping one before or after another, and keeps the order", async () => {
      const { f } = setup(null, { "GET /agent/agents": three, "GET /api/v1/menu-orders/agents": { orders: {} }, "PUT /api/v1/menu-orders/agents": (r: FakeRequest) => r.body });
      await screen.findByRole("button", { name: /Bee$/ });
      await waitFor(() => expect(f.to("GET /api/v1/menu-orders/agents")).toHaveLength(1));
      dragOnto(rowOf(/Bee$/), rowOf(/Wysebee$/), "before");
      await waitFor(() => expect(f.to("PUT /api/v1/menu-orders/agents")[0]?.body).toEqual({ scope: "", keys: ["a2", "w", "a1"] }));
      expect(names()).toEqual(["Bee", "Wysebee", "Aide"]);
    });
  });
});
