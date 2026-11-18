import { createHash, createHmac, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { closedWithin, guestHeaders, guestPeer, hostPeer, makeRoomId, rawRequest, startRelay, stats, waitUntil, type Relay } from "./support/harness";

describe("host relay admin", () => {
	let relay: Relay;
	beforeAll(async () => { relay = await startRelay({ adapter: "token" }); });
	afterAll(async () => relay.stop());

	function signed(op: "viewers" | "disconnect", room = "", peer = "", time = String(Date.now()), nonce = randomBytes(16).toString("hex"), target = relay, device8 = ""): string {
		const key = Buffer.from(fs.readFileSync(path.join(target.checkDir, "check.key"), "utf8").trim(), "hex");
		const mac = createHmac("sha256", key).update(`relay-admin\n${op}\n${room}\n${peer}\n${device8}\n${nonce}\n${time}`).digest("hex");
		return `/${op}?${new URLSearchParams({ room, peer, device8, nonce, time, mac })}`;
	}

	function responseProof(op: "viewers" | "disconnect", request: string, body: string): string {
		const key = Buffer.from(fs.readFileSync(path.join(relay.checkDir, "check.key"), "utf8").trim(), "hex");
		const nonce = new URL(request, "http://localhost").searchParams.get("nonce");
		const digest = createHash("sha256").update(Buffer.from(body, "latin1")).digest("hex");
		return createHmac("sha256", key).update(`relay-admin-resp\n${op}\n${nonce}\n${digest}`).digest("hex");
	}

	function expectProof(op: "viewers" | "disconnect", request: string, response: { headers: Record<string, string>; body: string }): void {
		const proof = response.headers["x-relay-admin-proof"];
		expect(proof).toMatch(/^[0-9a-f]{64}$/);
		expect(proof).toBe(responseProof(op, request, response.body));
		expect(proof).not.toBe(responseProof(op, request, `${response.body} `));
		expect(proof).not.toBe("0".repeat(64));
	}

	test("lists admitted guests and disconnects only the requested peer", async () => {
		const room = makeRoomId("admin");
		const host = hostPeer(relay, room);
		expect(await host.opened).toBe(true);
		const first = guestPeer(relay, room, { "User-Agent": "viewer one" });
		const second = guestPeer(relay, room, { "User-Agent": "viewer two" });
		expect(await first.opened).toBe(true);
		expect(await second.opened).toBe(true);
		expect(await waitUntil(() => host.control.filter(m => m.includes("peer-joined")).length === 2, 2000)).toBe(true);
		const viewersRequest = signed("viewers");
		const response = await rawRequest(relay.hostPort, viewersRequest, {});
		expect(response.status).toBe(200);
		expectProof("viewers", viewersRequest, response);
		const body = JSON.parse(response.body);
		expect(body.pairingMode).toBe("own");
		expect(body.pairingStore).toBe(fs.realpathSync(path.join(relay.pairingDir, "devices.json")));
		const device8 = createHash("sha256").update(relay.cookie).digest("hex").slice(0, 8);
		expect(body.viewers).toHaveLength(2);
		expect(body.viewers.map((v: { ua: string }) => v.ua).sort()).toEqual(["viewer one", "viewer two"]);
		for (const viewer of body.viewers) {
			expect(viewer.room).toBe(room);
			expect(viewer.device8).toBe(device8);
			expect(viewer.connectedAt).toBeGreaterThan(0);
		}
		expect(body.lastAdmittedAt[device8]).toBeGreaterThan(0);
		expect((await stats(relay)).capabilities).toEqual(["viewers"]);
		expect((await stats(relay)).pairingMode).toBe("own");
		const target = body.viewers.find((v: { ua: string }) => v.ua === "viewer one");
		const mismatchRequest = signed("disconnect", room, String(target.peer), undefined, undefined, relay, device8 === "00000000" ? "00000001" : "00000000");
		const mismatch = await rawRequest(relay.hostPort, mismatchRequest, {}, { method: "POST" });
		expect(mismatch.status).toBe(409);
		expect(JSON.parse(mismatch.body)).toEqual({ status: 409, error: "viewer changed" });
		expectProof("disconnect", mismatchRequest, mismatch);
		expect(await closedWithin(first, 100)).toBeNull();
		expect(await closedWithin(second, 100)).toBeNull();
		const disconnectRequest = signed("disconnect", room, String(target.peer), undefined, undefined, relay, device8);
		const disconnected = await rawRequest(relay.hostPort, disconnectRequest, {}, { method: "POST" });
		expect(disconnected.status).toBe(200);
		expectProof("disconnect", disconnectRequest, disconnected);
		expect(JSON.parse(disconnected.body)).toEqual({ ok: true, device8 });
		expect((await closedWithin(first, 2000))?.code).toBe(4409);
		expect(await closedWithin(second, 100)).toBeNull();
		const remainingRequest = signed("viewers");
		const remainingResponse = await rawRequest(relay.hostPort, remainingRequest, {});
		expectProof("viewers", remainingRequest, remainingResponse);
		expect(remainingResponse.headers["x-relay-admin-proof"]).not.toBe(responseProof("viewers", viewersRequest, remainingResponse.body));
		const remaining = JSON.parse(remainingResponse.body);
		expect(remaining.viewers).toHaveLength(1);
		expect(remaining.lastAdmittedAt[device8]).toBe(body.lastAdmittedAt[device8]);
		second.ws.close();
		host.ws.close();
	});

	test("shared pairing is reported as hub in stats and the authenticated viewers response", async () => {
		const shared = await startRelay({ adapter: "token", pairingMode: "shared" });
		try {
			expect((await stats(shared)).pairingMode).toBe("hub");
			const request = signed("viewers", "", "", String(Date.now()), randomBytes(16).toString("hex"), shared);
			const response = await rawRequest(shared.hostPort, request, {});
			expect(response.status).toBe(200);
			const body = JSON.parse(response.body);
			const key = Buffer.from(fs.readFileSync(path.join(shared.checkDir, "check.key"), "utf8").trim(), "hex");
			const nonce = new URL(request, "http://localhost").searchParams.get("nonce");
			const digest = createHash("sha256").update(Buffer.from(response.body, "latin1")).digest("hex");
			expect(response.headers["x-relay-admin-proof"]).toBe(createHmac("sha256", key).update(`relay-admin-resp\nviewers\n${nonce}\n${digest}`).digest("hex"));
			expect(body.pairingMode).toBe("hub");
			expect(body.pairingStore).toBe(fs.realpathSync(path.join(shared.pairingDir, "devices.json")));
			const store = path.join(shared.pairingDir, "devices.json");
			fs.renameSync(store, `${store}.saved`);
			try {
				const missing = await rawRequest(shared.hostPort, signed("viewers", "", "", String(Date.now()), randomBytes(16).toString("hex"), shared), {});
				expect(missing.status).toBe(200);
				expect(JSON.parse(missing.body).pairingStore).toBeNull();
			} finally {
				fs.renameSync(`${store}.saved`, store);
			}
		} finally {
			await shared.stop();
		}
	});

	test("a stale peer ID cannot disconnect a guest after a room is recreated", async () => {
		const room = makeRoomId("recreate");
		const firstHost = hostPeer(relay, room);
		expect(await firstHost.opened).toBe(true);
		const firstGuest = guestPeer(relay, room);
		expect(await firstGuest.opened).toBe(true);
		expect(await waitUntil(() => firstHost.control.some(m => m.includes("peer-joined")), 2000)).toBe(true);
		const oldPeer = JSON.parse(firstHost.control.find(m => m.includes("peer-joined")) as string).peer;
		firstHost.ws.close();
		expect((await closedWithin(firstGuest, 2000))?.code).toBe(4001);
		const secondHost = hostPeer(relay, room);
		expect(await secondHost.opened).toBe(true);
		const secondGuest = guestPeer(relay, room);
		expect(await secondGuest.opened).toBe(true);
		expect(await waitUntil(() => secondHost.control.some(m => m.includes("peer-joined")), 2000)).toBe(true);
		const newPeer = JSON.parse(secondHost.control.find(m => m.includes("peer-joined")) as string).peer;
		expect(newPeer).toBeGreaterThan(oldPeer);
		const device8 = createHash("sha256").update(relay.cookie).digest("hex").slice(0, 8);
		const request = signed("disconnect", room, String(oldPeer), undefined, undefined, relay, device8);
		const response = await rawRequest(relay.hostPort, request, {}, { method: "POST" });
		expect(response.status).toBe(404);
		expect(JSON.parse(response.body)).toEqual({ status: 404, error: "viewer already left" });
		expectProof("disconnect", request, response);
		expect(await closedWithin(secondGuest, 100)).toBeNull();
		secondGuest.ws.close();
		secondHost.ws.close();
	});

	test("rejects unsigned, stale, replayed and browser or proxied requests", async () => {
		expect((await rawRequest(relay.hostPort, "/viewers", {})).status).not.toBe(200);
		expect((await rawRequest(relay.hostPort, signed("viewers", "", "", String(Date.now() - 31_000)), {})).status).toBe(403);
		const request = signed("viewers");
		expect((await rawRequest(relay.hostPort, request, {})).status).toBe(200);
		expect((await rawRequest(relay.hostPort, request, {})).status).toBe(403);
		for (const headers of [{ Origin: "http://evil.example" }, { "X-Forwarded-For": "127.0.0.1" }, { "X-Forwarded-Host": "evil.example" }, { "Tailscale-Funnel-Request": "?1" }, { Forwarded: "for=127.0.0.1" }]) {
			expect((await rawRequest(relay.hostPort, signed("viewers"), headers)).status).toBe(403);
		}
		expect((await rawRequest(relay.guestPort, signed("viewers"), guestHeaders(relay))).status).toBe(404);
		expect((await rawRequest(relay.guestPort, signed("disconnect", makeRoomId("x"), "1", undefined, undefined, relay, "01234567"), guestHeaders(relay), { method: "POST" })).status).toBe(404);
	});

	test("binds and validates device8 in the signed request", async () => {
		const room = makeRoomId("signed");
		const request = signed("disconnect", room, "1", undefined, undefined, relay, "01234567");
		const changed = new URL(request, "http://localhost");
		changed.searchParams.set("device8", "01234568");
		expect((await rawRequest(relay.hostPort, changed.pathname + changed.search, {}, { method: "POST" })).status).toBe(403);
		for (const device8 of ["", "0123456", "012345678", "ABCDEF01", "0123456g"]) {
			expect((await rawRequest(relay.hostPort, signed("disconnect", room, "1", undefined, undefined, relay, device8), {}, { method: "POST" })).status).toBe(400);
		}
		expect((await rawRequest(relay.hostPort, signed("viewers", "", "", undefined, undefined, relay, "01234567"), {})).status).toBe(400);
	});

	test("reads the key again on every request and fails closed when it is unavailable", async () => {
		const old = signed("viewers");
		const keyPath = path.join(relay.checkDir, "check.key");
		const original = fs.readFileSync(keyPath);
		try {
			fs.writeFileSync(keyPath, randomBytes(32).toString("hex"), { mode: 0o600 });
			expect((await rawRequest(relay.hostPort, old, {})).status).toBe(403);
			const request = signed("viewers");
			const response = await rawRequest(relay.hostPort, request, {});
			expect(response.status).toBe(200);
			expectProof("viewers", request, response);
			fs.chmodSync(keyPath, 0o644);
			expect((await rawRequest(relay.hostPort, signed("viewers"), {})).status).toBe(503);
		} finally {
			fs.chmodSync(keyPath, 0o600);
			fs.writeFileSync(keyPath, original);
		}
	});
});
