// A user can create the marker (or replace the directory, or remove the off file) at the very moment the extension arms
// its directory watch. The watch may not be live yet and the event is then lost; the extension must still notice, by
// looking again after it arms a watcher (watch-then-recheck) and by a bounded periodic recheck that does not depend on
// events at all. These tests hammer that window, and then remove the watcher altogether.
import { afterEach, describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { FakeHost } from "./support/fake-host";

const hosts: FakeHost[] = [];
afterEach(async () => {
	for (const h of hosts.splice(0)) await h.dispose();
});
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const LOOPS = Number(process.env.WATCH_RACE_LOOPS ?? 200);
/** Seconds the extension gets to notice; its periodic recheck (2 s unscaled) is scaled by 0.1 in these tests. */
const NOTICE_MS = 3000;

async function host(opts: { enable?: boolean } = {}): Promise<FakeHost> {
	const h = await FakeHost.start({ timerScale: 0.1, ...opts });
	hosts.push(h);
	return h;
}
async function drop(h: FakeHost): Promise<void> {
	hosts.splice(hosts.indexOf(h), 1);
	await h.dispose();
}

describe("the marker is noticed however close to the watch it appears", () => {
	test(`${LOOPS} sessions: marker created in the same tick as the start, and straight after it`, async () => {
		let missed = 0;
		for (let i = 0; i < LOOPS; i++) {
			const h = await host();
			fs.mkdirSync(h.dir, { mode: 0o700 });
			const startPromise = h.fire("session_start"); // arms the watch synchronously
			if (i % 2 === 0) fs.writeFileSync(path.join(h.dir, "enabled"), ""); // same tick as the arming
			await startPromise;
			if (i % 2 === 1) fs.writeFileSync(path.join(h.dir, "enabled"), ""); // immediately after it
			try {
				await h.waitFor(() => h.registry(), "the registry", NOTICE_MS);
			} catch {
				missed++;
			}
			await drop(h);
		}
		console.log(`marker race: ${missed} of ${LOOPS} missed`);
		expect(missed).toBe(0);
	}, 600_000);

	test("the same, but the directory is created a moment after the session starts (parent already watched)", async () => {
		let missed = 0;
		for (let i = 0; i < Math.min(LOOPS, 60); i++) {
			const h = await host({ enable: true });
			await h.fire("session_start");
			await h.waitFor(() => h.registry(), "the registry");
			fs.rmSync(h.dir, { recursive: true, force: true });
			await sleep(i % 3 === 0 ? 0 : 30); // sometimes before the extension has looked, sometimes after
			fs.mkdirSync(h.dir, { mode: 0o700 });
			fs.writeFileSync(path.join(h.dir, "enabled"), "");
			try {
				await h.waitFor(() => h.registry(), "the registry after the directory returned", NOTICE_MS);
			} catch {
				missed++;
			}
			await drop(h);
		}
		console.log(`directory race: ${missed} missed`);
		expect(missed).toBe(0);
	}, 600_000);
});

/** Replaces fs.watch with a watcher that never reports anything: every event is lost. */
function loseEveryWatchEvent() {
	return spyOn(fs, "watch").mockImplementation((() =>
		({ close: () => undefined, on: () => undefined, ref: () => undefined, unref: () => undefined }) as unknown as fs.FSWatcher) as unknown as typeof fs.watch);
}

describe("with every watch event lost, the periodic recheck still gets there", () => {
	test("the marker appears", async () => {
		const spy = loseEveryWatchEvent();
		try {
			const h = await host();
			fs.mkdirSync(h.dir, { mode: 0o700 });
			await h.fire("session_start");
			expect(h.registry()).toBeUndefined();
			fs.writeFileSync(path.join(h.dir, "enabled"), "");
			await h.waitFor(() => h.registry(), "the registry through the periodic recheck", NOTICE_MS);
		} finally {
			spy.mockRestore();
		}
	});

	test("the off file is created and removed", async () => {
		const spy = loseEveryWatchEvent();
		try {
			const h = await host({ enable: true });
			await h.fire("session_start");
			await h.waitFor(() => h.registry(), "the registry");
			fs.writeFileSync(path.join(h.dir, "off"), "");
			await h.waitFor(() => h.registry() === undefined, "control to close", NOTICE_MS);
			fs.rmSync(path.join(h.dir, "off"));
			await h.waitFor(() => h.registry(), "control to come back", NOTICE_MS);
		} finally {
			spy.mockRestore();
		}
	});

	test("the directory is replaced, and then removed and put back", async () => {
		const spy = loseEveryWatchEvent();
		try {
			const h = await host({ enable: true });
			await h.fire("session_start");
			const first = await h.waitFor(() => h.registry(), "the registry");
			fs.rmSync(h.dir, { recursive: true, force: true });
			fs.mkdirSync(h.dir, { mode: 0o700 });
			fs.writeFileSync(path.join(h.dir, "enabled"), "");
			const second = await h.waitFor(
				() => {
					const r = h.registry();
					return r && r.data.nonce !== first.data.nonce ? r : undefined;
				},
				"a new listener in the replaced directory",
				NOTICE_MS,
			);
			fs.rmSync(h.dir, { recursive: true, force: true });
			await h.waitFor(() => !fs.existsSync(h.dir), "the directory to be gone");
			await sleep(600); // the extension has noticed that it is gone
			fs.mkdirSync(h.dir, { mode: 0o700 });
			fs.writeFileSync(path.join(h.dir, "enabled"), "");
			await h.waitFor(
				() => {
					const r = h.registry();
					return r && r.data.nonce !== second.data.nonce ? r : undefined;
				},
				"control after the directory came back",
				NOTICE_MS,
			);
		} finally {
			spy.mockRestore();
		}
	}, 30_000);
});

describe("the periodic recheck is bounded and clean", () => {
	test("it runs only where there is something to wait for, and stops at shutdown", async () => {
		// no control directory and never active: nothing is scheduled
		const none = await host();
		await none.fire("session_start");
		expect(none.pendingTimers()).toBe(0);
		// a directory without the marker: one recheck timer, never more
		const waiting = await host();
		fs.mkdirSync(waiting.dir, { mode: 0o700 });
		await waiting.fire("session_start");
		await sleep(500);
		expect(waiting.pendingTimers()).toBeLessThanOrEqual(1);
		await waiting.fire("session_shutdown");
		expect(waiting.pendingTimers()).toBe(0);
		// enabled and listening: the idle timers of clients are separate; the recheck is still a single timer
		const live = await host({ enable: true });
		await live.fire("session_start");
		await live.waitFor(() => live.registry(), "the registry");
		await sleep(500);
		expect(live.pendingTimers()).toBe(1);
		await live.fire("session_shutdown");
		expect(live.pendingTimers()).toBe(0);
	});

	test("many reconciliations never stack timers", async () => {
		const h = await host({ enable: true });
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		for (let i = 0; i < 200; i++) await h.fire("session_switch");
		expect(h.pendingTimers()).toBe(1);
	});
});
