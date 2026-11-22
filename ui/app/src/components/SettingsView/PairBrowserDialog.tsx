import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { btn, btnPrimary, cn, errorBanner, inputBase, muted, surface } from "@shared/cn";
import { BrowserLinkError, claimBrowserLink, fetchAgentTypes, fetchPairingOffer, offerPairing, pairingQrText, type BrowserLink, type TakenName } from "../../api";
import { QrCode } from "../common/QrCode";

/** The refusals the cloud names by `code` (the extension's docs/protocol.md), each with a
 * sentence of its own here (`settings.browsers.refused.<key>`); any other is shown in the
 * cloud's words. */
export const REFUSALS: Record<string, string> = {
  code_unknown: "codeUnknown",
  name_taken: "nameTaken",
  too_many: "tooMany",
  invalid_name: "invalidName",
  reserved: "reserved",
  too_many_tries: "tooManyTries",
};

/** Which kind of device the dialog was opened for: its title, steps and notes. A Windows PC's
 * link is held where a Mac's is (its legacy `kind` is `osx`), so it is told apart by its type. */
export type PairKind = "browser" | "osx" | "windows" | "linux" | "android";
/** The devices "Your devices" pairs (2026-11-16; a Linux machine 2026-11-18), in the order its menu offers them. */
export const DEVICE_KINDS: PairKind[] = ["osx", "windows", "linux", "android"];
/** The agent type each dialog pairs (the seeded keys, 2026-11-13): whose name the Reconnect
 * prompt says, since a taken name says the agent, not its type. */
const TYPE_KEY: Record<PairKind, string> = { browser: "wysebee-browser-chrome", osx: "wysebee-osx", windows: "wysebee-windows", linux: "wysebee-linux", android: "wysebee-android" };
/** Each kind's strings: the shared ones are under `settings.browsers.pairDialog`. */
export const AREA: Record<PairKind, string> = {
  browser: "settings.browsers",
  osx: "settings.macAgents",
  windows: "settings.pcAgents",
  linux: "settings.linuxAgents",
  android: "settings.phoneAgents",
};
/** Each kind's word in analytics' `data-track`. */
export const TRACK: Record<PairKind, string> = { browser: "browser", osx: "Mac", windows: "PC", linux: "Linux machine", android: "phone" };

/** What `link` is paired with: by its type when it names one, else by its legacy kind. */
export function linkKind(link: Pick<BrowserLink, "kind" | "agent_type">): PairKind {
  const byType = (Object.keys(TYPE_KEY) as PairKind[]).find((k) => TYPE_KEY[k] === link.agent_type?.key);
  return byType ?? (link.kind === "osx" ? "osx" : link.kind === "android" ? "android" : "browser");
}

/** How often the dialog asks whether a phone scanned its QR code. */
export const OFFER_POLL_MS = 2000;

/**
 * **A QR code a phone scans to pair** (2026-11-17, the person's call: typing the phone's code
 * here was inconvenient): an offer from the cloud, one use for ten minutes, shown as a QR code.
 * Wysebee Agent on the phone scans it — and pairs with the server it names, which the dialog
 * shows (2026-11-22) — and is paired at once under the name typed there after the scan; the
 * dialog asks every {@link OFFER_POLL_MS} until then and says which agent was made. Run out, it
 * offers a new one. The code field stays below it, for a phone that cannot scan.
 */
export function PairingQr({ onPaired, pollMs = OFFER_POLL_MS }: { onPaired: (agent: { name: string; label: string }) => void; pollMs?: number }) {
  const { t } = useTranslation();
  const area = `${AREA.android}.pairDialog`;
  const [offer, setOffer] = useState<{ id: string; token: string; cloud: string } | null>(null);
  const [state, setState] = useState<"making" | "waiting" | "expired" | "failed">("making");
  const [round, setRound] = useState(0);
  const paired = useRef(onPaired);
  paired.current = onPaired;

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState("making");
    setOffer(null);
    const ask = (id: string) => {
      timer = setTimeout(async () => {
        try {
          const now = await fetchPairingOffer(id);
          if (!live) return;
          if (now.state === "paired" && now.agent) return paired.current(now.agent);
          if (now.state === "expired") return setState("expired");
        } catch {
          // A blip: ask again; the offer runs out by itself.
        }
        if (live) ask(id);
      }, pollMs);
    };
    offerPairing().then(
      (made) => {
        if (!live) return;
        // The cloud's own public address (its public_url), not this page's: a phone pairs there.
        setOffer({ id: made.id, token: made.token, cloud: (made.cloud || window.location.origin).replace(/\/+$/, "") });
        setState("waiting");
        ask(made.id);
      },
      () => live && setState("failed"),
    );
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [round, pollMs]);

  return (
    <div className="flex flex-col items-center gap-2 rounded-md border border-stone-200 p-3 text-center dark:border-stone-700">
      <div className="text-sm font-medium">{t(`${area}.qrTitle`)}</div>
      {state === "waiting" && offer ? (
        <>
          <QrCode text={pairingQrText(offer.token, offer.cloud)} label={t(`${area}.qrAlt`)} />
          <div className={cn(muted, "text-xs break-all")}>{t(`${area}.qrServer`, { url: offer.cloud })}</div>
        </>
      ) : (
        <div className="flex h-[184px] w-[184px] items-center justify-center rounded-md bg-stone-100 p-3 text-xs dark:bg-stone-800">
          {state === "making" ? t(`${area}.qrLoading`) : state === "expired" ? t(`${area}.qrExpired`) : t(`${area}.qrFailed`)}
        </div>
      )}
      {(state === "expired" || state === "failed") && (
        <button type="button" className={btn} onClick={() => setRound((r) => r + 1)} data-track="New pairing QR code">
          {t(`${area}.qrNew`)}
        </button>
      )}
      <p className={cn(muted, "text-xs")}>{t(`${area}.qrHint`)}</p>
    </div>
  );
}

/** The refusals the person settles here with another name, keeping the code (2026-11-13). */
const NAME_REFUSALS = ["name_taken", "invalid_name", "reserved"];

/** A pairing code as typed — with or without the dash, any case — down to its characters. */
export function cleanCode(typed: string): string {
  return typed.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

/**
 * **Pair a browser** (Devices → Browsers) or **Pair a device** (Devices → Your devices: a Mac,
 * `kind` `osx`, 2026-11-13; a Windows PC or an Android phone, 2026-11-16): the
 * three steps — install the Wysebee Agent extension or app, type a name for the agent there,
 * type the code it shows here — and the code. Pairing makes **a new agent** under the name
 * typed there, the only one that uses that browser or device; made on the website, it lives in
 * Wysebee Cloud. The claim does not care which kind a code is, so the paired message is by
 * the kind of link the code turned out to be. Paired, the dialog says which agent it is and
 * offers its settings.
 *
 * **A name refused is settled here, with the same code** (2026-11-13, the owner's call; the
 * protocol's "a taken name is settled where the code is typed"): taken by an agent of this
 * kind with no device now, the dialog offers **Reconnect** to it; taken otherwise, reserved
 * or unusable, a field for another name. Either claims again with the code already typed.
 */
export function PairBrowserDialog({ kind = "browser", onClose, onPaired, onOpenAgent, pollMs }: { kind?: PairKind; onClose: () => void; onPaired: () => void; onOpenAgent: (name: string) => void; pollMs?: number }) {
  const { t } = useTranslation();
  const area = `${AREA[kind]}.pairDialog`;
  const [code, setCode] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [agent, setAgent] = useState<{ name: string; label: string; browser: string; kind: PairKind; type?: string; byQr?: boolean } | null>(null);
  // The agent types' names (2026-11-13, later), read once; without them, the kind's own word.
  const [typeNames, setTypeNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let live = true;
    fetchAgentTypes().then(
      ({ types }) => live && setTypeNames(Object.fromEntries(types.map((ty) => [ty.key, ty.name]))),
      () => {},
    );
    return () => {
      live = false;
    };
  }, []);
  // Once a name is refused: the field for another, what was last sent, and whom it named.
  const [asking, setAsking] = useState(false);
  const [name, setName] = useState("");
  const [sent, setSent] = useState<string | undefined>(undefined);
  const [taken, setTaken] = useState<TakenName | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const pair = async (reconnect = false) => {
    const clean = cleanCode(code);
    if (clean.length !== 8) return setProblem(t(`${area}.needCode`));
    // Reconnecting asks for the agent the last claim named; a new name is sent once typed.
    const chosen = reconnect ? sent : asking && name.trim() ? name : undefined;
    setBusy(true);
    setProblem(null);
    try {
      const made = await claimBrowserLink(clean, { ...(chosen !== undefined ? { name: chosen } : {}), ...(reconnect ? { reconnect: true } : {}) });
      setAgent({ ...made.agent, browser: made.link.browser, kind: linkKind(made.link), type: made.link.agent_type?.name });
      onPaired();
    } catch (e) {
      const code = e instanceof BrowserLinkError ? e.code : "";
      const key = REFUSALS[code];
      const may = code === "name_taken" && e instanceof BrowserLinkError && e.taken ? e.taken : null;
      if (NAME_REFUSALS.includes(code)) {
        setAsking(true);
        setSent(chosen);
      }
      setTaken(may);
      // A taken name the cloud said nothing more of (an older cloud's) is said without it.
      const sentence = code === "name_taken" && !may ? "nameTakenPlain" : key;
      setProblem(may?.reconnect ? null : sentence ? t(`settings.browsers.refused.${sentence}`, may?.agent ?? {}) : e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  };

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 text-stone-900 dark:text-stone-100" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t(`${area}.title`)}
        className={cn(surface, "flex w-full max-w-md flex-col gap-4 p-5 shadow-xl")}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold">{t(`${area}.title`)}</h2>
        {agent ? (
          <>
            <p className="text-sm" data-track-private>
              {t(`${AREA[agent.kind]}.pairDialog.${agent.byQr ? "pairedByQr" : "paired"}`, { label: agent.label, name: agent.name, browser: agent.browser })}
            </p>
            {agent.type && <p className="text-xs font-medium">{agent.type}</p>}
            <p className={cn(muted, "text-xs")}>{t(`${AREA[agent.kind]}.pairDialog.pairedNote`)}</p>
            <div className="flex justify-end gap-2">
              <button type="button" className={btn} onClick={onClose}>
                {t("settings.browsers.pairDialog.done")}
              </button>
              <button type="button" className={btnPrimary} autoFocus onClick={() => onOpenAgent(agent.name)} data-track="Open paired agent">
                {t("settings.browsers.pairDialog.open")}
              </button>
            </div>
          </>
        ) : (
          <>
            <ol className="list-decimal space-y-1 pl-5 text-sm">
              <li>{t(`${area}.step1`)}</li>
              <li>{t(`${area}.step2`)}</li>
              <li>{t(`${area}.step3`)}</li>
            </ol>
            <p className={cn(muted, "text-xs")}>{t(`${area}.note`)}</p>
            {kind === "android" && (
              <PairingQr
                pollMs={pollMs}
                onPaired={(made) => {
                  setAgent({ ...made, browser: "", kind: "android", type: typeNames[TYPE_KEY.android], byQr: true });
                  onPaired();
                }}
              />
            )}
            <form
              className="flex flex-col gap-4"
              onSubmit={(e) => {
                e.preventDefault();
                void pair();
              }}
            >
              <label className="flex flex-col gap-1 text-sm">
                <span className="font-medium">{t("settings.browsers.pairDialog.code")}</span>
                <input
                  className={cn(inputBase, "font-mono uppercase tracking-widest")}
                  aria-label={t("settings.browsers.pairDialog.code")}
                  autoFocus
                  autoComplete="off"
                  spellCheck={false}
                  value={code}
                  maxLength={20}
                  placeholder="XXXX-XXXX"
                  onChange={(e) => setCode(e.target.value)}
                />
              </label>
              {taken?.reconnect && (
                <div role="note" className="flex flex-col gap-2 rounded-md border border-stone-200 p-3 text-sm dark:border-stone-700" data-track-private>
                  <p>{t(`${area}.takenReconnect`, { ...taken.agent, type: typeNames[TYPE_KEY[kind]] ?? t(`${area}.typeFallback`) })}</p>
                  <button type="button" className={cn(btnPrimary, "self-start")} disabled={busy} onClick={() => void pair(true)} data-track={`Reconnect ${TRACK[kind]}`}>
                    {t("settings.browsers.pairDialog.reconnect", taken.agent)}
                  </button>
                </div>
              )}
              {asking && (
                <label className="flex flex-col gap-1 text-sm">
                  <span className="font-medium">{t("settings.browsers.pairDialog.otherName")}</span>
                  <input className={inputBase} aria-label={t("settings.browsers.pairDialog.otherName")} autoComplete="off" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} data-track-private />
                  <span className={cn(muted, "text-[11px]")}>{t("settings.browsers.pairDialog.otherNameHint")}</span>
                </label>
              )}
              {problem && <div className={cn(errorBanner, "mb-0 text-xs")}>{problem}</div>}
              <div className="flex justify-end gap-2">
                <button type="button" className={btn} onClick={onClose}>
                  {t("settings.browsers.pairDialog.cancel")}
                </button>
                <button type="submit" className={btnPrimary} disabled={busy} data-track={`Pair ${TRACK[kind]}`}>
                  {t("settings.browsers.pairDialog.pair")}
                </button>
              </div>
            </form>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
