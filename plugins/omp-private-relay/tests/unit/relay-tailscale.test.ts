/**
 * The relay with the `tailscale-serve` adapter, against a real relay process on free ports: the auth matrix on HTTP and
 * upgrades, the room contract, revocation, the pairing store failing closed, and the identity not being known yet.
 * These drive real sockets, so waits are on the wall clock: revocation re-checks and the 4401 cut are timers in the relay.
 */
import { createHmac } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import {
	closedWithin,
	COOKIE_NAME,
	frame,
	guestHeaders,
	guestPeer,
	hostPeer,
	makeRoomId,
	OWNER,
	PUBLIC_HOST,
	type Relay,
	rawGuest,
	rawStatus,
	relayCloses,
	SERVE_PORT,
	startRelay,
	stats,
	waitUntil,
} from "./support/harness";

const MiB = 1024 * 1024;

describe("tailscale-serve adapter", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ env: { OMP_PRIVATE_RELAY_RESTART_GRACE_MS: "0", OMP_PRIVATE_RELAY_RECHECK_MS: "2000" } });
	});
	afterAll(async () => relay.stop());

	describe("guest listener auth matrix", () => {
		const room = "AAAAAAAAAAAAAAAAAAAAAA";
		const other = `https://${PUBLIC_HOST}:9999`;
		type Case = [string, number, () => [string, Record<string, string>, { method?: string; upgrade?: boolean }]];
		const cases: Case[] = [
			["GET / paired", 200, () => ["/", guestHeaders(relay, { Origin: null }), {}]],
			["GET / paired, same Origin", 200, () => ["/", guestHeaders(relay), {}]],
			["GET / no cookie", 401, () => ["/", guestHeaders(relay, { Cookie: null, Origin: null }), {}]],
			["GET / unknown cookie", 401, () => ["/", guestHeaders(relay, { Cookie: `${COOKIE_NAME}=nope`, Origin: null }), {}]],
			["GET / paired cookie under another name", 401, () => ["/", guestHeaders(relay, { Cookie: `omp_other=${relay.cookie}`, Origin: null }), {}]],
			["GET / a wrong cookie first, the paired one second", 200, () => ["/", guestHeaders(relay, { Cookie: `${COOKIE_NAME}=nope; ${COOKIE_NAME}=${relay.cookie}`, Origin: null }), {}]],
			["GET / an over-long cookie value", 401, () => ["/", guestHeaders(relay, { Cookie: `${COOKIE_NAME}=${"a".repeat(600)}`, Origin: null }), {}]],
			["GET manifest without cookie (browsers omit it)", 200, () => ["/manifest.webmanifest", guestHeaders(relay, { Cookie: null, Origin: null }), {}]],
			["GET icon without cookie", 200, () => ["/favicon-180x180.png", guestHeaders(relay, { Cookie: null, Origin: null }), {}]],
			["GET index.html by name without cookie", 401, () => ["/index.html", guestHeaders(relay, { Cookie: null, Origin: null }), {}]],
			["GET JS bundle without cookie", 401, () => ["/abcd1234.js", guestHeaders(relay, { Cookie: null, Origin: null }), {}]],
			["GET manifest, non-owner login", 403, () => ["/manifest.webmanifest", guestHeaders(relay, { Cookie: null, Origin: null, "Tailscale-User-Login": "alice@example.com" }), {}]],
			["GET / foreign Origin", 403, () => ["/", guestHeaders(relay, { Origin: "https://evil.example" }), {}]],
			["GET / Origin of another port of the same machine", 403, () => ["/", guestHeaders(relay, { Origin: other }), {}]],
			["GET / Sec-Fetch-Site cross-site", 403, () => ["/", guestHeaders(relay, { Origin: null, "Sec-Fetch-Site": "cross-site" }), {}]],
			["GET / no Tailscale login", 403, () => ["/", guestHeaders(relay, { "Tailscale-User-Login": null, Origin: null }), {}]],
			["GET / non-owner login", 403, () => ["/", guestHeaders(relay, { "Tailscale-User-Login": "alice@example.com", Origin: null }), {}]],
			["GET / via Funnel", 403, () => ["/", guestHeaders(relay, { "Tailscale-Funnel-Request": "?1", Origin: null }), {}]],
			["GET / wrong Host (DNS rebinding)", 403, () => ["/", guestHeaders(relay, { Host: `evil.example:${SERVE_PORT}`, Origin: null }), {}]],
			["POST / paired", 405, () => ["/", guestHeaders(relay), { method: "POST" }]],
			["GET /r/<room> without upgrade", 426, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Origin: null }), {}]],
			// Sentinels exist (harness): <root>/secret.txt just outside the web root, web/.hidden inside it. Plain "/../x" is
			// normalised away by URL parsing, so the encoded forms are what reach the guard.
			["GET /..%2fsecret.txt (encoded traversal)", 404, () => ["/..%2fsecret.txt", guestHeaders(relay, { Origin: null }), {}]],
			["GET /%2e%2e%2fsecret.txt (encoded traversal)", 404, () => ["/%2e%2e%2fsecret.txt", guestHeaders(relay, { Origin: null }), {}]],
			["GET /%2e%2e/secret.txt", 404, () => ["/%2e%2e/secret.txt", guestHeaders(relay, { Origin: null }), {}]],
			["GET /.hidden (dotfile that exists)", 404, () => ["/.hidden", guestHeaders(relay, { Origin: null }), {}]],
			["GET /%2ehidden (encoded dotfile)", 404, () => ["/%2ehidden", guestHeaders(relay, { Origin: null }), {}]],
			["GET malformed percent-encoding", 400, () => ["/%E0%A4%A", guestHeaders(relay, { Origin: null }), {}]],
			["GET /__relay_check through Serve", 403, () => [`/__relay_check?nonce=${"a".repeat(32)}`, guestHeaders(relay, { Origin: null }), {}]],
			["WS guest paired + Origin", 101, () => [`/r/${room}?role=guest`, guestHeaders(relay), { upgrade: true }]],
			["WS guest no cookie", 401, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Cookie: null }), { upgrade: true }]],
			["WS guest unknown cookie", 401, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Cookie: `${COOKIE_NAME}=nope` }), { upgrade: true }]],
			["WS guest no Origin", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Origin: null }), { upgrade: true }]],
			["WS guest foreign Origin", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Origin: "https://evil.example" }), { upgrade: true }]],
			["WS guest non-owner login", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { "Tailscale-User-Login": "alice@example.com" }), { upgrade: true }]],
			["WS role=host on guest listener", 403, () => [`/r/${room}?role=host`, guestHeaders(relay), { upgrade: true }]],
			["WS guest wrong Host (DNS rebinding)", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Host: `evil.example:${SERVE_PORT}` }), { upgrade: true }]],
			["WS guest via Funnel", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { "Tailscale-Funnel-Request": "?1" }), { upgrade: true }]],
			["WS guest Sec-Fetch-Site cross-site", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { "Sec-Fetch-Site": "cross-site" }), { upgrade: true }]],
			["WS guest Origin of another port", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Origin: other }), { upgrade: true }]],
			["WS guest no Tailscale login", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { "Tailscale-User-Login": null }), { upgrade: true }]],
			["WS bad room id", 404, () => ["/r/short?role=guest", guestHeaders(relay), { upgrade: true }]],
		];
		for (const [name, want, build] of cases) {
			test(name, async () => {
				const [target, headers, opts] = build();
				expect(await rawStatus(relay.guestPort, target, headers, opts)).toBe(want);
			});
		}

		test("static responses carry CSP and no-referrer, and the CSP names this machine's wss origin", async () => {
			const r = await fetch(`http://127.0.0.1:${relay.guestPort}/`, { headers: guestHeaders(relay, { Origin: null }) });
			expect(r.status).toBe(200);
			const csp = r.headers.get("content-security-policy") ?? "";
			expect(csp).toContain(`connect-src 'self' wss://${PUBLIC_HOST}:${SERVE_PORT}`);
			expect(csp).toContain("'sha256-");
			expect(csp).toContain("frame-ancestors 'none'");
			expect(r.headers.get("referrer-policy")).toBe("no-referrer");
			expect(r.headers.get("cross-origin-opener-policy")).toBe("same-origin");
		});

		test("the unpaired page says how to pair, in terms of this tool", async () => {
			const r = await fetch(`http://127.0.0.1:${relay.guestPort}/`, { headers: guestHeaders(relay, { Cookie: null, Origin: null }) });
			expect(r.status).toBe(401);
			expect(await r.text()).toContain("omp-private-relay pair");
		});
	});

	describe("host listener", () => {
		const room = "BBBBBBBBBBBBBBBBBBBBBB";
		const cases: Array<[string, number, string, (r: Relay) => Record<string, string>, boolean]> = [
			["GET /healthz", 200, "/healthz", () => ({}), false],
			["WS role=host", 101, `/r/${room}?role=host`, () => ({}), true],
			["WS role=guest refused", 403, `/r/${room}?role=guest`, () => ({}), true],
			["WS host with a browser Origin refused", 403, `/r/${room}?role=host`, () => ({ Origin: "https://evil.example" }), true],
			["WS host via proxy headers refused", 403, `/r/${room}?role=host`, () => ({ "X-Forwarded-For": "192.0.2.1" }), true],
			["WS host with Tailscale login refused", 403, `/r/${room}?role=host`, () => ({ "Tailscale-User-Login": OWNER }), true],
			["WS host DNS-rebinding Host refused", 403, `/r/${room}?role=host`, r => ({ Host: `evil.example:${r.hostPort}` }), true],
			["WS host with Forwarded refused", 403, `/r/${room}?role=host`, () => ({ Forwarded: "for=192.0.2.1" }), true],
			["WS host via localhost:<port>", 101, "/r/CCCCCCCCCCCCCCCCCCCCCC?role=host", r => ({ Host: `localhost:${r.hostPort}` }), true],
		];
		for (const [name, want, target, headers, upgrade] of cases) {
			test(name, async () => {
				expect(await rawStatus(relay.hostPort, target, headers(relay), { upgrade })).toBe(want);
			});
		}

		test("stats are counts only: no owner, machine name, paths or secrets", async () => {
			const s = await stats(relay);
			expect(s.staticReady).toBe(true);
			expect(s.ready).toBe(true);
			expect(s.adapter).toBe("tailscale-serve");
			const body = JSON.stringify(s);
			for (const secret of [OWNER, PUBLIC_HOST, relay.pairingDir, relay.webDir, relay.checkDir, os.homedir(), os.tmpdir(), relay.cookie]) {
				expect(body).not.toContain(secret);
			}
			expect(s.limits.recheckMs).toBeLessThanOrEqual(30_000);
		});

		test("listener check: both listeners answer an HMAC under the private key, only to direct loopback", async () => {
			const keyFile = path.join(relay.checkDir, "check.key");
			expect(fs.statSync(relay.checkDir).mode & 0o777).toBe(0o700);
			expect(fs.statSync(keyFile).mode & 0o777).toBe(0o600);
			const key = Buffer.from(fs.readFileSync(keyFile, "utf8").trim(), "hex");
			const nonce = "0123456789abcdef0123456789abcdef";
			for (const port of [relay.guestPort, relay.hostPort]) {
				const r = await fetch(`http://127.0.0.1:${port}/__relay_check?nonce=${nonce}`);
				expect(r.status).toBe(200);
				const want = createHmac("sha256", key).update(`omp-private-relay-check/1:${port}:${nonce}`).digest("hex");
				expect(((await r.json()) as { mac: string }).mac).toBe(want);
				expect(await rawStatus(port, `/__relay_check?nonce=${nonce}`, { Host: `localhost:${port}` })).toBe(403);
				expect(await rawStatus(port, `/__relay_check?nonce=${nonce}`, { "X-Forwarded-For": "192.0.2.1" })).toBe(403);
				expect(await rawStatus(port, "/__relay_check?nonce=zz", {})).toBe(400);
			}
		});
	});

	test("room contract: relay, peer ids, 4009, 4004, room-closed 4001", async () => {
		const room = makeRoomId("ctr");
		const h = hostPeer(relay, room);
		expect(await h.opened).toBe(true);
		const g = guestPeer(relay, room);
		expect(await g.opened).toBe(true);
		await waitUntil(() => h.control.length === 1, 2000);
		expect(JSON.parse(h.control[0] as string)).toEqual({ t: "peer-joined", peer: 1 });
		const second = hostPeer(relay, room);
		expect((await closedWithin(second, 3000))?.code).toBe(4009);
		const missing = guestPeer(relay, makeRoomId("none"));
		expect((await closedWithin(missing, 3000))?.code).toBe(4004);
		// guest -> host: envelope rewritten to the sender's peer id
		const got = Promise.withResolvers<Uint8Array>();
		h.ws.onmessage = ev => {
			if (typeof ev.data !== "string") got.resolve(new Uint8Array(ev.data as ArrayBuffer));
		};
		g.ws.send(frame(77, 64));
		const fromGuest = await got.promise;
		expect(new DataView(fromGuest.buffer).getUint32(0)).toBe(1);
		// host -> guest, then host leaves
		h.ws.send(frame(1, 128));
		await waitUntil(() => g.received === 1, 2000);
		expect(g.receivedBytes).toBe(128);
		h.ws.close();
		const c = await closedWithin(g, 3000);
		expect(c?.code).toBe(4001);
		expect(g.control.some(m => m.includes("room-closed"))).toBe(true);
	});

	test("valid large host frame (16 MiB, the default cap) passes unchanged", async () => {
		const room = makeRoomId("big");
		const h = hostPeer(relay, room);
		await h.opened;
		const g = guestPeer(relay, room);
		await g.opened;
		h.ws.send(frame(0, 16 * MiB));
		expect(await waitUntil(() => g.received === 1, 15_000)).toBe(true);
		expect(g.receivedBytes).toBe(16 * MiB);
		h.ws.close();
		await closedWithin(g, 3000);
	}, 30_000);

	test("logs hold connection metadata only: no cookie, no check key", async () => {
		const room = makeRoomId("log");
		const h = hostPeer(relay, room);
		await h.opened;
		const g = guestPeer(relay, room);
		await g.opened;
		g.ws.close();
		h.ws.close();
		await Bun.sleep(200);
		const key = fs.readFileSync(path.join(relay.checkDir, "check.key"), "utf8").trim();
		expect(relay.log()).not.toContain(relay.cookie);
		expect(relay.log()).not.toContain(key);
		expect(relay.log()).not.toContain(room);
	});

	test("revoked browser's open connection is closed with 4401 within the recheck interval", async () => {
		const room = makeRoomId("rev");
		const h = hostPeer(relay, room);
		await h.opened;
		const g = guestPeer(relay, room);
		expect(await g.opened).toBe(true);
		const t0 = Date.now();
		relay.unpair();
		const c = await closedWithin(g, 30_000);
		expect(c?.code).toBe(4401);
		expect(Date.now() - t0).toBeLessThan(5_000);
		// and it can't come back
		expect(await rawStatus(relay.guestPort, `/r/${room}?role=guest`, guestHeaders(relay), { upgrade: true })).toBe(401);
		expect(h.control.some(m => m.includes("peer-left"))).toBe(true);
		h.ws.close();
		relay.repair();
	}, 35_000);
});

describe("revoked client that ignores the close frame", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ env: { OMP_PRIVATE_RELAY_RECHECK_MS: "500" } });
	});
	afterAll(async () => relay.stop());

	test("nothing it sends after the 4401 reaches the host, and it is cut within the grace", async () => {
		const room = makeRoomId("hos");
		const h = hostPeer(relay, room);
		await h.opened;
		const raw = await rawGuest(relay, room);
		expect(await waitUntil(() => h.control.some(m => m.includes("peer-joined")), 3000)).toBe(true);
		raw.send(frame(0, 64));
		expect(await waitUntil(() => h.received === 1, 3000)).toBe(true);

		relay.unpair();
		expect(await waitUntil(() => raw.gotCloseFrame(), 5000)).toBe(true);
		// keep sending, never answer the close
		for (let i = 0; i < 20; i++) {
			raw.send(frame(0, 64));
			await Bun.sleep(50);
		}
		expect(h.received).toBe(1);
		expect(relayCloses(relay, room, "guest")).toContain(4401);
		expect(await waitUntil(() => raw.closed(), 8000)).toBe(true);
		expect(h.received).toBe(1);
		raw.destroy();
		h.ws.close();
	}, 20_000);
});

describe("pairing store fails closed", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ env: { OMP_PRIVATE_RELAY_RECHECK_MS: "500" } });
	});
	afterEach(() => relay.repair());
	afterAll(async () => relay.stop());
	const devices = () => path.join(relay.pairingDir, "devices.json");
	const paired = () => rawStatus(relay.guestPort, "/", guestHeaders(relay, { Origin: null }));

	test("baseline: paired browser gets in", async () => {
		expect(await paired()).toBe(200);
	});

	const breakages: Array<[string, (r: Relay) => void]> = [
		["devices.json readable by others (0644)", () => fs.chmodSync(devices(), 0o644)],
		["pairing dir open to others (0755)", r => fs.chmodSync(r.pairingDir, 0o755)],
		["devices.json missing", () => fs.rmSync(devices())],
		["devices.json corrupt", () => fs.writeFileSync(devices(), "{not json", { mode: 0o600 })],
		["devices.json is a JSON array", () => fs.writeFileSync(devices(), "[]", { mode: 0o600 })],
		[
			"devices.json is a symlink",
			r => {
				const real = path.join(r.pairingDir, "real.json");
				fs.renameSync(devices(), real);
				fs.symlinkSync(real, devices());
			},
		],
		[
			"the pairing directory is replaced by a symlink",
			r => {
				const real = `${r.pairingDir}.real`;
				fs.renameSync(r.pairingDir, real);
				fs.symlinkSync(real, r.pairingDir);
			},
		],
		["devices.json is a FIFO", () => (fs.rmSync(devices()), Bun.spawnSync(["mkfifo", devices()]))],
	];
	for (const [name, breakIt] of breakages) {
		test(`${name}: 401 and open guests closed with 4401`, async () => {
			const room = makeRoomId("fcl");
			const h = hostPeer(relay, room);
			await h.opened;
			const g = guestPeer(relay, room);
			expect(await g.opened).toBe(true);
			breakIt(relay);
			expect(await paired()).toBe(401);
			expect((await closedWithin(g, 5000))?.code).toBe(4401);
			h.ws.close();
			// put the directory back as a directory for the next case
			const stat = fs.lstatSync(relay.pairingDir);
			if (stat.isSymbolicLink()) {
				fs.rmSync(relay.pairingDir);
				fs.renameSync(`${relay.pairingDir}.real`, relay.pairingDir);
			}
		}, 10_000);
	}

	test("a device list with hashes of the wrong shape pairs nobody", async () => {
		fs.writeFileSync(devices(), JSON.stringify({ [relay.cookie]: {}, ABC: {} }), { mode: 0o600 });
		expect(await paired()).toBe(401);
	});
});

describe("before the owner and machine name are known", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ unknownIdentity: true });
	});
	afterAll(async () => relay.stop());

	test("every guest request and upgrade gets 503, even with forged owner headers", async () => {
		const h = guestHeaders({ ...relay, adapter: "tailscale-serve" });
		expect(await rawStatus(relay.guestPort, "/", { ...h, Origin: "" })).toBe(503);
		expect(await rawStatus(relay.guestPort, "/manifest.webmanifest", { ...h, Cookie: "" })).toBe(503);
		expect(await rawStatus(relay.guestPort, "/r/AAAAAAAAAAAAAAAAAAAAAA?role=guest", h, { upgrade: true })).toBe(503);
		expect((await stats(relay)).ready).toBe(false);
	});
});
