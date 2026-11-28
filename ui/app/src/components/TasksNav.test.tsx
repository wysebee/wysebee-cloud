import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import type { Task } from "../api";
import { resetMenuOrders } from "../menuOrders";
import { TasksNav } from "./TasksNav";
import { mockFetch, reply, type FakeRequest } from "../../../test/fetch";
import { useFetchTransport } from "../../../test/transport";
import { dragOnto, useDragEvents } from "../../../test/drag";

useFetchTransport();
useDragEvents();
beforeEach(() => {
  resetMenuOrders();
  mockFetch({ "GET /api/v1/menu-orders/tasks": { orders: {} } });
});

const task = (id: string, name: string, over: Partial<Task> = {}) => ({ id, name, trigger: { type: "manual" }, ...over }) as Task;

describe("TasksNav", () => {
  it("lists All tasks, then the tasks by name with their kind, and opens one", async () => {
    const onSelect = vi.fn();
    const onCreate = vi.fn();
    render(
      <TasksNav
        rows={[task("t2", "zeta", { label: "Weekly report", trigger: { type: "recurring" } as Task["trigger"] }), task("t1", "check-inbox", { label: "  " })]}
        selected="t2"
        onSelect={onSelect}
        onCreate={onCreate}
      />,
    );
    expect(screen.getByRole("navigation", { name: "Tasks" })).toBeTruthy();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["", "All tasks", "check-inbox", "Weekly report"]);
    const weekly = screen.getByRole("button", { name: /Weekly report/ });
    expect(weekly.getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("button", { name: "All tasks" }).getAttribute("aria-current")).toBeNull();
    expect(screen.getByRole("list").hasAttribute("data-track-private")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: /check-inbox/ }));
    expect(onSelect).toHaveBeenLastCalledWith("t1");
    await userEvent.click(screen.getByRole("button", { name: "All tasks" }));
    expect(onSelect).toHaveBeenLastCalledWith(null);
    await userEvent.click(screen.getByRole("button", { name: "Add Task" }));
    expect(onCreate).toHaveBeenCalled();
  });

  it("marks All tasks on the list, says when there are none or they are loading, and has no + without leave to add", () => {
    const { rerender } = render(<TasksNav rows={null} selected={null} onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: "All tasks" }).getAttribute("aria-current")).toBe("page");
    expect(screen.getByText("Loading…")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add Task" })).toBeNull();
    rerender(<TasksNav rows={[]} selected={null} onSelect={vi.fn()} />);
    expect(screen.getByText("No tasks yet.")).toBeTruthy();
  });

  describe("dragging (2026-11-28)", () => {
    const rows = [task("t1", "alpha"), task("t2", "beta"), task("t3", "gamma")];
    const names = () => screen.getAllByRole("button").map((b) => b.textContent).filter((n) => n && n !== "All tasks");
    const rowOf = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}`) }).parentElement!;

    it("shows the tasks in the order they were dragged into, any others after by name", async () => {
      mockFetch({ "GET /api/v1/menu-orders/tasks": { orders: { "": ["t3", "gone", "t1"] } } });
      render(<TasksNav rows={[...rows, task("t0", "aardvark")]} selected={null} onSelect={vi.fn()} />);
      await waitFor(() => expect(names()).toEqual(["gamma", "alpha", "aardvark", "beta"]));
    });

    it("reorders the tasks by dropping one before or after another, and keeps the order", async () => {
      const f = mockFetch({ "GET /api/v1/menu-orders/tasks": { orders: {} }, "PUT /api/v1/menu-orders/tasks": (r: FakeRequest) => r.body });
      render(<TasksNav rows={rows} selected={null} onSelect={vi.fn()} />);
      await waitFor(() => expect(f.to("GET /api/v1/menu-orders/tasks")).toHaveLength(1));
      dragOnto(rowOf("alpha"), rowOf("gamma"), "after");
      await waitFor(() => expect(f.to("PUT /api/v1/menu-orders/tasks")[0]?.body).toEqual({ scope: "", keys: ["t2", "t3", "t1"] }));
      expect(names()).toEqual(["beta", "gamma", "alpha"]);
      dragOnto(rowOf("gamma"), rowOf("beta"), "before");
      await waitFor(() => expect(f.to("PUT /api/v1/menu-orders/tasks")[1]?.body).toEqual({ scope: "", keys: ["t3", "t2", "t1"] }));
      expect(names()).toEqual(["gamma", "beta", "alpha"]);
    });

    it("says why an order could not be kept", async () => {
      const error = vi.spyOn(toast, "error").mockImplementation(() => "");
      mockFetch({ "GET /api/v1/menu-orders/tasks": { orders: {} }, "PUT /api/v1/menu-orders/tasks": reply(500, { detail: "Down." }) });
      render(<TasksNav rows={rows} selected={null} onSelect={vi.fn()} />);
      dragOnto(rowOf("alpha"), rowOf("beta"), "after");
      await waitFor(() => expect(error).toHaveBeenCalled());
      await waitFor(() => expect(names()).toEqual(["alpha", "beta", "gamma"]));
      error.mockRestore();
    });
  });
});
