/**
 * Caps, slow consumers and restart behaviour, against real relay processes. Volumes are adaptive: a kernel that buffers
 * a lot on a stalled loopback peer (Linux auto-tunes up to tens of MiB) needs more data before the relay's own cap is
 * reached, so each test keeps sending, within a bound, until the relay says it hit the cap.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import {
	closedWithin,
	frame,
	guestPeer,
	hostPeer,
	makeRoomId,
	type Relay,
	relayCloses,
	stalledGuest,
	startRelay,
	stats,
	waitUntil,
} from "./support/harness";

const MiB = 1024 * 1024;
const KiB = 1024;

describe("limits", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({
			env: {
				OMP_PRIVATE_RELAY_MAX_ROOMS: "3",
				OMP_PRIVATE_RELAY_MAX_GUESTS: "2",
				OMP_PRIVATE_RELAY_MAX_HOST_FRAME: String(1 * MiB),
				OMP_PRIVATE_RELAY_MAX_GUEST_FRAME: String(64 * KiB),
				OMP_PRIVATE_RELAY_CONN_QUEUE_MAX: String(4 * MiB),
				OMP_PRIVATE_RELAY_QUEUE_MAX: String(6 * MiB),
				OMP_PRIVATE_RELAY_RESTART_GRACE_MS: "0",
			},
		});
	});
	afterAll(async () => relay.stop());

	test("room cap: the 4th host gets 4029", async () => {
		const hosts = [0, 1, 2].map(() => hostPeer(relay, makeRoomId("cap")));
		await Promise.all(hosts.map(h => h.opened));
		const fourth = hostPeer(relay, makeRoomId("cap"));
		expect((await closedWithin(fourth, 3000))?.code).toBe(4029);
		for (const h of hosts) h.ws.close();
		await Bun.sleep(200);
	});

	test("guest cap, frame caps and invalid frames", async () => {
		const room = makeRoomId("gst");
		const h = hostPeer(relay, room);
		await h.opened;
		const g1 = guestPeer(relay, room);
		const g2 = guestPeer(relay, room);
		await Promise.all([g1.opened, g2.opened]);
		const g3 = guestPeer(relay, room);
		expect((await closedWithin(g3, 3000))?.code).toBe(4029);

		h.ws.send(frame(0, 1 * MiB));
		expect(await waitUntil(() => g1.received === 1 && g2.received === 1, 3000)).toBe(true);
		g1.ws.send("text frames are ignored");
		g1.ws.send(new Uint8Array(2));
		h.ws.send(new Uint8Array(3));
		h.ws.send(frame(99, 1000));
		await Bun.sleep(300);
		expect(g1.ws.readyState).toBe(WebSocket.OPEN);
		expect(h.ws.readyState).toBe(WebSocket.OPEN);
		expect(h.received).toBe(0);
		expect(g2.received).toBe(1);

		g1.ws.send(frame(0, 64 * KiB + 1));
		expect(await closedWithin(g1, 3000)).not.toBeNull();
		expect(h.received).toBe(0);
		h.ws.send(frame(0, 1 * MiB + 1));
		expect(await closedWithin(h, 3000)).not.toBeNull();
		expect(g2.received).toBe(1);
		expect((await closedWithin(g2, 3000))?.code).toBe(4001);
	});

	test("slow guest is cut with 4008 while a fast guest gets everything", async () => {
		const room = makeRoomId("slw");
		const h = hostPeer(relay, room);
		await h.opened;
		const fast = guestPeer(relay, room);
		await fast.opened;
		const slow = await stalledGuest(relay, room);
		await Bun.sleep(200);
		let sent = 0;
		while (!relayCloses(relay, room, "guest").includes(4008) && !relay.log().includes("slow_consumer") && sent < 2000) {
			h.ws.send(frame(0, 256 * KiB));
			sent++;
			await Bun.sleep(5);
		}
		expect(await waitUntil(() => fast.received === sent, 30_000)).toBe(true);
		expect(await waitUntil(() => relayCloses(relay, room, "guest").includes(4008), 8_000)).toBe(true);
		const s = await stats(relay);
		expect(s.maxConnQueued).toBeLessThanOrEqual(4 * MiB + 256 * KiB);
		slow.destroy();
		fast.ws.close();
		h.ws.close();
		await Bun.sleep(200);
	}, 60_000);

	test("relay-wide queue cap cuts a stalled guest before its own cap", async () => {
		const rooms = [makeRoomId("rq1"), makeRoomId("rq2")];
		const hosts = rooms.map(r => hostPeer(relay, r));
		await Promise.all(hosts.map(h => h.opened));
		const stalled = await Promise.all(rooms.map(r => stalledGuest(relay, r)));
		await Bun.sleep(200);
		let sent = 0;
		while (!relay.log().includes("relay_queue_full") && sent < 4000) {
			for (const h of hosts) h.ws.send(frame(0, 128 * KiB));
			sent++;
			await Bun.sleep(5);
		}
		expect(await waitUntil(() => relay.log().includes("relay_queue_full"), 8_000)).toBe(true);
		expect(await waitUntil(() => rooms.some(r => relayCloses(relay, r, "guest").includes(4008)), 8_000)).toBe(true);
		for (const s of stalled) s.destroy();
		for (const h of hosts) h.ws.close();
	}, 60_000);
});

describe("restart", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ env: { OMP_PRIVATE_RELAY_RESTART_GRACE_MS: "3000" } });
	});
	afterAll(async () => relay.stop());

	test("during the grace window a missing room gets 4001 (retry), after it 4004", async () => {
		await relay.restart();
		const early = guestPeer(relay, makeRoomId("grc"));
		expect((await closedWithin(early, 3000))?.code).toBe(4001);
		await Bun.sleep(3200);
		const late = guestPeer(relay, makeRoomId("grc"));
		expect((await closedWithin(late, 3000))?.code).toBe(4004);
	}, 15_000);

	test("relay restart: host reconnects to the same room and a guest rejoins", async () => {
		const room = makeRoomId("rst");
		const h1 = hostPeer(relay, room);
		await h1.opened;
		const g1 = guestPeer(relay, room);
		await g1.opened;
		await relay.restart();
		// both sides see the drop (killed process: abnormal close)
		expect(await closedWithin(h1, 3000)).not.toBeNull();
		expect(await closedWithin(g1, 3000)).not.toBeNull();
		// guest retries first, inside the grace window: told to retry, not "no such room"
		const early = guestPeer(relay, room);
		expect((await closedWithin(early, 3000))?.code).toBe(4001);
		// host comes back like omp's reconnect loop does, then the guest rejoins and traffic flows
		const h2 = hostPeer(relay, room);
		expect(await h2.opened).toBe(true);
		const g2 = guestPeer(relay, room);
		expect(await g2.opened).toBe(true);
		h2.ws.send(frame(0, 512));
		expect(await waitUntil(() => g2.received === 1, 3000)).toBe(true);
		h2.ws.close();
		await closedWithin(g2, 3000);
	}, 20_000);
});
