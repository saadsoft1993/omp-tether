// pairing-store/1 (docs/protocol/pairing-store-1.md): the reader the relay uses to decide whether a browser is paired,
// and the writer the relay's own CLI uses to pair and revoke browsers. A dashboard that implements the same
// specification can write the same files and be read by this reader (the conformance suite checks that).
//
// Everything here fails closed: a store that is missing, corrupt, a link, owned by someone else or open to other
// accounts means that nobody is paired.
import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { getFlock, LOCK_EX, LOCK_NB } from "./flock";
import { ensurePrivateDir, NOFOLLOW, NONBLOCK, UID, UnsafePathError } from "./fsutil";

export const COOKIE_VALUE_MAX = 512;
export const STORE_MAX_BYTES = 4 * 1024 * 1024;
export const PAIR_TTL_S = 300;
export const LABEL_MAX = 80;
export const DEVICES_FILE = "devices.json";
export const PENDING_FILE = "pending.json";
export const LOCK_FILE = ".lock";

/** Machine-readable reasons a store is refused (specification section 4). */
export type Refusal =
	| "store-missing"
	| "store-symlink"
	| "store-foreign-owner"
	| "store-open-to-others"
	| "store-not-regular"
	| "store-too-large"
	| "store-invalid-json"
	| "store-not-object"
	| "store-unreadable";

export class PairingError extends Error {
	constructor(
		message: string,
		readonly code: "locked" | "unsafe" | "corrupt" | "bad-request" | "ambiguous",
	) {
		super(message);
		this.name = "PairingError";
	}
}

export function sha256Hex(text: string): string {
	return createHash("sha256").update(text, "utf8").digest("hex");
}

/** The stored form of a cookie value or pairing token. */
export const cookieHash = sha256Hex;

/** Every value the header carries for cookie `name`, in order, ignoring empty and over-long values. */
export function cookieValues(header: string | null, name: string): string[] {
	if (!header) return [];
	const values: string[] = [];
	for (const part of header.split(";")) {
		const eq = part.indexOf("=");
		if (eq < 0 || part.slice(0, eq).trim() !== name) continue;
		const value = part.slice(eq + 1).trim();
		if (value && value.length <= COOKIE_VALUE_MAX) values.push(value);
	}
	return values;
}

function dirProblem(st: fs.Stats): Refusal | null {
	if (st.isSymbolicLink()) return "store-symlink";
	if (!st.isDirectory()) return "store-not-regular";
	if (st.uid !== UID) return "store-foreign-owner";
	if (st.mode & 0o077) return "store-open-to-others";
	return null;
}

function fileProblem(st: fs.Stats): Refusal | null {
	if (!st.isFile()) return "store-not-regular";
	if (st.uid !== UID) return "store-foreign-owner";
	if (st.mode & 0o077) return "store-open-to-others";
	return null;
}

export interface StoreView {
	hashes: ReadonlySet<string>;
	refusal: Refusal | null;
}

const NOBODY: ReadonlySet<string> = new Set();
const HASH_RE = /^[0-9a-f]{64}$/;

export class PairingReader {
	private cache: { key: string; view: StoreView } | null = null;

	constructor(
		readonly dir: string,
		private readonly onRefusal: (reason: Refusal) => void = () => {},
	) {}

	private refuse(reason: Refusal): StoreView {
		if (this.cache?.key !== `refused:${reason}`) this.onRefusal(reason);
		const view: StoreView = { hashes: NOBODY, refusal: reason };
		this.cache = { key: `refused:${reason}`, view };
		return view;
	}

	/** The hashes of the paired browsers, re-read only when the file changes. */
	read(): StoreView {
		let dirStat: fs.Stats;
		try {
			dirStat = fs.lstatSync(this.dir);
		} catch (err) {
			return this.refuse((err as NodeJS.ErrnoException).code === "ENOENT" ? "store-missing" : "store-unreadable");
		}
		const dirBad = dirProblem(dirStat);
		if (dirBad) return this.refuse(dirBad);
		let fd: number;
		try {
			fd = fs.openSync(path.join(this.dir, DEVICES_FILE), fs.constants.O_RDONLY | NOFOLLOW | NONBLOCK);
		} catch (err) {
			const code = (err as NodeJS.ErrnoException).code;
			if (code === "ENOENT") return this.refuse("store-missing");
			if (code === "ELOOP" || code === "EMLINK") return this.refuse("store-symlink");
			return this.refuse("store-unreadable");
		}
		try {
			const st = fs.fstatSync(fd);
			const bad = fileProblem(st);
			if (bad) return this.refuse(bad);
			if (st.size > STORE_MAX_BYTES) return this.refuse("store-too-large");
			const key = `${st.ino}:${st.size}:${st.mtimeMs}`;
			if (this.cache?.key === key) return this.cache.view;
			const buf = Buffer.alloc(st.size);
			let got = 0;
			while (got < st.size) {
				const n = fs.readSync(fd, buf, got, st.size - got, got);
				if (n === 0) break;
				got += n;
			}
			let parsed: unknown;
			try {
				parsed = JSON.parse(buf.subarray(0, got).toString("utf8"));
			} catch {
				return this.refuse("store-invalid-json");
			}
			if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return this.refuse("store-not-object");
			const view: StoreView = { hashes: new Set(Object.keys(parsed).filter(k => HASH_RE.test(k))), refusal: null };
			this.cache = { key, view };
			return view;
		} finally {
			fs.closeSync(fd);
		}
	}

	/** The hash of the first cookie named `name` whose value is paired, or null. Every cookie of that name is tried. */
	match(cookieHeader: string | null, name: string): string | null {
		const { hashes } = this.read();
		if (hashes.size === 0) return null;
		for (const value of cookieValues(cookieHeader, name)) {
			const digest = cookieHash(value);
			if (hashes.has(digest)) return digest;
		}
		return null;
	}
}

// ── writer ──────────────────────────────────────────────────────────────────────────────────────

export interface DeviceInfo {
	id: string;
	hash: string;
	label: string;
	pairedAt: number;
}

const LOCK_WAIT_MS = 5_000;

/** Drops control and invisible format characters and caps the length: a label is shown in terminals and web pages. */
export function cleanLabel(label: string | null | undefined): string {
	const text = [...(label ?? "").replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, " ").trim()].slice(0, LABEL_MAX).join("").trim();
	return text || "browser";
}

function base64url(bytes: Buffer): string {
	return bytes.toString("base64url");
}

export class PairingWriter {
	constructor(
		readonly dir: string,
		private readonly nowSeconds: () => number = () => Date.now() / 1000,
	) {}

	/** Creates the directory (0700) or checks the one that exists; tightens a too-open mode on our own directory. */
	ensureDir(): void {
		try {
			ensurePrivateDir(this.dir);
		} catch (err) {
			if (err instanceof UnsafePathError) throw new PairingError(err.message, "unsafe");
			throw err;
		}
	}

	private async withLock<T>(fn: () => T): Promise<T> {
		this.ensureDir();
		const flock = getFlock();
		if (!flock) throw new PairingError("cannot lock the pairing store: advisory locking is unavailable here", "locked");
		const fd = fs.openSync(path.join(this.dir, LOCK_FILE), fs.constants.O_RDWR | fs.constants.O_CREAT | NOFOLLOW | NONBLOCK, 0o600);
		try {
			const st = fs.fstatSync(fd);
			if (fileProblem(st)) throw new PairingError("the pairing store lock file is not a private plain file of ours (check who could write it, then chmod 600)", "unsafe");
			const deadline = Date.now() + LOCK_WAIT_MS;
			while (flock(fd, LOCK_EX | LOCK_NB) !== 0) {
				if (Date.now() > deadline) throw new PairingError("the pairing store is locked by another process", "locked");
				await Bun.sleep(20);
			}
			return fn(); // the lock is released when the descriptor is closed
		} finally {
			fs.closeSync(fd);
		}
	}

	private load(name: string): Record<string, unknown> {
		let fd: number;
		try {
			fd = fs.openSync(path.join(this.dir, name), fs.constants.O_RDONLY | NOFOLLOW | NONBLOCK);
		} catch (err) {
			if ((err as NodeJS.ErrnoException).code === "ENOENT") return {};
			throw new PairingError(`${name} cannot be opened (a link or unreadable file)`, "unsafe");
		}
		try {
			const st = fs.fstatSync(fd);
			const bad = fileProblem(st);
			if (bad) throw new PairingError(`${name} is not safe to use (${bad}); check who could read it, then chmod 600`, "unsafe");
			if (st.size > STORE_MAX_BYTES) throw new PairingError(`${name} is too large`, "corrupt");
			let parsed: unknown;
			try {
				parsed = JSON.parse(fs.readFileSync(fd, "utf8")); // a zero-byte file is a truncated store, not an empty one
			} catch {
				throw new PairingError(`${name} is not valid JSON; refusing to overwrite it`, "corrupt");
			}
			if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
				throw new PairingError(`${name} is not a JSON object; refusing to overwrite it`, "corrupt");
			}
			return parsed as Record<string, unknown>;
		} finally {
			fs.closeSync(fd);
		}
	}

	private save(name: string, data: Record<string, unknown>): void {
		const final = path.join(this.dir, name);
		const tmp = path.join(this.dir, `.${name}.${process.pid}.${randomBytes(4).toString("hex")}`);
		const fd = fs.openSync(tmp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | NOFOLLOW, 0o600);
		try {
			fs.writeSync(fd, JSON.stringify(data));
			fs.fsyncSync(fd);
		} catch (err) {
			fs.closeSync(fd);
			fs.rmSync(tmp, { force: true });
			throw err;
		}
		fs.closeSync(fd);
		try {
			fs.renameSync(tmp, final);
		} catch (err) {
			fs.rmSync(tmp, { force: true });
			throw err;
		}
	}

	/** A single-use pairing token, valid for five minutes. Only its hash is stored. */
	async createToken(): Promise<string> {
		const token = base64url(randomBytes(32));
		await this.withLock(() => {
			const now = this.nowSeconds();
			const pending: Record<string, unknown> = {};
			for (const [hash, expiry] of Object.entries(this.load(PENDING_FILE))) {
				if (typeof expiry === "number" && expiry > now) pending[hash] = expiry;
			}
			pending[cookieHash(token)] = now + PAIR_TTL_S;
			this.save(PENDING_FILE, pending);
		});
		return token;
	}

	/** Spends a token. Returns the new browser's session id (its cookie value), or null for an unknown or expired token. */
	async redeem(token: unknown, label: string | null): Promise<string | null> {
		if (typeof token !== "string" || token === "" || token.length > COOKIE_VALUE_MAX) return null;
		return this.withLock(() => {
			const now = this.nowSeconds();
			const pending = this.load(PENDING_FILE);
			const expiry = pending[cookieHash(token)];
			delete pending[cookieHash(token)];
			const live: Record<string, unknown> = {};
			for (const [hash, exp] of Object.entries(pending)) if (typeof exp === "number" && exp > now) live[hash] = exp;
			// A wrong guess writes nothing; the token is consumed before the session exists, so a failure cannot double-spend it.
			if (typeof expiry === "number" || Object.keys(live).length !== Object.keys(pending).length) this.save(PENDING_FILE, live);
			if (typeof expiry !== "number" || expiry <= now) return null;
			const session = base64url(randomBytes(32));
			const devices = this.load(DEVICES_FILE);
			devices[cookieHash(session)] = { label: cleanLabel(label), paired_at: Math.floor(now) };
			this.save(DEVICES_FILE, devices);
			return session;
		});
	}

	/** Paired browsers, oldest first. Read-only, takes no lock (writers replace the file atomically). */
	devices(): DeviceInfo[] {
		const out: DeviceInfo[] = [];
		for (const [hash, value] of Object.entries(this.load(DEVICES_FILE))) {
			if (!HASH_RE.test(hash)) continue;
			const v = (value && typeof value === "object" ? value : {}) as { label?: unknown; paired_at?: unknown };
			out.push({
				id: hash.slice(0, 8),
				hash,
				label: cleanLabel(typeof v.label === "string" ? v.label : null),
				pairedAt: typeof v.paired_at === "number" ? v.paired_at : 0,
			});
		}
		return out.sort((a, b) => a.pairedAt - b.pairedAt);
	}

	/** Unpairs the one browser whose id starts with `prefix` (4 or more hex characters), or every browser for null. */
	async revoke(prefix: string | null): Promise<number> {
		if (prefix !== null && !/^[0-9a-f]{4,64}$/.test(prefix)) {
			throw new PairingError("give at least 4 hex characters of the id from `devices`", "bad-request");
		}
		return this.withLock(() => {
			const devices = this.load(DEVICES_FILE);
			const gone = Object.keys(devices).filter(k => prefix === null || k.startsWith(prefix));
			if (prefix !== null && gone.length > 1) throw new PairingError("that id prefix matches more than one browser; give more characters", "ambiguous");
			for (const k of gone) delete devices[k];
			if (gone.length > 0 || prefix === null) this.save(DEVICES_FILE, devices);
			return gone.length;
		});
	}
}
