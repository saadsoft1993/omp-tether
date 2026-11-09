import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";
import { runConformance } from "../conformance/suite";
import { FakeHost } from "./support/fake-host";

const IDLE_MS = 400;

describe("session-control/1 conformance suite against the extension", () => {
	let host: FakeHost;
	beforeAll(async () => {
		host = await FakeHost.start({ enable: true, env: { OMP_SESSION_CONTROL_IDLE_MS: String(IDLE_MS) } });
		await host.fire("session_start");
		await host.waitFor(() => host.registry(), "the registry");
	});
	afterAll(() => host.dispose());

	test("every check passes, including mutation, client-limit, idle and off-switch groups", async () => {
		const results = await runConformance({ dir: host.dir, mutate: true, exclusive: true, idleMs: IDLE_MS, offSwitch: true });
		const failed = results.filter(r => !r.ok);
		expect(failed.map(r => `${r.id}: ${r.detail}`)).toEqual([]);
		expect(results.length).toBeGreaterThanOrEqual(24);
		expect(host.uncaught).toEqual([]);
		expect(host.timerErrors).toEqual([]);
	});

	test("the suite reports a failure for a registry that is missing", async () => {
		const results = await runConformance({ dir: "/tmp/osc-does-not-exist-" + process.pid });
		expect(results.length).toBe(1);
		expect(results[0]?.ok).toBe(false);
	});
});

describe("the suite rejects a wrong implementation", () => {
	test("a server that says ok to everything fails the validation checks", async () => {
		const dir = fs.mkdtempSync("/tmp/oscB-");
		fs.chmodSync(dir, 0o700);
		const nonce = "ab".repeat(16);
		const socketPath = path.join(dir, "bad.sock");
		const server = net.createServer(sock => {
			let buf = "";
			sock.on("data", d => {
				buf += d.toString();
				for (let nl = buf.indexOf("\n"); nl >= 0; nl = buf.indexOf("\n")) {
					buf = buf.slice(nl + 1);
					sock.write(`${JSON.stringify({ v: 1, id: "snap", ok: true, result: {} })}\n`);
				}
			});
			sock.on("error", () => {});
		});
		await new Promise<void>(r => server.listen(socketPath, r));
		fs.chmodSync(socketPath, 0o600);
		fs.writeFileSync(path.join(dir, `${nonce}.json`), JSON.stringify({ v: 1, versions: [1], nonce, pid: process.pid, socket: socketPath, sessionId: "s", generation: 0 }), { mode: 0o600 });
		try {
			const results = await runConformance({ dir });
			const failed = new Set(results.filter(r => !r.ok).map(r => r.id));
			for (const id of ["P1", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11", "P12", "P13"]) expect(failed.has(id)).toBe(true);
			for (const id of ["R1", "R2", "R3", "R4", "R5"]) expect(failed.has(id)).toBe(false);
		} finally {
			server.close();
			fs.rmSync(dir, { recursive: true, force: true });
		}
	});
});
