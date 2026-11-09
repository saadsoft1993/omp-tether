// The conformance suite must not trust the directory it is pointed at before it has checked it, and must not modify
// anything outside the directory it was given.
import { afterEach, describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";
import { runConformance } from "../conformance/suite";

const roots: string[] = [];
const servers: net.Server[] = [];
const newRoot = () => {
	const r = fs.mkdtempSync("/tmp/oscC-");
	roots.push(r);
	return r;
};
afterEach(() => {
	for (const s of servers.splice(0)) s.close();
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});

/** A listener that says ok to everything and counts connections, plus a registry for it in `dir`. */
async function fakeImplementation(dir: string, connections: { n: number }) {
	const nonce = "cd".repeat(16);
	const socketPath = path.join(dir, "fake.sock");
	const server = net.createServer(sock => {
		connections.n += 1;
		sock.on("data", () => sock.write(`${JSON.stringify({ v: 1, id: "snap", ok: true, result: {} })}\n`));
		sock.on("error", () => {});
	});
	servers.push(server);
	await new Promise<void>(r => server.listen(socketPath, r));
	fs.chmodSync(socketPath, 0o600);
	fs.writeFileSync(path.join(dir, `${nonce}.json`), JSON.stringify({ v: 1, versions: [1], nonce, pid: process.pid, socket: socketPath, sessionId: "s", generation: 0 }), { mode: 0o600 });
}

describe("conformance suite: boundary checks come before any connection", () => {
	test("a symlinked control directory fails R1 and no connection or mutation is attempted", async () => {
		const root = newRoot();
		const real = path.join(root, "real");
		fs.mkdirSync(real, { mode: 0o700 });
		const connections = { n: 0 };
		await fakeImplementation(real, connections);
		const link = path.join(root, "link");
		fs.symlinkSync(real, link);
		const results = await runConformance({ dir: link, mutate: true, exclusive: true, offSwitch: true });
		expect(results.find(r => r.id === "R1")?.ok).toBe(false);
		expect(connections.n).toBe(0);
		expect(results.some(r => r.id.startsWith("M") || r.id === "O1")).toBe(false);
	});

	test("a directory that group or others can enter fails R1 and nothing is connected", async () => {
		const root = newRoot();
		const dir = path.join(root, "open");
		fs.mkdirSync(dir, { mode: 0o755 });
		fs.chmodSync(dir, 0o755);
		const connections = { n: 0 };
		await fakeImplementation(dir, connections);
		const results = await runConformance({ dir, mutate: true });
		expect(results.find(r => r.id === "R1")?.ok).toBe(false);
		expect(connections.n).toBe(0);
	});

	test("a failed prerequisite stops the mutation groups", async () => {
		const root = newRoot();
		const dir = path.join(root, "d");
		fs.mkdirSync(dir, { mode: 0o700 });
		const connections = { n: 0 };
		await fakeImplementation(dir, connections);
		fs.chmodSync(path.join(dir, "fake.sock"), 0o666); // R4 fails
		const results = await runConformance({ dir, mutate: true, offSwitch: true });
		expect(results.find(r => r.id === "R4")?.ok).toBe(false);
		expect(connections.n).toBe(0);
		expect(results.some(r => r.id.startsWith("M") || r.id === "O1")).toBe(false);
	});

	test("a FIFO named like a registry does not hang the suite", () => {
		const root = newRoot();
		const dir = path.join(root, "d");
		fs.mkdirSync(dir, { mode: 0o700 });
		spawnSync("mkfifo", [path.join(dir, `${"ab".repeat(16)}.json`)]);
		const suite = path.resolve(import.meta.dir, "../conformance/suite.ts");
		const r = spawnSync(process.execPath, [suite, "--dir", dir], { timeout: 10_000, encoding: "utf8" });
		expect(r.error).toBeUndefined();
		expect(r.status).toBe(2); // no usable registry, reported, not hung
	}, 20_000);
});

describe("conformance suite: the off-switch check never writes through a symlink", () => {
	test("an existing off entry that points elsewhere is left alone and the check fails", async () => {
		const root = newRoot();
		const dir = path.join(root, "d");
		fs.mkdirSync(dir, { mode: 0o700 });
		const connections = { n: 0 };
		await fakeImplementation(dir, connections); // ignores the off file, like a broken implementation
		const victim = path.join(root, "victim.txt");
		fs.writeFileSync(victim, "keep me");
		fs.symlinkSync(victim, path.join(dir, "off"));
		const results = await runConformance({ dir, offSwitch: true });
		expect(fs.readFileSync(victim, "utf8")).toBe("keep me");
		expect(results.find(r => r.id === "O1")?.ok).toBe(false);
		expect(fs.lstatSync(path.join(dir, "off")).isSymbolicLink()).toBe(true); // not removed either
	}, 30_000);
});
