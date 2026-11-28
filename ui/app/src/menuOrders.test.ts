import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { mockFetch, reply, type FakeRequest } from "../../test/fetch";
import { useFetchTransport } from "../../test/transport";
import { loadMenuOrders, ordered, placed, resetMenuOrders, saveMenuOrder, useMenuOrders } from "./menuOrders";

beforeEach(() => resetMenuOrders());
useFetchTransport();

describe("menu orders", () => {
  it("reads a menu's orders once, keeps one at once and on the server", async () => {
    let kept: Record<string, string[]> = { "": ["b", "a"] };
    const f = mockFetch({
      "GET /api/v1/menu-orders/workspaces": () => ({ orders: kept }),
      "PUT /api/v1/menu-orders/workspaces": (r: FakeRequest) => {
        const { scope, keys } = r.body as { scope: string; keys: string[] };
        kept = { ...kept, [scope]: keys };
        return { scope, keys };
      },
    });
    const { result } = renderHook(() => useMenuOrders("workspaces"));
    expect(result.current).toEqual({});
    await waitFor(() => expect(result.current).toEqual({ "": ["b", "a"] }));
    await act(() => loadMenuOrders("workspaces"));
    expect(f.to("GET /api/v1/menu-orders/workspaces")).toHaveLength(1);
    await act(() => saveMenuOrder("workspaces", "w1", ["x"]));
    expect(result.current).toEqual({ "": ["b", "a"], w1: ["x"] });
    expect(f.to("PUT /api/v1/menu-orders/workspaces")[0].body).toEqual({ scope: "w1", keys: ["x"] });
  });

  it("keeps an order saved before the menu was read", async () => {
    mockFetch({ "PUT /api/v1/menu-orders/agents": { scope: "", keys: ["x"] } });
    await saveMenuOrder("agents", "", ["x"]);
    const { result } = renderHook(() => useMenuOrders("agents"));
    expect(result.current).toEqual({ "": ["x"] });
  });

  it("keeps items by name when a menu cannot be read, and reads it again when a save fails", async () => {
    const f = mockFetch({
      "GET /api/v1/menu-orders/tasks": reply(503, { detail: "Down." }),
      "PUT /api/v1/menu-orders/tasks": reply(500, { detail: "Boom." }),
    });
    const { result } = renderHook(() => useMenuOrders("tasks"));
    await waitFor(() => expect(f.to("GET /api/v1/menu-orders/tasks")).toHaveLength(1));
    expect(result.current).toEqual({});
    await act(() => expect(saveMenuOrder("tasks", "", ["t1"])).rejects.toThrow("Boom."));
    expect(f.to("GET /api/v1/menu-orders/tasks")).toHaveLength(2);
    expect(result.current).toEqual({ "": ["t1"] });
  });

  it("puts a list in a kept order, the items it does not name after, as they came", () => {
    const id = (s: string) => s;
    expect(ordered(["a", "b", "c"], id, undefined)).toEqual(["a", "b", "c"]);
    expect(ordered(["a", "b", "c"], id, [])).toEqual(["a", "b", "c"]);
    expect(ordered(["a", "b", "c", "d"], id, ["c", "gone", "a"])).toEqual(["c", "a", "b", "d"]);
  });

  it("places a key before or after another, or last when that one is not there", () => {
    expect(placed(["a", "b", "c"], "c", "a", "before")).toEqual(["c", "a", "b"]);
    expect(placed(["a", "b", "c"], "a", "c", "after")).toEqual(["b", "c", "a"]);
    expect(placed(["a", "b"], "new", "a", "after")).toEqual(["a", "new", "b"]);
    expect(placed(["a", "b"], "a", "gone", "before")).toEqual(["b", "a"]);
  });
});
