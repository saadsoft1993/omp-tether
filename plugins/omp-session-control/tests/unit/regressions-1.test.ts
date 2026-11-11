// Regression tests: branching, control directory replacement, hung operations, connection handling, reply contents, socket mode, registry timing.
import { afterEach, describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
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
const fdCount = () => fs.readdirSync("/dev/fd").length;

describe("session_branch", () => {
	test("a branch bumps the generation and republishes the registry with the live session id", async () => {
		const h = await start();
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		h.sessionId = "01a1b65b-3333-7000-8000-000000000003";
		await h.fire("session_branch");
		const reg = await h.waitFor(() => {
			const r = h.registry();
			return r && r.data.generation === generation + 1 ? r : undefined;
		}, "the registry after the branch");
		expect(reg.data.sessionId).toBe(h.sessionId);
		const stale = await client.request({ v: 1, id: 1, nonce, op: "rename", sessionId, generation, args: { name: "x" } });
		expect(stale.code).toBe("stale_session");
		client.close();
	});
});

describe("a replaced control directory", () => {
	test("removing and recreating the directory in one tick brings up a new listener", async () => {
		const h = await start();
		const old = await h.waitFor(() => h.registry(), "the registry");
		fs.rmSync(h.dir, { recursive: true, force: true });
		fs.mkdirSync(h.dir, { mode: 0o700 });
		fs.writeFileSync(path.join(h.dir, "enabled"), "");
		const fresh = await h.waitFor(() => {
			const r = h.registry();
			return r && r.data.nonce !== old.data.nonce ? r : undefined;
		}, "a new registry in the recreated directory");
		const client = await Client.connect(fresh.data.socket as string);
		expect((await client.request({ v: 1, id: 1, nonce: fresh.data.nonce, op: "snapshot" })).ok).toBe(true);
		client.close();
		// the watcher follows the new directory: the off file still works
		fs.writeFileSync(path.join(h.dir, "off"), "");
		await h.waitFor(() => h.registry() === undefined, "the registry to go after the off file");
	});

	test("a deleted registry or socket is republished", async () => {
		const h = await start();
		const first = await h.waitFor(() => h.registry(), "the registry");
		fs.unlinkSync(first.file);
		const second = await h.waitFor(() => {
			const r = h.registry();
			return r && r.data.nonce !== first.data.nonce ? r : undefined;
		}, "a republished registry");
		fs.unlinkSync(second.data.socket as string);
		await h.waitFor(() => {
			const r = h.registry();
			return r && r.data.nonce !== second.data.nonce ? r : undefined;
		}, "a republished listener after the socket vanished");
	});
});

describe("an operation that never finishes", () => {
	test("a hung rename gets a timeout reply and does not block reads or exhaust the in-flight limit", async () => {
		const h = await start({ timerScale: 0.01 });
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		const setName = h.pi.setSessionName as (n: string) => Promise<void>;
		h.pi.setSessionName = () => new Promise(() => {});
		const r = await client.request({ v: 1, id: 1, nonce, op: "rename", sessionId, generation, args: { name: "x" } });
		expect(r.code).toBe("timeout");
		h.pi.setSessionName = setName;
		for (let i = 0; i < 20; i++) expect((await client.request({ v: 1, id: `s${i}`, nonce, op: "snapshot" })).ok).toBe(true);
		// the change that timed out never settled, so further changes are refused (reads keep working)
		const refused = await client.request({ v: 1, id: 2, nonce, op: "rename", sessionId, generation, args: { name: "fine" } });
		expect(refused.code).toBe("mutation_pending");
		client.close();
	});

	test("16 hung mutations do not outlive an off and on cycle", async () => {
		const h = await start();
		const first = await Client.toHost(h);
		h.pi.setSessionName = () => new Promise(() => {});
		for (let i = 0; i < 16; i++) {
			first.client.send({ v: 1, id: `h${i}`, nonce: first.nonce, op: "rename", sessionId: first.sessionId, generation: first.generation, args: { name: "x" } });
		}
		await sleep(100);
		fs.writeFileSync(path.join(h.dir, "off"), "");
		await h.waitFor(() => h.registry() === undefined, "control to close");
		fs.rmSync(path.join(h.dir, "off"));
		const again = await Client.toHost(h);
		expect(again.nonce).not.toBe(first.nonce);
		const r = await again.client.request({ v: 1, id: 1, nonce: again.nonce, op: "snapshot" });
		expect(r.ok).toBe(true);
		first.client.close();
		again.client.close();
	});

	test("a mutation queued by a client that has since disconnected is not run", async () => {
		const h = await start();
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		const setName = h.pi.setSessionName as (n: string) => Promise<void>;
		h.pi.setSessionName = async (n: string) => {
			await sleep(150);
			await setName(n);
		};
		client.send({ v: 1, id: 1, nonce, op: "rename", sessionId, generation, args: { name: "first" } });
		client.send({ v: 1, id: 2, nonce, op: "rename", sessionId, generation, args: { name: "second" } });
		await sleep(30);
		client.close();
		await sleep(500);
		expect(h.name).toBe("first");
	});
});

describe("connections that are turned away or closed", () => {
	test("sockets turned away with too_many_clients are destroyed even when the peer never closes", async () => {
		const h = await start();
		const { client, nonce } = await Client.toHost(h);
		const socketPath = h.registry()?.data.socket as string;
		const held = [client];
		for (let i = 0; i < 3; i++) {
			const c = await Client.connect(socketPath);
			expect((await c.request({ v: 1, id: 1, nonce, op: "snapshot" })).ok).toBe(true);
			held.push(c);
		}
		const base = fdCount();
		const rejected: net.Socket[] = [];
		for (let i = 0; i < 12; i++) {
			const s = net.createConnection({ path: socketPath, allowHalfOpen: true }); // never ends its side
			s.on("error", () => {});
			s.resume();
			rejected.push(s);
		}
		await sleep(3500);
		const after = fdCount();
		for (const s of rejected) s.destroy();
		for (const c of held) c.close();
		// Each rejected connection costs one descriptor here (the test's own end). Two each means the extension still
		// holds its side of a socket whose peer never closed.
		expect(after - base).toBeLessThanOrEqual(rejected.length + 2);
	}, 15_000);

	test("a burst of connections that never close is cleaned up and the listener stays healthy", async () => {
		const h = await start();
		const socketPath = (await h.waitFor(() => h.registry(), "the registry")).data.socket as string;
		const base = fdCount();
		const socks: net.Socket[] = [];
		for (let i = 0; i < 80; i++) {
			const s = net.createConnection({ path: socketPath, allowHalfOpen: true });
			s.on("error", () => {});
			s.resume();
			socks.push(s);
		}
		await sleep(3500);
		const after = fdCount();
		for (const s of socks) s.destroy();
		expect(after - base).toBeLessThanOrEqual(socks.length + 2);
		await sleep(200);
		const { client, nonce } = await Client.toHost(h);
		expect((await client.request({ v: 1, id: 1, nonce, op: "snapshot" })).ok).toBe(true);
		client.close();
	}, 15_000);

	test("every open connection receives the closing frame", async () => {
		const h = await start();
		const first = await Client.toHost(h);
		const socketPath = first.client.sock.remoteAddress ?? (h.registry()?.data.socket as string);
		const others = [await Client.connect(socketPath), await Client.connect(socketPath), await Client.connect(socketPath)];
		for (const o of others) await o.request({ v: 1, id: 1, nonce: first.nonce, op: "snapshot" });
		fs.writeFileSync(path.join(h.dir, "off"), "");
		for (const c of [first.client, ...others]) expect((await c.next())?.code).toBe("closing");
	});

	test("a client that trickles bytes of one frame is cut off at the frame deadline", async () => {
		const h = await start({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } });
		const { client } = await Client.toHost(h);
		const started = Date.now();
		for (let i = 0; i < 60 && !client.closed; i++) {
			client.sock.write(" ".repeat(0) + (i === 0 ? '{"v":1,' : "1"));
			await sleep(20);
		}
		await h.waitFor(() => client.closed, "the trickling client to be closed", 3000);
		expect(Date.now() - started).toBeLessThan(1500);
	});

	test("a client that sends complete requests steadily is not cut off by the frame deadline", async () => {
		const h = await start({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } });
		const { client, nonce } = await Client.toHost(h);
		for (let i = 0; i < 20; i++) {
			expect((await client.request({ v: 1, id: i, nonce, op: "snapshot" })).ok).toBe(true);
			await sleep(20);
		}
		expect(client.closed).toBe(false);
		client.close();
	});
});

describe("what replies and guards contain", () => {
	test("an internal error reply carries a fixed message and the log holds no exception text", async () => {
		const h = await start();
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		h.failSetName = "throw";
		const real = h.pi.setSessionName as () => Promise<void>;
		h.pi.setSessionName = async () => {
			throw new Error("cannot write /home/someone/.omp/agent/sessions/secret.jsonl");
		};
		const r = await client.request({ v: 1, id: 1, nonce, op: "rename", sessionId, generation, args: { name: "x" } });
		h.pi.setSessionName = real;
		expect(r.code).toBe("internal");
		expect(JSON.stringify(r)).not.toContain("/home/someone");
		expect(fs.readFileSync(path.join(h.dir, "events.log"), "utf8")).not.toContain("/home/someone"); // categories only
		client.close();
	});

	test("a thrown value whose toString throws does not escape the guards", async () => {
		const h = await start();
		await h.waitFor(() => h.registry(), "the registry");
		const poison = {
			toString() {
				throw new Error("poison");
			},
		};
		h.failSessionId = true;
		h.failValue = poison;
		await h.fire("session_switch"); // the registry rewrite throws the poisoned value inside a guarded handler
		h.failSessionId = false;
		const { client, nonce } = await Client.toHost(h);
		expect((await client.request({ v: 1, id: 1, nonce, op: "snapshot" })).ok).toBe(true);
		client.close();
		expect(fs.readFileSync(path.join(h.dir, "events.log"), "utf8")).toContain('"kind":"handler_error"');
		await sleep(200);
		expect(h.uncaught).toEqual([]);
	});

	test("an over-long id is not echoed back", async () => {
		const h = await start();
		const { client, nonce } = await Client.toHost(h);
		const r = await client.request({ v: 1, id: "x".repeat(5000), nonce, op: "snapshot" });
		expect(r.code).toBe("bad_request");
		expect(r.id).toBeNull();
		const r2 = await client.request({ v: 2, id: "y".repeat(5000), nonce, op: "snapshot" });
		expect(r2.code).toBe("bad_version");
		expect(r2.id).toBeNull();
		client.close();
	});
});

describe("the socket mode", () => {
	test("if the socket cannot be tightened to 0600 the listener is closed instead of left open and unregistered", async () => {
		const real = fs.chmodSync;
		const spy = spyOn(fs, "chmodSync").mockImplementation(((p: fs.PathLike, mode: fs.Mode) => {
			if (String(p).endsWith(".sock")) throw new Error("injected chmod failure");
			return real(p, mode);
		}) as typeof fs.chmodSync);
		try {
			const h = await start();
			await sleep(400);
			expect(h.registry()).toBeUndefined();
			expect(fs.readdirSync(h.dir).filter(f => f.endsWith(".sock"))).toEqual([]);
		} finally {
			spy.mockRestore();
		}
	});
});

describe("registry timing and directory permissions", () => {
	test("the registry is not published before the socket is bound", async () => {
		const h = await FakeHost.start({ enable: true });
		hosts.push(h);
		await h.fire("session_start");
		await h.fire("session_switch"); // synchronously after start: the listener is not bound yet
		expect(h.registry()).toBeUndefined();
		await h.waitFor(() => h.registry(), "the registry after the bind");
	});

	test("a directory that other users can write to is refused, not tightened", async () => {
		const h = await FakeHost.start();
		hosts.push(h);
		fs.mkdirSync(h.dir, { mode: 0o777 });
		fs.chmodSync(h.dir, 0o777);
		fs.writeFileSync(path.join(h.dir, "enabled"), "");
		await h.fire("session_start");
		await sleep(200);
		expect(h.registry()).toBeUndefined();
		expect(fs.statSync(h.dir).mode & 0o777).toBe(0o777);
		expect(h.notices[0]?.message).toContain("writable by other");
		expect(await h.run("session-control", "on")).toContain("writable by other");
	});

	test("the directory mode is fixed through the open directory, never by path", () => {
		const source = fs.readFileSync(path.resolve(import.meta.dir, "../../src/omp-session-control.ts"), "utf8");
		expect(source).toContain("fchmodSync");
		expect(source).not.toMatch(/\bchmodSync\(\s*dir\b/);
	});
});
