import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import i18n from "../../i18n";
import { mockFetch, reply } from "../../../../test/fetch";
import { useFetchTransport } from "../../../../test/transport";
import { cleanCode, PairBrowserDialog, PairingQr, REFUSALS, type PairKind } from "./PairBrowserDialog";

useFetchTransport();

const CLAIM = "POST /api/v1/browser-links/claim";
const LINK = { id: "l1", agent: "sky", label: "Sky", browser: "Chrome", extension_version: "1.0.0", status: "connected", paused: false, last_seen: null, created_at: "" };
const MAC = { ...LINK, id: "m1", agent: "studio", label: "Studio", browser: "Jo's MacBook", kind: "osx" };

function setup(claim: unknown, kind?: PairKind) {
  const f = mockFetch({ [CLAIM]: claim });
  const props = { onClose: vi.fn(), onPaired: vi.fn(), onOpenAgent: vi.fn() };
  render(<PairBrowserDialog kind={kind} {...props} />);
  return { f, ...props };
}

const type = async (code: string) => {
  const box = screen.getByLabelText("Pairing code");
  await userEvent.clear(box);
  if (code) await userEvent.type(box, code);
  await userEvent.click(screen.getByRole("button", { name: "Pair" }));
};

describe("cleanCode", () => {
  it("takes a code with or without the dash, in any case", () => {
    expect(cleanCode(" abcd-efgh ")).toBe("ABCDEFGH");
    expect(cleanCode("ABCD EFGH")).toBe("ABCDEFGH");
    expect(cleanCode("abcd2345")).toBe("ABCD2345");
  });
});

describe("PairBrowserDialog", () => {
  it("explains the three steps, takes the code, and offers the new agent's settings", async () => {
    const { f, onPaired, onOpenAgent, onClose } = setup({ link: LINK, agent: { name: "sky", label: "Sky" } });
    const dialog = screen.getByRole("dialog", { name: "Pair a browser" });
    expect(screen.getAllByRole("listitem").map((l) => l.textContent)).toEqual([
      "Install the Wysebee Agent extension in the browser.",
      "Open the extension and type a name for the agent that will use this browser.",
      "Type the code the extension shows here.",
    ]);
    // A code that is not eight characters is not sent.
    await type("");
    expect(screen.getByText("Type the 8 characters of the code the extension shows.")).toBeTruthy();
    await type("abc-d");
    expect(f.to(CLAIM)).toHaveLength(0);
    // Enter sends it, as typed but for its dash and case.
    await userEvent.clear(screen.getByLabelText("Pairing code"));
    await userEvent.type(screen.getByLabelText("Pairing code"), "abcd-efgh{Enter}");
    expect(await screen.findByText("Sky (@sky) now uses that browser.")).toBeTruthy();
    expect(f.to(CLAIM)[0].body).toEqual({ code: "ABCDEFGH" });
    expect(onPaired).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("Pairing code")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Open its settings" }));
    expect(onOpenAgent).toHaveBeenCalledWith("sky");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    // Escape and a click outside close it; one inside does not.
    await userEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);
    await userEvent.keyboard("{Escape}");
    await userEvent.click(dialog.parentElement as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("says why a pairing was refused, in its own words for each code and the cloud's for any other", async () => {
    const statuses: Record<string, number> = { code_unknown: 404, name_taken: 409, too_many: 409, invalid_name: 422, reserved: 422, too_many_tries: 429 };
    let code = "";
    const { f, onPaired, onClose } = setup(() => reply(statuses[code] ?? 500, code === "none" ? { detail: "The cloud is busy." } : { detail: { code, message: `cloud says ${code}` } }));
    const sentences = new Set<string>();
    for (code of Object.keys(REFUSALS)) {
      await type("ABCD-EFGH");
      // A taken name with no agent named (an older cloud's) has a sentence without one.
      const sentence = i18n.t(`settings.browsers.refused.${code === "name_taken" ? "nameTakenPlain" : REFUSALS[code]}`);
      expect(await screen.findByText(sentence)).toBeTruthy();
      expect(screen.queryByText(`cloud says ${code}`)).toBeNull();
      sentences.add(sentence);
    }
    expect(sentences.size).toBe(6);
    expect(sentences.has("Too many wrong codes. Wait ten minutes, then try again.")).toBe(true);
    // A code this page does not know, or none: what the cloud said.
    code = "browser_gone";
    await type("ABCD-EFGH");
    expect(await screen.findByText("cloud says browser_gone")).toBeTruthy();
    code = "none";
    await type("ABCD-EFGH");
    expect(await screen.findByText("The cloud is busy.")).toBeTruthy();
    // …and whatever else went wrong.
    f.set(CLAIM, () => {
      throw "offline";
    });
    await type("ABCD-EFGH");
    expect(await screen.findByText("offline")).toBeTruthy();
    expect(onPaired).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("pairs a Mac in its own words, and says which kind the code turned out to be", async () => {
    const { f, onPaired } = setup({ link: MAC, agent: { name: "studio", label: "Studio" } }, "osx");
    screen.getByRole("dialog", { name: "Pair a Mac" });
    expect(screen.getAllByRole("listitem").map((l) => l.textContent)).toEqual([
      "Install and open Wysebee Agent on the Mac.",
      "Type a name there for the agent that will use this Mac.",
      "Type the code Wysebee Agent shows here.",
    ]);
    expect(screen.getByText(/It lives in Wysebee Cloud, and only it runs programs/)).toBeTruthy();
    await type("abc");
    expect(screen.getByText("Type the 8 characters of the code Wysebee Agent shows.")).toBeTruthy();
    await type("abcd-efgh");
    expect(await screen.findByText("Studio (@studio) was created and uses Jo's MacBook.")).toBeTruthy();
    expect(screen.getByText(/must be allowed in Wysebee Agent on the Mac/)).toBeTruthy();
    expect(f.to(CLAIM)[0].body).toEqual({ code: "ABCDEFGH" });
    expect(onPaired).toHaveBeenCalledTimes(1);
  });

  it("says a browser was paired when a browser's code is typed in Pair a Mac", async () => {
    setup({ link: { ...LINK, kind: "browser" }, agent: { name: "sky", label: "Sky" } }, "osx");
    await type("ABCD-EFGH");
    expect(await screen.findByText("Sky (@sky) now uses that browser.")).toBeTruthy();
  });

  it("pairs a Windows PC or an Android phone in its own words, by the type the code turned out to be (2026-11-16)", async () => {
    setup({ link: { ...MAC, kind: "android", browser: "Pixel 9", agent_type: { id: "t5", key: "wysebee-android", name: "Wysebee Android Agent" } }, agent: { name: "pixel", label: "Pixel" } }, "windows");
    screen.getByRole("dialog", { name: "Pair a Windows PC" });
    expect(screen.getByText("Install and open Wysebee Agent on the PC.")).toBeTruthy();
    await type("ABCD-EFGH");
    expect(await screen.findByText("Pixel (@pixel) was created and uses Pixel 9.")).toBeTruthy();
    expect(screen.getByText(/only the apps you allow in Wysebee Agent on the phone/)).toBeTruthy();
  });

  it("settles a taken name with the same code: Reconnect to an agent of this kind with no device, or another name", async () => {
    const taken = (reconnect: boolean, name = "sky", label = "Sky") => reply(409, { detail: { code: "name_taken", message: "taken", agent: { name, label }, reconnect } });
    let answer: unknown = taken(true);
    const { f, onPaired } = setup(() => answer);
    await type("abcd-efgh");
    // No error: what is there, and a way to have it.
    expect(await screen.findByText(/Sky \(@sky\) already has that name\. It is a browser agent with no browser now/)).toBeTruthy();
    expect(screen.queryByText(/cannot be reconnected/)).toBeNull();
    answer = { link: LINK, agent: { name: "sky", label: "Sky" } };
    await userEvent.click(screen.getByRole("button", { name: "Reconnect to Sky" }));
    expect(await screen.findByText("Sky (@sky) now uses that browser.")).toBeTruthy();
    expect(f.to(CLAIM).map((c) => c.body)).toEqual([{ code: "ABCDEFGH" }, { code: "ABCDEFGH", reconnect: true }]);
    expect(onPaired).toHaveBeenCalledTimes(1);
  });

  it("asks for another name when the one typed cannot be had, and reconnects under the name typed here", async () => {
    let answer: unknown = reply(409, { detail: { code: "name_taken", message: "taken", agent: { name: "sky", label: "Sky" }, reconnect: false } });
    const { f } = setup(() => answer, "osx");
    await type("abcd-efgh");
    expect(await screen.findByText("You already have an agent named Sky (@sky), and this device cannot be reconnected to it. Type another name below, then Pair: the same code still works.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Reconnect/ })).toBeNull();
    // Left empty, the device's name is asked for again.
    await userEvent.click(screen.getByRole("button", { name: "Pair" }));
    expect(f.to(CLAIM)[1].body).toEqual({ code: "ABCDEFGH" });
    // Another name, reserved: asked again; then one of an agent that may be reconnected.
    answer = reply(422, { detail: { code: "reserved", message: "reserved" } });
    await userEvent.type(screen.getByLabelText("Another name"), "Pairing");
    await userEvent.click(screen.getByRole("button", { name: "Pair" }));
    expect(await screen.findByText(/That name is reserved\. Type another name below/)).toBeTruthy();
    expect(f.to(CLAIM)[2].body).toEqual({ code: "ABCDEFGH", name: "Pairing" });
    answer = reply(409, { detail: { code: "name_taken", message: "taken", agent: { name: "studio", label: "Studio" }, reconnect: true } });
    await userEvent.clear(screen.getByLabelText("Another name"));
    await userEvent.type(screen.getByLabelText("Another name"), "Studio");
    await userEvent.click(screen.getByRole("button", { name: "Pair" }));
    expect(await screen.findByText(/It is a Mac agent with no Mac now/)).toBeTruthy();
    answer = { link: MAC, agent: { name: "studio", label: "Studio" } };
    await userEvent.click(screen.getByRole("button", { name: "Reconnect to Studio" }));
    expect(await screen.findByText("Studio (@studio) was created and uses Jo's MacBook.")).toBeTruthy();
    expect(f.to(CLAIM)[4].body).toEqual({ code: "ABCDEFGH", name: "Studio", reconnect: true });
  });

  it("asks for a name an agent can be made from, and keeps the field once asked", async () => {
    let answer: unknown = reply(422, { detail: { code: "invalid_name", message: "bad" } });
    const { f } = setup(() => answer);
    expect(screen.queryByLabelText("Another name")).toBeNull();
    await type("abcd-efgh");
    expect(await screen.findByText(/That name cannot be made into an agent's @ name/)).toBeTruthy();
    // A refusal that is not about the name keeps the field for the next try.
    answer = reply(409, { detail: { code: "too_many", message: "many" } });
    await userEvent.type(screen.getByLabelText("Another name"), "Nova");
    await userEvent.click(screen.getByRole("button", { name: "Pair" }));
    expect(await screen.findByText("You have 20 agents already. Delete one, then Pair again: the same code still works.")).toBeTruthy();
    expect(screen.getByLabelText("Another name")).toBeTruthy();
    expect(f.to(CLAIM)[1].body).toEqual({ code: "ABCDEFGH", name: "Nova" });
    // A name taken that the cloud said nothing more of is a plain refusal.
    answer = reply(409, { detail: { code: "name_taken", message: "taken" } });
    await userEvent.click(screen.getByRole("button", { name: "Pair" }));
    expect(await screen.findByText("You already have an agent with that name. Type another name below, then Pair: the same code still works.")).toBeTruthy();
  });

  it("names the agent type: the one paired, and the one a taken name may be reconnected to", async () => {
    const types = { types: [{ id: "t1", key: "wysebee-browser-chrome", name: "Wysebee Chrome Agent", description: "", enabled: true }, { id: "t2", key: "wysebee-osx", name: "Wysebee OSX Agent", description: "", enabled: true }] };
    let answer: unknown = reply(409, { detail: { code: "name_taken", message: "taken", agent: { name: "studio", label: "Studio" }, reconnect: true } });
    const f = mockFetch({ [CLAIM]: () => answer, "GET /api/v1/agent-types": types });
    render(<PairBrowserDialog kind="osx" onClose={vi.fn()} onPaired={vi.fn()} onOpenAgent={vi.fn()} />);
    await waitFor(() => expect(f.to("GET /api/v1/agent-types")).toHaveLength(1));
    await type("abcd-efgh");
    expect(await screen.findByText(/Studio \(@studio\) already has that name\. It is a Wysebee OSX Agent with no Mac now/)).toBeTruthy();
    answer = { link: { ...MAC, agent_type: { id: "t2", key: "wysebee-osx", name: "Wysebee OSX Agent" } }, agent: { name: "studio", label: "Studio" } };
    await userEvent.click(screen.getByRole("button", { name: "Reconnect to Studio" }));
    expect(await screen.findByText("Studio (@studio) was created and uses Jo's MacBook.")).toBeTruthy();
    expect(screen.getByText("Wysebee OSX Agent")).toBeTruthy();
  });

  it("says the kind of agent when the types cannot be read, and drops them once closed", async () => {
    let types = (_: unknown) => {};
    mockFetch({ [CLAIM]: reply(409, { detail: { code: "name_taken", message: "taken", agent: { name: "sky", label: "Sky" }, reconnect: true } }), "GET /api/v1/agent-types": () => new Promise((resolve) => (types = resolve)) });
    const { unmount } = render(<PairBrowserDialog onClose={vi.fn()} onPaired={vi.fn()} onOpenAgent={vi.fn()} />);
    await type("abcd-efgh");
    expect(await screen.findByText(/It is a browser agent with no browser now/)).toBeTruthy();
    unmount();
    types({ types: [] });
    await new Promise((r) => setTimeout(r, 0));
  });

  it("sends one claim at a time", async () => {
    let answer = (_: unknown) => {};
    const { f } = setup(() => new Promise((resolve) => (answer = resolve)));
    await userEvent.type(screen.getByLabelText("Pairing code"), "ABCDEFGH{Enter}{Enter}");
    expect(f.to(CLAIM)).toHaveLength(1);
    answer({ link: LINK, agent: { name: "sky", label: "Sky" } });
    await waitFor(() => expect(screen.getByText("Sky (@sky) now uses that browser.")).toBeTruthy());
  });

  it("shows a phone a QR code to scan, and says which agent was made once it did (2026-11-17)", async () => {
    let state: unknown = { state: "waiting", expires_in: 500 };
    const f = mockFetch([
      ["POST /api/v1/browser-links/offers", { id: "o1", token: "T".repeat(43), expires_in: 600, cloud: "https://cloud.example/" }],
      [/^GET \/api\/v1\/browser-links\/offers\/o1/, () => state],
    ]);
    const props = { onClose: vi.fn(), onPaired: vi.fn(), onOpenAgent: vi.fn() };
    render(<PairBrowserDialog kind="android" pollMs={5} {...props} />);
    screen.getByRole("dialog", { name: "Pair an Android phone" });
    expect(await screen.findByRole("img", { name: "QR code for pairing an Android phone" })).toBeTruthy();
    // It names the cloud's own public address, which the phone pairs with (2026-11-22).
    expect(screen.getByText("Server: https://cloud.example")).toBeTruthy();
    // The code field stays, for a phone that cannot scan.
    expect(screen.getByLabelText("Pairing code")).toBeTruthy();
    await waitFor(() => expect(f.calls.filter((c) => c.path === "/api/v1/browser-links/offers/o1").length).toBeGreaterThan(1));
    state = { state: "paired", agent: { name: "pixel", label: "Pixel" } };
    expect(await screen.findByText("Pixel (@pixel) was created and uses the phone that scanned the code.")).toBeTruthy();
    expect(props.onPaired).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Open its settings" }));
    expect(props.onOpenAgent).toHaveBeenCalledWith("pixel");
  });

  it("offers a new QR code when one runs out or cannot be made, and keeps asking through a blip", async () => {
    let made: unknown = reply(503, { detail: "down" });
    let state: unknown = reply(502, { detail: "blip" });
    const f = mockFetch([
      ["POST /api/v1/browser-links/offers", () => made],
      [/^GET \/api\/v1\/browser-links\/offers\/o2/, () => state],
    ]);
    render(<PairingQr pollMs={5} onPaired={vi.fn()} />);
    expect(await screen.findByText("The QR code could not be made.")).toBeTruthy();
    made = { id: "o2", token: "U".repeat(43), expires_in: 600 };
    await userEvent.click(screen.getByRole("button", { name: "Show a new QR code" }));
    expect(await screen.findByRole("img", { name: "QR code for pairing an Android phone" })).toBeTruthy();
    // A failed look is asked again; then it runs out.
    await waitFor(() => expect(f.calls.filter((c) => c.path === "/api/v1/browser-links/offers/o2").length).toBeGreaterThan(1));
    state = { state: "expired" };
    expect(await screen.findByText("This QR code ran out.")).toBeTruthy();
    expect(screen.queryByRole("img", { name: "QR code for pairing an Android phone" })).toBeNull();
    expect(screen.getByRole("button", { name: "Show a new QR code" })).toBeTruthy();
  });

  it("stops asking about a QR code once it is closed, whether the code was still being made or looked at", async () => {
    let made!: (value: unknown) => void;
    let looked!: (value: unknown) => void;
    const f = mockFetch([
      ["POST /api/v1/browser-links/offers", () => new Promise((resolve) => (made = resolve))],
      [/^GET \/api\/v1\/browser-links\/offers\/o3/, () => new Promise((resolve) => (looked = resolve))],
    ]);
    const onPaired = vi.fn();
    // Closed while the code is made: it never shows, and nothing is looked at.
    const first = render(<PairingQr pollMs={5} onPaired={onPaired} />);
    await waitFor(() => expect(made).toBeTypeOf("function"));
    first.unmount();
    made({ id: "o3", token: "V".repeat(43), expires_in: 600 });
    await new Promise((r) => setTimeout(r, 20));
    expect(f.calls.filter((c) => c.path === "/api/v1/browser-links/offers/o3")).toHaveLength(0);
    // Closed while a look is out: its answer, paired or not, is dropped and no look follows.
    const second = render(<PairingQr pollMs={5} onPaired={onPaired} />);
    await waitFor(() => expect(f.calls.filter((c) => c.path === "/api/v1/browser-links/offers").length).toBe(2));
    made({ id: "o3", token: "V".repeat(43), expires_in: 600 });
    await waitFor(() => expect(looked).toBeTypeOf("function"));
    second.unmount();
    looked({ state: "paired", agent: { name: "pixel", label: "Pixel" } });
    await new Promise((r) => setTimeout(r, 20));
    expect(onPaired).not.toHaveBeenCalled();
    expect(f.calls.filter((c) => c.path === "/api/v1/browser-links/offers/o3")).toHaveLength(1);
  });

  it("shows no QR code for a browser, a Mac or a PC", () => {
    for (const kind of ["browser", "osx", "windows"] as PairKind[]) {
      const { unmount } = render(<PairBrowserDialog kind={kind} onClose={vi.fn()} onPaired={vi.fn()} onOpenAgent={vi.fn()} />);
      expect(screen.queryByText("Scan with the phone")).toBeNull();
      unmount();
    }
  });
});

