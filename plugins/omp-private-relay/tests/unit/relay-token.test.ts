/**
 * The `token` adapter (loopback only, paired by a one-time link) and the pairing endpoints of the relay, against a real
 * relay process on free ports: the identity layer (Host, Origin, proxy headers), redeeming a pairing link, the failure
 * limit, revocation closing an open page, and the shared mode in which the relay never writes the store.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { PairingWriter } from "../../src/pairing-store";
import {
	closedWithin,
	COOKIE_NAME,
	freePorts,
	guestHeaders,
	guestPeer,
	hostPeer,
	makeRoomId,
	type Relay,
	rawRequest,
	rawStatus,
	startRelay,
	waitUntil,
} from "./support/harness";

const room = "AAAAAAAAAAAAAAAAAAAAAA";

describe("token adapter: identity layer", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ adapter: "token", env: { OMP_PRIVATE_RELAY_RESTART_GRACE_MS: "0" } });
	});
	afterAll(async () => relay.stop());

	type Case = [string, number, () => [string, Record<string, string>, { method?: string; upgrade?: boolean }]];
	const cases: Case[] = [
		["GET / paired", 200, () => ["/", guestHeaders(relay, { Origin: null }), {}]],
		["GET / paired, own Origin", 200, () => ["/", guestHeaders(relay), {}]],
		["GET / paired through the name localhost", 200, () => ["/", guestHeaders(relay, { Host: `localhost:${relay.guestPort}`, Origin: `http://localhost:${relay.guestPort}` }), {}]],
		["GET / no cookie", 401, () => ["/", guestHeaders(relay, { Cookie: null, Origin: null }), {}]],
		["GET / wrong Host (DNS rebinding)", 403, () => ["/", guestHeaders(relay, { Host: `evil.example:${relay.guestPort}`, Origin: null }), {}]],
		["GET / Host with the right name but another port", 403, () => ["/", guestHeaders(relay, { Host: `127.0.0.1:${relay.guestPort + 1}`, Origin: null }), {}]],
		["GET / no Host port at all", 403, () => ["/", guestHeaders(relay, { Host: "127.0.0.1", Origin: null }), {}]],
		["GET / foreign Origin", 403, () => ["/", guestHeaders(relay, { Origin: "https://evil.example" }), {}]],
		["GET / Origin of another loopback port", 403, () => ["/", guestHeaders(relay, { Origin: `http://127.0.0.1:${relay.guestPort + 1}` }), {}]],
		["GET / Sec-Fetch-Site cross-site", 403, () => ["/", guestHeaders(relay, { Origin: null, "Sec-Fetch-Site": "cross-site" }), {}]],
		["GET / X-Forwarded-For (a proxy in front)", 403, () => ["/", guestHeaders(relay, { Origin: null, "X-Forwarded-For": "192.0.2.1" }), {}]],
		["GET / X-Forwarded-Host", 403, () => ["/", guestHeaders(relay, { Origin: null, "X-Forwarded-Host": "evil.example" }), {}]],
		["GET / Forwarded", 403, () => ["/", guestHeaders(relay, { Origin: null, Forwarded: "for=192.0.2.1" }), {}]],
		["GET / Tailscale login header", 403, () => ["/", guestHeaders(relay, { Origin: null, "Tailscale-User-Login": "owner@example.com" }), {}]],
		["GET / via Funnel", 403, () => ["/", guestHeaders(relay, { Origin: null, "Tailscale-Funnel-Request": "?1" }), {}]],
		["GET manifest without cookie", 200, () => ["/manifest.webmanifest", guestHeaders(relay, { Cookie: null, Origin: null }), {}]],
		["GET manifest, wrong Host", 403, () => ["/manifest.webmanifest", guestHeaders(relay, { Cookie: null, Origin: null, Host: "evil.example:1" }), {}]],
		["WS guest paired + Origin", 101, () => [`/r/${room}?role=guest`, guestHeaders(relay), { upgrade: true }]],
		["WS guest no Origin", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Origin: null }), { upgrade: true }]],
		["WS guest foreign Origin", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Origin: "http://evil.example" }), { upgrade: true }]],
		["WS guest wrong Host", 403, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Host: "evil.example:1" }), { upgrade: true }]],
		["WS guest no cookie", 401, () => [`/r/${room}?role=guest`, guestHeaders(relay, { Cookie: null }), { upgrade: true }]],
		["WS role=host on the guest listener", 403, () => [`/r/${room}?role=host`, guestHeaders(relay), { upgrade: true }]],
	];
	for (const [name, want, build] of cases) {
		test(name, async () => {
			const [target, headers, opts] = build();
			expect(await rawStatus(relay.guestPort, target, headers, opts)).toBe(want);
		});
	}

	test("the page's CSP lets it connect to its own ws origin, and nothing else", async () => {
		const r = await rawRequest(relay.guestPort, "/", guestHeaders(relay, { Origin: null }));
		const csp = r.headers["content-security-policy"] ?? "";
		expect(csp).toContain(`connect-src 'self' ws://127.0.0.1:${relay.guestPort} ws://localhost:${relay.guestPort}`);
		expect(csp).not.toContain("wss:");
		expect(csp).toContain("default-src 'none'");
	});

	test("a link followed from another site is refused (403) with a plain explanation and the fix, not only 'cross-site request'", async () => {
		const r = await rawRequest(relay.guestPort, "/pair", { Cookie: "", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", Accept: "text/html" });
		expect(r.status).toBe(403);
		expect(r.body).toContain("another site");
		expect(r.body).toContain("address bar");
		expect(r.body).toContain("omp-private-relay pair");
		// the explanation never reflects anything the client sent, and a socket upgrade still gets only the short reason
		const ws = await rawRequest(relay.guestPort, `/r/${room}?role=guest`, guestHeaders(relay, { "Sec-Fetch-Site": "cross-site" }), { upgrade: true });
		expect(ws.status).toBe(403);
		expect(ws.body).toBe("cross-site request");
	});

	test("a host (an omp session) still connects on the loopback listener and a paired page reaches it through the relay", async () => {
		const id = makeRoomId("tok");
		const h = hostPeer(relay, id);
		expect(await h.opened).toBe(true);
		const g = guestPeer(relay, id);
		expect(await g.opened).toBe(true);
		expect(await waitUntil(() => h.control.some(m => m.includes("peer-joined")), 2000)).toBe(true);
		g.ws.close();
		h.ws.close();
	});
});

describe("pairing a browser with a one-time link", () => {
	let relay: Relay;
	let writer: PairingWriter;
	beforeAll(async () => {
		relay = await startRelay({ adapter: "token" });
		writer = new PairingWriter(relay.pairingDir);
	});
	afterAll(async () => relay.stop());

	const headers = (over: Record<string, string | null> = {}) =>
		guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1", ...over });
	const redeem = (token: unknown, over: Record<string, string | null> = {}, rawBody?: string) =>
		rawRequest(relay.guestPort, "/__pair", headers(over), { method: "POST", body: rawBody ?? JSON.stringify({ token }) });

	test("the pairing page and its script need no cookie, but do need the right Host", async () => {
		const page = await rawRequest(relay.guestPort, "/pair", guestHeaders(relay, { Cookie: null, Origin: null }));
		expect(page.status).toBe(200);
		expect(page.headers["content-type"]).toContain("text/html");
		expect(page.headers["content-security-policy"]).toContain("script-src 'self'");
		expect(page.body).toContain('<script src="/__pair.js">');
		const js = await rawRequest(relay.guestPort, "/__pair.js", guestHeaders(relay, { Cookie: null, Origin: null }));
		expect(js.status).toBe(200);
		expect(js.body).toContain("history.replaceState");
		expect(await rawStatus(relay.guestPort, "/pair", guestHeaders(relay, { Cookie: null, Origin: null, Host: "evil.example:1" }))).toBe(403);
	});

	test("a link works once: the cookie it sets pairs the browser, the same link again is refused", async () => {
		const token = await writer.createToken();
		const first = await redeem(token);
		expect(first.status).toBe(200);
		const setCookie = first.headers["set-cookie"] ?? "";
		expect(setCookie).toMatch(new RegExp(`^${COOKIE_NAME}=[A-Za-z0-9_-]{43}; `));
		for (const attribute of ["Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=31536000"]) expect(setCookie).toContain(attribute);
		expect(setCookie).not.toContain("Secure"); // loopback http: browsers refuse Secure cookies there in some cases
		const cookie = setCookie.split(";")[0] as string;
		expect(await rawStatus(relay.guestPort, "/", guestHeaders(relay, { Cookie: cookie, Origin: null }))).toBe(200);
		expect((await redeem(token)).status).toBe(403);
		// the store holds a hash, never the cookie or the token
		const stored = fs.readFileSync(path.join(relay.pairingDir, "devices.json"), "utf8");
		expect(stored).not.toContain(cookie.split("=")[1] as string);
		expect(fs.readFileSync(path.join(relay.pairingDir, "pending.json"), "utf8")).not.toContain(token);
		expect(relay.log()).not.toContain(token);
		expect(relay.log()).not.toContain(cookie.split("=")[1] as string);
	});

	test("an expired link is refused", async () => {
		const clock = { now: 1_000_000 };
		const old = new PairingWriter(relay.pairingDir, () => clock.now);
		const token = await old.createToken();
		// the relay's own clock is the real one: the expiry (1_000_300) is long past
		expect((await redeem(token)).status).toBe(403);
	});

	// The guards (custom header, Origin, Host, proxy headers, framing) are tested with REAL tokens in regressions.test.ts: a
	// refusal of an unknown token would pass even without the guard. What stays here needs no token.
	test("a body that is not JSON, and a token of the wrong type, are refused", async () => {
		expect((await redeem(null, {}, "{nope")).status).toBe(400);
		expect((await redeem(12345)).status).toBe(403);
		expect((await redeem("")).status).toBe(403);
	});

	test("a body over 1 KiB is refused", async () => {
		expect((await redeem("x".repeat(2000))).status).toBe(413);
	});

	test("a GET to /__pair, and a POST elsewhere, are not redemption", async () => {
		expect(await rawStatus(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, Origin: null }))).toBe(401);
		expect(await rawStatus(relay.guestPort, "/", headers(), { method: "POST" })).toBe(401);
	});

	test("ten wrong links in a minute stop all redemption, even with a valid link", async () => {
		const fresh = await startRelay({ adapter: "token" });
		try {
			const w = new PairingWriter(fresh.pairingDir);
			const post = (token: string) =>
				rawRequest(fresh.guestPort, "/__pair", guestHeaders(fresh, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }), { method: "POST", body: JSON.stringify({ token }) });
			for (let i = 0; i < 10; i++) expect((await post(`wrong-${i}`)).status).toBe(403);
			const valid = await w.createToken();
			expect((await post(valid)).status).toBe(429);
		} finally {
			await fresh.stop();
		}
	});

	test("revoking a paired browser closes its open page with 4401 and refuses it afterwards", async () => {
		const live = await startRelay({ adapter: "token", env: { OMP_PRIVATE_RELAY_RECHECK_MS: "300" } });
		try {
			const w = new PairingWriter(live.pairingDir);
			const post = await rawRequest(live.guestPort, "/__pair", guestHeaders(live, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }), {
				method: "POST",
				body: JSON.stringify({ token: await w.createToken() }),
			});
			const cookie = (post.headers["set-cookie"] as string).split(";")[0] as string;
			const id = makeRoomId("rvk");
			const h = hostPeer(live, id);
			await h.opened;
			const g = guestPeer(live, id, { Cookie: cookie });
			expect(await g.opened).toBe(true);
			const device = w.devices().find(d => d.label !== "test");
			expect(device).toBeDefined();
			expect(await w.revoke((device as { id: string }).id)).toBe(1);
			expect((await closedWithin(g, 5000))?.code).toBe(4401);
			expect(await rawStatus(live.guestPort, "/", guestHeaders(live, { Cookie: cookie, Origin: null }))).toBe(401);
			h.ws.close();
		} finally {
			await live.stop();
		}
	}, 20_000);
});

describe("pairing with the tailscale-serve adapter", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ adapter: "tailscale-serve" });
	});
	afterAll(async () => relay.stop());

	test("the cookie is set Secure, and a request from a stranger's login is refused before the token is looked at", async () => {
		const writer = new PairingWriter(relay.pairingDir);
		const token = await writer.createToken();
		const stranger = await rawRequest(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, "Tailscale-User-Login": "alice@example.com", "Content-Type": "application/json", "X-Omp-Relay": "1" }), {
			method: "POST",
			body: JSON.stringify({ token }),
		});
		expect(stranger.status).toBe(403);
		const ok = await rawRequest(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }), {
			method: "POST",
			body: JSON.stringify({ token }),
		});
		expect(ok.status).toBe(200);
		expect(ok.headers["set-cookie"]).toContain("; Secure");
	});
});

describe("shared pairing mode: another program owns the store", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ adapter: "token", pairingMode: "shared", env: { OMP_PRIVATE_RELAY_COOKIE_NAME: "dash_session" } });
	});
	afterAll(async () => relay.stop());

	test("it reads the store under the configured cookie name and has no pairing endpoints", async () => {
		const ok = guestHeaders(relay, { Origin: null, Cookie: `dash_session=${relay.cookie}` });
		expect(await rawStatus(relay.guestPort, "/", ok)).toBe(200);
		expect(await rawStatus(relay.guestPort, "/", guestHeaders(relay, { Origin: null }))).toBe(401); // the default name is not used
		expect(await rawStatus(relay.guestPort, "/pair", guestHeaders(relay, { Cookie: null, Origin: null }))).toBe(401);
		const post = await rawRequest(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }), { method: "POST", body: "{}" });
		expect(post.status).toBe(401);
		const help = await rawRequest(relay.guestPort, "/", guestHeaders(relay, { Cookie: null, Origin: null }));
		expect(help.body).not.toContain("omp-private-relay pair");
		expect(fs.existsSync(path.join(relay.pairingDir, "pending.json"))).toBe(false);
		expect(fs.existsSync(path.join(relay.pairingDir, ".lock"))).toBe(false);
	});
});

describe("extra allowed hosts from the config file (an SSH port forward on another local port)", () => {
	let relay: Relay;
	let forward: number;
	beforeAll(async () => {
		[forward] = (await freePorts(1)) as [number];
		const configFile = path.join(os.tmpdir(), `omp-pr-cfg-${process.pid}-${forward}.json`);
		fs.writeFileSync(configFile, JSON.stringify({ schemaVersion: 1, auth: { adapter: "token", token: { allowedHosts: [`127.0.0.1:${forward}`] } } }), { mode: 0o600 });
		relay = await startRelay({ adapter: "token", env: { OMP_PRIVATE_RELAY_CONFIG: configFile } });
	});
	afterAll(async () => relay.stop());

	test("that host and its origin are accepted, others are not", async () => {
		const h = guestHeaders(relay, { Host: `127.0.0.1:${forward}`, Origin: `http://127.0.0.1:${forward}` });
		expect(await rawStatus(relay.guestPort, "/", h)).toBe(200);
		expect(await rawStatus(relay.guestPort, "/", { ...h, Host: `127.0.0.1:${forward + 1}` })).toBe(403);
	});
});
