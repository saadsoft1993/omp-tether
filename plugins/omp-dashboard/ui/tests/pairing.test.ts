import { afterEach, describe, expect, it } from "bun:test";
import { redeemPairingLink, takePairingError } from "../src/data/pairing";

const saved = new Map<string, PropertyDescriptor | undefined>();
function install(name: string, value: unknown): void {
	saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
	Object.defineProperty(globalThis, name, { configurable: true, value, writable: true });
}
afterEach(() => {
	for (const [name, descriptor] of saved) {
		if (descriptor) Object.defineProperty(globalThis, name, descriptor);
		else Reflect.deleteProperty(globalThis, name);
	}
	saved.clear();
});

function browser(hash: string, answer: () => Response) {
	const state = { hash, replaced: [] as string[], reloads: 0, posts: [] as { url: string; init?: RequestInit }[], stored: new Map<string, string>() };
	install("window", {
		location: {
			pathname: "/",
			get hash() {
				return state.hash;
			},
			reload: () => {
				state.reloads += 1;
			},
		},
	});
	install("history", { replaceState: (_s: unknown, _t: string, url: string) => state.replaced.push(url) });
	install("sessionStorage", {
		getItem: (k: string) => state.stored.get(k) ?? null,
		setItem: (k: string, v: string) => void state.stored.set(k, v),
		removeItem: (k: string) => void state.stored.delete(k),
	});
	install("fetch", async (url: string, init?: RequestInit) => {
		state.posts.push({ url, init });
		return answer();
	});
	return state;
}

describe("pairing link", () => {
	it("does nothing when the address holds no token", () => {
		const s = browser("#/sessions", () => Response.json({ ok: true }));
		expect(redeemPairingLink()).toBe(false);
		expect(s.posts).toHaveLength(0);
		for (const odd of ["#pair=", "#pair=a b", "#pair=a/b", "#/pair=abc", "#pair=abc&x=1"]) {
			expect(browser(odd, () => Response.json({})) && redeemPairingLink()).toBe(false);
		}
	});

	it("takes the token out of the address bar, posts it with the custom header, and reloads", async () => {
		const s = browser("#pair=Abc_123-xyz", () => Response.json({ ok: true }));
		expect(redeemPairingLink()).toBe(true);
		expect(s.replaced).toEqual(["/#/sessions"]);
		await Bun.sleep(5);
		expect(s.posts).toHaveLength(1);
		expect(s.posts[0].url).toBe("/api/hub/pair");
		expect((s.posts[0].init?.headers as Record<string, string>)["X-Omp-Dashboard"]).toBe("1");
		expect(JSON.parse(s.posts[0].init?.body as string)).toEqual({ token: "Abc_123-xyz" });
		expect(s.reloads).toBe(1);
		expect(takePairingError()).toBeNull();
	});

	it("keeps the hub's refusal for the Pair screen shown after the reload, once", async () => {
		const s = browser("#pair=used-up", () => Response.json({ error: "the pairing link was already used or expired" }, { status: 403 }));
		expect(redeemPairingLink()).toBe(true);
		await Bun.sleep(5);
		expect(s.reloads).toBe(1);
		expect(takePairingError()).toBe("the pairing link was already used or expired");
		expect(takePairingError()).toBeNull();
	});
});
