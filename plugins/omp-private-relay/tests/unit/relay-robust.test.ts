// Hostile and broken input on both listeners, and the service entry point's start and stop: the relay must stay up, say
// nothing about itself in error pages, and leave nothing listening when it is told to stop.
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { freePorts, guestHeaders, MAIN, type Relay, rawRequest, rawStatus, startRelay, waitForHealth } from "./support/harness";

function sendRaw(port: number, bytes: string | Buffer, closeAfterMs = 300): Promise<string> {
	const { promise, resolve } = Promise.withResolvers<string>();
	const sock = net.connect(port, "127.0.0.1");
	let got = "";
	sock.on("data", d => {
		got += d.toString("latin1");
	});
	sock.on("error", () => resolve(got));
	sock.on("close", () => resolve(got));
	sock.write(bytes);
	setTimeout(() => sock.destroy(), closeAfterMs);
	return promise;
}

describe("hostile input", () => {
	let relay: Relay;
	beforeAll(async () => {
		relay = await startRelay({ adapter: "token" });
	});
	afterAll(async () => relay.stop());
	const alive = async () => expect(await rawStatus(relay.hostPort, "/healthz", {})).toBe(200);

	test("random bytes, partial requests and invalid HTTP on both listeners do not take the relay down", async () => {
		const junk = [Buffer.from(crypto.getRandomValues(new Uint8Array(4096))), "GET", "GET / HTTP/1.1\r\n", "\r\n\r\n\r\n", "POST / HTTP/1.1\r\nContent-Length: -5\r\n\r\n", "GET / HTTP/9.9\r\n\r\n", `GET /${"a".repeat(70_000)} HTTP/1.1\r\n\r\n`, "GET / HTTP/1.1\r\nHost: x\r\nX: \u0000\r\n\r\n"];
		for (const port of [relay.hostPort, relay.guestPort]) for (const j of junk) await sendRaw(port, j, 150);
		await alive();
		expect(await rawStatus(relay.guestPort, "/", guestHeaders(relay, { Origin: null }))).toBe(200);
	});

	test("a header block of 100 KiB is refused or dropped, never served", async () => {
		const reply = await sendRaw(relay.guestPort, `GET / HTTP/1.1\r\nHost: 127.0.0.1:${relay.guestPort}\r\nX-Pad: ${"a".repeat(100_000)}\r\n\r\n`, 500);
		expect(reply).not.toMatch(/^HTTP\/1\.1 200/);
		await alive();
	});

	test("two hundred connections opened and abandoned at once leave it healthy", async () => {
		const socks = await Promise.all(
			Array.from({ length: 200 }, () => {
				const { promise, resolve } = Promise.withResolvers<net.Socket>();
				const s = net.connect(relay.guestPort, "127.0.0.1", () => resolve(s));
				s.on("error", () => resolve(s));
				return promise;
			}),
		);
		for (const s of socks) s.destroy();
		await alive();
	});

	test("error pages carry no stack trace, runtime name or path", async () => {
		const cases: Array<[number, string, Record<string, string>]> = [
			[relay.guestPort, "/%E0%A4%A", guestHeaders(relay, { Origin: null })],
			[relay.guestPort, "/%ff", guestHeaders(relay, { Origin: null })],
			[relay.guestPort, "/nope", guestHeaders(relay, { Origin: null })],
			[relay.guestPort, "/", guestHeaders(relay, { Cookie: null, Origin: null })],
			[relay.hostPort, "/nope", {}],
			[relay.hostPort, "/r/short?role=host", {}],
		];
		for (const [port, target, headers] of cases) {
			const r = await rawRequest(port, target, headers);
			expect([400, 401, 404]).toContain(r.status);
			expect(r.body).not.toMatch(/Bun v|at .*\.ts:\d+|node_modules|\/Users\/|\/home\//);
		}
	});

	test("room ids of the wrong length or alphabet never open a room", async () => {
		for (const id of ["a".repeat(9), "a".repeat(65), "bad id with spaces", "../../etc/passwd", "a".repeat(10) + "%00"]) {
			const status = await rawStatus(relay.hostPort, `/r/${encodeURIComponent(id)}?role=host`, {}, { upgrade: true });
			expect(status).toBe(404);
		}
		expect(await rawStatus(relay.hostPort, `/r/${"a".repeat(10)}`, {}, { upgrade: true })).toBe(404); // no role
		expect(await rawStatus(relay.hostPort, `/r/${"a".repeat(10)}?role=admin`, {}, { upgrade: true })).toBe(404);
	});

	test("HEAD works on static files without a body; unknown methods are refused", async () => {
		const head = await rawRequest(relay.guestPort, "/", guestHeaders(relay, { Origin: null }), { method: "HEAD" });
		expect(head.status).toBe(200);
		expect(head.body).toBe("");
		for (const method of ["DELETE", "PUT", "PATCH", "OPTIONS"]) {
			expect(await rawStatus(relay.guestPort, "/", guestHeaders(relay, { Origin: null }), { method })).toBe(405);
		}
	});

	test("static files are not followed out of the web directory through a symlink", async () => {
		fs.writeFileSync(path.join(relay.root, "outside.txt"), "outside");
		fs.symlinkSync(path.join(relay.root, "outside.txt"), path.join(relay.webDir, "link.txt"));
		expect(await rawStatus(relay.guestPort, "/link.txt", guestHeaders(relay, { Origin: null }))).toBe(404);
	});
});

describe("the service entry point", () => {
	function run(env: Record<string, string>) {
		return Bun.spawn([process.execPath, MAIN], { env: { PATH: process.env.PATH ?? "", ...env }, stdout: "pipe", stderr: "pipe" });
	}

	test("a bad configuration stops it with a message and exit 1, before anything listens", async () => {
		const root = fs.mkdtempSync(path.join(os.tmpdir(), "oprm-"));
		try {
			const [host] = await freePorts(1);
			const proc = run({ HOME: root, OMP_PRIVATE_RELAY_HOST_PORT: String(host), OMP_PRIVATE_RELAY_GUEST_PORT: "80" });
			expect(await proc.exited).toBe(1);
			expect(await new Response(proc.stderr).text()).toContain("guestPort");
			await expect(waitForHealth(host as number, 300)).rejects.toThrow();
		} finally {
			fs.rmSync(root, { recursive: true, force: true });
		}
	});

	test("a port that is taken stops it with exit 1 instead of running half a relay", async () => {
		const root = fs.mkdtempSync(path.join(os.tmpdir(), "oprm-"));
		const [host, guest] = await freePorts(2);
		const holder = net.createServer();
		await new Promise<void>(r => holder.listen(guest as number, "127.0.0.1", () => r()));
		try {
			const proc = run({ HOME: root, OMP_PRIVATE_RELAY_HOST_PORT: String(host), OMP_PRIVATE_RELAY_GUEST_PORT: String(guest), OMP_PRIVATE_RELAY_WEB_DIR: path.join(root, "web") });
			expect(await proc.exited).toBe(1);
			expect(await new Response(proc.stderr).text()).toContain("cannot start");
			// the host listener, which did bind first, was not left behind
			await expect(waitForHealth(host as number, 300)).rejects.toThrow();
		} finally {
			holder.close();
			fs.rmSync(root, { recursive: true, force: true });
		}
	});

	test("SIGTERM stops it cleanly: exit 0 and both ports are free again", async () => {
		const root = fs.mkdtempSync(path.join(os.tmpdir(), "oprm-"));
		const [host, guest] = await freePorts(2);
		const proc = run({ HOME: root, OMP_PRIVATE_RELAY_HOST_PORT: String(host), OMP_PRIVATE_RELAY_GUEST_PORT: String(guest), OMP_PRIVATE_RELAY_WEB_DIR: path.join(root, "web"), OMP_PRIVATE_RELAY_CHECK_DIR: path.join(root, "check") });
		try {
			await waitForHealth(host as number);
			proc.kill("SIGTERM");
			expect(await proc.exited).toBe(0);
			for (const port of [host, guest]) await expect(fetch(`http://127.0.0.1:${port}/healthz`)).rejects.toThrow();
			// the check key stays on disk but is private
			expect(fs.statSync(path.join(root, "check", "check.key")).mode & 0o777).toBe(0o600);
		} finally {
			proc.kill("SIGKILL");
			fs.rmSync(root, { recursive: true, force: true });
		}
	});

	test("it binds loopback only: the listeners are not reachable on another interface address", async () => {
		const external = Object.values(os.networkInterfaces())
			.flat()
			.find(i => i && i.family === "IPv4" && !i.internal);
		if (!external) return; // a machine with no network interface
		const root = fs.mkdtempSync(path.join(os.tmpdir(), "oprm-"));
		const [host, guest] = await freePorts(2);
		const proc = run({ HOME: root, OMP_PRIVATE_RELAY_HOST_PORT: String(host), OMP_PRIVATE_RELAY_GUEST_PORT: String(guest), OMP_PRIVATE_RELAY_WEB_DIR: path.join(root, "web"), OMP_PRIVATE_RELAY_CHECK_DIR: path.join(root, "check") });
		try {
			await waitForHealth(host as number);
			for (const port of [host, guest]) {
				await expect(fetch(`http://${external.address}:${port}/healthz`, { signal: AbortSignal.timeout(1500) })).rejects.toThrow();
			}
		} finally {
			proc.kill("SIGTERM");
			await proc.exited;
			fs.rmSync(root, { recursive: true, force: true });
		}
	});
});
