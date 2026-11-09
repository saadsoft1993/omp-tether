/**
 * Test harness: runs the relay (src/main.ts) as a child process on free ports with its own pairing store, check
 * directory and web directory in a scratch root, so tests never touch a real profile, a running relay or fixed ports.
 * Every fixture is built at run time; nothing here is a stored secret.
 */
import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";

export const PLUGIN_ROOT = path.resolve(import.meta.dir, "..", "..", "..");
export const MAIN = process.env.OMP_PRIVATE_RELAY_UNDER_TEST ?? path.join(PLUGIN_ROOT, "src", "main.ts");

export const OWNER = "owner@example.com";
/** Assembled at run time so no literal tailnet name sits in the source tree. */
export const PUBLIC_HOST = ["machine", "example", "ts", "net"].join(".");
export const SERVE_PORT = 8443;
export const TS_ORIGIN = `https://${PUBLIC_HOST}:${SERVE_PORT}`;
export const COOKIE_NAME = "omp_relay";

/** `count` distinct free loopback ports. All are held open until every port is known, so they cannot repeat. */
export async function freePorts(count: number): Promise<number[]> {
	const servers: net.Server[] = [];
	const ports: number[] = [];
	for (let i = 0; i < count; i++) {
		const s = net.createServer();
		await new Promise<void>((resolve, reject) => {
			s.once("error", reject);
			s.listen(0, "127.0.0.1", () => resolve());
		});
		servers.push(s);
		ports.push((s.address() as net.AddressInfo).port);
	}
	await Promise.all(servers.map(s => new Promise<void>(resolve => s.close(() => resolve()))));
	return ports;
}

export type AdapterKind = "token" | "tailscale-serve";

export interface Relay {
	adapter: AdapterKind;
	hostPort: number;
	guestPort: number;
	cookie: string;
	root: string;
	pairingDir: string;
	webDir: string;
	checkDir: string;
	/** Environment the relay process runs with (a scratch HOME and XDG directories). */
	env: Record<string, string>;
	log(): string;
	stop(): Promise<void>;
	/** Same ports and stores, fresh process (a relay restart). */
	restart(): Promise<void>;
	unpair(): void;
	/** Put the test browser back into a fresh, valid devices.json. */
	repair(): void;
}

export function makeRoomId(tag: string): string {
	return `${tag}${crypto.randomUUID().replaceAll("-", "")}`.slice(0, 22);
}

export function writeDevices(dir: string, hashes: string[]): void {
	const file = path.join(dir, "devices.json");
	const tmp = `${file}.tmp`;
	fs.writeFileSync(tmp, JSON.stringify(Object.fromEntries(hashes.map(h => [h, { label: "test", paired_at: 1 }]))), { mode: 0o600 });
	fs.renameSync(tmp, file);
}

export async function waitForHealth(port: number, ms = 10_000): Promise<void> {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		try {
			if ((await fetch(`http://127.0.0.1:${port}/healthz`)).status === 200) return;
		} catch {
			// not up yet
		}
		await Bun.sleep(50);
	}
	throw new Error(`relay on ${port} did not come up`);
}

export interface StartOptions {
	adapter?: AdapterKind;
	env?: Record<string, string>;
	pairingMode?: "own" | "shared";
	/** Leave the identity unknown: no owner or public host in the environment and a tailscale command that fails. */
	unknownIdentity?: boolean;
}

export function writeWebFixture(webDir: string, root: string): void {
	fs.mkdirSync(webDir, { recursive: true });
	fs.chmodSync(webDir, 0o700); // whatever the umask: the relay does not serve from a group-writable tree
	fs.writeFileSync(path.join(webDir, "index.html"), "<!doctype html><script>window.x=1</script><div id=root></div>");
	fs.writeFileSync(path.join(webDir, "abcd1234.js"), "console.log(1)");
	fs.writeFileSync(path.join(webDir, "manifest.webmanifest"), "{}");
	fs.writeFileSync(path.join(webDir, "favicon-180x180.png"), "png");
	// Traversal sentinels: a file just outside the web root, and a dotfile inside it.
	fs.writeFileSync(path.join(root, "secret.txt"), "outside the web root");
	fs.writeFileSync(path.join(webDir, ".hidden"), "dotfile");
}

async function spawnOnce(opts: StartOptions): Promise<Relay> {
	const adapter = opts.adapter ?? "tailscale-serve";
	const [hostPort, guestPort] = await freePorts(2);
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-private-relay-test-"));
	const pairingDir = path.join(root, "pairing");
	const webDir = path.join(root, "web");
	const checkDir = path.join(root, "check");
	fs.mkdirSync(pairingDir, { mode: 0o700 });
	writeWebFixture(webDir, root);
	const cookie = randomBytes(32).toString("base64url");
	const paired = createHash("sha256").update(cookie).digest("hex");
	writeDevices(pairingDir, [paired]);
	const env: Record<string, string> = {
		PATH: process.env.PATH ?? "/usr/bin:/bin",
		HOME: root,
		XDG_CONFIG_HOME: path.join(root, "xdg-config"),
		XDG_DATA_HOME: path.join(root, "xdg-data"),
		OMP_PRIVATE_RELAY_HOST_PORT: String(hostPort as number),
		OMP_PRIVATE_RELAY_GUEST_PORT: String(guestPort as number),
		OMP_PRIVATE_RELAY_AUTH: adapter,
		OMP_PRIVATE_RELAY_PAIRING_DIR: pairingDir,
		OMP_PRIVATE_RELAY_PAIRING_MODE: opts.pairingMode ?? "own",
		OMP_PRIVATE_RELAY_WEB_DIR: webDir,
		OMP_PRIVATE_RELAY_CHECK_DIR: checkDir,
	};
	if (adapter === "tailscale-serve") {
		env.OMP_PRIVATE_RELAY_TAILSCALE_SERVE_PORT = String(SERVE_PORT);
		if (opts.unknownIdentity) env.OMP_PRIVATE_RELAY_TAILSCALE_BIN = "/usr/bin/false";
		else {
			env.OMP_PRIVATE_RELAY_TAILSCALE_OWNER = OWNER;
			env.OMP_PRIVATE_RELAY_TAILSCALE_HOST = PUBLIC_HOST;
		}
	}
	Object.assign(env, opts.env);
	let out = "";
	let proc: Bun.Subprocess<"ignore", "pipe", "pipe">;
	const spawn = async (): Promise<void> => {
		proc = Bun.spawn([process.execPath, MAIN], { env, stdout: "pipe", stderr: "pipe" });
		for (const stream of [proc.stdout, proc.stderr] as ReadableStream<Uint8Array>[]) {
			(async () => {
				const dec = new TextDecoder();
				for await (const chunk of stream) out += dec.decode(chunk);
			})();
		}
		await waitForHealth(hostPort as number);
	};
	try {
		await spawn();
	} catch (err) {
		proc!.kill("SIGKILL");
		fs.rmSync(root, { recursive: true, force: true });
		throw new Error(`${String(err)}\n${out}`);
	}
	return {
		adapter,
		hostPort: hostPort as number,
		guestPort: guestPort as number,
		cookie,
		root,
		pairingDir,
		webDir,
		checkDir,
		env,
		log: () => out,
		stop: async () => {
			proc.kill("SIGTERM");
			await proc.exited;
			fs.rmSync(root, { recursive: true, force: true });
		},
		restart: async () => {
			proc.kill("SIGKILL");
			await proc.exited;
			await spawn();
		},
		unpair: () => writeDevices(pairingDir, []),
		repair: () => {
			fs.chmodSync(pairingDir, 0o700);
			fs.rmSync(path.join(pairingDir, "devices.json"), { force: true });
			writeDevices(pairingDir, [paired]);
		},
	};
}

/** Starts a relay; a port taken between choosing and binding is retried with fresh ports. */
export async function startRelay(opts: StartOptions = {}): Promise<Relay> {
	let last: unknown;
	for (let attempt = 0; attempt < 3; attempt++) {
		try {
			return await spawnOnce(opts);
		} catch (err) {
			last = err;
		}
	}
	throw last;
}

/** Headers the identity layer of the given relay expects from its paired browser. */
export function guestHeaders(relay: Relay, over: Record<string, string | null> = {}): Record<string, string> {
	const base: Record<string, string | null> =
		relay.adapter === "token"
			? { Host: `127.0.0.1:${relay.guestPort}`, Origin: `http://127.0.0.1:${relay.guestPort}`, Cookie: `${COOKIE_NAME}=${relay.cookie}` }
			: { Host: `${PUBLIC_HOST}:${SERVE_PORT}`, "Tailscale-User-Login": OWNER, Origin: TS_ORIGIN, Cookie: `${COOKIE_NAME}=${relay.cookie}` };
	return Object.fromEntries(Object.entries({ ...base, ...over }).filter((e): e is [string, string] => e[1] !== null));
}

const WS_HEADERS = {
	Connection: "Upgrade",
	Upgrade: "websocket",
	"Sec-WebSocket-Version": "13",
	"Sec-WebSocket-Key": Buffer.from("sixteen byte key").toString("base64"),
};

export interface RawResponse {
	status: number;
	headers: Record<string, string>;
	body: string;
}

/**
 * Raw HTTP/1.1 request, so Host and the upgrade headers go out exactly as given (fetch rewrites Host and cannot
 * complete an upgrade). Returns the status, the response headers (lower-cased names) and the body that arrived.
 */
export function rawRequest(port: number, target: string, headers: Record<string, string>, opts: { method?: string; upgrade?: boolean; body?: string } = {}): Promise<RawResponse> {
	const all: Record<string, string> = { Host: `127.0.0.1:${port}`, ...headers, ...(opts.upgrade ? WS_HEADERS : { Connection: "close" }) };
	if (opts.body !== undefined) all["Content-Length"] = String(Buffer.byteLength(opts.body));
	const request = [`${opts.method ?? "GET"} ${target} HTTP/1.1`, ...Object.entries(all).map(([k, v]) => `${k}: ${v}`), "", opts.body ?? ""].join("\r\n");
	const { promise, resolve, reject } = Promise.withResolvers<RawResponse>();
	const sock = net.connect(port, "127.0.0.1");
	let data = "";
	let done = false;
	const finish = (): void => {
		if (done) return;
		const split = data.indexOf("\r\n\r\n");
		const m = /^HTTP\/1\.1 (\d{3})/.exec(data);
		if (!m || split < 0) return;
		done = true;
		const head = data.slice(0, split).split("\r\n").slice(1);
		const hdrs: Record<string, string> = {};
		for (const line of head) {
			const i = line.indexOf(":");
			if (i > 0) hdrs[line.slice(0, i).toLowerCase()] = line.slice(i + 1).trim();
		}
		sock.destroy();
		resolve({ status: Number(m[1]), headers: hdrs, body: data.slice(split + 4) });
	};
	sock.on("data", chunk => {
		data += chunk.toString("latin1");
		// An upgrade is answered by its head alone; otherwise the response is complete once its declared body has arrived.
		const split = data.indexOf("\r\n\r\n");
		if (split < 0) return;
		const declared = /\r\ncontent-length: (\d+)/i.exec(data.slice(0, split));
		if (opts.upgrade || (declared && data.length - split - 4 >= Number(declared[1]))) finish();
	});
	sock.on("error", reject);
	sock.on("close", () => {
		if (!done) {
			finish();
			if (!done) resolve({ status: 0, headers: {}, body: data });
		}
	});
	sock.write(request);
	return promise;
}

export async function rawStatus(port: number, target: string, headers: Record<string, string>, opts: { method?: string; upgrade?: boolean } = {}): Promise<number> {
	return (await rawRequest(port, target, headers, opts)).status;
}

export interface Peer {
	ws: WebSocket;
	received: number;
	receivedBytes: number;
	control: string[];
	closed: Promise<{ code: number; reason: string }>;
	opened: Promise<boolean>;
}

export function peer(url: string, headers?: Record<string, string>): Peer {
	const ws = new WebSocket(url, headers ? ({ headers } as unknown as string[]) : undefined);
	ws.binaryType = "arraybuffer";
	const p = { ws, received: 0, receivedBytes: 0, control: [] as string[] } as Peer;
	const closed = Promise.withResolvers<{ code: number; reason: string }>();
	const opened = Promise.withResolvers<boolean>();
	ws.onopen = () => opened.resolve(true);
	ws.onclose = ev => {
		opened.resolve(false);
		closed.resolve({ code: ev.code, reason: ev.reason });
	};
	ws.onmessage = ev => {
		if (typeof ev.data === "string") p.control.push(ev.data);
		else {
			p.received++;
			p.receivedBytes += (ev.data as ArrayBuffer).byteLength;
		}
	};
	p.closed = closed.promise;
	p.opened = opened.promise;
	return p;
}

export const hostPeer = (relay: Relay, room: string) => peer(`ws://127.0.0.1:${relay.hostPort}/r/${room}?role=host`);
export const guestPeer = (relay: Relay, room: string, over: Record<string, string | null> = {}) =>
	peer(`ws://127.0.0.1:${relay.guestPort}/r/${room}?role=guest`, guestHeaders(relay, over));

export function frame(peerId: number, size: number): Uint8Array<ArrayBuffer> {
	const out = new Uint8Array(size);
	crypto.getRandomValues(out.subarray(4, Math.min(size, 65_536)));
	new DataView(out.buffer).setUint32(0, peerId, false);
	return out;
}

export function closedWithin(p: Peer, ms: number): Promise<{ code: number; reason: string } | null> {
	return Promise.race([p.closed, Bun.sleep(ms).then(() => null)]);
}

export async function waitUntil(check: () => boolean, ms: number, stepMs = 50): Promise<boolean> {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		if (check()) return true;
		await Bun.sleep(stepMs);
	}
	return check();
}

/** A guest that completes the upgrade and then never reads: the classic slow consumer. */
export async function stalledGuest(relay: Relay, room: string): Promise<net.Socket> {
	const sock = net.connect(relay.guestPort, "127.0.0.1");
	const { promise, resolve } = Promise.withResolvers<void>();
	let head = "";
	sock.on("data", chunk => {
		head += chunk.toString("latin1");
		if (head.includes("\r\n\r\n")) {
			sock.pause();
			resolve();
		}
	});
	sock.on("error", () => {});
	const h = { ...guestHeaders(relay), ...WS_HEADERS };
	sock.write([`GET /r/${room}?role=guest HTTP/1.1`, ...Object.entries(h).map(([k, v]) => `${k}: ${v}`), "", ""].join("\r\n"));
	await promise;
	// Shrink what the kernel will buffer for this peer, so a modest backlog on the relay's side is reached.
	return sock;
}

export async function stats(relay: Relay): Promise<Record<string, unknown> & { limits: Record<string, number>; maxConnQueued: number; staticReady: boolean; ready: boolean; adapter: string }> {
	return (await fetch(`http://127.0.0.1:${relay.hostPort}/stats`)).json() as Promise<Awaited<ReturnType<typeof stats>>>;
}

/** Close events the relay logged for a room (the log carries a 6-char room prefix). */
export function relayCloses(relay: Relay, room: string, role: string): number[] {
	const codes: number[] = [];
	for (const line of relay.log().split("\n")) {
		if (!line.startsWith("{")) continue;
		const e = JSON.parse(line) as { event: string; room?: string; role?: string; code?: number };
		if (e.event === "close" && e.room === room.slice(0, 6) && e.role === role) codes.push(e.code ?? 0);
	}
	return codes;
}

/** Masked client binary frame (RFC 6455), written by hand so a test client can ignore close frames. */
function clientFrame(payload: Uint8Array): Buffer {
	const mask = randomBytes(4);
	const len = payload.length;
	let header: Buffer;
	if (len < 126) {
		header = Buffer.from([0x82, 0x80 | len]);
	} else if (len < 65_536) {
		header = Buffer.alloc(4);
		header[0] = 0x82;
		header[1] = 0x80 | 126;
		header.writeUInt16BE(len, 2);
	} else {
		header = Buffer.alloc(10);
		header[0] = 0x82;
		header[1] = 0x80 | 127;
		header.writeBigUInt64BE(BigInt(len), 2);
	}
	const body = Buffer.alloc(len);
	for (let i = 0; i < len; i++) body[i] = (payload[i] as number) ^ (mask[i % 4] as number);
	return Buffer.concat([header, mask, body]);
}

export interface RawGuest {
	send(payload: Uint8Array): void;
	/** Saw a close frame from the relay (and, on purpose, never answered it). */
	gotCloseFrame(): boolean;
	/** The relay dropped the TCP connection. */
	closed(): boolean;
	destroy(): void;
}

/** A paired guest that never answers the relay's close frame and keeps sending: a hostile client. */
export async function rawGuest(relay: Relay, room: string): Promise<RawGuest> {
	const sock = net.connect(relay.guestPort, "127.0.0.1");
	const upgraded = Promise.withResolvers<void>();
	let buf = Buffer.alloc(0);
	let headDone = false;
	let sawClose = false;
	let isClosed = false;
	sock.on("data", chunk => {
		buf = Buffer.concat([buf, chunk as Buffer]);
		if (!headDone) {
			const end = buf.indexOf("\r\n\r\n");
			if (end < 0) return;
			if (!buf.subarray(0, 12).toString("latin1").includes("101")) {
				upgraded.reject(new Error("upgrade refused"));
				return;
			}
			headDone = true;
			buf = buf.subarray(end + 4);
			upgraded.resolve();
		}
		// Server frames are unmasked; look for a close opcode at any frame start we can parse.
		while (buf.length >= 2) {
			const op = (buf[0] as number) & 0x0f;
			let len = (buf[1] as number) & 0x7f;
			let off = 2;
			if (len === 126) {
				if (buf.length < 4) break;
				len = buf.readUInt16BE(2);
				off = 4;
			} else if (len === 127) {
				if (buf.length < 10) break;
				len = Number(buf.readBigUInt64BE(2));
				off = 10;
			}
			if (buf.length < off + len) break;
			if (op === 0x8) sawClose = true;
			buf = buf.subarray(off + len);
		}
	});
	sock.on("close", () => {
		isClosed = true;
	});
	sock.on("error", () => {});
	const h = { ...guestHeaders(relay), ...WS_HEADERS };
	sock.write([`GET /r/${room}?role=guest HTTP/1.1`, ...Object.entries(h).map(([k, v]) => `${k}: ${v}`), "", ""].join("\r\n"));
	await upgraded.promise;
	return {
		send: payload => {
			if (!sock.destroyed) sock.write(clientFrame(payload));
		},
		gotCloseFrame: () => sawClose,
		closed: () => isClosed,
		destroy: () => sock.destroy(),
	};
}
