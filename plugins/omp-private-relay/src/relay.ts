/**
 * The Collab relay: a private host listener for omp sessions and a gated guest listener for paired browsers.
 *
 * Room contract taken from omp's MIT stand-in `packages/collab-web/scripts/local-relay.ts`:
 * - `GET /r/<roomId>?role=host|guest` upgrades to a WebSocket.
 * - The host creates the room; a second host gets 4009, a guest of a missing room 4004.
 * - Host binary frames: envelope peerId 0 goes to every guest, peerId N to guest N, unchanged.
 * - Guest binary frames: the first 4 bytes are rewritten to the sender's peerId, then go to the host.
 * - TEXT control to the host: `{"t":"peer-joined","peer":N}` / `{"t":"peer-left","peer":N}`.
 * - Host gone: TEXT `{"t":"room-closed"}` to every guest, close 4001, room deleted.
 * The relay never sees plaintext.
 *
 * Added here:
 * - host listener: host upgrades only, no browser Origin, no proxy headers, loopback Host only;
 * - guest listener: every HTTP request and upgrade passes the configured auth adapter (identity layer) and then the
 *   pairing check (a paired browser cookie, checked as a hash against the pairing store); upgrades also need the guest
 *   Origin; serves the static web client and, in `own` pairing mode, the pairing page;
 * - open guests are re-checked against the pairing store and closed with 4401 once revoked;
 * - caps on rooms, guests per room, frame size and queued bytes (per connection and relay-wide); a guest over its queue
 *   cap is closed with 4008, which the browser treats as reconnectable;
 * - `/__relay_check` on both listeners: an HMAC under a private key file, so a program that wants to know whether the
 *   listener on our port really is this relay can ask (docs/protocol/relay-check-1.md).
 * Both listeners bind 127.0.0.1 and nothing else: there is no setting that moves them.
 * Logs carry connection metadata only (listener, role, room prefix, peer, device prefix, close code).
 */
import { createHash, createHmac, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { createAuth, type GuestAuth } from "./auth";
import type { Config } from "./config";
import { ensurePrivateDir, readSmallFile, UID } from "./fsutil";
import { CHECK_PREFIX } from "./listener-check";
import { PairingError, PairingReader, PairingWriter } from "./pairing-store";
import { PAIR_JS, PAIR_PAGE } from "./pair-page";
import { trustWalk } from "./trustwalk";

/** The only address either listener ever binds. */
export const LOOPBACK = "127.0.0.1";
const ROOM_PATH_RE = /^\/r\/([A-Za-z0-9_-]{10,64})$/;
const ENVELOPE_HEADER = 4;
export const CLOSE_NOT_PAIRED = 4401;
const CLOSE_SLOW = 4008;
const CLOSE_FULL = 4029;
/** A socket the relay closes gets this long to take its close frame before it is cut. */
const CLOSE_GRACE_MS = 5_000;
const PAIR_FAILURES_PER_MINUTE = 10;
const PAIR_BODY_MAX = 1024;

/**
 * The web client's manifest and icons. Browsers fetch the web manifest without cookies, and iOS fetches icons for the
 * home-screen app the same way, so these get the identity check but not the pairing check. They hold no data.
 */
const COOKIELESS_ASSET_RE = /^\/(?:[a-z0-9]{8}\.(?:webmanifest|png|ico|svg)|manifest\.webmanifest|favicon(?:-\d+x\d+)?\.(?:png|ico|svg))$/;

interface SocketData {
	listener: "host" | "guest";
	roomId: string;
	role: "host" | "guest";
	peerId: number;
	/** SHA-256 hex of the guest's pairing cookie; never the cookie itself. */
	device: string;
	closing: boolean;
}
type RelaySocket = Bun.ServerWebSocket<SocketData>;

interface Room {
	host: RelaySocket;
	guests: Map<number, RelaySocket>;
	nextPeerId: number;
}

export interface RelayHandle {
	hostPort: number;
	guestPort: number;
	stop(): void;
}

export interface RelayOptions {
	/** Replaces the adapter built from the configuration (tests). */
	auth?: GuestAuth;
	log?: (line: string) => void;
}

type Gate = { ok: true; device: string } | { ok: false; status: number; reason: string };

export function startRelay(config: Config, opts: RelayOptions = {}): RelayHandle {
	const { limits } = config;
	const startedAt = Date.now();
	const stats = {
		startedAt: new Date().toISOString(),
		maxHostFrame: 0,
		maxGuestFrame: 0,
		maxConnQueued: 0,
		maxRelayQueued: 0,
		closes: {} as Record<string, number>,
		denied: {} as Record<string, number>,
	};
	const rooms = new Map<string, Room>();
	const sockets = new Set<RelaySocket>();
	const write = opts.log ?? ((line: string) => console.log(line));

	const log = (event: string, fields: Record<string, unknown> = {}): void => {
		write(JSON.stringify({ t: new Date().toISOString(), event, ...fields }));
	};
	const meta = (ws: RelaySocket): Record<string, unknown> => {
		const d = ws.data;
		return { listener: d.listener, role: d.role, room: d.roomId.slice(0, 6), peer: d.peerId, device: d.device.slice(0, 8) || undefined };
	};

	const auth = opts.auth ?? createAuth(config);
	const reader = new PairingReader(config.pairing.dir, reason => log("pairing_store_refused", { reason }));
	const writer = config.pairing.mode === "own" ? new PairingWriter(config.pairing.dir) : null;
	const cookieName = config.pairing.cookieName;

	// ── static web client ───────────────────────────────────────────────────────────────────────
	// The tree the browser client is served from must not be replaceable by another account. Its path is walked component by
	// component (symbolic links followed and their targets checked too, owners, writable bits, ACLs, shared groups) at most
	// every five seconds, and the client is served from the RESOLVED directory, whose identity is re-checked on every use: a
	// path swapped for another directory after the walk is not served. Error pages and the health answers never read the tree.
	const MAX_INDEX_BYTES = 2 * 1024 * 1024;
	const BASE_HEADERS: Record<string, string> = {
		"Referrer-Policy": "no-referrer",
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options": "DENY",
		"Cross-Origin-Opener-Policy": "same-origin",
	};
	const PLAIN_HEADERS: Record<string, string> = {
		"Content-Type": "text/plain; charset=utf-8",
		"Cache-Control": "no-store",
		"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
		...BASE_HEADERS,
	};
	let web = { at: 0, problem: null as string | null, root: null as string | null, dev: 0, ino: 0 };
	function webState(): { problem: string | null; root: string | null } {
		if (web.root !== null) {
			try {
				const st = fs.lstatSync(web.root);
				if (!st.isDirectory() || st.ino !== web.ino || st.dev !== web.dev) web = { ...web, at: 0 };
			} catch {
				web = { ...web, at: 0 };
			}
		}
		if (Date.now() - web.at > 5_000) {
			const walk = trustWalk(config.web.dir);
			let root: string | null = null;
			let dev = 0;
			let ino = 0;
			if (!walk.problem && walk.real) {
				try {
					const st = fs.lstatSync(walk.real);
					root = walk.real;
					dev = st.dev;
					ino = st.ino;
				} catch {
					// vanished between the walk and now: treated as not installed
				}
			}
			if (walk.problem && !web.problem) log("web_dir_unsafe", {});
			web = { at: Date.now(), problem: walk.problem, root, dev, ino };
		}
		return web;
	}

	function buildCsp(html: string): string {
		const hashes: string[] = [];
		for (const m of html.matchAll(/<script(?![^>]*\bsrc=)(?![^>]*application\/ld\+json)[^>]*>([\s\S]*?)<\/script>/g)) {
			hashes.push(`'sha256-${createHash("sha256").update(m[1] as string).digest("base64")}'`);
		}
		return [
			"default-src 'none'",
			`script-src 'self' ${hashes.join(" ")}`.trim(),
			"style-src 'self' 'unsafe-inline'",
			"img-src 'self' data: blob:",
			"font-src 'self' data:",
			`connect-src 'self' ${auth.connectSources().join(" ")}`.trim(),
			"manifest-src 'self'",
			"base-uri 'none'",
			"form-action 'none'",
			"frame-ancestors 'none'",
		].join("; ");
	}

	/** The headers of a static answer, built from a regular index.html inside the approved tree; null when there is none. Recomputed when index.html or the adapter's connect sources change. */
	let headersCache: { key: string; headers: Record<string, string> | null } | null = null;
	function staticHeaders(root: string): Record<string, string> | null {
		const index = path.join(root, "index.html");
		let st: fs.Stats;
		try {
			st = fs.lstatSync(index);
		} catch {
			return null;
		}
		if (!st.isFile()) return null;
		const key = `${root}|${st.ino}:${st.size}:${st.mtimeMs}|${auth.connectSources().join(" ")}`;
		if (headersCache?.key !== key) {
			const html = readSmallFile(index, MAX_INDEX_BYTES);
			headersCache = { key, headers: html === null ? null : { ...BASE_HEADERS, "Content-Security-Policy": buildCsp(html.toString("utf8")) } };
		}
		return headersCache.headers;
	}
	const staticReady = (): boolean => {
		const state = webState();
		return !state.problem && state.root !== null && staticHeaders(state.root) !== null;
	};

	function text(status: number, body: string, extra: Record<string, string> = {}): Response {
		return new Response(body, { status, headers: { ...PLAIN_HEADERS, ...extra } });
	}

	function staticResponse(pathname: string, method: string): Response {
		const state = webState();
		if (state.problem) return text(503, "the web client's directory is not safe to serve from (another account could replace it); run: omp-private-relay doctor");
		const root = state.root;
		const headers = root === null ? null : staticHeaders(root);
		if (root === null || headers === null) return text(503, "the web client is not installed; run: omp-private-relay init");
		let rel: string;
		try {
			rel = pathname === "/" ? "index.html" : decodeURIComponent(pathname.slice(1));
		} catch {
			return text(400, "bad request");
		}
		if (rel.split("/").some(seg => seg === "" || seg === "." || seg === ".." || seg.startsWith("."))) return text(404, "not found");
		const file = path.resolve(root, rel);
		let real: string;
		try {
			real = fs.realpathSync(file);
			if (!real.startsWith(`${root}${path.sep}`) || !fs.statSync(real).isFile()) return text(404, "not found");
		} catch {
			return text(404, "not found");
		}
		const hashed = /^[a-z0-9]{8}\.(js|css|svg|png|ico|webmanifest)$/.test(rel);
		return new Response(method === "HEAD" ? null : Bun.file(real), {
			headers: { ...headers, "Cache-Control": hashed ? "private, max-age=31536000, immutable" : "no-cache" },
		});
	}

	// ── guest gate ──────────────────────────────────────────────────────────────────────────────
	function deny(status: number, reason: string): Gate {
		stats.denied[reason] = (stats.denied[reason] ?? 0) + 1;
		return { ok: false, status, reason };
	}

	/** Identity layer (adapter), then the pairing check, for every guest request and upgrade. */
	function guestGate(req: Request, upgrade: boolean, pathname: string): Gate {
		const verdict = auth.check({ headers: req.headers, upgrade });
		if (!verdict.ok) return deny(verdict.status, verdict.reason);
		const device = reader.match(req.headers.get("cookie"), cookieName);
		if (device) return { ok: true, device };
		if (!upgrade && isPairingRoute(pathname, req.method)) return { ok: true, device: "" };
		if (!upgrade && COOKIELESS_ASSET_RE.test(pathname)) return { ok: true, device: "" };
		return deny(401, "browser not paired");
	}

	// ── pairing endpoints (own mode only) ───────────────────────────────────────────────────────
	const pairingOwn = writer !== null;
	/** The pairing page, its script and the redeem call: reachable without a cookie (that is their purpose), never without the identity layer. */
	function isPairingRoute(pathname: string, method: string): boolean {
		if (!pairingOwn) return false;
		if (method === "POST") return pathname === "/__pair";
		return (method === "GET" || method === "HEAD") && (pathname === "/pair" || pathname === "/__pair.js");
	}

	const pairFailures: number[] = [];
	const pairHeaders = (): Record<string, string> => ({
		"Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
		"Referrer-Policy": "no-referrer",
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options": "DENY",
		"Cross-Origin-Opener-Policy": "same-origin",
		"Cache-Control": "no-store",
	});

	function pairAsset(pathname: string, method: string): Response {
		const page = pathname === "/pair";
		return new Response(method === "HEAD" ? null : page ? PAIR_PAGE : PAIR_JS, {
			headers: { ...pairHeaders(), "Content-Type": page ? "text/html; charset=utf-8" : "text/javascript; charset=utf-8" },
		});
	}

	let pairInflight = 0;

	/** The request body as text, or null once it passes PAIR_BODY_MAX bytes (counted as they arrive, not as declared). */
	async function readBounded(req: Request): Promise<string | null> {
		const stream = req.body?.getReader();
		if (!stream) return "";
		const chunks: Uint8Array[] = [];
		let total = 0;
		for (;;) {
			const { done, value } = await stream.read();
			if (done) break;
			total += value.byteLength;
			if (total > PAIR_BODY_MAX) {
				await stream.cancel().catch(() => {});
				return null;
			}
			chunks.push(value);
		}
		return Buffer.concat(chunks).toString("utf8");
	}

	async function redeem(req: Request): Promise<Response> {
		const json = (status: number, body: unknown, extra: Record<string, string> = {}): Response =>
			new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...extra } });
		// A custom header makes this a non-simple request, so a page on another origin cannot send it without a preflight
		// that this server never approves; the Origin check (adapter) must also have named a page of ours.
		const origin = req.headers.get("origin");
		if (req.headers.get("x-omp-relay") !== "1" || origin === null || !auth.origins().includes(origin)) return json(403, { error: "forbidden" });
		// Framing: a declared length is required (chunked bodies are refused), and the bytes are counted as they arrive.
		const lengthHeader = req.headers.get("content-length");
		if (req.headers.has("transfer-encoding") || lengthHeader === null) return json(411, { error: "a Content-Length is required" });
		const declared = Number(lengthHeader);
		if (!Number.isInteger(declared) || declared < 0 || declared > PAIR_BODY_MAX) return json(413, { error: "bad body" });
		// Attempts are reserved before anything is awaited, so simultaneous requests cannot all pass a check made before any
		// of them has failed: failures in the last minute plus attempts still in flight stay within the budget.
		const now = Date.now();
		while (pairFailures.length > 0 && now - (pairFailures[0] as number) > 60_000) pairFailures.shift();
		if (pairFailures.length + pairInflight >= PAIR_FAILURES_PER_MINUTE) return json(429, { error: "too many attempts; wait a minute" });
		pairInflight++;
		try {
			const raw = await readBounded(req);
			if (raw === null) return json(413, { error: "bad body" });
			let body: unknown;
			try {
				body = JSON.parse(raw);
			} catch {
				pairFailures.push(now);
				return json(400, { error: "bad body" });
			}
			const token = (body as { token?: unknown } | null)?.token;
			let session: string | null;
			try {
				session = await (writer as PairingWriter).redeem(token, req.headers.get("user-agent"));
			} catch (err) {
				log("pairing_failed", { reason: err instanceof PairingError ? err.code : "error" });
				return json(500, { error: "the pairing store could not be updated" });
			}
			if (session === null) {
				pairFailures.push(now);
				return json(403, { error: "pairing link was already used or expired; run `omp-private-relay pair` again" });
			}
			log("paired", { device: createHash("sha256").update(session).digest("hex").slice(0, 8) });
			const cookie = `${cookieName}=${session}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict${auth.secureCookies() ? "; Secure" : ""}`;
			return json(200, { ok: true }, { "Set-Cookie": cookie });
		} finally {
			pairInflight--;
		}
	}

	function notPairedHelp(): string {
		const how = pairingOwn
			? ["To pair it (once per browser):", "  1. In a terminal on the machine that runs omp, run:", "       omp-private-relay pair", "  2. Open the link it prints in THIS browser within 5 minutes.", "  3. Then open the session link again."]
			: ["Pair it with the program that manages your pairing store (the relay only reads it), then open the session link again."];
		return ["browser not paired", "", "This browser has not been paired with the relay, so it will not open sessions.", "", ...how].join("\n");
	}

	function crossSiteHelp(): string {
		return [
			"cross-site request",
			"",
			"This page was opened from another site (a chat, a mail client, a document or a redirect), and the relay does not accept being opened that way.",
			"",
			"To fix it: copy the link, paste it into the address bar of the browser you want to use, and press Enter. If the link has expired or was already used, run:",
			"       omp-private-relay pair",
		].join("\n");
	}

	// ── room plumbing ───────────────────────────────────────────────────────────────────────────
	function relayQueued(): number {
		let total = 0;
		for (const s of sockets) total += s.getBufferedAmount();
		return total;
	}

	function closeSocket(ws: RelaySocket, code: number, reason: string): void {
		if (ws.data.closing) return;
		ws.data.closing = true;
		ws.close(code, reason);
		// The close frame may wait behind a backlog (4008), or the peer may ignore it (a revoked client that keeps the TCP
		// connection open): either way the socket is cut after a short grace.
		setTimeout(() => {
			if (sockets.has(ws)) ws.terminate();
		}, CLOSE_GRACE_MS).unref();
	}

	/** Send with queue caps; a socket over its cap is closed instead of buffering without bound. */
	function send(ws: RelaySocket, data: string | Uint8Array): void {
		if (ws.data.closing) return;
		const size = typeof data === "string" ? Buffer.byteLength(data) : data.byteLength;
		const queued = ws.getBufferedAmount();
		if (queued + size > limits.connQueueMax) {
			log("slow_consumer", { ...meta(ws), queued, size });
			closeSocket(ws, CLOSE_SLOW, "slow consumer");
			return;
		}
		const total = relayQueued();
		if (total + size > limits.relayQueueMax) {
			log("relay_queue_full", { ...meta(ws), total, size });
			closeSocket(ws, CLOSE_SLOW, "relay queue full");
			return;
		}
		if (ws.send(data) === 0) {
			closeSocket(ws, CLOSE_SLOW, "send dropped");
			return;
		}
		const after = ws.getBufferedAmount();
		if (after > stats.maxConnQueued) stats.maxConnQueued = after;
		// `total` already counted this socket's queue before the send.
		if (total - queued + after > stats.maxRelayQueued) stats.maxRelayQueued = total - queued + after;
	}

	/** Upgrades accepted but not yet open: they count against the connection cap, so a burst cannot get past it. */
	let upgrading = 0;
	/** True when one more WebSocket may be accepted. Checked before the upgrade, so a refused client never gets a socket. */
	function admit(): boolean {
		if (sockets.size + upgrading >= limits.maxConnections) return false;
		upgrading++;
		return true;
	}

	function onOpen(ws: RelaySocket): void {
		upgrading = Math.max(0, upgrading - 1);
		sockets.add(ws);
		if (sockets.size > limits.maxConnections) {
			// Not reachable through admit(); if it ever happens the socket is cut at once, without a close handshake.
			ws.terminate();
			return;
		}
		const { roomId, role } = ws.data;
		if (role === "host") {
			if (rooms.has(roomId)) {
				closeSocket(ws, 4009, "a host is already connected for this room");
				return;
			}
			if (rooms.size >= limits.maxRooms) {
				closeSocket(ws, CLOSE_FULL, "relay is full");
				return;
			}
			rooms.set(roomId, { host: ws, guests: new Map(), nextPeerId: 1 });
			log("open", meta(ws));
			return;
		}
		const room = rooms.get(roomId);
		if (!room) {
			// After a restart, hosts need up to ~37 s to come back (omp's backoff caps at 30 s, ±25 % jitter). A browser that
			// reconnects first would get 4004, which the web client treats as final; during the grace window it gets 4001.
			if (Date.now() - startedAt < limits.restartGraceMs) closeSocket(ws, 4001, "room not back yet");
			else closeSocket(ws, 4004, "no such room");
			return;
		}
		if (room.guests.size >= limits.maxGuestsPerRoom) {
			closeSocket(ws, CLOSE_FULL, "room is full");
			return;
		}
		const peerId = room.nextPeerId++;
		ws.data.peerId = peerId;
		room.guests.set(peerId, ws);
		log("open", meta(ws));
		send(room.host, JSON.stringify({ t: "peer-joined", peer: peerId }));
	}

	function onMessage(ws: RelaySocket, message: string | Buffer): void {
		if (typeof message === "string") return; // clients never send TEXT
		if (ws.data.closing) return; // revoked, slow or refused: nothing more from it is relayed
		const room = rooms.get(ws.data.roomId);
		if (!room) return;
		if (ws.data.role === "host") {
			if (room.host !== ws) return;
			if (message.byteLength > stats.maxHostFrame) stats.maxHostFrame = message.byteLength;
			if (message.byteLength < ENVELOPE_HEADER) return;
			const peerId = message.readUInt32BE(0);
			if (peerId === 0) {
				for (const guest of room.guests.values()) send(guest, message);
			} else {
				const guest = room.guests.get(peerId);
				if (guest) send(guest, message);
			}
			return;
		}
		if (message.byteLength > stats.maxGuestFrame) stats.maxGuestFrame = message.byteLength;
		if (message.byteLength < ENVELOPE_HEADER) return;
		if (room.guests.get(ws.data.peerId) !== ws) return;
		message.writeUInt32BE(ws.data.peerId, 0);
		send(room.host, message);
	}

	function onClose(ws: RelaySocket, code: number): void {
		sockets.delete(ws);
		stats.closes[String(code)] = (stats.closes[String(code)] ?? 0) + 1;
		log("close", { ...meta(ws), code });
		const { roomId, role, peerId } = ws.data;
		const room = rooms.get(roomId);
		if (!room) return;
		if (role === "host") {
			if (room.host !== ws) return; // a refused second host
			rooms.delete(roomId);
			const closure = JSON.stringify({ t: "room-closed" });
			for (const guest of room.guests.values()) {
				send(guest, closure);
				closeSocket(guest, 4001, "room closed");
			}
			room.guests.clear();
			return;
		}
		if (room.guests.get(peerId) === ws) {
			room.guests.delete(peerId);
			send(room.host, JSON.stringify({ t: "peer-left", peer: peerId }));
		}
	}

	function websocketHandlers(maxPayloadLength: number): Bun.WebSocketHandler<SocketData> {
		return {
			maxPayloadLength,
			// Above the per-connection cap so Bun never silently drops a frame; send() closes first.
			backpressureLimit: limits.connQueueMax + limits.maxHostFrame,
			closeOnBackpressureLimit: true,
			idleTimeout: 120,
			sendPings: true,
			open: onOpen,
			message: onMessage,
			close: (ws, code) => onClose(ws, code),
		};
	}

	function roomRequest(url: URL): { roomId: string; role: "host" | "guest" } | null {
		const match = ROOM_PATH_RE.exec(url.pathname);
		const role = url.searchParams.get("role");
		if (!match || (role !== "host" && role !== "guest")) return null;
		return { roomId: match[1] as string, role };
	}

	const loopbackHosts = new Set([`127.0.0.1:${config.listen.hostPort}`, `localhost:${config.listen.hostPort}`]);

	// ── listener check (relay-check/1) ──────────────────────────────────────────────────────────
	let checkKey: Buffer | null = null;

	/** A fresh random key in <checkDir>/check.key (0600, in a 0700 directory we own), rewritten at every start. */
	function writeCheckKey(): void {
		try {
			ensurePrivateDir(config.checkDir, { refuseOpen: true });
			const key = randomBytes(32);
			const tmp = path.join(config.checkDir, `.check.key.${process.pid}`);
			fs.rmSync(tmp, { force: true });
			fs.writeFileSync(tmp, key.toString("hex"), { mode: 0o600, flag: "wx" });
			fs.renameSync(tmp, path.join(config.checkDir, "check.key"));
			checkKey = key;
		} catch (err) {
			checkKey = null;
			log("check_key_failed", { reason: err instanceof Error ? err.name : "error" });
		}
	}

	/** `GET /__relay_check?nonce=<hex>` from a direct loopback client: HMAC(key, prefix + port + nonce). */
	function relayCheck(req: Request, url: URL, port: number): Response {
		const h = req.headers;
		if (h.get("host") !== `127.0.0.1:${port}` || h.has("origin") || h.has("tailscale-user-login") || h.has("x-forwarded-for") || h.has("forwarded")) {
			return text(403, "forbidden");
		}
		const nonce = url.searchParams.get("nonce") ?? "";
		if (!/^[0-9a-f]{32,128}$/.test(nonce)) return text(400, "bad nonce");
		if (!checkKey) return text(503, "no check key");
		const mac = createHmac("sha256", checkKey).update(`${CHECK_PREFIX}:${port}:${nonce}`).digest("hex");
		return new Response(JSON.stringify({ mac }), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
	}

	/** `GET /__relay_stats?nonce=<hex>`: the counts of /stats, with a MAC over them under the check key (relay-check/1, section 4). */
	function relayStats(req: Request, url: URL, port: number): Response {
		const h = req.headers;
		if (h.get("host") !== `127.0.0.1:${port}` || h.has("origin") || h.has("tailscale-user-login") || h.has("x-forwarded-for") || h.has("forwarded")) {
			return text(403, "forbidden");
		}
		const nonce = url.searchParams.get("nonce") ?? "";
		if (!/^[0-9a-f]{32,128}$/.test(nonce)) return text(400, "bad nonce");
		if (!checkKey) return text(503, "no check key");
		const body = statsBody();
		const mac = createHmac("sha256", checkKey).update(`${CHECK_PREFIX}:stats:${port}:${nonce}:${body}`).digest("hex");
		return new Response(JSON.stringify({ body, mac }), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
	}

	const serveOptions = {
		hostname: LOOPBACK,
		// A second relay must fail on a taken port, not share it (macOS would otherwise let two listeners bind the same port).
		reusePort: false,
		development: false,
		error(err: Error): Response {
			log("handler_error", { error: err.name });
			return new Response("internal error", { status: 500, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } });
		},
	};

	function statsBody(): string {
		let guests = 0;
		for (const room of rooms.values()) guests += room.guests.size;
		let build: unknown = null;
		// only from the approved tree, through a bounded read of a regular file (never from a tree that was rejected)
		const root = webState().problem ? null : web.root;
		const raw = root === null ? null : readSmallFile(path.join(root, ".omp-private-relay-build.json"), 65_536);
		try {
			if (raw !== null) build = JSON.parse(raw.toString("utf8"));
		} catch {
			// not installed by init
		}
		return JSON.stringify({
			...stats,
			adapter: auth.name,
			rooms: rooms.size,
			guests,
			sockets: sockets.size,
			relayQueued: relayQueued(),
			uptimeS: Math.round((Date.now() - startedAt) / 1000),
			inRestartGrace: Date.now() - startedAt < limits.restartGraceMs,
			staticReady: staticReady(),
			build: build && typeof build === "object" ? { ompTag: (build as Record<string, unknown>).ompTag ?? null, bun: (build as Record<string, unknown>).bun ?? null } : null,
			ready: auth.ready(),
			limits: { ...limits },
		});
	}

	// ── listeners ───────────────────────────────────────────────────────────────────────────────
	const adapterRun = auth.start({
		log,
		dropGuests: reason => {
			for (const ws of sockets) if (ws.data.listener === "guest") closeSocket(ws, CLOSE_NOT_PAIRED, reason);
		},
	});


	const hostServer = Bun.serve<SocketData>({
		...serveOptions,
		port: config.listen.hostPort,
		fetch(req, srv) {
			const url = new URL(req.url);
			const h = req.headers;
			// Browsers always send Origin on WebSocket upgrades; omp hosts never do. Proxy headers mean this port was exposed
			// through a proxy by mistake.
			if (h.has("origin")) return text(403, "host listener: browsers are not allowed");
			if (h.has("tailscale-user-login") || h.has("x-forwarded-for") || h.has("forwarded")) {
				return text(403, "host listener: proxied requests are not allowed");
			}
			if (!loopbackHosts.has((h.get("host") ?? "").toLowerCase())) return text(403, "host listener: wrong host");
			if (url.pathname === "/healthz") return text(200, "ok");
			if (url.pathname === "/__relay_check") return relayCheck(req, url, config.listen.hostPort);
			if (url.pathname === "/__relay_stats") return relayStats(req, url, config.listen.hostPort);
			if (url.pathname === "/stats") return new Response(statsBody(), { headers: { "Content-Type": "application/json" } });
			const room = roomRequest(url);
			if (!room) return text(404, "not found");
			if (room.role !== "host") return text(403, "host listener: browsers use the guest listener");
			const data: SocketData = { listener: "host", ...room, peerId: 0, device: "", closing: false };
			if (!admit()) return text(503, "too many connections");
			if (srv.upgrade(req, { data })) return undefined;
			upgrading--;
			return text(426, "websocket upgrade required");
		},
		websocket: websocketHandlers(limits.maxHostFrame),
	});

	let guestServer: Bun.Server<SocketData>;
	try {
	guestServer = Bun.serve<SocketData>({
		...serveOptions,
		port: config.listen.guestPort,
		// The only request body this listener reads is a pairing link; Bun refuses anything bigger before the handler runs.
		maxRequestBodySize: PAIR_BODY_MAX * 4,
		async fetch(req, srv) {
			const url = new URL(req.url);
			if (url.pathname === "/__relay_check") return relayCheck(req, url, config.listen.guestPort);
			const upgrade = (req.headers.get("upgrade") ?? "").toLowerCase() === "websocket";
			const gate = guestGate(req, upgrade, url.pathname);
			if (!gate.ok) {
				// Only a bounded, plain path is logged: a hostile client chooses this text.
				const shown = url.pathname.startsWith("/r/") ? "/r/…" : /^\/[A-Za-z0-9._-]{0,63}$/.test(url.pathname) ? url.pathname : "(other)";
				log("denied", { listener: "guest", path: shown, upgrade, status: gate.status, reason: gate.reason });
				if (gate.status === 401 && !upgrade) return text(401, notPairedHelp());
				if (gate.reason === "cross-site request" && !upgrade) return text(gate.status, crossSiteHelp());
				return text(gate.status, gate.reason);
			}
			if (pairingOwn && req.method === "POST" && url.pathname === "/__pair") return redeem(req);
			if (req.method !== "GET" && req.method !== "HEAD") return text(405, "method not allowed");
			if (!upgrade) {
				if (url.pathname.startsWith("/r/")) return text(426, "websocket upgrade required");
				if (isPairingRoute(url.pathname, req.method)) return pairAsset(url.pathname, req.method);
				return staticResponse(url.pathname, req.method);
			}
			const room = roomRequest(url);
			if (!room) return text(404, "not found");
			if (room.role !== "guest") return text(403, "guest listener: hosts use the loopback listener");
			const data: SocketData = { listener: "guest", ...room, peerId: 0, device: gate.device, closing: false };
			if (!admit()) return text(503, "too many connections");
			if (srv.upgrade(req, { data })) return undefined;
			upgrading--;
			return text(426, "websocket upgrade required");
		},
		websocket: websocketHandlers(limits.maxGuestFrame),
	});
	} catch (err) {
		hostServer.stop(true);
		adapterRun.stop();
		throw err;
	}
	// The listener proof key is published only after BOTH listeners are bound: a second start that fails on a taken port
	// must not replace the key that the running relay's proofs are made with.
	writeCheckKey();

	// Revocation reaches open connections; an unusable store closes every guest (nobody is paired).
	const recheck = setInterval(() => {
		const { hashes } = reader.read();
		for (const ws of sockets) {
			if (ws.data.listener === "guest" && !hashes.has(ws.data.device)) {
				log("revoked", meta(ws));
				closeSocket(ws, CLOSE_NOT_PAIRED, "browser no longer paired");
			}
		}
	}, limits.recheckMs);

	log("listening", {
		adapter: auth.name,
		host: `ws://${LOOPBACK}:${hostServer.port}`,
		guest: `http://${LOOPBACK}:${guestServer.port}`,
		pairing: config.pairing.mode,
		uid: UID,
		static: staticReady() ? "ready" : "missing (run: omp-private-relay init)",
	});

	return {
		hostPort: hostServer.port as number,
		guestPort: guestServer.port as number,
		stop(): void {
			clearInterval(recheck);
			adapterRun.stop();
			for (const room of rooms.values()) {
				const closure = JSON.stringify({ t: "room-closed" });
				for (const guest of room.guests.values()) {
					send(guest, closure);
					closeSocket(guest, 4001, "room closed");
				}
				room.host.close(1001, "relay shutting down");
			}
			rooms.clear();
			hostServer.stop(true);
			guestServer.stop(true);
		},
	};
}
