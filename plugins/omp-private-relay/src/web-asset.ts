// The patched collab-web client: how it is packed for a release, downloaded, verified and installed.
//
// A release asset is a plain (uncompressed, so byte-for-byte reproducible) ustar archive of regular files, one per omp
// minor: `omp-private-relay-web-18.18.tar`. Two hashes pin it in `web-builds.json`, which ships inside the plugin:
//   sha256      of the archive as downloaded;
//   treeSha256  of its content: sha256 over the sorted lines "<file sha256>  <name>\n". It is the integrity anchor that
//               a fresh build from source can be compared with, whatever archive or compression carried it.
// The download is checked against both before a single file is written. The extractor accepts regular files with plain
// names only: no links, devices, directories entries, extended headers, absolute or `..` paths, or dot files.
import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { getFlock, LOCK_EX, LOCK_NB } from "./flock";
import { ensurePrivateDir, NOFOLLOW, NONBLOCK, readSmallFile, UID } from "./fsutil";
import { pluginRoot } from "./paths";
import { servedPathProblem } from "./trustwalk";

export const BUILD_META_FILE = ".omp-private-relay-build.json";
export const MAX_FILES = 500;
export const MAX_ASSET_BYTES = 64 * 1024 * 1024;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*(?:\/[A-Za-z0-9][A-Za-z0-9._-]*)*$/;

export interface WebBuild {
	/** The omp tag the client was built from, for example `v18.18.0`. */
	ompTag: string;
	/** The Bun that built it. */
	bun: string;
	asset: string;
	sha256: string;
	treeSha256: string;
	/** sha256 of the NOTICE file inside the archive. It is outside treeSha256, so editing it is a visible, separate change. */
	noticeSha256: string;
	bytes: number;
	files: number;
}

export interface WebBuilds {
	schema: 1;
	/** GitHub `owner/repo` that hosts the release assets. */
	repository: string;
	/** Tag of the release that holds the assets. */
	releaseTag: string;
	/** Keyed by omp minor (`18.18`). */
	builds: Record<string, WebBuild>;
}

export class WebAssetError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "WebAssetError";
	}
}

export function sha256(data: Uint8Array | string): string {
	return createHash("sha256").update(data).digest("hex");
}

export function webBuildsFile(): string {
	return path.join(pluginRoot(), "web-builds.json");
}

export function loadWebBuilds(file = webBuildsFile()): WebBuilds {
	const parsed = JSON.parse(fs.readFileSync(file, "utf8")) as WebBuilds;
	if (parsed.schema !== 1 || typeof parsed.builds !== "object") throw new WebAssetError(`${file}: not a web-builds/1 file`);
	return parsed;
}

// ── content hash ────────────────────────────────────────────────────────────────────────────────

/** The listing that treeSha256 is computed over. Names are ASCII (enforced elsewhere), so string order is byte order. */
export function treeHash(files: ReadonlyMap<string, Uint8Array>): string {
	// The licence notice is added by the packer, not built; it is pinned on its own (noticeSha256) so that the content hash
	// stays the comparison between a fresh build and the pin.
	const lines = [...files.keys()].filter(name => name !== "NOTICE").sort().map(name => `${sha256(files.get(name) as Uint8Array)}  ${name}\n`);
	return sha256(lines.join(""));
}

/** Reads a build output directory: regular files only, dot files skipped, every name valid. */
export function readTree(dir: string): Map<string, Buffer> {
	const files = new Map<string, Buffer>();
	const walk = (rel: string): void => {
		for (const entry of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
			if (entry.name.startsWith(".")) continue;
			const name = rel ? `${rel}/${entry.name}` : entry.name;
			if (entry.isDirectory()) walk(name);
			else if (entry.isFile()) {
				if (!NAME_RE.test(name) || name.length > 100) throw new WebAssetError(`file name not allowed in a web asset: ${name}`);
				files.set(name, fs.readFileSync(path.join(dir, name)));
			} else throw new WebAssetError(`not a regular file: ${name}`);
		}
	};
	walk("");
	if (files.size === 0) throw new WebAssetError(`${dir} holds no files`);
	if (files.size > MAX_FILES) throw new WebAssetError(`${dir} holds more than ${MAX_FILES} files`);
	return files;
}

// ── tar ─────────────────────────────────────────────────────────────────────────────────────────

function octal(value: number, width: number): string {
	return `${value.toString(8).padStart(width - 1, "0")}\0`;
}

function field(buf: Buffer, offset: number, text: string): void {
	buf.write(text, offset, "latin1");
}

/** Deterministic ustar: sorted names, mode 0644, owner 0, mtime 0. */
export function packTar(files: ReadonlyMap<string, Uint8Array>): Buffer {
	const parts: Buffer[] = [];
	for (const name of [...files.keys()].sort()) {
		if (!NAME_RE.test(name) || name.length > 100) throw new WebAssetError(`file name not allowed in a web asset: ${name}`);
		const data = files.get(name) as Uint8Array;
		const header = Buffer.alloc(512);
		field(header, 0, name);
		field(header, 100, octal(0o644, 8));
		field(header, 108, octal(0, 8));
		field(header, 116, octal(0, 8));
		field(header, 124, octal(data.byteLength, 12));
		field(header, 136, octal(0, 12));
		field(header, 148, "        ");
		header[156] = "0".charCodeAt(0);
		field(header, 257, "ustar\0");
		field(header, 263, "00");
		let sum = 0;
		for (const b of header) sum += b;
		field(header, 148, `${sum.toString(8).padStart(6, "0")}\0 `);
		parts.push(header, Buffer.from(data), Buffer.alloc((512 - (data.byteLength % 512)) % 512));
	}
	parts.push(Buffer.alloc(1024));
	return Buffer.concat(parts);
}

function cstring(buf: Buffer, start: number, length: number): string {
	const slice = buf.subarray(start, start + length);
	const nul = slice.indexOf(0);
	return slice.subarray(0, nul < 0 ? length : nul).toString("latin1");
}

function parseOctal(buf: Buffer, start: number, length: number): number | null {
	const text = cstring(buf, start, length).trim();
	return /^[0-7]+$/.test(text) ? Number.parseInt(text, 8) : null;
}

/** Strict extraction into memory. Throws WebAssetError for anything but plain regular files. */
export function unpackTar(data: Uint8Array, limits = { files: MAX_FILES, bytes: MAX_ASSET_BYTES }): Map<string, Buffer> {
	const buf = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
	const files = new Map<string, Buffer>();
	let total = 0;
	let offset = 0;
	let ended = false;
	while (offset + 512 <= buf.length) {
		const header = buf.subarray(offset, offset + 512);
		if (header.every(b => b === 0)) {
			ended = true;
			offset += 512;
			break;
		}
		const name = cstring(header, 0, 100);
		const stored = parseOctal(header, 148, 8);
		const probe = Buffer.from(header);
		probe.fill(0x20, 148, 156);
		let sum = 0;
		for (const b of probe) sum += b;
		if (stored === null || stored !== sum) throw new WebAssetError("corrupt archive: bad header checksum");
		const type = String.fromCharCode(header[156] as number);
		if (type !== "0" && type !== "\0") throw new WebAssetError(`archive entry ${JSON.stringify(name)} is not a regular file`);
		if (cstring(header, 345, 155) !== "") throw new WebAssetError("archive entry uses a path prefix");
		if (!NAME_RE.test(name)) throw new WebAssetError(`archive entry name not allowed: ${JSON.stringify(name)}`);
		if (files.has(name)) throw new WebAssetError(`archive lists ${name} twice`);
		const size = parseOctal(header, 124, 12);
		if (size === null) throw new WebAssetError("corrupt archive: bad size field");
		total += size;
		if (files.size + 1 > limits.files || total > limits.bytes) throw new WebAssetError("archive is larger than allowed");
		const start = offset + 512;
		if (start + size > buf.length) throw new WebAssetError("corrupt archive: truncated entry");
		files.set(name, Buffer.from(buf.subarray(start, start + size)));
		offset = start + Math.ceil(size / 512) * 512;
	}
	if (!ended) throw new WebAssetError("corrupt archive: no end marker");
	for (let i = offset; i < buf.length; i++) if (buf[i] !== 0) throw new WebAssetError("corrupt archive: data after the end marker");
	if (files.size === 0) throw new WebAssetError("archive holds no files");
	return files;
}

// ── download ────────────────────────────────────────────────────────────────────────────────────

export function assetUrl(builds: WebBuilds, build: WebBuild, base?: string): string {
	const root = (base ?? `https://github.com/${builds.repository}/releases/download/${builds.releaseTag}`).replace(/\/+$/, "");
	return `${root}/${build.asset}`;
}

/** https only; plain http is accepted for loopback (tests and a local mirror). */
export function checkAssetUrl(url: string, allowHttpLoopback = true): void {
	let u: URL;
	try {
		u = new URL(url);
	} catch {
		throw new WebAssetError(`not a URL: ${url}`);
	}
	const loopback = u.hostname === "127.0.0.1" || u.hostname === "localhost" || u.hostname === "[::1]";
	if (u.protocol !== "https:" && !(allowHttpLoopback && u.protocol === "http:" && loopback)) throw new WebAssetError("the web client is only downloaded over https");
	if (u.username || u.password) throw new WebAssetError("a download URL must not carry credentials");
}

export type Fetcher = (url: string) => Promise<Response>;

const MAX_REDIRECTS = 5;

/**
 * Downloads the asset, refusing more than MAX_ASSET_BYTES. Redirects (GitHub release downloads redirect to its storage)
 * are followed by hand, and every hop is checked against the URL rules before any connection is made to it.
 */
export async function download(url: string, fetcher: Fetcher = u => fetch(u, { redirect: "manual", signal: AbortSignal.timeout(120_000) })): Promise<Buffer> {
	// Plain http to loopback is for a local mirror or a test: allowed only when the download itself started there, never as
	// the target of a redirect from somewhere else.
	let allowHttp = true;
	try {
		const first = new URL(url);
		allowHttp = first.protocol === "http:";
	} catch {
		allowHttp = false;
	}
	let current = url;
	let response: Response | null = null;
	for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
		checkAssetUrl(current, allowHttp);
		response = await fetcher(current);
		const location = response.status >= 300 && response.status < 400 ? response.headers.get("location") : null;
		if (!location) break;
		current = new URL(location, current).toString();
		response = null;
	}
	if (!response) throw new WebAssetError("too many redirects");
	if (!response.ok) throw new WebAssetError(`download failed: HTTP ${response.status}`);
	const declared = Number(response.headers.get("content-length") ?? "0");
	if (declared > MAX_ASSET_BYTES) throw new WebAssetError("download is larger than allowed");
	const chunks: Buffer[] = [];
	let size = 0;
	for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
		size += chunk.byteLength;
		if (size > MAX_ASSET_BYTES) throw new WebAssetError("download is larger than allowed");
		chunks.push(Buffer.from(chunk));
	}
	return Buffer.concat(chunks);
}

// ── verify and install ──────────────────────────────────────────────────────────────────────────

/** Checks a downloaded archive against the pinned hashes and returns its files. Nothing is written. */
export function verifyAsset(data: Uint8Array, build: WebBuild): Map<string, Buffer> {
	if (sha256(data) !== build.sha256) throw new WebAssetError("the downloaded file does not match the pinned sha256; refusing to use it");
	const files = unpackTar(data);
	if (treeHash(files) !== build.treeSha256) throw new WebAssetError("the archive content does not match the pinned tree hash; refusing to use it");
	const notice = files.get("NOTICE");
	if (!notice || sha256(notice) !== build.noticeSha256) throw new WebAssetError("the archive's NOTICE does not match the pin; refusing to use it");
	return files;
}

export interface InstalledBuild {
	ompTag: string;
	bun: string;
	treeSha256: string;
	/** Absent in records written by older versions; such an install is "legacy", never verified. */
	noticeSha256?: string;
	asset: string;
	installedAt: string;
}

const LOCK_WAIT_MS = 10_000;

/**
 * One installer at a time per target: an advisory flock held on a validated descriptor for the whole install. The lock file
 * is never unlinked or recreated, so there is no stale lock to take over and no window in which two installers each hold
 * "the" lock; the lock is released by the kernel when the descriptor closes, also when the holder dies.
 */
function withInstallLock<T>(target: string, fn: () => T): T {
	const flock = getFlock();
	if (!flock) throw new WebAssetError("cannot lock the install: advisory locking is unavailable here");
	const lock = path.join(path.dirname(target), `.${path.basename(target)}.install-lock`);
	let fd: number;
	try {
		fd = fs.openSync(lock, fs.constants.O_RDWR | fs.constants.O_CREAT | NOFOLLOW | NONBLOCK, 0o600);
	} catch {
		throw new WebAssetError(`${lock} cannot be opened (a link or an unreadable file); remove it and retry`);
	}
	try {
		const st = fs.fstatSync(fd);
		if (!st.isFile() || st.uid !== UID || st.mode & 0o077) throw new WebAssetError(`${lock} is not a private plain file of yours; remove it and retry`);
		const deadline = Date.now() + LOCK_WAIT_MS;
		while (flock(fd, LOCK_EX | LOCK_NB) !== 0) {
			if (Date.now() > deadline) throw new WebAssetError("another install of the web client is running");
			Bun.sleepSync(25);
		}
		return fn();
	} finally {
		fs.closeSync(fd);
	}
}

interface Transaction {
	stage: string;
	old: string;
}

const txnFile = (target: string): string => path.join(path.dirname(target), `.${path.basename(target)}.txn.json`);

const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** A name an install gives its directories: unpredictable, so no existing path (a leftover, a PID-named directory of somebody's) can have it. */
function installName(target: string, kind: "new" | "old"): string {
	return `${path.basename(target)}.${kind}.${randomBytes(8).toString("hex")}`;
}

function isInstallName(target: string, name: unknown, kind: "new" | "old"): name is string {
	return typeof name === "string" && new RegExp(`^${escapeRe(path.basename(target))}\\.${kind}\\.[0-9a-f]{16}$`).test(name);
}

/** The record of an interrupted install, null when there is none; a record that cannot be read or is not ours is refused, never ignored. */
function readTransaction(target: string): Transaction | null {
	const file = txnFile(target);
	const raw = readSmallFile(file, 4096);
	if (raw === null) {
		if (fs.existsSync(file) || lexists(file)) throw new WebAssetError(`${file} exists but cannot be read; inspect it, remove it if it is stale, and retry`);
		return null;
	}
	let txn: unknown;
	try {
		txn = JSON.parse(raw.toString("utf8"));
	} catch {
		throw new WebAssetError(`${file} is damaged; the previous copy may be beside ${target} (named *.old.*); put it back by hand, remove ${file}, and retry`);
	}
	const rec = txn as Partial<Transaction> | null;
	if (!rec || !isInstallName(target, rec.stage, "new") || !isInstallName(target, rec.old, "old")) {
		throw new WebAssetError(`${file} does not name this install's directories; inspect it, remove it if it is stale, and retry`);
	}
	return { stage: rec.stage, old: rec.old };
}

function lexists(file: string): boolean {
	try {
		fs.lstatSync(file);
		return true;
	} catch {
		return false;
	}
}

/** Puts the previous copy back and removes exactly the directories the record names. Under the install lock. */
function recoverLocked(target: string): void {
	const txn = readTransaction(target);
	if (txn === null) return;
	const parent = path.dirname(target);
	if (!fs.existsSync(target) && lexists(path.join(parent, txn.old))) fs.renameSync(path.join(parent, txn.old), target);
	fs.rmSync(path.join(parent, txn.stage), { recursive: true, force: true });
	fs.rmSync(path.join(parent, txn.old), { recursive: true, force: true });
	fs.rmSync(txnFile(target), { force: true });
}

/**
 * An install is recorded (the exact, unpredictable names of its staging directory and of the previous copy) before it
 * creates anything. A crash between the two renames leaves `target` missing and the previous copy aside: this puts exactly
 * that copy back and removes exactly the directories the record names. Nothing else beside the target is touched, whatever
 * its name, and a record that is not recognisably ours stops the install instead of being skipped.
 */
export function recoverInterrupted(target: string): void {
	withInstallLock(target, () => recoverLocked(target));
}

/** Writes the files to `target` through a staging directory and swaps it in. The previous version is removed afterwards. */
export function installTree(files: ReadonlyMap<string, Uint8Array>, build: WebBuild, target: string): InstalledBuild {
	const parent = path.dirname(target);
	ensurePrivateDir(parent, { tighten: false });
	const unsafe = servedPathProblem(parent);
	if (unsafe) throw new WebAssetError(`${unsafe}; the web client is not installed into a directory another account could change`);
	if (fs.existsSync(target) && fs.lstatSync(target).isSymbolicLink()) throw new WebAssetError(`${target} is a symlink; remove it first`);
	return withInstallLock(target, () => {
		recoverLocked(target);
		const stageName = installName(target, "new");
		const oldName = installName(target, "old");
		const stage = path.join(parent, stageName);
		const old = path.join(parent, oldName);
		// recorded before anything is created, atomically, so a crash at any later point is recoverable from exactly these names
		const pending = `${txnFile(target)}.tmp`;
		fs.writeFileSync(pending, JSON.stringify({ stage: stageName, old: oldName }), { mode: 0o600 });
		fs.renameSync(pending, txnFile(target));
		const meta: InstalledBuild = {
			ompTag: build.ompTag,
			bun: build.bun,
			treeSha256: build.treeSha256,
			noticeSha256: build.noticeSha256,
			asset: build.asset,
			installedAt: new Date().toISOString(),
		};
		let created = false;
		try {
			fs.mkdirSync(stage, { mode: 0o700 }); // exclusive: an existing path of that name is an error, never deleted
			created = true;
			for (const [name, data] of files) {
				if (!NAME_RE.test(name) || name.length > 100) throw new WebAssetError(`file name not allowed in a web asset: ${name}`);
				const file = path.join(stage, name);
				fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
				fs.writeFileSync(file, data, { mode: 0o600, flag: "wx" });
			}
			fs.writeFileSync(path.join(stage, BUILD_META_FILE), `${JSON.stringify(meta, null, "\t")}\n`, { mode: 0o600, flag: "wx" });
			if (fs.existsSync(target)) fs.renameSync(target, old);
			fs.renameSync(stage, target);
		} catch (err) {
			if (created) fs.rmSync(stage, { recursive: true, force: true }); // ours: created above, under a name only this install knows
			if (!fs.existsSync(target) && lexists(old)) fs.renameSync(old, target);
			fs.rmSync(txnFile(target), { force: true });
			throw err;
		}
		fs.rmSync(old, { recursive: true, force: true });
		fs.rmSync(txnFile(target), { force: true });
		return meta;
	});
}

export type Installed =
	| { state: "missing" }
	| { state: "unmanaged"; reason: string }
	| { state: "ok"; meta: InstalledBuild }
	/** Installed by an older version that did not record the NOTICE hash: not certified until it is installed again. */
	| { state: "legacy"; meta: InstalledBuild }
	| { state: "modified"; meta: InstalledBuild; actualTreeSha256: string };

/** Reads what is installed in `dir` and recomputes its content hash, so a hand-edited or damaged copy is noticed. */
export function inspectInstalled(dir: string): Installed {
	if (!fs.existsSync(path.join(dir, "index.html"))) return { state: "missing" };
	let meta: InstalledBuild;
	try {
		meta = JSON.parse(fs.readFileSync(path.join(dir, BUILD_META_FILE), "utf8")) as InstalledBuild;
	} catch {
		return { state: "unmanaged", reason: "no build record: it was not installed by omp-private-relay" };
	}
	let actual: string;
	try {
		actual = treeHash(readTree(dir));
	} catch (err) {
		return { state: "unmanaged", reason: err instanceof Error ? err.message : "unreadable" };
	}
	// NOTICE is outside the content hash, so it is checked on its own against the hash recorded at install time. A record
	// without that hash cannot certify it, so such an install is reported as legacy, never as verified.
	if (actual !== meta.treeSha256) return { state: "modified", meta, actualTreeSha256: actual };
	if (!meta.noticeSha256) return { state: "legacy", meta };
	const notice = readSmallFile(path.join(dir, "NOTICE"), 1024 * 1024);
	if (!notice || sha256(notice) !== meta.noticeSha256) return { state: "modified", meta, actualTreeSha256: actual };
	return { state: "ok", meta };
}
