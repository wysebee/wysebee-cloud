import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { mockFetch, reply, type FakeRequest } from "../../../test/fetch";
import { resetWorkspaces } from "../workspaces";
import { WorkspacesNav } from "./WorkspacesNav";
import { useFetchTransport } from "../../../test/transport";
import { dialogs } from "./common/confirm";


const workspace = (id: string, name: string) => ({ id, name, resource_url: `https://acct/user-workspaces/me/${name}/` });
const tree = {
  ...workspace("p1", "Docs"),
  files: [{ path: "README.md", size: 9 }, { path: "guide/setup.md", size: 5 }, { path: "sales.html", size: 5 }],
  folders: ["guide"],
};

beforeEach(() => {
  resetWorkspaces();
  vi.restoreAllMocks();
});

// After the reset above, which would undo its spy.
useFetchTransport();

function setup(selectedWorkspace: string | null = null, selectedFile: string | null = null, routes: Record<string, unknown> = {}) {
  const f = mockFetch({
    "GET /api/v1/workspaces": { workspaces: [workspace("p1", "Docs"), workspace("p2", "Other")] },
    "GET /api/v1/workspaces/p1": tree,
    "GET /api/v1/workspaces/p2": reply(503, { detail: "Could not reach Wysebee Cloud." }),
    "POST /api/v1/workspaces": (r: FakeRequest) =>
      (r.body as { name: string }).name === "Taken" ? reply(409, { detail: "You already have a workspace named 'Taken'." }) : reply(201, workspace("p3", (r.body as { name: string }).name)),
    "DELETE /api/v1/workspaces/p1/folders/guide": reply(204),
    ...routes,
  });
  const onSelect = vi.fn();
  // The page's route follows what the menu picks, as App's does.
  function Harness() {
    const [at, setAt] = useState<[string | null, string | null, string | null]>([selectedWorkspace, selectedFile, null]);
    return (
      <WorkspacesNav
        selectedWorkspace={at[0]}
        selectedFile={at[1]}
        selectedFolder={at[2]}
        onSelect={(p, file, folder) => {
          onSelect(...(folder !== undefined ? [p, file, folder] : file === undefined ? [p] : [p, file]));
          setAt([p, file ?? null, folder ?? null]);
        }}
      />
    );
  }
  const utils = render(<Harness />);
  return { f, onSelect, ...utils };
}

describe("WorkspacesNav", () => {
  it("has All workspaces first, marked when none is open, opening their list", async () => {
    const { onSelect } = setup("p1");
    const all = screen.getByRole("button", { name: "All workspaces" });
    expect(all.getAttribute("aria-current")).toBeNull();
    await screen.findByRole("button", { name: "Docs" });
    await userEvent.click(all);
    expect(onSelect).toHaveBeenLastCalledWith(null);
    // The page follows: nothing open, All workspaces marked.
    await waitFor(() => expect(screen.getByRole("button", { name: "All workspaces" }).getAttribute("aria-current")).toBe("page"));
  });

  it("lists the workspaces and opens one into its folder structure", async () => {
    const { onSelect } = setup();
    expect(screen.getByText("Loading…")).toBeTruthy();
    const docs = await screen.findByRole("button", { name: "Docs" });
    await userEvent.click(docs);
    expect(onSelect).toHaveBeenCalledWith("p1");
    // Folders first, then files.
    const folder = await screen.findByRole("button", { name: "guide" });
    expect(screen.getByRole("button", { name: "README.md" })).toBeTruthy();
    // A page (.html) by its name alone.
    await userEvent.click(screen.getByRole("button", { name: "sales" }));
    expect(onSelect).toHaveBeenCalledWith("p1", "sales.html");
    expect(screen.queryByText("sales.html")).toBeNull();
    expect(screen.queryByRole("button", { name: "setup.md" })).toBeNull();
    // A folder opens onto its own page, unfolded and marked; shown already, it folds away.
    await userEvent.click(folder);
    expect(onSelect).toHaveBeenLastCalledWith("p1", undefined, "guide");
    expect(folder.getAttribute("aria-current")).toBe("page");
    expect(docs.getAttribute("aria-current")).toBeNull();
    await userEvent.click(folder);
    expect(screen.queryByRole("button", { name: "setup.md" })).toBeNull();
    await userEvent.click(folder);
    await userEvent.click(screen.getByRole("button", { name: "setup.md" }));
    expect(onSelect).toHaveBeenCalledWith("p1", "guide/setup.md");
    expect(folder.getAttribute("aria-current")).toBeNull();
    // With a file open, the folder's row opens the folder again, still unfolded.
    await userEvent.click(folder);
    expect(onSelect).toHaveBeenLastCalledWith("p1", undefined, "guide");
    expect(screen.getByRole("button", { name: "setup.md" })).toBeTruthy();
    await userEvent.click(folder);
    expect(screen.queryByRole("button", { name: "setup.md" })).toBeNull();
    // With a file open, the workspace's row opens its overview, still unfolded.
    await userEvent.click(docs);
    expect(onSelect).toHaveBeenLastCalledWith("p1");
    expect(docs.getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("button", { name: "guide" })).toBeTruthy();
    // Shown already: it folds away, and out again with its tree already read.
    await userEvent.click(docs);
    expect(screen.queryByRole("button", { name: "guide" })).toBeNull();
    await userEvent.click(docs);
    expect(screen.getByRole("button", { name: "guide" })).toBeTruthy();
  });

  it("shows the workspace and file open, and says why a tree could not be read", async () => {
    setup("p1", "guide/setup.md");
    const active = await screen.findByRole("button", { name: "README.md" });
    expect(active.getAttribute("aria-current")).toBeNull();
    // The folder the file is in is unfolded to show it.
    expect((await screen.findByRole("button", { name: "setup.md" })).getAttribute("aria-current")).toBe("page");
    const toastError = vi.spyOn(toast, "error");
    const other = screen.getByRole("button", { name: "Other" });
    await userEvent.click(other);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Could not reach Wysebee Cloud."));
    expect(within(other.closest("li")!).getByText("Loading…")).toBeTruthy();
    // Folded and unfolded again, its tree is asked for again.
    await userEvent.click(other);
    expect(within(other.closest("li")!).queryByText("Loading…")).toBeNull();
    await userEvent.click(other);
    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(2));
  });

  it("reads the tree of a workspace opened by its route, and says when it cannot", async () => {
    const toastError = vi.spyOn(toast, "error");
    setup("p2");
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Could not reach Wysebee Cloud."));
  });

  it("starts a workspace and opens its README", async () => {
    const { onSelect } = setup();
    await screen.findByRole("button", { name: "Docs" });
    await userEvent.click(screen.getByRole("button", { name: "Start a workspace" }));
    const input = screen.getByRole("textbox", { name: "Workspace name" });
    await userEvent.type(input, "Taken{Enter}");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Workspace name" })).toBeTruthy());
    await userEvent.clear(input);
    await userEvent.type(input, "New one{Enter}");
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("p3", "README.md"));
    expect(await screen.findByRole("button", { name: "New one" })).toBeTruthy();
    // Empty: the form just closes; Escape and a blur with nothing typed close it too.
    await userEvent.click(screen.getByRole("button", { name: "Start a workspace" }));
    fireEvent.submit(screen.getByRole("textbox", { name: "Workspace name" }).closest("form")!);
    expect(screen.queryByRole("textbox", { name: "Workspace name" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Start a workspace" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Workspace name" }), "x{Escape}");
    expect(screen.queryByRole("textbox", { name: "Workspace name" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Start a workspace" }));
    fireEvent.blur(screen.getByRole("textbox", { name: "Workspace name" }));
    expect(screen.queryByRole("textbox", { name: "Workspace name" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Start a workspace" }));
    const typed = screen.getByRole("textbox", { name: "Workspace name" });
    await userEvent.type(typed, "kept");
    fireEvent.blur(typed);
    expect(screen.getByRole("textbox", { name: "Workspace name" })).toBeTruthy();
  });

  it("renames a workspace in place from its pencil", async () => {
    const { f } = setup(null, null, {
      "PUT /api/v1/workspaces/p1": (r: FakeRequest) =>
        (r.body as { name: string }).name === "Other" ? reply(409, { detail: "You already have a workspace named 'Other'." }) : workspace("p1", (r.body as { name: string }).name),
    });
    const error = vi.spyOn(toast, "error").mockImplementation(() => "");
    await screen.findByRole("button", { name: "Docs" });
    const pencil = () => screen.getByRole("button", { name: "Rename Docs" });
    const field = () => screen.getByRole("textbox", { name: "Rename Docs" }) as HTMLInputElement;
    // Taken: said, and the field stays to try again.
    await userEvent.click(pencil());
    expect(field().value).toBe("Docs");
    await userEvent.clear(field());
    await userEvent.type(field(), "Other{Enter}");
    await waitFor(() => expect(error).toHaveBeenCalledWith("You already have a workspace named 'Other'."));
    expect(field()).toBeTruthy();
    // Escape leaves it as it was; so do the same name and an empty one, sending nothing.
    await userEvent.type(field(), "{Escape}");
    await userEvent.click(pencil());
    fireEvent.submit(field().closest("form")!);
    await userEvent.click(pencil());
    await userEvent.clear(field());
    fireEvent.blur(field());
    expect(f.to("PUT /api/v1/workspaces/p1")).toHaveLength(1);
    // A new name: saved, and shown.
    await userEvent.click(pencil());
    await userEvent.clear(field());
    await userEvent.type(field(), "Guides{Enter}");
    expect(await screen.findByRole("button", { name: "Guides" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: /Rename/ })).toBeNull();
  });

  it("says when there are none, or when they could not be read", async () => {
    const first = setup(null, null, { "GET /api/v1/workspaces": { workspaces: [] } });
    expect(await screen.findByText("No workspaces yet.")).toBeTruthy();
    first.unmount();
    resetWorkspaces();
    setup(null, null, { "GET /api/v1/workspaces": reply(503, { detail: "Offline." }) });
    expect(await screen.findByText("Offline.")).toBeTruthy();
  });

  it("deletes a folder when confirmed, leaving a file of it that was open", async () => {
    const { onSelect } = setup("p1", "guide/setup.md", {
      "DELETE /api/v1/workspaces/p1/folders/guide": (() => {
        let n = 0;
        return () => (++n === 1 ? reply(500, { detail: "Boom." }) : reply(204));
      })(),
    });
    await screen.findByRole("button", { name: "guide" });
    const confirm = vi.spyOn(dialogs, "confirm").mockResolvedValue(false);
    const del = screen.getByRole("button", { name: "Delete the folder guide" });
    await userEvent.click(del);
    expect(onSelect).not.toHaveBeenCalled();
    confirm.mockResolvedValue(true);
    const toastError = vi.spyOn(toast, "error");
    await userEvent.click(del);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Boom."));
    await userEvent.click(del);
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("p1"));
  });

  it("leaves a folder shown when it or a folder it is in is deleted, and only then", async () => {
    const { f, onSelect } = setup("p1", null, {
      "GET /api/v1/workspaces/p1": { ...tree, folders: ["guide", "guide/deep", "other"] },
      "DELETE /api/v1/workspaces/p1/folders/other": reply(204),
    });
    vi.spyOn(dialogs, "confirm").mockResolvedValue(true);
    await userEvent.click(await screen.findByRole("button", { name: "guide" }));
    await userEvent.click(await screen.findByRole("button", { name: "deep" }));
    expect(onSelect).toHaveBeenLastCalledWith("p1", undefined, "guide/deep");
    await userEvent.click(screen.getByRole("button", { name: "Delete the folder other" }));
    await waitFor(() => expect(f.to("DELETE /api/v1/workspaces/p1/folders/other")).toHaveLength(1));
    expect(onSelect).toHaveBeenLastCalledWith("p1", undefined, "guide/deep");
    await userEvent.click(screen.getByRole("button", { name: "Delete the folder guide" }));
    await waitFor(() => expect(onSelect).toHaveBeenLastCalledWith("p1"));
  });

  it("renames a folder in place when its name is double-clicked, keeping what was open", async () => {
    let renames = 0;
    const { f, onSelect } = setup("p1", null, {
      "GET /api/v1/workspaces/p1": () =>
        renames === 0
          ? { ...tree, folders: ["guide", "guide/deep", "other"] }
          : { ...tree, files: [{ path: "README.md", size: 9 }, { path: "Handbook/setup.md", size: 5 }], folders: ["Handbook", "Handbook/deep", "other"] },
      "PATCH /api/v1/workspaces/p1/folders/guide": (r: FakeRequest) => {
        const name = (r.body as { name: string }).name;
        if (name === "Taken") return reply(409, { detail: "'Taken' is already there." });
        renames++;
        return { path: name };
      },
    });
    const toastError = vi.spyOn(toast, "error");
    await userEvent.click(await screen.findByRole("button", { name: "other" }));
    await userEvent.click(screen.getByRole("button", { name: "guide" }));
    await userEvent.click(await screen.findByRole("button", { name: "deep" }));
    // Double-clicked: its first click shows it, the second renames it.
    await userEvent.dblClick(screen.getByRole("button", { name: "guide" }));
    expect(onSelect).toHaveBeenLastCalledWith("p1", undefined, "guide");
    const field = () => screen.getByRole("textbox", { name: "Rename guide" });
    expect((field() as HTMLInputElement).value).toBe("guide");
    // Escape keeps it; its own name or none, nothing is asked.
    await userEvent.type(field(), "{Escape}");
    expect(screen.queryByRole("textbox", { name: "Rename guide" })).toBeNull();
    await userEvent.dblClick(screen.getByRole("button", { name: "guide" }));
    fireEvent.submit(field().closest("form")!);
    await userEvent.dblClick(screen.getByRole("button", { name: "guide" }));
    await userEvent.clear(field());
    fireEvent.blur(field());
    expect(f.to("PATCH /api/v1/workspaces/p1/folders/guide")).toHaveLength(0);
    // A name taken: said, the field stays.
    await userEvent.dblClick(screen.getByRole("button", { name: "guide" }));
    await userEvent.clear(field());
    await userEvent.type(field(), "Taken{Enter}");
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("'Taken' is already there."));
    // Renamed: shown under its new path, it and what was open in it still unfolded.
    await userEvent.clear(field());
    await userEvent.type(field(), "Handbook{Enter}");
    await waitFor(() => expect(onSelect).toHaveBeenLastCalledWith("p1", undefined, "Handbook"));
    expect(f.to("PATCH /api/v1/workspaces/p1/folders/guide").map((r) => r.body)).toEqual([{ name: "Taken" }, { name: "Handbook" }]);
    expect(await screen.findByRole("button", { name: "setup.md" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "deep" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Handbook" }).getAttribute("title")).toBe("Double-click to rename");
  });

  it("deletes a file or page from its trash when confirmed, leaving it when it was open", async () => {
    const { f, onSelect } = setup("p1", "guide/setup.md", {
      "DELETE /api/v1/workspaces/p1/files/sales.html": (() => {
        let n = 0;
        return () => (++n === 1 ? reply(500, { detail: "Boom." }) : reply(204));
      })(),
      "DELETE /api/v1/workspaces/p1/files/guide/setup.md": reply(204),
      "DELETE /api/v1/workspaces/p1/files/README.md": reply(204),
    });
    const sales = await screen.findByRole("button", { name: "Delete sales.html" });
    // Each row is a hover group of its own: hovering a row inside a folder shows only that
    // row's buttons, not the folder's (which would hold every row in it).
    const guideRow = screen.getByRole("button", { name: "guide" }).parentElement!;
    expect(guideRow.className).toContain("group/folder");
    expect(guideRow.closest("li")!.className).not.toContain("group/");
    expect(within(guideRow).getByRole("button", { name: "Delete the folder guide" })).toBeTruthy();
    expect(within(guideRow).queryByRole("button", { name: "Delete guide/setup.md" })).toBeNull();
    expect(sales.getAttribute("title")).toBe("Delete sales.html");
    // Asked first; kept when not.
    const confirm = vi.spyOn(dialogs, "confirm").mockResolvedValue(false);
    await userEvent.click(sales);
    expect(confirm).toHaveBeenCalledWith("Delete “sales.html”? This cannot be undone.", { action: "delete" });
    expect(f.to("DELETE /api/v1/workspaces/p1/files/sales.html")).toHaveLength(0);
    // Refused: said. Deleted beside the file open: that file stays open.
    confirm.mockResolvedValue(true);
    const toastError = vi.spyOn(toast, "error");
    await userEvent.click(sales);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Boom."));
    await userEvent.click(sales);
    await waitFor(() => expect(f.to("DELETE /api/v1/workspaces/p1/files/sales.html")).toHaveLength(2));
    expect(onSelect).not.toHaveBeenCalled();
    // The file open, in a folder: the folder is shown.
    await userEvent.click(screen.getByRole("button", { name: "Delete guide/setup.md" }));
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("p1", undefined, "guide"));
    // The file open, at the top: the workspace is shown.
    await userEvent.click(screen.getByRole("button", { name: "README.md" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete README.md" }));
    await waitFor(() => expect(onSelect).toHaveBeenLastCalledWith("p1"));
  });

  it("deletes a folder another file is open beside without leaving it", async () => {
    const { onSelect } = setup("p1", "README.md");
    await screen.findByRole("button", { name: "guide" });
    vi.spyOn(dialogs, "confirm").mockResolvedValue(true);
    await userEvent.click(screen.getByRole("button", { name: "Delete the folder guide" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete the folder guide" })).toBeTruthy());
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("adds a page or a folder inside a folder from its + menu", async () => {
    let folders = 0;
    const { f, onSelect } = setup("p1", "README.md", {
      "PUT /api/v1/workspaces/p1/files/guide/intro.md": { path: "guide/intro.md", size: 8 },
      "PUT /api/v1/workspaces/p1/files/guide/home.html": { path: "guide/home.html", size: 9 },
      "POST /api/v1/workspaces/p1/folders/guide/drafts": () => (++folders === 1 ? reply(500, { detail: "No room." }) : reply(201, { path: "guide/drafts" })),
    });
    await screen.findByRole("button", { name: "guide" });
    const plus = screen.getByRole("button", { name: "Add to guide" });

    // The menu opens and closes: by its +, by Escape, by a click elsewhere.
    await userEvent.click(plus);
    expect(screen.getByRole("menu", { name: "Add to guide" })).toBeTruthy();
    expect(plus.getAttribute("aria-expanded")).toBe("true");
    await userEvent.click(plus);
    expect(screen.queryByRole("menu")).toBeNull();
    await userEvent.click(plus);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    await userEvent.click(plus);
    fireEvent.mouseDown(screen.getByRole("menuitem", { name: "New document" }));
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();

    // A page: named under the folder, made with its title, and opened.
    await userEvent.click(plus);
    await userEvent.click(screen.getByRole("menuitem", { name: "New document" }));
    expect(screen.queryByRole("menu")).toBeNull();
    await userEvent.type(screen.getByRole("textbox", { name: "Document name" }), "intro{Enter}");
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("p1", "guide/intro.md"));
    expect(f.calls.find((c) => c.method === "PUT")!.body).toEqual({ content: "# intro\n" });

    // A page made directly: a .html file under the folder, from a starter, and opened.
    await userEvent.click(screen.getByRole("button", { name: "Add to guide" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "New page" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Page name" }), "home{Enter}");
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("p1", "guide/home.html"));
    expect((f.calls.filter((c) => c.method === "PUT").pop()!.body as { content: string }).content).toContain("<title>home</title>");

    // A folder: a refusal is said and the name kept; then it is made.
    await userEvent.click(screen.getByRole("button", { name: "Add to guide" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "New folder" }));
    const toastError = vi.spyOn(toast, "error");
    await userEvent.type(screen.getByRole("textbox", { name: "Folder name" }), "/drafts/{Enter}");
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("No room."));
    expect(screen.getByRole("textbox", { name: "Folder name" })).toBeTruthy();
    await userEvent.type(screen.getByRole("textbox", { name: "Folder name" }), "{Enter}");
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Folder name" })).toBeNull());
    expect(f.calls.filter((c) => c.method === "POST" && c.path.endsWith("/folders/guide/drafts"))).toHaveLength(2);

    // Nothing named: Enter, Escape or leaving the box puts it away.
    for (const leave of ["{Enter}", "{Escape}", "blur"]) {
      await userEvent.click(screen.getByRole("button", { name: "Add to guide" }));
      await userEvent.click(screen.getByRole("menuitem", { name: "New document" }));
      const box = screen.getByRole("textbox", { name: "Document name" });
      if (leave === "blur") fireEvent.blur(box);
      else await userEvent.type(box, leave === "{Escape}" ? "x{Escape}" : leave);
      expect(screen.queryByRole("textbox", { name: "Document name" })).toBeNull();
    }
    // A name typed is kept when the box loses focus.
    await userEvent.click(screen.getByRole("button", { name: "Add to guide" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "New folder" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Folder name" }), "kept");
    fireEvent.blur(screen.getByRole("textbox", { name: "Folder name" }));
    expect(screen.getByRole("textbox", { name: "Folder name" })).toBeTruthy();
  });
});
