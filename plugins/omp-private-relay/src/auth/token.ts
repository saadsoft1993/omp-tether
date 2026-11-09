// `token`: the standalone adapter. The guest listener is reached directly on loopback (or through an SSH port forward
// to it). There is no second identity: the proof is the pairing cookie, and the Host and Origin checks keep a web page
// from reaching the listener through DNS rebinding or from another site.
import type { AdapterHooks, GuestAuth, RequestFacts, Verdict } from "./types";

/** Headers a reverse proxy adds. Their presence means the listener was exposed through something we do not vouch for. */
const PROXY_HEADERS = ["tailscale-funnel-request", "tailscale-user-login", "x-forwarded-for", "x-forwarded-host", "forwarded"];

export interface TokenOptions {
	guestPort: number;
	/** Extra lowercase host:port values from the configuration. */
	allowedHosts: string[];
}

export function createTokenAuth(opts: TokenOptions): GuestAuth {
	const hosts = new Set([`127.0.0.1:${opts.guestPort}`, `localhost:${opts.guestPort}`, ...opts.allowedHosts]);
	const origins = [...hosts].map(h => `http://${h}`);
	return {
		name: "token",
		start(_hooks: AdapterHooks) {
			return { stop() {} };
		},
		ready: () => true,
		settled: () => Promise.resolve(),
		check({ headers, upgrade }: RequestFacts): Verdict {
			for (const name of PROXY_HEADERS) {
				if (headers.has(name)) return { ok: false, status: 403, reason: "proxied request" };
			}
			if (!hosts.has((headers.get("host") ?? "").toLowerCase())) return { ok: false, status: 403, reason: "wrong host" };
			const origin = headers.get("origin");
			if (upgrade ? origin === null || !origins.includes(origin) : origin !== null && !origins.includes(origin)) {
				return { ok: false, status: 403, reason: "wrong origin" };
			}
			if (headers.get("sec-fetch-site") === "cross-site") return { ok: false, status: 403, reason: "cross-site request" };
			return { ok: true };
		},
		origins: () => origins,
		connectSources: () => [...hosts].map(h => `ws://${h}`),
		secureCookies: () => false,
		pairingLink: token => `http://127.0.0.1:${opts.guestPort}/pair#token=${token}`,
	};
}
