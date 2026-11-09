// The two omp-internal modules the extension reads optionally (plugin settings, the disabledExtensions setting) are
// replaced by stand-ins. mock.module is process-wide, so `make test-unit` runs every unit file in its own process.
import { afterAll, describe, expect, mock, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { EXTENSION_ID } from "../../src/omp-session-control";
import { FakeHost } from "./support/fake-host";

let stored: Record<string, unknown> = {};
let storedCalls: unknown[][] = [];
let storedFails = false;
const listeners: Array<() => void> = [];
let disabled: string[] = [];
let lookupMode: "ok" | "throws" | "missing" | "listen_throws" = "ok";
let unsubscribed = 0;

mock.module("@oh-my-pi/pi-coding-agent/extensibility/plugins/loader", () => ({
	getPluginSettings: async (...args: unknown[]) => {
		storedCalls.push(args);
		if (storedFails) throw new Error("injected loader failure");
		return stored;
	},
}));
mock.module("@oh-my-pi/pi-coding-agent/config/registry", () => ({
	// `missing` models omp renaming the function: the module still imports, the export is gone.
	lookup:
		lookupMode === "missing"
			? undefined
			: (id: string) => {
					if (lookupMode === "throws") throw new Error("injected lookup failure");
					return id === "disabledExtensions"
						? {
								get: () => disabled,
								listen: (_settings: unknown, fn: () => void) => {
									if (lookupMode === "listen_throws") throw new Error("injected listen failure");
									listeners.push(fn);
									return () => void unsubscribed++;
								},
							}
						: undefined;
				},
}));

const hosts: FakeHost[] = [];
afterAll(async () => {
	for (const h of hosts) await h.dispose();
});
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

describe("plugin settings read through omp", () => {
	test("the extension asks omp for its own package's settings and uses the stored directory when no env is set", async () => {
		const dir = path.join(fs.mkdtempSync("/tmp/oscS-"), "stored");
		fs.mkdirSync(dir, { mode: 0o700 });
		fs.writeFileSync(path.join(dir, "enabled"), "");
		storedCalls = [];
		stored = { controlDir: dir, idleTimeoutMs: 700 };
		const h2 = await FakeHost.start({ env: { OMP_SESSION_CONTROL_DIR: undefined } });
		hosts.push(h2);
		await h2.fire("session_start");
		await h2.waitFor(() => fs.readdirSync(dir).some(f => f.endsWith(".sock")), "a socket in the stored directory");
		expect(storedCalls[0]?.[0]).toBe("omp-session-control");
		expect(storedCalls[0]?.[1]).toBe(process.env.HOME); // the home directory, so that no repository can override the setting
		expect(await h2.run("session-control", "status")).toContain(dir);
		fs.rmSync(path.dirname(dir), { recursive: true, force: true });
	});

	test("the environment overrides the stored directory", async () => {
		stored = { controlDir: "/tmp/never-used-by-this-test" };
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry in the env directory");
		expect(fs.existsSync("/tmp/never-used-by-this-test")).toBe(false);
	});

	test("a failing settings loader falls back to defaults and the extension still loads", async () => {
		storedFails = true;
		stored = {};
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		storedFails = false;
	});
});

describe("disabledExtensions", () => {
	test("adding the extension's own id closes control at once, removing it reopens", async () => {
		disabled = [];
		listeners.length = 0;
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		expect(EXTENSION_ID).toBe("extension-module:omp-session-control");

		disabled = ["extension-module:something-else"];
		for (const fn of listeners) fn();
		await sleep(100);
		expect(h.registry()).toBeDefined(); // another extension's id does not matter

		disabled = [EXTENSION_ID];
		for (const fn of listeners) fn();
		await h.waitFor(() => h.registry() === undefined, "the registry to go");
		expect(await h.run("session-control", "status")).toContain("disabledExtensions");

		disabled = [];
		for (const fn of listeners) fn();
		await h.waitFor(() => h.registry(), "the registry to come back");
	});

	test("a session that starts with the id already disabled never listens", async () => {
		disabled = [EXTENSION_ID];
		listeners.length = 0;
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		await h.fire("session_start");
		await sleep(150);
		expect(h.registry()).toBeUndefined();
		disabled = [];
	});
});

describe("optional internals really are optional", () => {
	// The mock factory above reads lookupMode when the module is first imported, so each mode re-registers it.
	const remock = (mode: typeof lookupMode) => {
		lookupMode = mode;
		mock.module("@oh-my-pi/pi-coding-agent/config/registry", () => ({
			lookup:
				mode === "missing"
					? undefined
					: (id: string) => {
							if (mode === "throws") throw new Error("injected lookup failure");
							return id === "disabledExtensions"
								? {
										get: () => disabled,
										listen: () => {
											if (mode === "listen_throws") throw new Error("injected listen failure");
											return () => void unsubscribed++;
										},
									}
								: undefined;
						},
		}));
	};

	for (const mode of ["throws", "missing", "listen_throws"] as const) {
		test(`control still works when the settings registry ${mode === "missing" ? "has no lookup" : mode === "throws" ? "lookup throws" : "listen throws"}`, async () => {
			remock(mode);
			const h = await FakeHost.start({ enable: true });
			hosts.push(h);
			await h.fire("session_start");
			const reg = await h.waitFor(() => h.registry(), "the registry");
			expect(reg.data.pid).toBe(process.pid);
			remock("ok");
		});
	}

	test("the settings listener is removed when the session shuts down", async () => {
		remock("ok");
		unsubscribed = 0;
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		await h.fire("session_shutdown");
		expect(unsubscribed).toBe(1);
	});
});
