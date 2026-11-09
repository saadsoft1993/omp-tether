// Regression tests for the second independent review. Each test failed on the code it reviewed.
import { afterEach, describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { Client } from "./support/client";
import { FakeHost, type HostOptions } from "./support/fake-host";

const hosts: FakeHost[] = [];
async function start(opts: HostOptions = {}): Promise<FakeHost> {
	const h = await FakeHost.start({ enable: true, ...opts });
	hosts.push(h);
	await h.fire("session_start");
	return h;
}
afterEach(async () => {
	for (const h of hosts.splice(0)) await h.dispose();
});
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const rename = (nonce: string, sessionId: string, generation: number, name: string, id: string | number = 1) => ({
	v: 1,
	id,
	nonce,
	op: "rename",
	sessionId,
	generation,
	args: { name },
});

describe("review 2, finding 4: serialization lasts until the host operation settles", () => {
	test("a timed-out rename that finishes late cannot overwrite a later rename: the later one is refused until it settles", async () => {
		const h = await start({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } });
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		const setName = h.pi.setSessionName as (n: string) => Promise<void>;
		h.pi.setSessionName = async (n: string) => {
			await sleep(600); // longer than the (scaled) 100 ms deadline, then it finishes
			await setName(n);
		};
		expect((await client.request(rename(nonce, sessionId, generation, "A", "a"))).code).toBe("timeout");
		const b = await client.request(rename(nonce, sessionId, generation, "B", "b"));
		expect(b.code).toBe("mutation_pending");
		expect((await client.request({ v: 1, id: "s", nonce, op: "snapshot" })).ok).toBe(true); // reads keep working
		await sleep(700);
		expect(h.name).toBe("A");
		h.pi.setSessionName = setName;
		const c = await client.request(rename(nonce, sessionId, generation, "C", "c"));
		expect(c.ok).toBe(true);
		expect(h.name).toBe("C");
		client.close();
	});

	test("an off and on cycle does not let the new listener run a mutation alongside the old one", async () => {
		const h = await start({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } });
		const first = await Client.toHost(h);
		const setName = h.pi.setSessionName as (n: string) => Promise<void>;
		let release: () => void = () => {};
		h.pi.setSessionName = (n: string) =>
			new Promise<void>(resolve => {
				release = () => void setName(n).then(() => resolve());
			});
		expect((await first.client.request(rename(first.nonce, first.sessionId, first.generation, "old"))).code).toBe("timeout");
		fs.writeFileSync(path.join(h.dir, "off"), "");
		await h.waitFor(() => h.registry() === undefined, "control to close");
		fs.rmSync(path.join(h.dir, "off"));
		const again = await Client.toHost(h);
		const r = await again.client.request(rename(again.nonce, again.sessionId, again.generation, "new"));
		expect(r.code).toBe("mutation_pending");
		release();
		h.pi.setSessionName = setName;
		await sleep(100);
		const r2 = await again.client.request(rename(again.nonce, again.sessionId, again.generation, "new2"));
		expect(r2.ok).toBe(true);
		first.client.close();
		again.client.close();
	});
});

describe("review 2, finding 5: special files in the control directory never block the session", () => {
	const runChild = async (mode: string) => {
		const child = Bun.spawn([process.execPath, path.join(import.meta.dir, "support/fifo-child.ts"), mode], { stdout: "pipe", stderr: "pipe" });
		const timer = setTimeout(() => child.kill(), 12_000);
		const [out, , code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
		clearTimeout(timer);
		return { out, code };
	};

	test("a FIFO named events.log does not hang logging", async () => {
		const { out, code } = await runChild("log");
		expect({ code, done: out.includes("done") }).toEqual({ code: 0, done: true });
	}, 20_000);

	test("a FIFO named enabled does not hang /session-control on", async () => {
		const { out, code } = await runChild("enabled");
		expect({ code, done: out.includes("done") }).toEqual({ code: 0, done: true });
		expect(out).toContain("not a regular file");
	}, 20_000);

	test("a FIFO named off does not hang /session-control off", async () => {
		const { out, code } = await runChild("off");
		expect({ code, done: out.includes("done") }).toEqual({ code: 0, done: true });
	}, 20_000);

	test("an existing events.log with a loose mode is tightened to 0600", async () => {
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		fs.writeFileSync(path.join(h.dir, "events.log"), "old\n", { mode: 0o644 });
		fs.chmodSync(path.join(h.dir, "events.log"), 0o644);
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		expect(fs.statSync(path.join(h.dir, "events.log")).mode & 0o777).toBe(0o600);
	});
});

describe("review 2, finding 6: the log holds categories, not arbitrary exception text", () => {
	test("exception text, including a proposed name, never reaches events.log by default", async () => {
		const h = await start();
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		h.pi.setSessionName = async () => {
			throw new Error("rename to PROPOSED-NAME-9f3 failed with token sk-secret-123");
		};
		expect((await client.request(rename(nonce, sessionId, generation, "x"))).code).toBe("internal");
		const log = fs.readFileSync(path.join(h.dir, "events.log"), "utf8");
		expect(log).not.toContain("PROPOSED-NAME-9f3");
		expect(log).not.toContain("sk-secret-123");
		expect(log).toContain('"kind":"op_error"');
		client.close();
	});

	test("with the debug setting on, the text is logged, bounded to 200 characters", async () => {
		const h = await start({ env: { OMP_SESSION_CONTROL_DEBUG: "1" } });
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		h.pi.setSessionName = async () => {
			throw new Error(`DIAG-${"z".repeat(5000)}`);
		};
		await client.request(rename(nonce, sessionId, generation, "x"));
		const log = fs.readFileSync(path.join(h.dir, "events.log"), "utf8");
		expect(log).toContain("DIAG-");
		expect(log.split("\n").every(line => line.length < 1000)).toBe(true);
		client.close();
	});

	test("one oversized record cannot push the log far past its cap", async () => {
		const h = await start({ env: { OMP_SESSION_CONTROL_DEBUG: "1" } });
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		h.pi.setSessionName = async () => {
			throw new Error("q".repeat(2_000_000));
		};
		for (let i = 0; i < 40; i++) await client.request(rename(nonce, sessionId, generation, "x", i));
		expect(fs.statSync(path.join(h.dir, "events.log")).size).toBeLessThan(256 * 1024 + 4096);
		client.close();
	});
});

describe("review 2, finding 8: recovery after the directory was seen missing", () => {
	test("removing the directory, letting control close, then recreating it brings control back", async () => {
		const h = await start();
		const old = await h.waitFor(() => h.registry(), "the registry");
		fs.rmSync(h.dir, { recursive: true, force: true });
		await sleep(500); // reconciliation has run and found the directory missing
		fs.mkdirSync(h.dir, { mode: 0o700 });
		fs.writeFileSync(path.join(h.dir, "enabled"), "");
		const fresh = await h.waitFor(() => {
			const r = h.registry();
			return r && r.data.nonce !== old.data.nonce ? r : undefined;
		}, "control to come back after the directory returned");
		expect(fresh.data.pid).toBe(process.pid);
	});
});

describe("review 2, finding 9: publishing the registry is part of starting", () => {
	test("a registry that cannot be published closes the listener, leaves no temporary file, and reports it", async () => {
		const real = fs.renameSync;
		const spy = spyOn(fs, "renameSync").mockImplementation(((from: fs.PathLike, to: fs.PathLike) => {
			if (String(to).endsWith(".json")) throw new Error("injected rename failure");
			return real(from, to);
		}) as typeof fs.renameSync);
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		try {
			await h.fire("session_start");
			await sleep(400);
			const leftovers = fs.readdirSync(h.dir).filter(f => f.endsWith(".sock") || f.endsWith(".tmp") || f.endsWith(".json"));
			expect(leftovers).toEqual([]);
			expect(h.notices.some(n => n.message.includes("could not publish"))).toBe(true);
			expect(await h.run("session-control", "status")).toContain("failed");
		} finally {
			spy.mockRestore();
		}
		await h.run("session-control", "on");
		await h.waitFor(() => h.registry(), "control to start once publishing works again");
	});
});

describe("review 2, finding 7: failing to enforce the live disable is visible", () => {
	test("status and a one-time notice say that live disabling is unavailable when the settings registry cannot be used", async () => {
		const h = await start();
		// the default test environment has no omp registry module at all
		expect(await h.run("session-control", "status")).toContain("live disabling");
		expect(h.notices.some(n => n.message.includes("disabledExtensions"))).toBe(true);
	});
});

describe("review 2, findings 10 and 14: frame-local failures and wire-format edges", () => {
	test("a malformed op is answered and the frames pipelined behind it still run", async () => {
		const h = await start();
		const { client, nonce } = await Client.toHost(h);
		client.send(
			`{"v":1,"id":"bad","nonce":"${nonce}","op":{"toString":null}}\n{"v":1,"id":"good","nonce":"${nonce}","op":"snapshot"}`,
		);
		const first = await client.next();
		const second = await client.next();
		expect(first).toMatchObject({ id: "bad", ok: false, code: "unknown_op" });
		expect(second).toMatchObject({ id: "good", ok: true });
		client.close();
	});

	test("a numeric id that is not finite is rejected, not echoed as null", async () => {
		const h = await start();
		const { client, nonce } = await Client.toHost(h);
		const r = await client.request(`{"v":1,"id":1e400,"nonce":"${nonce}","op":"snapshot"}`);
		expect(r).toMatchObject({ ok: false, code: "bad_request", id: null });
		client.close();
	});

	test("a frame that is not valid UTF-8 is rejected instead of being repaired", async () => {
		const h = await start();
		const { client, nonce } = await Client.toHost(h);
		client.sock.write(Buffer.concat([Buffer.from(`{"v":1,"id":"u","nonce":"${nonce}","op":"snapshot","args":{"x":"`), Buffer.from([0xff, 0xfe]), Buffer.from('"}}\n')]));
		const r = await client.next();
		expect(r).toMatchObject({ ok: false, code: "bad_json" });
		expect((await client.request({ v: 1, id: 2, nonce, op: "snapshot" })).ok).toBe(true);
		client.close();
	});

	test("a reply is never larger than a frame, and an unknown op is not reflected", async () => {
		const h = await start();
		const { client, nonce } = await Client.toHost(h);
		const op = "o".repeat(60_000);
		client.send(`{"v":1,"id":"big","nonce":"${nonce}","op":"${op}"}`);
		const r = await client.next();
		expect(JSON.stringify(r).length).toBeLessThan(1000);
		h.name = "n".repeat(100_000); // a session title far beyond any limit
		client.send({ v: 1, id: "snap", nonce, op: "snapshot" });
		let raw = "";
		client.sock.setEncoding("utf8");
		const reply = await client.next();
		raw = JSON.stringify(reply);
		expect(raw.length).toBeLessThanOrEqual(65_536);
		client.close();
	});
});
