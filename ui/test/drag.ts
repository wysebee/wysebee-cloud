import { fireEvent } from "@testing-library/react";
import { beforeAll, vi } from "vitest";

/** jsdom has no DragEvent: one that carries the pointer (clientY) as a MouseEvent does. */
export function useDragEvents(): void {
  beforeAll(() => {
    if (!("DragEvent" in window)) Object.assign(window, { DragEvent: class DragEvent extends MouseEvent {} });
  });
}

const zone = { before: 2, into: 10, after: 18 } as const;

/** Drag `from` and drop it on `to`: before it (its top), into it (its middle) or after it (its bottom). */
export function dragOnto(from: Element, to: Element, at: keyof typeof zone): void {
  vi.spyOn(to, "getBoundingClientRect").mockReturnValue({ top: 0, height: 20, bottom: 20, left: 0, right: 100, width: 100, x: 0, y: 0, toJSON: () => ({}) });
  fireEvent.dragStart(from, { dataTransfer: { setData: vi.fn(), effectAllowed: "" } });
  fireEvent.dragOver(to, { clientY: zone[at] });
  fireEvent.drop(to);
  fireEvent.dragEnd(from);
}
