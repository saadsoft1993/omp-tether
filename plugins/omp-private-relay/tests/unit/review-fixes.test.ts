// Regression tests for the findings of the two independent reviews of this plugin (docs/research of the workspace that
// built it). Each test names the finding it pins. Every one of them was run against the code as it was before the fix and
// failed there; relay-mutation.test.ts additionally deletes each relay-side guard and requires these tests to notice.
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { createTailscaleAuth, lookupFromCommand } from "../../src/auth/tailscale-serve";
import { runCli, type Io } from "../../src/cli";
import { loadConfig, readConfigFile } from "../../src/config";
import { runDoctor } from "../../src/doctor";
import { dropinPaths, healthDropin, servicesDropin, strayOverrides, writeDropin } from "../../src/dropins";
import { ensurePrivateDir, readFdBounded } from "../../src/fsutil";
import { fetchProvenStats, proveListener, readCheckKey, readLimited } from "../../src/listener-check";
import { PairingWriter } from "../../src/pairing-store";
import { checkAssetUrl, download, inspectInstalled, installTree, packTar, recoverInterrupted, sha256, treeHash, verifyAsset, type WebBuild } from "../../src/web-asset";
import { writeFakeOmp } from "./support/fake-omp";
import { COOKIE_NAME, freePorts, guestHeaders, hostPeer, makeRoomId, MAIN, type Relay, rawRequest, rawStatus, startRelay } from "./support/harness";

const roots: string[] = [];
const scratch = (): string => {
	const r = fs.mkdtempSync(path.join(os.tmpdir(), "oprx-"));
	roots.push(r);
	return r;
};
afterEach(() => {
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});
function io(): Io & { stdout: string; stderr: string } {
	const self = {
		stdout: "",
		stderr: "",
		out(t: string) {
			self.stdout += t;
		},
		err(t: string) {
			self.stderr += t;
		},
		async line() {
			return null;
		},
	};
	return self;
}

// ── pairing endpoint: framing, budget, guards that a real token proves ─────────────────────────

describe("pairing endpoint (review 1 F3 and F5, review 2 M2 and M3)", () => {
	let relay: Relay;
	let writer: PairingWriter;
	const post = (over: Record<string, string | null>, body: string, extra: { method?: string } = {}) =>
		rawRequest(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1", ...over }), { method: extra.method ?? "POST", body });

	const start = async () => {
		relay = await startRelay({ adapter: "token" });
		writer = new PairingWriter(relay.pairingDir);
	};
	afterEach(async () => {
		await relay?.stop();
	});

	// F5: every refusal is proven with a REAL token, which must still be redeemable afterwards: a 403 for an unknown token
	// would pass even without the guard under test.
	const guards: Array<[string, Record<string, string | null>]> = [
		["no custom header (a plain cross-site form post)", { "X-Omp-Relay": null }],
		["no Origin (plain requests with no Origin pass the identity layer, so only this check stops them)", { Origin: null }],
		["a foreign Origin", { Origin: "https://evil.example" }],
		["a wrong Host", { Host: "evil.example:1" }],
		["a proxy header", { "X-Forwarded-For": "192.0.2.1" }],
		["a wrong custom header value", { "X-Omp-Relay": "0" }],
	];
	for (const [name, over] of guards) {
		test(`${name}: refused, and the real token stays redeemable`, async () => {
			await start();
			const token = await writer.createToken();
			const refused = await post(over, JSON.stringify({ token }));
			expect(refused.status).toBe(403);
			expect(Object.keys(JSON.parse(fs.readFileSync(path.join(relay.pairingDir, "pending.json"), "utf8")))).toHaveLength(1);
			const ok = await post({}, JSON.stringify({ token }));
			expect(ok.status).toBe(200);
		});
	}

	test("F3/M2: a body without Content-Length, or sent chunked, is refused (411) even when small, and the token survives", async () => {
		await start();
		const token = await writer.createToken();
		// no Content-Length at all (rawRequest adds one only when a body is given)
		const none = await rawRequest(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }), { method: "POST" });
		expect(none.status).toBe(411);
		const small = await post({ "Transfer-Encoding": "chunked" }, `${JSON.stringify({ token }).length.toString(16)}\r\n${JSON.stringify({ token })}\r\n0\r\n\r\n`);
		expect([411, 400]).toContain(small.status);
		expect((await post({}, JSON.stringify({ token }))).status).toBe(200);
	});

	test("F3/M2: a large chunked body is cut off and refused, not buffered", async () => {
		await start();
		const token = await writer.createToken();
		const big = JSON.stringify({ token, pad: "x".repeat(200_000) });
		const r = await fetch(`http://127.0.0.1:${relay.guestPort}/__pair`, {
			method: "POST",
			headers: { ...guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }) },
			body: new ReadableStream({
				start(c) {
					c.enqueue(new TextEncoder().encode(big));
					c.close();
				},
			}),
			// @ts-expect-error Bun's fetch accepts duplex for a streamed body
			duplex: "half",
		});
		expect([411, 413]).toContain(r.status);
		expect((await post({}, JSON.stringify({ token }))).status).toBe(200);
	});

	test("M3: simultaneous failures cannot all pass a check made before any of them failed (the budget is reserved first)", async () => {
		await start();
		const results = await Promise.all(Array.from({ length: 25 }, (_, i) => post({}, JSON.stringify({ token: `wrong-${i}` }))));
		const refused = results.filter(r => r.status === 403).length;
		expect(refused).toBeLessThanOrEqual(10);
		expect(results.filter(r => r.status === 429).length).toBeGreaterThanOrEqual(15);
	});

	test("M3: attempts stalled before their body arrives hold their slot, so an eleventh attempt is refused even with a valid token", async () => {
		await start();
		const token = await writer.createToken();
		const head = [`POST /__pair HTTP/1.1`, ...Object.entries(guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1", "Content-Length": "40" })).map(([k, v]) => `${k}: ${v}`), "", ""].join("\r\n");
		const stalled: net.Socket[] = [];
		for (let i = 0; i < 10; i++) {
			const s = net.connect(relay.guestPort, "127.0.0.1");
			s.on("error", () => {});
			s.write(`${head}{"token":"`); // part of the body only
			stalled.push(s);
		}
		await Bun.sleep(300);
		const eleventh = await post({}, JSON.stringify({ token }));
		expect(eleventh.status).toBe(429);
		for (const s of stalled) s.destroy();
		await Bun.sleep(200);
		expect((await post({}, JSON.stringify({ token }))).status).toBe(200); // the slots were released, and the token never spent
	});
});

// ── identity: connection cap, hostile paths in the log, cache policy ───────────────────────────

describe("relay hygiene (review 2 L1 of the predecessor, review 1 F9)", () => {
	test("a hard cap on open sockets: the fifth upgrade is refused before it gets a socket, however many ask", async () => {
		const relay = await startRelay({ adapter: "token", env: { OMP_PRIVATE_RELAY_MAX_CONNECTIONS: "4" } });
		try {
			const hosts = Array.from({ length: 4 }, () => hostPeer(relay, makeRoomId("cap")));
			expect((await Promise.all(hosts.map(h => h.opened))).every(Boolean)).toBe(true);
			// twenty more, at once: each is answered 503 at the upgrade, so none becomes a socket
			const more = await Promise.all(Array.from({ length: 20 }, () => rawStatus(relay.hostPort, `/r/${makeRoomId("x")}?role=host`, {}, { upgrade: true })));
			expect(more.every(s => s === 503)).toBe(true);
			const guest = await rawStatus(relay.guestPort, `/r/${makeRoomId("g")}?role=guest`, guestHeaders(relay), { upgrade: true });
			expect(guest).toBe(503);
			const sockets = ((await (await fetch(`http://127.0.0.1:${relay.hostPort}/stats`)).json()) as { sockets: number }).sockets;
			expect(sockets).toBeLessThanOrEqual(4);
			hosts[0]?.ws.close();
			await Bun.sleep(300);
			expect(await rawStatus(relay.hostPort, `/r/${makeRoomId("y")}?role=host`, {}, { upgrade: true })).toBe(101); // the slot came back
			for (const h of hosts) h.ws.close();
		} finally {
			await relay.stop();
		}
	});

	test("a hostile path is not written to the log verbatim", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			await rawStatus(relay.guestPort, "/%1b%5b31mEVIL%00", guestHeaders(relay, { Cookie: null, Origin: null }));
			await rawStatus(relay.guestPort, `/${"a".repeat(200)}`, guestHeaders(relay, { Cookie: null, Origin: null }));
			await Bun.sleep(100);
			expect(relay.log()).not.toContain("EVIL");
			expect(relay.log()).not.toContain("a".repeat(100));
			expect(relay.log()).toContain("(other)");
		} finally {
			await relay.stop();
		}
	});

	test("hashed assets are cached privately: they sit behind an authenticated request", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			const r = await rawRequest(relay.guestPort, "/abcd1234.js", guestHeaders(relay, { Origin: null }));
			expect(r.status).toBe(200);
			expect(r.headers["cache-control"]).toMatch(/^private,/);
			expect(r.headers["cache-control"]).not.toContain("public");
		} finally {
			await relay.stop();
		}
	});

	test("restartGraceMs above two minutes is refused: the 60 s grace cannot be stretched to ten", () => {
		const root = scratch();
		expect(() => loadConfig({ HOME: root, OMP_PRIVATE_RELAY_RESTART_GRACE_MS: "600000" })).toThrow(/restartGraceMs/);
		expect(loadConfig({ HOME: root, OMP_PRIVATE_RELAY_RESTART_GRACE_MS: "120000" }).limits.restartGraceMs).toBe(120000);
	});
});

// ── listener proof: M1 (key published after the binds), H1/F2 (pair refuses an unproven port), M5 (status) ─────────────

describe("listener proof (review 2 M1, H1 and M5; review 1 F2)", () => {
	test("M1: a second start that fails on the taken port leaves the running relay's proof valid", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			const before = readCheckKey(relay.checkDir);
			expect(before.ok).toBe(true);
			const second = Bun.spawn([process.execPath, MAIN], { env: relay.env, stdout: "pipe", stderr: "pipe" });
			expect(await second.exited).toBe(1);
			const after = readCheckKey(relay.checkDir);
			if (!before.ok || !after.ok) throw new Error("no key");
			expect(after.key.equals(before.key)).toBe(true);
			expect((await proveListener(relay.guestPort, after.key)).ok).toBe(true);
			expect((await proveListener(relay.hostPort, after.key)).ok).toBe(true);
		} finally {
			await relay.stop();
		}
	});

	function pairEnv(root: string, host: number, guest: number) {
		return { HOME: root, PATH: "/usr/bin:/bin", OMP_PRIVATE_RELAY_HOST_PORT: String(host), OMP_PRIVATE_RELAY_GUEST_PORT: String(guest) };
	}

	test("F2/H1: pair makes no link and no token while the guest port does not prove to be this relay", async () => {
		const root = scratch();
		const [host, guest] = await freePorts(2);
		const impostor = Bun.serve({ hostname: "127.0.0.1", port: guest, fetch: () => Response.json({ mac: "0".repeat(64) }) });
		const check = path.join(root, ".config", "omp-private-relay", "check");
		fs.mkdirSync(check, { recursive: true, mode: 0o700 });
		fs.writeFileSync(path.join(check, "check.key"), "cd".repeat(32), { mode: 0o600 });
		try {
			const out = io();
			expect(await runCli(["pair"], pairEnv(root, host as number, guest as number), out)).toBe(1);
			expect(out.stderr).toContain("does not prove to be this relay");
			expect(out.stdout).not.toContain("#token=");
			expect(fs.existsSync(path.join(root, ".config", "omp-private-relay", "pairing", "pending.json"))).toBe(false);
		} finally {
			impostor.stop(true);
		}
	});

	test("F2: with no relay (no key) there is no link either", async () => {
		const root = scratch();
		const [host, guest] = await freePorts(2);
		const out = io();
		expect(await runCli(["pair"], pairEnv(root, host as number, guest as number), out)).toBe(1);
		expect(out.stderr).toContain("Start the relay first");
		expect(fs.existsSync(path.join(root, ".config", "omp-private-relay", "pairing"))).toBe(false);
	});

	test("F2 (reorder): when the address cannot be named, no orphan token is left behind", async () => {
		const relay = await startRelay({ adapter: "tailscale-serve", unknownIdentity: true });
		try {
			// the relay was started with an unknown identity (503 to browsers); pair runs with the same unusable tailscale
			const out = io();
			const code = await runCli(["pair"], { ...relay.env, PATH: "/usr/bin:/bin" }, out);
			expect(code).toBe(1);
			expect(out.stderr).toContain("No link was made");
			expect(fs.existsSync(path.join(relay.pairingDir, "pending.json"))).toBe(false);
		} finally {
			await relay.stop();
		}
	});

	test("M5: status does not trust an unproven listener: no redirect followed, no terminal control sequences shown", async () => {
		const root = scratch();
		const [host, guest] = await freePorts(2);
		const impostor = Bun.serve({ hostname: "127.0.0.1", port: host as number, fetch: () => Response.json({ rooms: "\u001b[31mRED", guests: "\u001b]0;pwn\u0007", ready: true, uptimeS: 5 }) });
		// the person's key exists, so status really asks the listener for authenticated counts, and gets an invented answer
		const keyDir = path.join(root, ".config", "omp-private-relay", "check");
		fs.mkdirSync(keyDir, { recursive: true, mode: 0o700 });
		fs.writeFileSync(path.join(keyDir, "check.key"), "ab".repeat(32), { mode: 0o600 });
		try {
			const out = io();
			expect(await runCli(["status"], pairEnv(root, host as number, guest as number), out)).toBe(0);
			expect(out.stdout).not.toContain("\u001b");
			expect(out.stdout).not.toContain("RED");
			expect(out.stdout).toContain("does not prove to be this relay");
			const json = io();
			await runCli(["status", "--json"], pairEnv(root, host as number, guest as number), json);
			expect(JSON.parse(json.stdout).relay).toEqual({ running: false, answers: true, proven: false });
		} finally {
			impostor.stop(true);
		}
	});

	test("M5: even a proven listener's numbers are shown only as numbers", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			const out = io();
			await runCli(["status", "--json"], { ...relay.env }, out);
			const report = JSON.parse(out.stdout).relay;
			expect(report.proven).toBe(true);
			expect(typeof report.rooms).toBe("string");
			expect(report.rooms).toMatch(/^\d+$/);
		} finally {
			await relay.stop();
		}
	});

	test("an answer to the proof challenge is read only up to a small bound", async () => {
		const [port] = await freePorts(1);
		// an answer that never ends: a bounded read gives up at once, an unbounded one waits for the request timeout
		const endless = new ReadableStream({ pull: c => void c.enqueue(new TextEncoder().encode("x".repeat(65_536))) });
		const server = Bun.serve({ hostname: "127.0.0.1", port: port as number, fetch: () => new Response(endless) });
		try {
			const t0 = Date.now();
			// a one-second request timeout: a bounded read gives up at once, an unbounded one runs into the timeout
			expect(await proveListener(port as number, Buffer.alloc(32, 1), 1000)).toMatchObject({ ok: false });
			expect(Date.now() - t0, "the answer was not abandoned at its bound").toBeLessThan(700);
		} finally {
			server.stop(true);
		}
	});
});

// ── CLI and install flows ────────────────────────────────────────────────────────────────────────────────────

describe("fresh install flows (review 1 F1, review 2 M6 and H2)", () => {
	const fakeBuild = (root: string): string => {
		const script = path.join(root, "fake-build.sh");
		fs.writeFileSync(script, `#!/bin/bash\nwhile [ $# -gt 0 ]; do case "$1" in --out) out="$2"; shift 2;; *) shift;; esac; done\nmkdir -p "$out"\necho '<!doctype html><title>built</title>' > "$out/index.html"\necho 'js' > "$out/abcd1234.js"\n`, { mode: 0o755 });
		return script;
	};

	test("rebuild-web works on a fresh machine whose data directory does not exist yet", async () => {
		const root = scratch();
		const home = path.join(root, "home");
		fs.mkdirSync(home, { mode: 0o700 });
		const fake = writeFakeOmp(root, { version: "18.18.0" });
		const out = io();
		const code = await runCli(["rebuild-web", "--omp-src", path.join(root, "src"), "--tag", "v18.18.0"], { HOME: home, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" }, out, { buildScript: fakeBuild(root) });
		expect(out.stderr).toBe("");
		expect(code).toBe(0);
		expect(fs.readFileSync(path.join(home, ".local", "share", "omp-private-relay", "web", "current", "index.html"), "utf8")).toContain("built");
		expect(fs.readdirSync(path.join(home, ".local", "share", "omp-private-relay", "web")).sort()).toEqual([".current.install-lock", "current"]); // no scratch left
	});

	test("rebuild-web into a data directory that other accounts can write to is refused, and nothing is changed in it", async () => {
		const root = scratch();
		const home = path.join(root, "home");
		const data = path.join(home, ".local", "share", "omp-private-relay", "web");
		fs.mkdirSync(data, { recursive: true });
		fs.chmodSync(data, 0o777);
		const fake = writeFakeOmp(root, { version: "18.18.0" });
		let thrown = "";
		try {
			await runCli(["rebuild-web", "--omp-src", root, "--tag", "v18.18.0"], { HOME: home, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" }, io(), { buildScript: fakeBuild(root) });
		} catch (err) {
			thrown = String(err);
		}
		expect(thrown).toContain("writable by group or others");
		expect(fs.statSync(data).mode & 0o777).toBe(0o777); // not chmod-ed behind the person's back
	});

	test("H2: init validates the effective configuration before it executes the omp that the file names", async () => {
		const root = scratch();
		const home = path.join(root, "home");
		const dir = path.join(home, ".config", "omp-private-relay");
		fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
		const marker = path.join(root, "executed");
		const bin = path.join(root, "evil");
		fs.writeFileSync(bin, `#!/bin/sh\ntouch '${marker}'\necho omp/18.18.0\n`, { mode: 0o755 });
		fs.writeFileSync(path.join(dir, "config.json"), JSON.stringify({ schemaVersion: 1, omp: { bin }, listen: { hostPort: 80 } }), { mode: 0o600 });
		const out = io();
		expect(await runCli(["init", "--yes", "--skip-web"], { HOME: home, PATH: "/usr/bin:/bin" }, out)).toBe(1);
		expect(out.stderr).toContain("hostPort");
		expect(fs.existsSync(marker)).toBe(false);
	});

	test("H2: a config file in a directory that others can write (not sticky) is refused before it is read", () => {
		const root = scratch();
		const dir = path.join(root, "shared");
		fs.mkdirSync(dir);
		fs.chmodSync(dir, 0o777);
		const file = path.join(dir, "config.json");
		fs.writeFileSync(file, JSON.stringify({ schemaVersion: 1 }), { mode: 0o600 });
		expect(() => readConfigFile(file)).toThrow(/directory is writable by group or others/);
		fs.chmodSync(dir, 0o1777); // a sticky directory (like /tmp) does not let another account replace our file
		expect(readConfigFile(file)).toEqual({ schemaVersion: 1 });
	});

	test("H2: the config is read through one descriptor: a FIFO or a link in its place is refused without blocking", () => {
		const root = scratch();
		const fifo = path.join(root, "fifo.json");
		Bun.spawnSync(["mkfifo", fifo]);
		expect(() => readConfigFile(fifo)).toThrow(/regular file/);
		fs.writeFileSync(path.join(root, "real.json"), JSON.stringify({ schemaVersion: 1 }), { mode: 0o600 });
		fs.symlinkSync(path.join(root, "real.json"), path.join(root, "link.json"));
		expect(() => readConfigFile(path.join(root, "link.json"))).toThrow(/regular file/);
	});

	test("an oversized --asset-file is refused before it is read", async () => {
		const root = scratch();
		const home = path.join(root, "home");
		fs.mkdirSync(home, { mode: 0o700 });
		const fake = writeFakeOmp(root, { version: "18.18.0" });
		const big = path.join(root, "big.tar");
		fs.closeSync(fs.openSync(big, "w"));
		fs.truncateSync(big, 70 * 1024 * 1024);
		const out = io();
		const builds = { schema: 1 as const, repository: "o/r", releaseTag: "t", builds: { "18.18": { ompTag: "v18.18.0", bun: "1", asset: "a.tar", sha256: "0".repeat(64), treeSha256: "0".repeat(64), noticeSha256: "0".repeat(64), bytes: 1, files: 1 } } };
		const [h, g] = await freePorts(2);
		expect(await runCli(["init", "--yes", "--asset-file", big, "--host-port", String(h), "--guest-port", String(g)], { HOME: home, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" }, out, { builds })).toBe(1);
		expect(out.stderr).toContain("cannot read");
	});
});

// ── drop-ins describe the relay that runs (review 2 M4) ─────────────────────────────────────────────────────────

describe("drop-ins select the same configuration (review 2 M4)", () => {
	test("both the service and the proof command carry the absolute config path, and run from a clean environment", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			// a config file that selects the ports and adapter of this relay, in a place that is not the default
			const configFile = path.join(relay.root, "alt-config.json");
			fs.writeFileSync(
				configFile,
				JSON.stringify({ schemaVersion: 1, listen: { hostPort: relay.hostPort + 0, guestPort: relay.guestPort }, auth: { adapter: "token" }, pairing: { dir: relay.pairingDir }, web: { dir: relay.webDir }, checkDir: relay.checkDir }),
				{ mode: 0o600 },
			);
			const env = { HOME: relay.root, OMP_PRIVATE_RELAY_CONFIG: configFile };
			const config = loadConfig(env);
			const bun = process.execPath; // stands for omp: BUN_BE_BUN=1 <bun> is a plain bun
			const services = servicesDropin({ config, ompBin: bun, env });
			const health = healthDropin({ config, ompBin: bun, env });
			const unit = (services.services as Array<{ env: Record<string, string> }>)[0] as { env: Record<string, string> };
			expect(unit.env.OMP_PRIVATE_RELAY_CONFIG).toBe(configFile);
			const proof = (health.checks as Array<{ id: string; argv?: string[] }>).find(c => c.id === "relay-proof")?.argv as string[];
			expect(proof[0]).toBe("/usr/bin/env");
			expect(proof).toContain(`OMP_PRIVATE_RELAY_CONFIG=${configFile}`);
			// run the proof command exactly as the heartbeat would: nothing of this environment but a bare PATH and HOME
			const r = Bun.spawnSync(proof, { env: { PATH: "/usr/bin:/bin", HOME: relay.root }, stdout: "pipe", stderr: "pipe" });
			expect(r.exitCode, r.stderr.toString()).toBe(0);
			expect(r.stdout.toString().trim()).toBe("ok");
			// and the same config file started as the service would start it, from a clean environment, on another port pair
			const [h2, g2] = await freePorts(2);
			const second = path.join(relay.root, "second.json");
			fs.writeFileSync(second, JSON.stringify({ schemaVersion: 1, listen: { hostPort: h2, guestPort: g2 }, pairing: { dir: path.join(relay.root, "p2") }, checkDir: path.join(relay.root, "c2"), web: { dir: relay.webDir } }), { mode: 0o600 });
			const svc = servicesDropin({ config: loadConfig({ HOME: relay.root, OMP_PRIVATE_RELAY_CONFIG: second }), ompBin: bun, env: { HOME: relay.root, OMP_PRIVATE_RELAY_CONFIG: second } });
			const svcEnv = ((svc.services as Array<{ env: Record<string, string> }>)[0] as { env: Record<string, string> }).env;
			const running = Bun.spawn([bun, MAIN], { env: { PATH: "/usr/bin:/bin", HOME: relay.root, BUN_BE_BUN: "1", ...svcEnv }, stdout: "pipe", stderr: "pipe" });
			try {
				const deadline = Date.now() + 8000;
				let up = false;
				while (Date.now() < deadline && !up) up = (await fetch(`http://127.0.0.1:${h2}/healthz`).catch(() => null))?.status === 200;
				expect(up).toBe(true);
			} finally {
				running.kill("SIGTERM");
				await running.exited;
			}
		} finally {
			await relay.stop();
		}
	});

	test("an override set only in the shell would make the drop-ins describe another relay: dropins refuses it", async () => {
		const root = scratch();
		const home = path.join(root, "home");
		fs.mkdirSync(path.join(home, ".config", "omp-private-relay"), { recursive: true, mode: 0o700 });
		fs.writeFileSync(path.join(home, ".config", "omp-private-relay", "config.json"), JSON.stringify({ schemaVersion: 1 }), { mode: 0o600 });
		const fake = writeFakeOmp(root);
		const env = { HOME: home, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin", OMP_PRIVATE_RELAY_AUTH: "tailscale-serve" };
		expect(strayOverrides(env)).toEqual(["OMP_PRIVATE_RELAY_AUTH"]);
		const out = io();
		expect(await runCli(["dropins", "--write"], env, out)).toBe(1);
		expect(out.stderr).toContain("OMP_PRIVATE_RELAY_AUTH");
		expect(fs.existsSync(path.join(home, ".config", "omp-services"))).toBe(false);
		const none = io();
		expect(await runCli(["dropins", "--write"], { ...env, OMP_PRIVATE_RELAY_AUTH: undefined }, none)).toBe(0);
	});

	test("dropins without a config file asks for init instead of describing defaults", async () => {
		const root = scratch();
		const fake = writeFakeOmp(root);
		const out = io();
		expect(await runCli(["dropins"], { HOME: root, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" }, out)).toBe(1);
		expect(out.stderr).toContain("run init first");
	});

	test("review 1 F9: writing a drop-in never changes the mode of a directory that belongs to another tool", () => {
		const root = scratch();
		const env = { HOME: root, XDG_CONFIG_HOME: path.join(root, "cfg") };
		const paths = dropinPaths(env);
		fs.mkdirSync(path.dirname(paths.health), { recursive: true });
		fs.chmodSync(path.dirname(paths.health), 0o755);
		writeDropin(paths.health, { protocol: "health/1" });
		expect(fs.statSync(path.dirname(paths.health)).mode & 0o777).toBe(0o755);
		fs.chmodSync(path.dirname(paths.health), 0o777);
		expect(() => writeDropin(paths.health, {})).toThrow(/writable by group or others/);
	});
});

// ── tailscale adapter: no blocking calls (review 1 F6) ─────────────────────────────────────────────────

describe("tailscale lookup is asynchronous (review 1 F6)", () => {
	test("a request that finds the identity unknown is answered at once, and the lookup finishes in the background", async () => {
		let release: (v: { host: string; login: string }) => void = () => {};
		const pending = new Promise<{ host: string; login: string }>(r => {
			release = r;
		});
		let calls = 0;
		const clock = { t: 0 };
		const auth = createTailscaleAuth({ guestPort: 1, servePort: 8443, owner: "", publicHost: "", now: () => clock.t, lookup: () => (calls++ === 0 ? Promise.resolve(null) : pending) });
		const run = auth.start({ log() {}, dropGuests() {} });
		await auth.settled();
		expect(auth.ready()).toBe(false);
		clock.t = 60_000;
		const t0 = performance.now();
		const verdict = auth.check({ headers: new Headers(), upgrade: false });
		expect(performance.now() - t0).toBeLessThan(50);
		expect(verdict).toMatchObject({ ok: false, status: 503 });
		expect(calls).toBe(2);
		release({ host: "m.example.test", login: "o@example.com" });
		await Bun.sleep(20);
		expect(auth.ready()).toBe(true);
		run.stop();
	});

	test("the real lookup does not block the event loop while the command runs", async () => {
		const root = scratch();
		const slow = path.join(root, "tailscale");
		fs.writeFileSync(slow, `#!/bin/sh\nsleep 1\necho '{"Self":{"DNSName":"m.example.test.","UserID":1},"User":{"1":{"LoginName":"o@example.com"}}}'\n`, { mode: 0o755 });
		let ticks = 0;
		const timer = setInterval(() => ticks++, 20);
		const self = await lookupFromCommand(slow)();
		clearInterval(timer);
		expect(self).toEqual({ host: "m.example.test", login: "o@example.com" });
		expect(ticks).toBeGreaterThan(20); // a blocking call would have allowed none
	});
});

// ── web asset: redirects, notice, interrupted install (review 1 F7 and F8, review 2 L3) ───────────────────────────

describe("web asset (review 1 F7 and F8, review 2 L3)", () => {
	test("F8: plain http to loopback is accepted only as the starting URL, never as the target of a redirect from https", async () => {
		const fetcher = async (u: string) => (u.startsWith("https://") ? new Response(null, { status: 302, headers: { Location: "http://127.0.0.1:9/probe" } }) : new Response("payload"));
		await expect(download("https://example.invalid/a.tar", fetcher)).rejects.toThrow(/only downloaded over https/);
		expect((await download("http://127.0.0.1:9/a.tar", async () => new Response("payload"))).toString()).toBe("payload");
		expect(() => checkAssetUrl("http://127.0.0.1:9/x", false)).toThrow(/https/);
	});

	const HEX_A = "a".repeat(16);
	const HEX_B = "b".repeat(16);
	const BUILD: WebBuild = { ompTag: "v1.0.0", bun: "1", asset: "a", sha256: "", treeSha256: "", noticeSha256: "", bytes: 0, files: 1 };
	const one = (text: string) => new Map([["index.html", Buffer.from(text)]]);
	const listing = (target: string) => fs.readdirSync(path.dirname(target)).sort();

	test("L3/R5: an install interrupted between its two renames is recovered from its own record, and nothing else beside it is touched", () => {
		const root = scratch();
		const target = path.join(root, "web", "current");
		installTree(one("old"), BUILD, target);
		// somebody's own directories whose names look like install leftovers, one named exactly as the previous version named its staging directory: never ours to delete
		const pidNamed = `current.new.${process.pid}`;
		for (const d of ["current.old.mine", "current.new.12345", pidNamed]) {
			fs.mkdirSync(path.join(path.dirname(target), d));
			fs.writeFileSync(path.join(path.dirname(target), d, "keep.txt"), "x");
		}
		// the crash: the record was written, the live copy renamed aside, the new one never arrived
		fs.writeFileSync(path.join(path.dirname(target), ".current.txn.json"), JSON.stringify({ stage: `current.new.${HEX_A}`, old: `current.old.${HEX_A}` }));
		fs.mkdirSync(`${target}.new.${HEX_A}`);
		fs.renameSync(target, `${target}.old.${HEX_A}`);
		recoverInterrupted(target);
		expect(fs.readFileSync(path.join(target, "index.html"), "utf8")).toBe("old");
		expect(listing(target)).toEqual([".current.install-lock", "current", pidNamed, "current.new.12345", "current.old.mine"].sort());
		// an install recovers an interrupted one before it starts, and again leaves unrecorded names alone (the staging name is random, never the pid)
		fs.writeFileSync(path.join(path.dirname(target), ".current.txn.json"), JSON.stringify({ stage: `current.new.${HEX_B}`, old: `current.old.${HEX_B}` }));
		fs.mkdirSync(`${target}.new.${HEX_B}`);
		fs.mkdirSync(`${target}.old.${HEX_B}`);
		installTree(one("again"), BUILD, target);
		expect(fs.readFileSync(path.join(target, "index.html"), "utf8")).toBe("again");
		expect(listing(target)).toEqual([".current.install-lock", "current", pidNamed, "current.new.12345", "current.old.mine"].sort());
		expect(fs.readFileSync(path.join(path.dirname(target), pidNamed, "keep.txt"), "utf8")).toBe("x");
	});

	test("R5: a record that is damaged, foreign or unreadable stops the install and removes nothing; it is never skipped", () => {
		const root = scratch();
		const target = path.join(root, "web", "current");
		installTree(one("old"), BUILD, target);
		const parent = path.dirname(target);
		const victim = path.join(root, "victim"); // what a record naming "../victim" would delete
		fs.mkdirSync(victim);
		fs.mkdirSync(`${target}.old.mine`);
		const record = path.join(parent, ".current.txn.json");
		for (const [content, message] of [
			["{not json", /damaged/],
			[JSON.stringify({ stage: "../victim", old: "current.old.mine" }), /does not name this install/],
			[JSON.stringify({ stage: `current.new.${HEX_A}`, old: "current.old.mine" }), /does not name this install/],
			[JSON.stringify(["current.new", "current.old"]), /does not name this install/],
		] as const) {
			fs.writeFileSync(record, content);
			expect(() => installTree(one("x"), BUILD, target), content).toThrow(message);
			expect(() => recoverInterrupted(target), content).toThrow(message);
			expect(fs.existsSync(victim) && fs.existsSync(`${target}.old.mine`)).toBe(true);
			expect(fs.readFileSync(path.join(target, "index.html"), "utf8")).toBe("old");
		}
		fs.rmSync(record);
		fs.mkdirSync(record); // present but not a file we can read
		expect(() => installTree(one("x"), BUILD, target)).toThrow(/cannot be read/);
	});

	const CHILD = path.join(import.meta.dir, "support", "installer-child.ts");

	test("R5: an installer that holds the lock, with nothing yet written to the lock file, is waited for, not taken over", async () => {
		const root = scratch();
		const target = path.join(root, "web", "current");
		fs.mkdirSync(path.dirname(target), { mode: 0o700 });
		const holder = Bun.spawn([process.execPath, CHILD, "hold", target, "1500"], { stdout: "pipe" });
		const reader = holder.stdout.getReader();
		expect(new TextDecoder().decode((await reader.read()).value)).toContain("held"); // locked, lock file still empty
		const t0 = Date.now();
		installTree(one("x"), BUILD, target);
		expect(Date.now() - t0).toBeGreaterThan(900);
		await holder.exited;
		expect(fs.readFileSync(path.join(target, "index.html"), "utf8")).toBe("x");
		// a dead holder leaves nothing to take over: the lock is released with its process
		const lock = path.join(path.dirname(target), ".current.install-lock");
		expect(fs.existsSync(lock)).toBe(true);
		installTree(one("y"), BUILD, target);
		expect(fs.readFileSync(path.join(target, "index.html"), "utf8")).toBe("y");
	}, 20_000);

	test("R5: two installers racing in separate processes serialise: no error, no leftover, one complete result", async () => {
		const root = scratch();
		const target = path.join(root, "web", "current");
		fs.mkdirSync(path.dirname(target), { mode: 0o700 });
		const go = path.join(root, "go");
		const children = ["A", "B", "C"].map(tag => Bun.spawn([process.execPath, CHILD, "install", target, tag, "15", go], { stdout: "pipe", stderr: "pipe" }));
		await Bun.sleep(300); // all started and waiting for the barrier
		fs.writeFileSync(go, "");
		const results = await Promise.all(children.map(async c => ({ code: await c.exited, err: await new Response(c.stderr).text() })));
		expect(results.map(r => r.code), results.map(r => r.err).join("\n")).toEqual([0, 0, 0]);
		expect(listing(target)).toEqual([".current.install-lock", "current"]);
		const state = inspectInstalled(target);
		expect(state.state).toBe("ok");
		expect(fs.readFileSync(path.join(target, "index.html"), "utf8")).toMatch(/^[ABC]\d+$/);
	}, 60_000);

	test("R5: a lock file that is not a private plain file of ours is refused", () => {
		const root = scratch();
		const target = path.join(root, "web", "current");
		fs.mkdirSync(path.dirname(target), { mode: 0o700 });
		const lock = path.join(path.dirname(target), ".current.install-lock");
		fs.writeFileSync(lock, "", { mode: 0o666 });
		fs.chmodSync(lock, 0o666);
		expect(() => installTree(one("x"), BUILD, target)).toThrow(/private plain file/);
		fs.rmSync(lock);
		fs.symlinkSync(path.join(root, "elsewhere"), lock);
		expect(() => installTree(one("x"), BUILD, target)).toThrow(/cannot be opened/);
	});

	test("F7: an archive whose content matches but whose NOTICE differs from its pin is refused", () => {
		const files = new Map<string, Buffer>([["index.html", Buffer.from("<title>x</title>")], ["NOTICE", Buffer.from("the real notice")]]);
		const tar = packTar(files);
		const build: WebBuild = { ompTag: "v1.0.0", bun: "1", asset: "a", sha256: sha256(tar), treeSha256: treeHash(files), noticeSha256: sha256(Buffer.from("the real notice")), bytes: tar.length, files: 2 };
		expect(verifyAsset(tar, build).size).toBe(2);
		const swapped = packTar(new Map([...files, ["NOTICE", Buffer.from("a different notice")]]));
		expect(() => verifyAsset(swapped, { ...build, sha256: sha256(swapped) })).toThrow(/NOTICE/);
		const none = packTar(new Map([["index.html", Buffer.from("<title>x</title>")]]));
		expect(() => verifyAsset(none, { ...build, sha256: sha256(none), treeSha256: treeHash(new Map([["index.html", Buffer.from("<title>x</title>")]])) })).toThrow(/NOTICE/);
	});

	test("F7: the NOTICE file is pinned on its own and the checked-in NOTICE is the one the pin names", () => {
		const builds = JSON.parse(fs.readFileSync(path.join(import.meta.dir, "..", "..", "web-builds.json"), "utf8")) as { builds: Record<string, WebBuild> };
		const notice = fs.readFileSync(path.join(import.meta.dir, "..", "..", "NOTICE"));
		for (const b of Object.values(builds.builds)) expect(b.noticeSha256, "editing NOTICE needs scripts/release-web.sh to be run again").toBe(sha256(notice));
	});
});

// ── doctor and the pairing store (review 2 L1, L2, L4) ───────────────────────────────────────────────────────

describe("doctor and store strictness (review 2 L1, L2, L4)", () => {
	test("L2: doctor does not call a pairing store healthy that the relay would refuse", async () => {
		const root = scratch();
		const real = path.join(root, "real");
		fs.mkdirSync(real, { mode: 0o700 });
		const link = path.join(root, "link");
		fs.symlinkSync(real, link);
		const findings = await runDoctor({ HOME: root, PATH: "/usr/bin:/bin", OMP_PRIVATE_RELAY_PAIRING_DIR: link });
		const pairing = findings.find(f => f.id === "pairing");
		expect(pairing?.level).toBe("fail");
		expect(pairing?.message).toContain("store-symlink");
	});

	test("a fresh own store that does not exist yet is a warning with the fix, not a failure; a missing shared store still fails", async () => {
		const root = scratch();
		const own = await runDoctor({ HOME: root, PATH: "/usr/bin:/bin", OMP_PRIVATE_RELAY_PAIRING_DIR: path.join(root, "absent-own") });
		const ownPairing = own.find(f => f.id === "pairing");
		expect(ownPairing?.level).toBe("warn");
		expect(ownPairing?.message).toContain("no browser is paired yet");
		expect(ownPairing?.fix).toContain("omp-private-relay pair");
		const shared = await runDoctor({ HOME: root, PATH: "/usr/bin:/bin", OMP_PRIVATE_RELAY_PAIRING_MODE: "shared", OMP_PRIVATE_RELAY_PAIRING_DIR: path.join(root, "absent-shared") });
		const sharedPairing = shared.find(f => f.id === "pairing");
		expect(sharedPairing?.level).toBe("fail");
		expect(sharedPairing?.message).toContain("store-missing");
	});

	test("L2: the runtime finding comes from the real version check", async () => {
		const findings = await runDoctor({ HOME: scratch(), PATH: "/usr/bin:/bin" });
		expect(findings.find(f => f.id === "runtime")?.level).toBe("ok"); // this suite runs on a supported Bun
	});

	test("L1: a zero-byte devices.json is a truncated store: the writer refuses it and leaves it alone", async () => {
		const dir = path.join(scratch(), "store");
		fs.mkdirSync(dir, { mode: 0o700 });
		fs.writeFileSync(path.join(dir, "devices.json"), "", { mode: 0o600 });
		const w = new PairingWriter(dir);
		const token = await w.createToken();
		await expect(w.redeem(token, "x")).rejects.toThrow(/not valid JSON/);
		expect(fs.readFileSync(path.join(dir, "devices.json"), "utf8")).toBe("");
		fs.writeFileSync(path.join(dir, "pending.json"), "", { mode: 0o600 });
		await expect(w.createToken()).rejects.toThrow(/not valid JSON/);
	});

	test("L4: an existing lock file that others can read or write is refused", async () => {
		const dir = path.join(scratch(), "store");
		fs.mkdirSync(dir, { mode: 0o700 });
		fs.writeFileSync(path.join(dir, ".lock"), "", { mode: 0o666 });
		fs.chmodSync(path.join(dir, ".lock"), 0o666);
		await expect(new PairingWriter(dir).createToken()).rejects.toThrow(/lock file/);
	});

	test("check passes for the proven relay and cookie names are not logged (sanity for the fixes above)", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			expect(relay.log()).not.toContain(COOKIE_NAME);
		} finally {
			await relay.stop();
		}
	});
});

// ── verification pass: R1 (served paths), R4 (authenticated stats), R6 (resolved roots), R8 (installed NOTICE) ─────────

describe("served and executed paths (verification R1)", () => {
	test("installing into a group-writable parent is refused, not accepted because the mode is only 0775", () => {
		const root = scratch();
		const parent = path.join(root, "web");
		fs.mkdirSync(parent, { mode: 0o700 });
		fs.chmodSync(parent, 0o775);
		const build: WebBuild = { ompTag: "v1.0.0", bun: "1", asset: "a", sha256: "", treeSha256: "", noticeSha256: "", bytes: 0, files: 1 };
		expect(() => installTree(new Map([["index.html", Buffer.from("x")]]), build, path.join(parent, "current"))).toThrow(/writable by group or others/);
	});

	test("directories this code creates are 0700 whatever the umask says (a umask of 002 is common)", () => {
		const root = scratch();
		const before = process.umask(0o002);
		try {
			ensurePrivateDir(path.join(root, "x", "y", "z"));
			for (const d of ["x", "x/y", "x/y/z"]) expect(fs.statSync(path.join(root, d)).mode & 0o777, d).toBe(0o700);
		} finally {
			process.umask(before);
		}
	});

	test("the relay does not serve its client from a tree another account could replace, and doctor says so", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			fs.chmodSync(relay.root, 0o777); // an ancestor of the web directory that any account can write
			await Bun.sleep(5_100); // the relay re-checks at most every five seconds; the first request has not been made yet
			const r = await rawRequest(relay.guestPort, "/", guestHeaders(relay, { Origin: null }));
			expect(r.status).toBe(503);
			expect(r.body).toContain("not safe to serve from");
			const doctor = io();
			await runCli(["doctor", "--json"], { ...relay.env, PATH: "/usr/bin:/bin" }, doctor);
			expect((JSON.parse(doctor.stdout) as Array<{ id: string; level: string }>).find(f => f.id === "web-dir-safe")?.level).toBe("fail");
			fs.chmodSync(relay.root, 0o700);
			await Bun.sleep(5_100);
			expect((await rawRequest(relay.guestPort, "/", guestHeaders(relay, { Origin: null }))).status).toBe(200);
		} finally {
			fs.chmodSync(relay.root, 0o700);
			await relay.stop();
		}
	}, 30_000);

	test("a configured check directory that is open to others is refused, not chmod-ed", async () => {
		const root = scratch();
		const dir = path.join(root, "check");
		fs.mkdirSync(dir, { mode: 0o750 });
		fs.chmodSync(dir, 0o750);
		expect(() => ensurePrivateDir(dir, { refuseOpen: true })).toThrow(/must be mode 700/);
		expect(fs.statSync(dir).mode & 0o777).toBe(0o750);
	});

	test("--asset-file must be a regular file: a FIFO or a link is refused without blocking", async () => {
		const root = scratch();
		const home = path.join(root, "home");
		fs.mkdirSync(home, { mode: 0o700 });
		const fake = writeFakeOmp(root, { version: "18.18.0" });
		const fifo = path.join(root, "f.tar");
		Bun.spawnSync(["mkfifo", fifo]);
		const builds = { schema: 1 as const, repository: "o/r", releaseTag: "t", builds: { "18.18": { ompTag: "v18.18.0", bun: "1", asset: "a.tar", sha256: "0".repeat(64), treeSha256: "0".repeat(64), noticeSha256: "0".repeat(64), bytes: 1, files: 1 } } };
		const [h, g] = await freePorts(2);
		const out = io();
		expect(await runCli(["init", "--yes", "--asset-file", fifo, "--host-port", String(h), "--guest-port", String(g)], { HOME: home, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" }, out, { builds })).toBe(1);
		expect(out.stderr).toContain("cannot read");
	});
});

describe("status believes only authenticated counts (verification R4)", () => {
	const keyOf = (relay: Relay) => {
		const k = readCheckKey(relay.checkDir);
		if (!k.ok) throw new Error(k.reason);
		return k.key;
	};

	test("the real relay's counts verify; a look-alike that holds the key for the proof but signs the counts wrongly does not", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			const ok = await fetchProvenStats(relay.hostPort, keyOf(relay));
			expect(ok.ok && typeof ok.stats.rooms).toBe("number");
			// a program that replaced the relay after a proof: it answers the proof (it was given the key) but cannot sign counts it invents
			const { createHmac } = await import("node:crypto");
			const key = keyOf(relay);
			const [port] = await freePorts(1);
			const fake = Bun.serve({
				hostname: "127.0.0.1",
				port: port as number,
				fetch(req) {
					const url = new URL(req.url);
					const nonce = url.searchParams.get("nonce") as string;
					if (url.pathname === "/__relay_check") return Response.json({ mac: createHmac("sha256", key).update(`omp-private-relay-check/1:${port}:${nonce}`).digest("hex") });
					return Response.json({ body: JSON.stringify({ rooms: 99, guests: 99 }), mac: "0".repeat(64) });
				},
			});
			try {
				expect((await proveListener(port as number, key)).ok).toBe(true); // the old design would have stopped here
				expect(await fetchProvenStats(port as number, key)).toMatchObject({ ok: false });
			} finally {
				fake.stop(true);
			}
		} finally {
			await relay.stop();
		}
	});

	test("an endless stats answer is abandoned at 64 KiB, not buffered", async () => {
		const [port] = await freePorts(1);
		const endless = new ReadableStream({ pull: c => void c.enqueue(new TextEncoder().encode("x".repeat(65_536))) });
		const server = Bun.serve({ hostname: "127.0.0.1", port: port as number, fetch: () => new Response(endless) });
		try {
			const t0 = Date.now();
			expect(await fetchProvenStats(port as number, Buffer.alloc(32, 1), 1000)).toMatchObject({ ok: false, reason: "answer too large" });
			expect(Date.now() - t0, "the answer was not abandoned at its bound").toBeLessThan(700);
		} finally {
			server.stop(true);
		}
	});

	test("the signed-stats endpoint answers only a direct loopback client with a good nonce", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			const n = "ab".repeat(16);
			expect(await rawStatus(relay.hostPort, `/__relay_stats?nonce=${n}`, { "X-Forwarded-For": "192.0.2.1" })).toBe(403);
			expect(await rawStatus(relay.hostPort, `/__relay_stats?nonce=${n}`, { Origin: "http://evil.example" })).toBe(403);
			expect(await rawStatus(relay.hostPort, "/__relay_stats?nonce=zz", {})).toBe(400);
			expect(await rawStatus(relay.guestPort, `/__relay_stats?nonce=${n}`, guestHeaders(relay))).not.toBe(200); // not on the guest listener
		} finally {
			await relay.stop();
		}
	});
});

describe("resolved roots in the drop-ins (verification R6)", () => {
	test("with no XDG variables when the drop-ins are written, the proof command still finds the relay when its own environment carries other ones", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			// a relay whose paths all come from the DEFAULT locations under HOME (no XDG variables anywhere)
			const home = path.join(relay.root, "h6");
			fs.mkdirSync(home, { mode: 0o700 });
			const [host, guest] = await freePorts(2);
			const cfgDir = path.join(home, ".config", "omp-private-relay");
			fs.mkdirSync(cfgDir, { recursive: true, mode: 0o700 });
			const configFile = path.join(cfgDir, "config.json");
			fs.writeFileSync(configFile, JSON.stringify({ schemaVersion: 1, listen: { hostPort: host, guestPort: guest }, web: { dir: relay.webDir } }), { mode: 0o600 });
			const env = { HOME: home };
			const config = loadConfig(env);
			const health = healthDropin({ config, ompBin: process.execPath, env });
			const svc = servicesDropin({ config, ompBin: process.execPath, env });
			const svcEnv = ((svc.services as Array<{ env: Record<string, string> }>)[0] as { env: Record<string, string> }).env;
			expect(svcEnv.XDG_CONFIG_HOME).toBe(path.join(home, ".config"));
			expect(svcEnv.XDG_DATA_HOME).toBe(path.join(home, ".local", "share"));
			const running = Bun.spawn([process.execPath, MAIN], { env: { PATH: "/usr/bin:/bin", HOME: home, BUN_BE_BUN: "1", ...svcEnv }, stdout: "pipe", stderr: "pipe" });
			try {
				const deadline = Date.now() + 8000;
				while (Date.now() < deadline && (await fetch(`http://127.0.0.1:${host}/healthz`).catch(() => null))?.status !== 200) await Bun.sleep(100);
				const proof = (health.checks as Array<{ id: string; argv?: string[] }>).find(c => c.id === "relay-proof")?.argv as string[];
				// the manager's environment has XDG variables of its own that point elsewhere
				const r = Bun.spawnSync(proof, { env: { PATH: "/usr/bin:/bin", HOME: home, XDG_CONFIG_HOME: path.join(relay.root, "elsewhere"), XDG_DATA_HOME: path.join(relay.root, "elsewhere2") }, stdout: "pipe", stderr: "pipe" });
				expect(r.exitCode, r.stderr.toString()).toBe(0);
				expect(r.stdout.toString().trim()).toBe("ok");
			} finally {
				running.kill("SIGTERM");
				await running.exited;
			}
		} finally {
			await relay.stop();
		}
	});
});

describe("installed NOTICE (verification R8)", () => {
	const mk = (notice: string): { files: Map<string, Buffer>; build: WebBuild } => {
		const files = new Map<string, Buffer>([["index.html", Buffer.from("<title>x</title>")], ["NOTICE", Buffer.from(notice)]]);
		const tar = packTar(files);
		return { files, build: { ompTag: "v1.0.0", bun: "1", asset: "a", sha256: sha256(tar), treeSha256: treeHash(files), noticeSha256: sha256(Buffer.from(notice)), bytes: tar.length, files: 2 } };
	};

	test("a deleted or edited installed NOTICE is noticed, though the content hash does not cover it", () => {
		const root = scratch();
		const target = path.join(root, "web", "current");
		const { files, build } = mk("the notice");
		installTree(files, build, target);
		expect(inspectInstalled(target).state).toBe("ok");
		fs.appendFileSync(path.join(target, "NOTICE"), " edited");
		expect(inspectInstalled(target).state).toBe("modified");
		fs.rmSync(path.join(target, "NOTICE"));
		expect(inspectInstalled(target).state).toBe("modified");
	});

	test("a release that changes only NOTICE is installed over an up-to-date-looking copy", async () => {
		const root = scratch();
		const home = path.join(root, "home");
		fs.mkdirSync(home, { mode: 0o700 });
		const fake = writeFakeOmp(root, { version: "18.18.0" });
		const [h, g] = await freePorts(2);
		const args = ["init", "--yes", "--host-port", String(h), "--guest-port", String(g)];
		const env = { HOME: home, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" };
		const first = mk("notice one");
		const second = mk("notice two");
		const wrap = (m: ReturnType<typeof mk>) => ({ schema: 1 as const, repository: "o/r", releaseTag: "t", builds: { "18.18": { ...m.build, ompTag: "v18.18.0" } } });
		const tarOf = (m: ReturnType<typeof mk>) => new Response(new Uint8Array(packTar(m.files)));
		expect(await runCli(args, env, io(), { builds: wrap(first), fetcher: async () => tarOf(first) })).toBe(0);
		const out = io();
		expect(await runCli(args, env, out, { builds: wrap(second), fetcher: async () => tarOf(second) })).toBe(0);
		expect(out.stdout).not.toContain("already installed");
		expect(fs.readFileSync(path.join(home, ".local", "share", "omp-private-relay", "web", "current", "NOTICE"), "utf8")).toBe("notice two");
	});
});

// ── second verification pass ───────────────────────────────────────────────────────────────────

describe("the web tree is read only after its path is approved (second verification: 2 and 5)", () => {
	const guestGet = (relay: Relay, p = "/") => rawRequest(relay.guestPort, p, guestHeaders(relay, { Origin: null }));

	test("a web directory that is a link into a tree another account can write to is refused at once, and its content is never served", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			const shared = path.join(relay.root, "shared");
			fs.mkdirSync(shared);
			fs.chmodSync(shared, 0o777);
			const evil = path.join(shared, "web");
			fs.mkdirSync(evil, { mode: 0o700 });
			fs.writeFileSync(path.join(evil, "index.html"), "<title>EVIL</title>");
			fs.renameSync(relay.webDir, path.join(relay.root, "web.real"));
			fs.symlinkSync(evil, relay.webDir);
			const r = await guestGet(relay);
			expect(r.status).toBe(503);
			expect(r.body).not.toContain("EVIL");
			expect(r.body).toContain("not safe to serve from");
			// the same link into a tree nobody else can write is followed and served
			fs.rmSync(relay.webDir);
			fs.symlinkSync(path.join(relay.root, "web.real"), relay.webDir);
			await Bun.sleep(5_100); // a refusal is remembered for a few seconds
			const ok = await guestGet(relay);
			expect([ok.status, ok.body.includes("window.x=1")]).toEqual([200, true]);
		} finally {
			await relay.stop();
		}
	}, 30_000);

	test("an index that is a FIFO or a directory neither blocks the relay nor turns a 503 into a failure; health and error pages stay up", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			const index = path.join(relay.webDir, "index.html");
			// a directory first: a read of it fails at once, so a relay that reads the index fails here and does not hang on the FIFO below
			fs.rmSync(index);
			fs.mkdirSync(index);
			expect((await guestGet(relay)).status).toBe(503);
			expect((await fetch(`http://127.0.0.1:${relay.hostPort}/healthz`)).status).toBe(200);
			fs.rmdirSync(index);
			Bun.spawnSync(["mkfifo", index]);
			for (const p of ["/", "/anything", "/abcd1234.js"]) {
				const r = await Promise.race([guestGet(relay, p), Bun.sleep(3_000).then(() => null)]);
				expect(r, `${p} did not answer (the index was read)`).not.toBeNull();
				expect(r?.status).toBe(503);
			}
			expect((await fetch(`http://127.0.0.1:${relay.hostPort}/healthz`)).status).toBe(200);
		} finally {
			await relay.stop();
		}
	}, 30_000);

	test("an unsafe tree answers with a fixed page: its index is not opened even when it is a FIFO", async () => {
		const relay = await startRelay({ adapter: "token" });
		try {
			await Bun.sleep(5_100); // the approval cached at start-up expires
			fs.chmodSync(relay.root, 0o777);
			const index = path.join(relay.webDir, "index.html");
			fs.rmSync(index);
			Bun.spawnSync(["mkfifo", index]);
			const r = await Promise.race([guestGet(relay), Bun.sleep(3_000).then(() => null)]);
			expect(r, "the unsafe tree's index was opened").not.toBeNull();
			expect(r?.status).toBe(503);
			expect(r?.body).toContain("not safe to serve from");
			expect((await rawRequest(relay.hostPort, "/healthz", {})).status).toBe(200);
		} finally {
			fs.chmodSync(relay.root, 0o700);
			await relay.stop();
		}
	}, 30_000);
});

describe("the bound on a signed answer is in bytes (second verification: 7)", () => {
	const streamOf = (...chunks: Uint8Array[]) => new Response(new ReadableStream({ start: c => { for (const x of chunks) c.enqueue(x); c.close(); } }));

	test("bytes, not characters: a multibyte answer past the bound is refused though it has fewer characters than the bound", async () => {
		const text = "é".repeat(600); // 600 characters, 1200 bytes
		expect(await readLimited(streamOf(Buffer.from(text)), 1024)).toBeNull();
		expect(await readLimited(streamOf(Buffer.from(text)), 1200)).toBe(text);
	});

	test("the boundary is exact, and an over-long chunk is not kept", async () => {
		expect(await readLimited(streamOf(Buffer.alloc(1024, 97)), 1024)).toHaveLength(1024);
		expect(await readLimited(streamOf(Buffer.alloc(1025, 97)), 1024)).toBeNull();
		expect(await readLimited(streamOf(Buffer.alloc(1000, 97), Buffer.alloc(25, 97)), 1024)).toBeNull();
	});

	test("a character split across two chunks is decoded whole", async () => {
		const bytes = Buffer.from("a€b𝄞c"); // 3-byte and 4-byte characters
		for (let cut = 1; cut < bytes.length; cut++) expect(await readLimited(streamOf(bytes.subarray(0, cut), bytes.subarray(cut)), 1024), `cut at ${cut}`).toBe("a€b𝄞c");
	});

	test("a descriptor is read up to its bound and no further, whatever its size was when measured", () => {
		const root = scratch();
		const file = path.join(root, "f");
		fs.writeFileSync(file, Buffer.alloc(100_000, 1));
		const open = () => fs.openSync(file, "r");
		const exact = open();
		const over = open();
		try {
			expect(readFdBounded(exact, 100_000)?.length).toBe(100_000);
			expect(readFdBounded(over, 99_999)).toBeNull();
		} finally {
			fs.closeSync(exact);
			fs.closeSync(over);
		}
	});
});

describe("an install recorded by an older version is not certified (second verification: 8)", () => {
	test("without a NOTICE hash the state is legacy, status says not verified, doctor fails it, and init installs it again", async () => {
		const root = scratch();
		const files = new Map<string, Buffer>([["index.html", Buffer.from("<title>x</title>")], ["NOTICE", Buffer.from("notice")]]);
		const tar = packTar(files);
		const build: WebBuild = { ompTag: "v18.18.0", bun: "1", asset: "a", sha256: sha256(tar), treeSha256: treeHash(files), noticeSha256: sha256(Buffer.from("notice")), bytes: tar.length, files: 2 };
		const dir = path.join(root, "web", "current");
		installTree(files, build, dir);
		const record = path.join(dir, ".omp-private-relay-build.json");
		const meta = JSON.parse(fs.readFileSync(record, "utf8")) as Record<string, unknown>;
		delete meta.noticeSha256;
		fs.writeFileSync(record, JSON.stringify(meta));
		expect(inspectInstalled(dir).state).toBe("legacy");
		fs.rmSync(path.join(dir, "NOTICE")); // nothing about NOTICE can be certified, whatever it holds
		expect(inspectInstalled(dir).state).toBe("legacy");
		fs.writeFileSync(path.join(dir, "NOTICE"), "notice");

		const relay = await startRelay({ adapter: "token" });
		try {
			const env = { ...relay.env, OMP_PRIVATE_RELAY_WEB_DIR: dir, PATH: "/usr/bin:/bin" };
			const status = io();
			await runCli(["status"], env, status);
			expect(status.stdout).toContain("not verified");
			expect(status.stdout).not.toContain("verified\n");
			const doctor = io();
			await runCli(["doctor", "--json"], env, doctor);
			expect((JSON.parse(doctor.stdout) as Array<{ id: string; level: string }>).find(f => f.id === "web")?.level).toBe("fail");
		} finally {
			await relay.stop();
		}
	}, 30_000);
});
