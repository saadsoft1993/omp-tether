import { createHash, createHmac, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { closedWithin, guestHeaders, guestPeer, hostPeer, makeRoomId, rawRequest, startRelay, stats, waitUntil, type Relay } from "./support/harness";

describe("host relay admin", () => {
	let relay: Relay;
	beforeAll(async () => { relay = await startRelay({ adapter: "token" }); });
	afterAll(async () => relay.stop());

	function signed(op: "viewers" | "disconnect", room = "", peer = "", time = String(Date.now()), nonce = randomBytes(16).toString("hex")): string {
		const key = Buffer.from(fs.readFileSync(path.join(relay.checkDir, "check.key"), "utf8").trim(), "hex");
		const mac = createHmac("sha256", key).update(`relay-admin\n${op}\n${room}\n${peer}\n${nonce}\n${time}`).digest("hex");
		return `/${op}?${new URLSearchParams({ room, peer, nonce, time, mac })}`;
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
		const target = body.viewers.find((v: { ua: string }) => v.ua === "viewer one");
		const disconnectRequest = signed("disconnect", room, String(target.peer));
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
		expect((await rawRequest(relay.guestPort, signed("disconnect", makeRoomId("x"), "1"), guestHeaders(relay), { method: "POST" })).status).toBe(404);
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
