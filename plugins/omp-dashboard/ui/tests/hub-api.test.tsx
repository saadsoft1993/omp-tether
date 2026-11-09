import { afterEach, describe, expect, it, vi } from "bun:test";
import { parseHTML } from "linkedom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import {
	forceKillHubSession,
	getHubLink,
	getHubSessions,
	killHubSession,
	renameHubSession,
	startHubSession,
	stopHubSession,
} from "../src/data/hub";
import { useUnpaired } from "../src/data/pairing";

const realFetch = globalThis.fetch;
afterEach(() => {
	globalThis.fetch = realFetch;
	vi.restoreAllMocks();
});

function recordFetch(answer: () => Response): { calls: { url: string; init?: RequestInit }[] } {
	const calls: { url: string; init?: RequestInit }[] = [];
	globalThis.fetch = (async (url: string, init?: RequestInit) => {
		calls.push({ url, init });
		return answer();
	}) as unknown as typeof fetch;
	return { calls };
}

describe("hub requests", () => {
	it("every POST carries the custom header that forces a preflight, and a JSON body", async () => {
		const { calls } = recordFetch(() => Response.json({ outcome: "stopped", url: "http://x", instanceId: null }));
		await getHubLink("inst0001", true);
		await startHubSession("/work/a");
		await stopHubSession("s-1");
		await renameHubSession("s-1", "new name");
		await killHubSession("s-1", "name");
		await forceKillHubSession("s-1", "name");
		expect(calls.map(c => c.url)).toEqual([
			"/api/hub/link",
			"/api/hub/start",
			"/api/hub/sessions/s-1/stop",
			"/api/hub/sessions/s-1/rename",
			"/api/hub/sessions/s-1/kill",
			"/api/hub/sessions/s-1/force-kill",
		]);
		for (const c of calls) {
			expect(c.init?.method).toBe("POST");
			expect((c.init?.headers as Record<string, string>)["X-Omp-Dashboard"]).toBe("1");
			expect((c.init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
		}
		expect(JSON.parse(calls[0].init?.body as string)).toEqual({ instanceId: "inst0001", view: true });
		expect(JSON.parse(calls[3].init?.body as string)).toEqual({ name: "new name" });
	});

	it("encodes a session id into the path, so it cannot add path segments", async () => {
		const { calls } = recordFetch(() => Response.json({}));
		await stopHubSession("a/../b");
		expect(calls[0].url).toBe("/api/hub/sessions/a%2F..%2Fb/stop");
	});

	it("shows the hub's own error text for a failed call", async () => {
		recordFetch(() => Response.json({ error: "could not list sessions: omp did not answer in time" }, { status: 503 }));
		await expect(getHubSessions()).rejects.toThrow("could not list sessions: omp did not answer in time");
		recordFetch(() => new Response("<html>", { status: 502 }));
		await expect(getHubSessions()).rejects.toThrow("HTTP error 502");
	});

	it("switches the app to the Pair screen on a 401", async () => {
		const seen: boolean[] = [];
		const domWindow = parseHTML('<html><body><div id="root"></div></body></html>').window;
		for (const [name, value] of Object.entries({ window: domWindow, document: domWindow.document, Node: domWindow.Node, Element: domWindow.Element, HTMLElement: domWindow.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
			Object.defineProperty(globalThis, name, { configurable: true, value, writable: true });
		}
		function Probe() {
			seen.push(useUnpaired());
			return null;
		}
		const root = createRoot(domWindow.document.getElementById("root") as unknown as Element);
		await act(async () => root.render(<Probe />));
		expect(seen.at(-1)).toBe(false);
		recordFetch(() => Response.json({ error: "not paired" }, { status: 401 }));
		await act(async () => {
			await getHubSessions().catch(() => undefined);
		});
		expect(seen.at(-1)).toBe(true);
		await act(async () => root.unmount());
	});
});
