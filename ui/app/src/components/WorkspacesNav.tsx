import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { ChevronRight, FilePlus2, FileText, Folder, FolderOpen, FolderKanban, FolderPlus, LayoutList, LayoutTemplate, Pencil, Plus, Trash2 } from "lucide-react";
import { cn } from "@shared/cn";
import {
  addFolder,
  addWorkspace,
  buildTree,
  htmlPagePath,
  isHtmlPage,
  loadWorkspaces,
  loadTree,
  pagePath,
  reasonOf,
  removeFile,
  removeFolder,
  renameFolder,
  renameWorkspace,
  starterPage,
  useWorkspaces,
  writeFile,
  type TreeNode,
} from "../workspaces";
import { dialogs } from "./common/confirm";

// The Agents menu's look (AgentsNav.tsx): the rail's own purple, white on it.
const row = "flex w-full min-w-0 items-center gap-1.5 rounded-md px-2 py-1 text-left text-sm text-white/85 hover:bg-white/10";
const activeRow = "bg-white/20 font-medium text-white hover:bg-white/20";
const hoverBtn =
  "absolute top-1/2 -translate-y-1/2 rounded p-1 text-white/75 opacity-0 hover:bg-white/20 hover:text-white focus:opacity-100";
const nameInput =
  "min-w-0 flex-1 rounded border border-white/30 bg-white/15 px-1.5 py-0.5 text-sm text-white outline-none placeholder:text-white/55 focus:ring-2 focus:ring-white/40";
const menuItem = "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-stone-700 hover:bg-stone-100 dark:text-stone-200 dark:hover:bg-stone-700";

/** What is being added inside a folder, named in the menu. */
interface AddingIn {
  workspace: string;
  folder: string;
  kind: "document" | "page" | "folder";
}

/**
 * The Workspaces menu beside the rail (2026-11-14): a **Workspaces** header with a + that starts
 * one, **All workspaces** (their list, `#/workspaces`, 2026-11-20 — like the Agents and Devices
 * menus), then each of the person's workspaces. A workspace opens into its **folder structure, as
 * Wysebee Cloud's `user-workspaces` container holds it** — its folders, then its files —
 * and a file opens in the page's editor. On hover, a folder's + adds a page or a folder
 * inside it (named in the menu, 2026-11-14) — a document (markdown) or a page (`.html`, 2026-11-27), and its trash deletes it and all it holds. A
 * workspace's pencil, on hover, renames it in place (2026-11-16): its files stay where they are.
 */
export function WorkspacesNav({
  selectedWorkspace,
  selectedFile,
  selectedFolder = null,
  onSelect,
}: {
  selectedWorkspace: string | null;
  selectedFile: string | null;
  /** The folder shown in the page (`?folder=`), when no file is open. */
  selectedFolder?: string | null;
  onSelect: (workspace: string | null, file?: string, folder?: string) => void;
}) {
  const { t } = useTranslation();
  const { items, error, trees } = useWorkspaces();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  // The folder whose + menu is open ("<workspace>/<folder path>"), and what is being named in one.
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [addingIn, setAddingIn] = useState<AddingIn | null>(null);
  const [childName, setChildName] = useState("");
  // The workspace being renamed, and the name typed.
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  // The folder being renamed ("<workspace>/<folder path>", double-clicked), and the name typed.
  const [renamingFolder, setRenamingFolder] = useState<string | null>(null);
  const [folderName, setFolderName] = useState("");
  // Which workspaces and folders are open: "<workspace>" or "<workspace>/<folder path>".
  const [open, setOpen] = useState<Set<string>>(() => new Set(selectedWorkspace ? [selectedWorkspace] : []));

  useEffect(() => {
    void loadWorkspaces();
  }, []);

  // A folder's + menu closes on a click anywhere outside it (or its +).
  useEffect(() => {
    if (!menuFor) return;
    const close = (e: MouseEvent) => {
      if (!(e.target instanceof Element) || !e.target.closest("[data-folder-menu]")) setMenuFor(null);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menuFor]);

  // The workspace shown is open, its tree read.
  useEffect(() => {
    if (!selectedWorkspace) return;
    setOpen((prev) => new Set(prev).add(selectedWorkspace));
    if (!trees[selectedWorkspace]) loadTree(selectedWorkspace).catch((e) => toast.error(reasonOf(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedWorkspace]);

  // The folder shown is open in the menu, and every folder it (or the file shown) is in.
  useEffect(() => {
    const within = selectedFolder ?? (selectedFile?.includes("/") ? selectedFile.slice(0, selectedFile.lastIndexOf("/")) : null);
    if (!selectedWorkspace || !within) return;
    setOpen((prev) => {
      const next = new Set(prev).add(selectedWorkspace);
      const parts = within.split("/");
      for (let i = 1; i <= parts.length; i++) next.add(`${selectedWorkspace}/${parts.slice(0, i).join("/")}`);
      return next;
    });
  }, [selectedWorkspace, selectedFolder, selectedFile]);

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  // A workspace not shown (or shown with a file open) is opened onto its overview; the one
  // already shown folds away or out.
  const openWorkspace = (id: string) => {
    if (selectedWorkspace !== id || selectedFile || selectedFolder) {
      setOpen((prev) => new Set(prev).add(id));
      onSelect(id);
      return;
    }
    if (!open.has(id) && !trees[id]) loadTree(id).catch((e) => toast.error(reasonOf(e)));
    toggle(id);
  };

  // A folder not shown is opened onto its own page (what it holds, adding to it); the one
  // already shown folds away or out, as a workspace does.
  const openFolder = (workspace: string, path: string) => {
    const key = `${workspace}/${path}`;
    if (selectedWorkspace !== workspace || selectedFile || selectedFolder !== path) {
      setOpen((prev) => new Set(prev).add(key));
      onSelect(workspace, undefined, path);
      return;
    }
    toggle(key);
  };

  const create = async () => {
    const value = name.trim();
    if (!value) {
      setAdding(false);
      return;
    }
    setBusy(true);
    try {
      const workspace = await addWorkspace(value);
      setAdding(false);
      setName("");
      onSelect(workspace.id, "README.md");
    } catch (e) {
      toast.error(reasonOf(e));
    } finally {
      setBusy(false);
    }
  };

  const rename = async (id: string, before: string) => {
    const value = newName.trim();
    if (!value || value === before) {
      setRenaming(null);
      return;
    }
    setBusy(true);
    try {
      await renameWorkspace(id, value);
      setRenaming(null);
    } catch (e) {
      toast.error(reasonOf(e));
    } finally {
      setBusy(false);
    }
  };

  const startAdding = (workspace: string, folder: string, kind: AddingIn["kind"]) => {
    setMenuFor(null);
    setChildName("");
    setAddingIn({ workspace, folder, kind });
    setOpen((prev) => new Set(prev).add(`${workspace}/${folder}`));
  };

  const addInFolder = async () => {
    const value = childName.trim().replace(/^\/+|\/+$/g, "");
    if (!addingIn || !value) {
      setAddingIn(null);
      return;
    }
    const { workspace, folder, kind } = addingIn;
    setBusy(true);
    try {
      if (kind === "document") {
        const path = pagePath(folder, value);
        const title = path.split("/").pop()!.replace(/\.[^.]+$/, "");
        await writeFile(workspace, path, `# ${title}\n`);
        setAddingIn(null);
        onSelect(workspace, path);
      } else if (kind === "page") {
        const path = htmlPagePath(folder, value);
        await writeFile(workspace, path, starterPage(path.split("/").pop()!.replace(/\.html$/i, "")));
        setAddingIn(null);
        onSelect(workspace, path);
      } else {
        await addFolder(workspace, `${folder}/${value}`);
        setAddingIn(null);
        setOpen((prev) => new Set(prev).add(`${workspace}/${folder}/${value}`));
      }
    } catch (e) {
      toast.error(reasonOf(e));
    } finally {
      setBusy(false);
    }
  };

  const startRenamingFolder = (workspace: string, node: TreeNode) => {
    setMenuFor(null);
    setFolderName(node.name);
    setRenamingFolder(`${workspace}/${node.path}`);
  };

  // Renamed: what was open stays open under the new name, and the folder is shown there (a
  // double-click's first click showed it).
  const commitFolderName = async (workspace: string, path: string, before: string) => {
    const value = folderName.trim();
    if (!value || value === before) {
      setRenamingFolder(null);
      return;
    }
    setBusy(true);
    try {
      const to = await renameFolder(workspace, path, value);
      setRenamingFolder(null);
      const moved = (p: string) => (`${p}/`.startsWith(`${path}/`) ? to + p.slice(path.length) : p);
      setOpen((prev) => new Set([...prev].map((k) => (k.startsWith(`${workspace}/`) ? `${workspace}/${moved(k.slice(workspace.length + 1))}` : k))));
      onSelect(workspace, undefined, to);
    } catch (e) {
      toast.error(reasonOf(e));
    } finally {
      setBusy(false);
    }
  };

  // A file or page deleted from the menu; the one open leaves for the folder it was in.
  const deleteFile = async (workspace: string, path: string) => {
    if (!(await dialogs.confirm(t("workspaces.editor.deleteConfirm", { name: path }), { action: "delete" }))) return;
    try {
      await removeFile(workspace, path);
      if (selectedWorkspace === workspace && selectedFile === path) {
        const parent = path.slice(0, Math.max(0, path.lastIndexOf("/")));
        if (parent) onSelect(workspace, undefined, parent);
        else onSelect(workspace);
      }
    } catch (e) {
      toast.error(reasonOf(e));
    }
  };

  const deleteFolder = async (workspace: string, path: string) => {
    if (!(await dialogs.confirm(t("workspaces.nav.deleteFolderConfirm", { name: path }), { action: "delete" }))) return;
    try {
      await removeFolder(workspace, path);
      const inside = (p: string | null) => !!p && (p === path || p.startsWith(path + "/"));
      if (selectedWorkspace === workspace && (selectedFile?.startsWith(path + "/") || inside(selectedFolder))) onSelect(workspace);
    } catch (e) {
      toast.error(reasonOf(e));
    }
  };

  const renderNodes = (workspace: string, nodes: TreeNode[], depth: number) =>
    nodes.map((node) => {
      const key = `${workspace}/${node.path}`;
      const indent = { paddingLeft: `${0.5 + depth * 0.85}rem` };
      if (node.kind === "file") {
        const active = selectedWorkspace === workspace && selectedFile === node.path;
        return (
          <li key={key} className="group/file relative">
            <button
              type="button"
              className={cn(row, "pr-8", active && activeRow)}
              style={indent}
              onClick={() => onSelect(workspace, node.path)}
              aria-current={active ? "page" : undefined}
            >
              {isHtmlPage(node.path) ? (
                <LayoutTemplate size={14} className="shrink-0 opacity-70" aria-hidden />
              ) : (
                <FileText size={14} className="shrink-0 opacity-70" aria-hidden />
              )}
              {/* A page shows by its name alone: "sales", not "sales.html". */}
              <span className="truncate">{isHtmlPage(node.path) ? node.name.replace(/\.html$/i, "") : node.name}</span>
            </button>
            <button
              type="button"
              className={cn(hoverBtn, "right-1 group-hover/file:opacity-100")}
              onClick={() => void deleteFile(workspace, node.path)}
              aria-label={t("workspaces.nav.deleteFile", { name: node.path })}
              title={t("workspaces.nav.deleteFile", { name: node.path })}
              data-track="Workspaces delete file in menu"
            >
              <Trash2 size={13} aria-hidden />
            </button>
          </li>
        );
      }
      const expanded = open.has(key);
      const menuOpen = menuFor === key;
      const naming = addingIn?.workspace === workspace && addingIn.folder === node.path ? addingIn : null;
      const shown = selectedWorkspace === workspace && !selectedFile && selectedFolder === node.path;
      const renamingThis = renamingFolder === key;
      const childIndent = { paddingLeft: `${0.5 + (depth + 1) * 0.85}rem` };
      return (
        <li key={key} className="relative">
          {/* The hover group is the folder's own row, not its <li>: what it holds is inside the
              <li>, and hovering that must not show this folder's buttons (nor a folder's
              inside it). */}
          <div className="group/folder relative">
            {renamingThis ? (
              <form
                className="flex items-center gap-1.5 py-0.5 pr-2"
                style={indent}
                onSubmit={(e) => {
                  e.preventDefault();
                  void commitFolderName(workspace, node.path, node.name);
                }}
              >
                <FolderOpen size={14} className="shrink-0 text-white/70" aria-hidden />
                <input
                  className={nameInput}
                  autoFocus
                  onFocus={(e) => e.currentTarget.select()}
                  value={folderName}
                  maxLength={100}
                  aria-label={t("workspaces.nav.rename", { name: node.path })}
                  disabled={busy}
                  onChange={(e) => setFolderName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setRenamingFolder(null);
                  }}
                  onBlur={() => {
                    if (!busy) void commitFolderName(workspace, node.path, node.name);
                  }}
                />
              </form>
            ) : (
              <button
                type="button"
                className={cn(row, "pr-14", shown && activeRow)}
                style={indent}
                // The second click of a double-click renames instead (below).
                onClick={(e) => {
                  if (e.detail < 2) openFolder(workspace, node.path);
                }}
                onDoubleClick={() => startRenamingFolder(workspace, node)}
                title={t("workspaces.nav.renameHint")}
                aria-expanded={expanded}
                aria-current={shown ? "page" : undefined}
              >
                <ChevronRight size={14} className={cn("shrink-0 transition-transform", expanded && "rotate-90")} aria-hidden />
                {expanded ? <FolderOpen size={14} className="shrink-0 opacity-70" aria-hidden /> : <Folder size={14} className="shrink-0 opacity-70" aria-hidden />}
                <span className="truncate">{node.name}</span>
              </button>
            )}
            <button
              type="button"
              className={cn(hoverBtn, "right-7 group-hover/folder:opacity-100", menuOpen && "bg-white/20 opacity-100")}
              onClick={() => setMenuFor(menuOpen ? null : key)}
              data-folder-menu
              aria-label={t("workspaces.nav.addIn", { name: node.path })}
              title={t("workspaces.nav.addIn", { name: node.path })}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              data-track="Workspaces add in folder"
            >
              <Plus size={13} aria-hidden />
            </button>
            <button
              type="button"
              className={cn(hoverBtn, "right-1 group-hover/folder:opacity-100")}
              onClick={() => void deleteFolder(workspace, node.path)}
              aria-label={t("workspaces.nav.deleteFolder", { name: node.path })}
              title={t("workspaces.nav.deleteFolder", { name: node.path })}
              data-track="Workspaces delete folder"
            >
              <Trash2 size={13} aria-hidden />
            </button>
            {menuOpen && (
              <div
                role="menu"
                data-folder-menu
                aria-label={t("workspaces.nav.addIn", { name: node.path })}
                className="absolute right-1 top-full z-20 mt-1 w-48 rounded-md border border-stone-200 bg-white p-1 shadow-lg dark:border-stone-700 dark:bg-stone-800"
                onKeyDown={(e) => {
                  if (e.key === "Escape") setMenuFor(null);
                }}
              >
                <button type="button" role="menuitem" className={menuItem} autoFocus onClick={() => startAdding(workspace, node.path, "document")} data-track="Workspaces new document in folder">
                  <FilePlus2 size={14} aria-hidden /> {t("workspaces.nav.newDocument")}
                </button>
                <button type="button" role="menuitem" className={menuItem} onClick={() => startAdding(workspace, node.path, "page")} data-track="Workspaces new page in folder">
                  <LayoutTemplate size={14} aria-hidden /> {t("workspaces.nav.newPage")}
                </button>
                <button type="button" role="menuitem" className={menuItem} onClick={() => startAdding(workspace, node.path, "folder")} data-track="Workspaces new folder in folder">
                  <FolderPlus size={14} aria-hidden /> {t("workspaces.nav.newFolder")}
                </button>
              </div>
            )}
          </div>
          {expanded && (
            <ul className="flex flex-col gap-0.5">
              {naming && (
                <li>
                  <form
                    className="flex items-center gap-1.5 py-0.5 pr-2"
                    style={childIndent}
                    onSubmit={(e) => {
                      e.preventDefault();
                      void addInFolder();
                    }}
                  >
                    {naming.kind === "document" ? (
                      <FileText size={14} className="shrink-0 text-white/70" aria-hidden />
                    ) : naming.kind === "page" ? (
                      <LayoutTemplate size={14} className="shrink-0 text-white/70" aria-hidden />
                    ) : (
                      <Folder size={14} className="shrink-0 text-white/70" aria-hidden />
                    )}
                    <input
                      className={nameInput}
                      autoFocus
                      value={childName}
                      maxLength={120}
                      placeholder={t(`workspaces.nav.${naming.kind}Name`)}
                      aria-label={t(`workspaces.nav.${naming.kind}Name`)}
                      disabled={busy}
                      onChange={(e) => setChildName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Escape") setAddingIn(null);
                      }}
                      onBlur={() => {
                        if (!childName.trim() && !busy) setAddingIn(null);
                      }}
                    />
                  </form>
                </li>
              )}
              {renderNodes(workspace, node.children, depth + 1)}
            </ul>
          )}
        </li>
      );
    });

  return (
    <nav
      className="sticky top-0 flex h-screen w-64 shrink-0 flex-col gap-0.5 self-start overflow-y-auto border-l border-white/30 bg-[#4b3283] px-2 py-3 text-white"
      aria-label={t("workspaces.nav.label")}
    >
      <div className="flex items-center pb-2">
        <h2 className="flex-1 px-2 text-base font-bold text-white">{t("workspaces.nav.title")}</h2>
        <button
          type="button"
          className="rounded p-1 text-white/80 hover:bg-white/20 hover:text-white"
          onClick={() => {
            setName("");
            setAdding(true);
          }}
          aria-label={t("workspaces.nav.add")}
          title={t("workspaces.nav.add")}
          data-track="Workspaces add"
        >
          <Plus size={16} aria-hidden />
        </button>
      </div>

      <ul className="flex flex-col gap-0.5" data-track-private>
        <li>
          <button type="button" className={cn(row, selectedWorkspace === null && activeRow)} onClick={() => onSelect(null)} aria-current={selectedWorkspace === null ? "page" : undefined}>
            <LayoutList size={14} className="shrink-0 opacity-80" aria-hidden />
            <span className="truncate">{t("workspaces.nav.all")}</span>
          </button>
        </li>
        {error && <li className="px-2 text-xs text-red-100">{error}</li>}
        {items === null && !error && <li className="px-2 text-xs text-white/70">{t("workspaces.nav.loading")}</li>}
        {items?.length === 0 && !adding && <li className="px-2 text-xs text-white/70">{t("workspaces.nav.none")}</li>}
        {(items ?? []).map((workspace) => {
          const expanded = open.has(workspace.id);
          const tree = trees[workspace.id];
          return (
            <li key={workspace.id}>
              {renaming === workspace.id ? (
                <form
                  className="flex items-center gap-2 px-2 py-0.5"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void rename(workspace.id, workspace.name);
                  }}
                >
                  <FolderKanban size={15} className="shrink-0 text-white/70" aria-hidden />
                  <input
                    className={nameInput}
                    autoFocus
                    value={newName}
                    maxLength={80}
                    aria-label={t("workspaces.nav.rename", { name: workspace.name })}
                    disabled={busy}
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setRenaming(null);
                    }}
                    onBlur={() => {
                      if (!busy) void rename(workspace.id, workspace.name);
                    }}
                  />
                </form>
              ) : (
                <div className="group/workspace relative">
                  <button
                    type="button"
                    className={cn(row, "pr-8", selectedWorkspace === workspace.id && !selectedFile && !selectedFolder && activeRow)}
                    onClick={() => openWorkspace(workspace.id)}
                    aria-expanded={expanded}
                    aria-current={selectedWorkspace === workspace.id && !selectedFile && !selectedFolder ? "page" : undefined}
                  >
                    <ChevronRight size={14} className={cn("shrink-0 transition-transform", expanded && "rotate-90")} aria-hidden />
                    <FolderKanban size={15} className="shrink-0 opacity-80" aria-hidden />
                    <span className="truncate">{workspace.name}</span>
                  </button>
                  <button
                    type="button"
                    className={cn(hoverBtn, "right-1 group-hover/workspace:opacity-100")}
                    onClick={() => {
                      setNewName(workspace.name);
                      setRenaming(workspace.id);
                    }}
                    aria-label={t("workspaces.nav.rename", { name: workspace.name })}
                    title={t("workspaces.nav.rename", { name: workspace.name })}
                    data-track="Workspaces rename"
                  >
                    <Pencil size={13} aria-hidden />
                  </button>
                </div>
              )}
              {expanded && (
                <ul className="flex flex-col gap-0.5">
                  {tree ? (
                    renderNodes(workspace.id, buildTree(tree), 1)
                  ) : (
                    <li className="px-6 text-xs text-white/70">{t("workspaces.nav.loading")}</li>
                  )}
                </ul>
              )}
            </li>
          );
        })}
        {adding && (
          <li>
            <form
              className="flex items-center gap-2 px-2 py-0.5"
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
            >
              <FolderKanban size={15} className="shrink-0 text-white/70" aria-hidden />
              <input
                className={nameInput}
                autoFocus
                value={name}
                maxLength={80}
                placeholder={t("workspaces.nav.placeholder")}
                aria-label={t("workspaces.nav.name")}
                disabled={busy}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setAdding(false);
                }}
                onBlur={() => {
                  if (!name.trim() && !busy) setAdding(false);
                }}
              />
            </form>
          </li>
        )}
      </ul>
    </nav>
  );
}
