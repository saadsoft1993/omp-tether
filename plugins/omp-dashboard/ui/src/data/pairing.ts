/**
 * Pairing with the hub that fronts this page. The hub answers 401 to every API call from a
 * browser it has not paired; the first such answer switches the app to the Pair screen.
 */
import { useSyncExternalStore } from "react";

let unpaired = false;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

/** Called on any 401 from the hub. */
export function markUnpaired(): void {
	if (unpaired) return;
	unpaired = true;
	for (const listener of listeners) listener();
}

export function useUnpaired(): boolean {
	return useSyncExternalStore(subscribe, () => unpaired);
}

/** Message of a failed pairing attempt, shown on the Pair screen after the reload. */
const PAIR_ERROR_KEY = "omp-dashboard-pair-error";

export function takePairingError(): string | null {
	try {
		const message = sessionStorage.getItem(PAIR_ERROR_KEY);
		sessionStorage.removeItem(PAIR_ERROR_KEY);
		return message;
	} catch {
		return null;
	}
}

const PAIR_HASH = /^#pair=([A-Za-z0-9_-]+)$/;

/**
 * The pairing link is `<dashboard address>/#pair=<single-use token>`. Trade the token for the hub's HttpOnly cookie, drop
 * it from the address bar, and reload so every page starts over with the cookie. The token is in the fragment, which a
 * browser never sends to a server. Returns false when the location holds no token.
 */
export function redeemPairingLink(): boolean {
	const match = PAIR_HASH.exec(window.location.hash);
	if (!match) return false;
	history.replaceState(null, "", `${window.location.pathname}#/sessions`);
	fetch("/api/hub/pair", {
		method: "POST",
		headers: { "Content-Type": "application/json", "X-Omp-Dashboard": "1" },
		body: JSON.stringify({ token: match[1] }),
	})
		.then(async res => {
			if (res.ok) return;
			const body = (await res.json().catch(() => null)) as { error?: unknown } | null;
			throw new Error(typeof body?.error === "string" ? body.error : `HTTP error ${res.status}`);
		})
		.catch((error: unknown) => {
			try {
				sessionStorage.setItem(PAIR_ERROR_KEY, error instanceof Error ? error.message : String(error));
			} catch {}
		})
		.finally(() => window.location.reload());
	return true;
}
