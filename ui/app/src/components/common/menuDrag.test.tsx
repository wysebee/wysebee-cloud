import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { dragOnto, useDragEvents } from "../../../../test/drag";
import { useMenuDrag, type DropWhere } from "./menuDrag";

useDragEvents();

/** Rows a to d: a takes nothing, b everything, c only into, d only before and after. */
function List({ onDrop }: { onDrop: (drag: string, target: string, where: DropWhere) => void }) {
  const drag = useMenuDrag<string, string>({
    accepts: (_d, t) => ({ a: [], b: ["before", "after", "into"], c: ["into"], d: ["before", "after"] })[t] as DropWhere[],
    onDrop,
  });
  return (
    <ul>
      {["a", "b", "c", "d"].map((k) => (
        <li key={k} data-testid={k} className={drag.markOf(k)} {...drag.source(k, k)} {...drag.target(k, k)}>
          {k}
          <span>inner {k}</span>
        </li>
      ))}
    </ul>
  );
}

describe("useMenuDrag", () => {
  it("drops before, after or into a row as it accepts, by where the pointer is on it", () => {
    const onDrop = vi.fn();
    render(<List onDrop={onDrop} />);
    const [a, b, c, d] = ["a", "b", "c", "d"].map((k) => screen.getByTestId(k));
    dragOnto(a, b, "before");
    dragOnto(a, b, "into");
    dragOnto(a, b, "after");
    dragOnto(b, c, "before");
    dragOnto(b, d, "into");
    expect(onDrop.mock.calls).toEqual([
      ["a", "b", "before"],
      ["a", "b", "into"],
      ["a", "b", "after"],
      ["b", "c", "into"],
      ["b", "d", "after"],
    ]);
    // A row that takes nothing, and a row onto itself: nothing dropped.
    dragOnto(b, a, "into");
    dragOnto(b, b, "into");
    expect(onDrop).toHaveBeenCalledTimes(5);
  });

  it("marks the row while over it, until the pointer leaves it or the drag ends", () => {
    render(<List onDrop={vi.fn()} />);
    const [a, b] = ["a", "d"].map((k) => screen.getByTestId(k));
    // Nothing dragged: nothing marked.
    fireEvent.dragOver(b, { clientY: 2 });
    expect(b.className).toBe("");
    fireEvent.dragStart(a);
    // No size (not laid out): its middle, so after.
    fireEvent.dragOver(b, { clientY: 0 });
    expect(b.className).toContain("inset_0_-2px");
    fireEvent.dragOver(b, { clientY: 0 });
    // Leaving for something inside it keeps the mark; leaving it clears it.
    fireEvent.dragLeave(b, { relatedTarget: b.querySelector("span") });
    expect(b.className).toContain("inset_0_-2px");
    fireEvent.dragLeave(b, { relatedTarget: document.body });
    expect(b.className).toBe("");
    fireEvent.dragLeave(b, { relatedTarget: document.body });
    // Dropped where nothing is marked: nothing happens.
    fireEvent.drop(b);
    fireEvent.dragEnd(a);
  });
});
