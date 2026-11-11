// `tailscale-serve`: the guest listener sits behind `tailscale serve`, which terminates TLS for the machine's MagicDNS
// name and tells us who is calling in the `Tailscale-User-Login` header. Every request must come from the owner of this
// machine, for this machine's name, with the matching Origin, and never through Funnel (the public internet).
//
// The owner and the name come from `tailscale status --json` (or from the configuration) and are refreshed every
// minute; while they are unknown every guest request is answered 503 (fail closed), and when they change the guests
// that were admitted under the old identity are dropped.
import { findTailscale } from "../runtime";
import type { AdapterHooks, GuestAuth, RequestFacts, Verdict } from "./types";

export interface TailscaleSelf {
	host: string;
	login: string;
}

export interface TailscaleOptions {
	guestPort: number;
	servePort: number | null;
	/** Fixed values from the configuration; when both are set, tailscale is never asked. */
	owner: string;
	publicHost: string;
	/** Runs `tailscale status --json` and returns this machine's name and owner login, or null. */
	lookup(): Promise<TailscaleSelf | null>;
	now?: () => number;
}

const RETRY_WHILE_UNKNOWN_MS = 30_000;
const REFRESH_MS = 60_000;

/** Parses `tailscale status --json`; the shape it relies on is `Self.DNSName`, `Self.UserID` and `User[<id>].LoginName`. */
export function parseTailscaleStatus(json: string): TailscaleSelf | null {
	try {
		const status = JSON.parse(json);
		const self = status.Self;
		const host = String(self.DNSName ?? "").replace(/\.$/, "").toLowerCase();
		const login = status.User?.[String(self.UserID)]?.LoginName;
		return host && typeof login === "string" && login ? { host, login } : null;
	} catch {
		return null;
	}
}

/** Runs `tailscale status --json` without blocking the event loop (the relay keeps serving while it runs). */
export function lookupFromCommand(bin: string): () => Promise<TailscaleSelf | null> {
	return async () => {
		try {
			const proc = Bun.spawn([findTailscale(bin) ?? bin, "status", "--json"], { stdout: "pipe", stderr: "ignore", stdin: "ignore", timeout: 5_000 });
			const [text, code] = await Promise.all([new Response(proc.stdout).text(), proc.exited]);
			return code === 0 ? parseTailscaleStatus(text) : null;
		} catch {
			return null;
		}
	};
}

export function createTailscaleAuth(opts: TailscaleOptions): GuestAuth {
	const now = opts.now ?? Date.now;
	const fixed = Boolean(opts.owner && opts.publicHost);
	const identity = { owner: opts.owner, host: opts.publicHost.toLowerCase(), checkedAt: 0 };
	let hooks: AdapterHooks | null = null;
	const servePort = opts.servePort ?? opts.guestPort;

	let inflight: Promise<void> | null = null;
	const refresh = (): Promise<void> => {
		if (fixed) return Promise.resolve();
		// One lookup at a time; a request that finds the identity unknown starts one and is answered 503 without waiting.
		inflight ??= (async () => {
			identity.checkedAt = now();
			const self = await opts.lookup();
			if (!self) return; // Tailscale briefly down: keep what we had
			if (self.login === identity.owner && self.host === identity.host) return;
			const changed = Boolean(identity.owner);
			identity.owner = self.login;
			identity.host = self.host;
			hooks?.log(changed ? "identity_changed" : "identity", { publicHostKnown: true, ownerConfigured: true });
			if (changed) hooks?.dropGuests("tailnet identity changed");
		})().finally(() => {
			inflight = null;
		});
		return inflight;
	};
	let initial: Promise<void> = Promise.resolve();
	const known = (): boolean => Boolean(identity.owner && identity.host);
	const origin = (): string => `https://${identity.host}${servePort === 443 ? "" : `:${servePort}`}`;

	return {
		name: "tailscale-serve",
		start(h: AdapterHooks) {
			hooks = h;
			initial = refresh();
			const timer = setInterval(() => void refresh(), REFRESH_MS);
			return {
				stop() {
					clearInterval(timer);
					hooks = null;
				},
			};
		},
		ready: known,
		settled: () => initial,
		check({ headers, upgrade }: RequestFacts): Verdict {
			if (!known() && now() - identity.checkedAt >= RETRY_WHILE_UNKNOWN_MS) void refresh();
			if (!known()) return { ok: false, status: 503, reason: "relay has no owner configured (is Tailscale up?)" };
			if (headers.has("tailscale-funnel-request")) return { ok: false, status: 403, reason: "funnel" };
			if (headers.get("tailscale-user-login") !== identity.owner) return { ok: false, status: 403, reason: "not the owner's tailnet login" };
			const hostName = (headers.get("host") ?? "").toLowerCase().replace(/:\d+$/, "");
			if (hostName !== identity.host) return { ok: false, status: 403, reason: "wrong host" };
			const got = headers.get("origin");
			if (upgrade ? got !== origin() : got !== null && got !== origin()) return { ok: false, status: 403, reason: "wrong origin" };
			if (headers.get("sec-fetch-site") === "cross-site") return { ok: false, status: 403, reason: "cross-site request" };
			return { ok: true };
		},
		origins: () => (known() ? [origin()] : []),
		connectSources: () => (known() ? [`wss://${origin().slice("https://".length)}`] : []),
		secureCookies: () => true,
		pairingLink: token => (known() ? `${origin()}/pair#token=${token}` : null),
	};
}
