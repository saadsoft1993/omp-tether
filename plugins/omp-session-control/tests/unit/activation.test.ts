import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { Client } from "./support/client";
import { FakeHost } from "./support/fake-host";

const hosts: FakeHost[] = [];
async function start(opts: Parameters<typeof FakeHost.start>[0] = {}): Promise<FakeHost> {
	const h = await FakeHost.start(opts);
	hosts.push(h);
	return h;
}
afterEach(async () => {
	for (const h of hosts.splice(0)) await h.dispose();
});

const mode = (p: string) => fs.lstatSync(p).mode & 0o777;
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

describe("inert by default", () => {
	test("no control directory: nothing is created, no registry, no notice", async () => {
		const h = await start();
		await h.fire("session_start");
		await sleep(150);
		expect(fs.existsSync(h.dir)).toBe(false);
		expect(h.notices).toEqual([]);
	});

	test("a control directory without the enabled marker stays inert and writes nothing", async () => {
		const h = await start();
		fs.mkdirSync(h.dir, { mode: 0o700 });
		await h.fire("session_start");
		await sleep(150);
		expect(fs.readdirSync(h.dir)).toEqual([]);
	});

	test("creating the enabled marker later turns it on in a running session", async () => {
		const h = await start();
		fs.mkdirSync(h.dir, { mode: 0o700 });
		await h.fire("session_start");
		expect(h.registry()).toBeUndefined();
		fs.writeFileSync(path.join(h.dir, "enabled"), "");
		const reg = await h.waitFor(() => h.registry(), "the registry after the marker appeared");
		expect(reg.data.sessionId).toBe(h.sessionId);
	});

	test("an enabled marker that is a symlink does not count", async () => {
		const h = await start();
		fs.mkdirSync(h.dir, { mode: 0o700 });
		fs.writeFileSync(path.join(h.root, "elsewhere"), "");
		fs.symlinkSync(path.join(h.root, "elsewhere"), path.join(h.dir, "enabled"));
		await h.fire("session_start");
		await sleep(150);
		expect(h.registry()).toBeUndefined();
	});

	test("only the main interactive session listens: subagents, print and rpc runs never do", async () => {
		for (const extra of [{ kind: "sub" as const }, { mode: "print" as const }, { mode: "rpc" as const }]) {
			const h = await start({ enable: true, ...extra });
			await h.fire("session_start");
			await sleep(150);
			expect(fs.readdirSync(h.dir)).toEqual(["enabled"]);
		}
	});

	test("the default directory is ~/.config/omp-session-control", async () => {
		const root = fs.mkdtempSync("/tmp/oscH-");
		const dir = path.join(root, ".config", "omp-session-control");
		fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
		fs.writeFileSync(path.join(dir, "enabled"), "");
		const h = await start({ env: { OMP_SESSION_CONTROL_DIR: undefined, HOME: root } });
		try {
			await h.fire("session_start");
			await h.waitFor(() => fs.readdirSync(dir).some(f => f.endsWith(".sock")), "a socket in the default directory");
		} finally {
			await h.dispose();
			fs.rmSync(root, { recursive: true, force: true });
		}
	});

	test("a relative OMP_SESSION_CONTROL_DIR keeps control off and tells the user once", async () => {
		const h = await start({ env: { OMP_SESSION_CONTROL_DIR: "relative/dir" } });
		await h.fire("session_start");
		await h.fire("session_switch");
		expect(h.notices.length).toBe(1);
		expect(h.notices[0]?.message).toContain("absolute");
		expect(fs.existsSync(path.resolve("relative/dir"))).toBe(false);
	});
});

describe("when enabled", () => {
	test("publishes a private registry and socket, and removes them at shutdown", async () => {
		const h = await start({ enable: true });
		await h.fire("session_start");
		const reg = await h.waitFor(() => h.registry(), "the registry");
		expect(mode(h.dir)).toBe(0o700);
		expect(mode(reg.file)).toBe(0o600);
		const sock = reg.data.socket as string;
		expect(fs.lstatSync(sock).isSocket()).toBe(true);
		expect(mode(sock)).toBe(0o600);
		expect(reg.data).toMatchObject({ v: 1, versions: [1], pid: process.pid, sessionId: h.sessionId, generation: 0 });
		expect((reg.data.plugin as { name: string }).name).toBe("omp-session-control");
		await h.fire("session_shutdown");
		expect(fs.readdirSync(h.dir).filter(f => f !== "enabled" && f !== "events.log")).toEqual([]);
		const log = fs.readFileSync(path.join(h.dir, "events.log"), "utf8");
		expect(log).toContain('"kind":"listening"');
		expect(log).toContain('"kind":"closed"');
		expect(mode(path.join(h.dir, "events.log"))).toBe(0o600);
	});

	test("a directory with loose permissions is tightened to 0700", async () => {
		const h = await start();
		fs.mkdirSync(h.dir, { mode: 0o755 });
		fs.chmodSync(h.dir, 0o755);
		fs.writeFileSync(path.join(h.dir, "enabled"), "");
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		expect(mode(h.dir)).toBe(0o700);
	});

	test("a symlinked control directory is refused and reported once", async () => {
		const h = await start();
		const real = path.join(h.root, "real");
		fs.mkdirSync(real, { mode: 0o700 });
		fs.writeFileSync(path.join(real, "enabled"), "");
		fs.symlinkSync(real, h.dir);
		await h.fire("session_start");
		await h.fire("session_switch");
		await sleep(150);
		expect(fs.readdirSync(real)).toEqual(["enabled"]);
		expect(h.notices.length).toBe(1);
		expect(h.notices[0]?.message).toContain("not a real directory");
	});

	test("a control directory path too long for a Unix socket is refused with a clear message", async () => {
		const h = await start();
		const long = path.join(h.root, "x".repeat(110));
		fs.mkdirSync(long, { mode: 0o700 });
		fs.writeFileSync(path.join(long, "enabled"), "");
		const h2 = await start({ dir: long });
		await h2.fire("session_start");
		await sleep(150);
		expect(h2.notices[0]?.message).toContain("too long");
		expect(fs.readdirSync(long).filter(f => f.endsWith(".sock") || f.endsWith(".json"))).toEqual([]);
	});

	test("registries and sockets left by dead sessions are removed at start, a live one's are kept", async () => {
		const h = await start({ enable: true });
		const dead = spawnSync("true");
		const deadPid = 2 ** 22 + (dead.pid ?? 1); // beyond the default pid range on Linux and macOS: not alive
		const staleSock = path.join(h.dir, "deaddeaddeaddead.sock");
		const server = net.createServer();
		await new Promise<void>(r => server.listen(staleSock, r));
		server.close();
		const staleReg = path.join(h.dir, `${"0".repeat(32)}.json`);
		fs.writeFileSync(staleReg, JSON.stringify({ v: 1, pid: deadPid, socket: staleSock, sessionId: "x" }), { mode: 0o600 });
		const liveReg = path.join(h.dir, `${"1".repeat(32)}.json`);
		fs.writeFileSync(liveReg, JSON.stringify({ v: 1, pid: process.ppid, socket: path.join(h.dir, "x.sock"), sessionId: "y" }), { mode: 0o600 });
		await h.fire("session_start");
		await h.waitFor(() => fs.readdirSync(h.dir).some(f => f.endsWith(".sock")), "the new socket");
		expect(fs.existsSync(staleReg)).toBe(false);
		expect(fs.existsSync(staleSock)).toBe(false);
		expect(fs.existsSync(liveReg)).toBe(true);
	});

	test("session_switch bumps the generation and republishes the registry; old targets go stale", async () => {
		const h = await start({ enable: true });
		await h.fire("session_start");
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		h.sessionId = "01a1b65b-2222-7000-8000-000000000002";
		await h.fire("session_switch");
		const reg = await h.waitFor(() => {
			const r = h.registry();
			return r && r.data.generation === generation + 1 ? r : undefined;
		}, "the republished registry");
		expect(reg.data.sessionId).toBe(h.sessionId);
		const refused = await client.request({ v: 1, id: 1, nonce, op: "rename", sessionId, generation, args: { name: "x" } });
		expect(refused.code).toBe("stale_session");
		const ok = await client.request({ v: 1, id: 2, nonce, op: "rename", sessionId: h.sessionId, generation: generation + 1, args: { name: "after switch" } });
		expect(ok.ok).toBe(true);
		client.close();
	});
});

describe("off switch and marker removal", () => {
	test("the off file closes open connections and the registry, removing it brings control back with a new nonce", async () => {
		const h = await start({ enable: true });
		await h.fire("session_start");
		const { client, nonce } = await Client.toHost(h);
		expect((await client.request({ v: 1, id: 1, nonce, op: "snapshot" })).ok).toBe(true);
		fs.writeFileSync(path.join(h.dir, "off"), "");
		await h.waitFor(() => h.registry() === undefined, "the registry to go");
		expect((await client.next())?.code).toBe("closing");
		client.close();
		fs.rmSync(path.join(h.dir, "off"));
		const reg = await h.waitFor(() => h.registry(), "the registry to come back");
		expect(reg.data.nonce).not.toBe(nonce);
	});

	test("removing the enabled marker closes control; restoring it reopens", async () => {
		const h = await start({ enable: true });
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		fs.rmSync(path.join(h.dir, "enabled"));
		await h.waitFor(() => h.registry() === undefined, "the registry to go");
		fs.writeFileSync(path.join(h.dir, "enabled"), "");
		await h.waitFor(() => h.registry(), "the registry to come back");
	});

	test("deleting the whole directory closes control and does not recreate anything", async () => {
		const h = await start({ enable: true });
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		fs.rmSync(h.dir, { recursive: true, force: true });
		await sleep(300);
		expect(fs.existsSync(h.dir)).toBe(false);
	});

	test("an off file placed while the session was not running keeps it off from the start", async () => {
		const h = await start({ enable: true });
		fs.writeFileSync(path.join(h.dir, "off"), "");
		await h.fire("session_start");
		await sleep(150);
		expect(h.registry()).toBeUndefined();
	});
});

describe("/session-control", () => {
	test("status describes an inert installation", async () => {
		const h = await start();
		await h.fire("session_start");
		const text = await h.run("session-control", "status");
		expect(text).toContain("omp-session-control 0.1.0");
		expect(text).toContain(h.dir);
		expect(text).toContain("inert");
		expect(await h.run("session-control", "")).toContain("inert");
	});

	test("on creates the private directory and marker and starts this session; status then shows listening", async () => {
		const h = await start();
		await h.fire("session_start");
		await h.run("session-control", "on");
		const reg = await h.waitFor(() => h.registry(), "the registry after /session-control on");
		expect(mode(h.dir)).toBe(0o700);
		expect(mode(path.join(h.dir, "enabled"))).toBe(0o600);
		expect(reg.data.pid).toBe(process.pid);
		expect(await h.run("session-control", "status")).toContain("listening");
	});

	test("off writes the off file and closes control; on removes it again", async () => {
		const h = await start({ enable: true });
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		expect(await h.run("session-control", "off")).toContain("off");
		expect(fs.existsSync(path.join(h.dir, "off"))).toBe(true);
		await h.waitFor(() => h.registry() === undefined, "the registry to go");
		expect(await h.run("session-control", "status")).toContain("off");
		await h.run("session-control", "on");
		expect(fs.existsSync(path.join(h.dir, "off"))).toBe(false);
		await h.waitFor(() => h.registry(), "the registry to come back");
	});

	test("off without a control directory creates nothing", async () => {
		const h = await start();
		await h.fire("session_start");
		expect(await h.run("session-control", "off")).toContain("already off");
		expect(fs.existsSync(h.dir)).toBe(false);
	});

	test("an unknown argument prints the usage and changes nothing", async () => {
		const h = await start();
		await h.fire("session_start");
		expect(await h.run("session-control", "frobnicate")).toContain("usage");
		expect(fs.existsSync(h.dir)).toBe(false);
	});

	test("the command does nothing in a subagent or a print run", async () => {
		const h = await start({ kind: "sub" });
		expect(await h.run("session-control", "on")).toContain("main interactive session");
		expect(fs.existsSync(h.dir)).toBe(false);
	});
});
