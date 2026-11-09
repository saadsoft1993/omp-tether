// The guest authentication adapter interface.
//
// An adapter answers one question about a request that arrived on the guest listener: does it come from the place,
// and the person, this adapter is built to admit (the identity layer)? Whether the *browser* is paired is decided by
// the relay itself, from the pairing store, for every adapter alike. Adapters are swappable: the relay talks to them
// only through this interface and never looks at which one it has.
import type { AuthAdapterName } from "../config";

export type Verdict = { ok: true } | { ok: false; status: number; reason: string };

export interface RequestFacts {
	headers: Headers;
	/** True for a WebSocket upgrade request. */
	upgrade: boolean;
}

export interface AdapterHooks {
	log(event: string, fields?: Record<string, unknown>): void;
	/** Close every open guest: the identity they were admitted under no longer holds. */
	dropGuests(reason: string): void;
}

export interface GuestAuth {
	readonly name: AuthAdapterName;
	/** Starts background work (identity refresh). Returns the function that stops it. */
	start(hooks: AdapterHooks): { stop(): void };
	/** False while the adapter cannot decide yet; the relay then answers 503 to every guest request. */
	ready(): boolean;
	/** Resolves when the first identity lookup has finished (immediately for adapters that have none). */
	settled(): Promise<void>;
	/** Identity layer only: Host, Origin, owner, proxy headers. Never looks at cookies. */
	check(req: RequestFacts): Verdict;
	/** Exact `Origin` values a page served by this relay sends. */
	origins(): string[];
	/** `ws:`/`wss:` origins the page may connect to, for the Content-Security-Policy. */
	connectSources(): string[];
	/** Whether the pairing cookie is set with the Secure attribute (it is on https, and cannot be on loopback http). */
	secureCookies(): boolean;
	/** Where, in the user's own words, to run the pairing step; shown on the "browser not paired" page. */
	pairingLink(token: string): string | null;
}
