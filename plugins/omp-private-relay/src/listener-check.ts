// relay-check/1 (docs/protocol/relay-check-1.md), client side: prove that the program listening on a loopback port is
// the relay this user started. The relay writes a fresh random key to <checkDir>/check.key (0600, in a 0700 directory
// of ours) at every start and answers `GET /__relay_check?nonce=<hex>` with HMAC-SHA256(key, "<prefix>:<port>:<nonce>").
// Another account that grabbed the port while the relay was down cannot read the key, so it cannot answer.
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { NOFOLLOW, NONBLOCK, UID } from "./fsutil";

/** Domain separator of the proof: HMAC-SHA256(key, `${CHECK_PREFIX}:${port}:${nonce}`). */
export const CHECK_PREFIX = "omp-private-relay-check/1";

export type KeyRead = { ok: true; key: Buffer } | { ok: false; reason: string };

/** The key file, read only when the directory and the file are private and ours. */
export function readCheckKey(checkDir: string): KeyRead {
	let dir: fs.Stats;
	try {
		dir = fs.lstatSync(checkDir);
	} catch {
		return { ok: false, reason: "no check directory (is the relay installed and started?)" };
	}
	if (dir.isSymbolicLink() || !dir.isDirectory() || dir.uid !== UID || dir.mode & 0o077) {
		return { ok: false, reason: "the check directory is not a private directory of ours" };
	}
	let fd: number;
	try {
		fd = fs.openSync(path.join(checkDir, "check.key"), fs.constants.O_RDONLY | NOFOLLOW | NONBLOCK);
	} catch {
		return { ok: false, reason: "no check key (the relay has not started with this directory)" };
	}
	try {
		const st = fs.fstatSync(fd);
		if (!st.isFile() || st.uid !== UID || st.mode & 0o077 || st.size > 1024) return { ok: false, reason: "the check key file is not private" };
		const hex = fs.readFileSync(fd, "utf8").trim();
		if (!/^[0-9a-f]{64}$/.test(hex)) return { ok: false, reason: "the check key file is malformed" };
		return { ok: true, key: Buffer.from(hex, "hex") };
	} finally {
		fs.closeSync(fd);
	}
}

export type Proof = { ok: true } | { ok: false; reason: string };

/**
 * The response body as text. The bound is in BYTES, counted before a chunk is kept: a chunk that would pass `limit` is not
 * copied anywhere and the stream is abandoned. The kept bytes are decoded once, so a character split across two chunks is
 * decoded whole.
 */
export async function readLimited(r: Response, limit: number): Promise<string | null> {
	const reader = r.body?.getReader();
	const chunks: Uint8Array[] = [];
	let total = 0;
	try {
		while (reader) {
			const { done, value } = await reader.read();
			if (done) break;
			if (total + value.byteLength > limit) return null;
			total += value.byteLength;
			chunks.push(value);
		}
	} finally {
		await reader?.cancel().catch(() => {});
	}
	return Buffer.concat(chunks).toString("utf8");
}

/** Challenges the listener on 127.0.0.1:<port> with a fresh nonce and checks its answer. */
export async function proveListener(port: number, key: Buffer, timeoutMs = 3_000): Promise<Proof> {
	const nonce = randomBytes(16).toString("hex");
	let body: unknown;
	try {
		const r = await fetch(`http://127.0.0.1:${port}/__relay_check?nonce=${nonce}`, { signal: AbortSignal.timeout(timeoutMs), redirect: "manual" });
		if (r.status !== 200) return { ok: false, reason: `answered ${r.status}` };
		const text = await readLimited(r, 1024);
		if (text === null) return { ok: false, reason: "answer too large" };
		body = JSON.parse(text);
	} catch {
		return { ok: false, reason: "no answer" };
	}
	const mac = (body as { mac?: unknown } | null)?.mac;
	if (typeof mac !== "string" || !/^[0-9a-f]{64}$/.test(mac)) return { ok: false, reason: "malformed answer" };
	const want = createHmac("sha256", key).update(`${CHECK_PREFIX}:${port}:${nonce}`).digest();
	return timingSafeEqual(want, Buffer.from(mac, "hex")) ? { ok: true } : { ok: false, reason: "wrong proof: that is not this relay" };
}

export type ProvenStats = { ok: true; stats: Record<string, unknown> } | { ok: false; reason: string };

/** The relay's counts, believed only because they carry a MAC over exactly those bytes under a fresh nonce (relay-check/1). */
export async function fetchProvenStats(port: number, key: Buffer, timeoutMs = 3_000): Promise<ProvenStats> {
	const nonce = randomBytes(16).toString("hex");
	try {
		const r = await fetch(`http://127.0.0.1:${port}/__relay_stats?nonce=${nonce}`, { signal: AbortSignal.timeout(timeoutMs), redirect: "manual" });
		if (r.status !== 200) return { ok: false, reason: `answered ${r.status}` };
		const text = await readLimited(r, 65_536);
		if (text === null) return { ok: false, reason: "answer too large" };
		const outer = JSON.parse(text) as { body?: unknown; mac?: unknown };
		if (typeof outer.body !== "string" || typeof outer.mac !== "string" || !/^[0-9a-f]{64}$/.test(outer.mac)) return { ok: false, reason: "malformed answer" };
		const want = createHmac("sha256", key).update(`${CHECK_PREFIX}:stats:${port}:${nonce}:${outer.body}`).digest();
		if (!timingSafeEqual(want, Buffer.from(outer.mac, "hex"))) return { ok: false, reason: "wrong proof: that is not this relay" };
		const stats: unknown = JSON.parse(outer.body);
		return stats && typeof stats === "object" && !Array.isArray(stats) ? { ok: true, stats: stats as Record<string, unknown> } : { ok: false, reason: "malformed answer" };
	} catch {
		return { ok: false, reason: "no answer" };
	}
}
