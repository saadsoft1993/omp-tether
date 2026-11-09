// Small file-system helpers shared by the pairing store and the relay's check key.
import * as fs from "node:fs";

export const UID = process.getuid?.() ?? -1;
export const NOFOLLOW = fs.constants.O_NOFOLLOW ?? 0;
export const NONBLOCK = fs.constants.O_NONBLOCK ?? 0;

export class UnsafePathError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "UnsafePathError";
	}
}

/**
 * A directory only we can use: created 0700 when missing (the mode is passed to mkdir and re-applied through the handle, so a
 * umask of 002, common where every user has a private group, cannot leave it group-writable); refused when it is a link, not
 * a directory, or someone else's.
 *
 *   default            a merely too-open mode on our own directory is tightened to 0700 through the open handle;
 *   tighten: false     somebody else's directory (another tool's drop-in folder, the parent of a served tree): never
 *                      chmod-ed, and refused when group or others can write to it;
 *   refuseOpen: true   (a directory that holds a key) refused when group or others can even read it, instead of tightened.
 */
export function ensurePrivateDir(dir: string, opts: { tighten?: boolean; refuseOpen?: boolean } = {}): void {
	const existed = fs.existsSync(dir);
	fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
	const st = fs.lstatSync(dir);
	if (st.isSymbolicLink() || !st.isDirectory()) throw new UnsafePathError(`${dir} is not a plain directory`);
	if (st.uid !== UID) throw new UnsafePathError(`${dir} belongs to another account`);
	if (!existed && st.mode & 0o077) tightenHandle(dir); // created by us: make it 0700 whatever the umask said
	else if (opts.tighten === false) {
		if (st.mode & 0o022) throw new UnsafePathError(`${dir} is writable by group or others`);
	} else if (opts.refuseOpen) {
		if (st.mode & 0o077) throw new UnsafePathError(`${dir} can be read or written by group or others; it must be mode 700`);
	} else if (st.mode & 0o077) tightenHandle(dir);
}

function tightenHandle(dir: string): void {
	const fd = fs.openSync(dir, fs.constants.O_RDONLY | NOFOLLOW | (fs.constants.O_DIRECTORY ?? 0));
	try {
		fs.fchmodSync(fd, 0o700);
	} finally {
		fs.closeSync(fd);
	}
}

/** Everything readable from `fd`, or null when there is more than `limit` bytes (a file can grow after it was measured). */
export function readFdBounded(fd: number, limit: number): Buffer | null {
	const chunks: Buffer[] = [];
	const buf = Buffer.allocUnsafe(65_536);
	let total = 0;
	for (;;) {
		const n = fs.readSync(fd, buf, 0, buf.length, null);
		if (n === 0) return Buffer.concat(chunks);
		total += n;
		if (total > limit) return null;
		chunks.push(Buffer.from(buf.subarray(0, n)));
	}
}

/** A regular file read through one descriptor, no link followed and no FIFO waited for; null on any failure or when it is larger than `limit`. */
export function readSmallFile(file: string, limit: number): Buffer | null {
	let fd: number;
	try {
		fd = fs.openSync(file, fs.constants.O_RDONLY | NOFOLLOW | NONBLOCK);
	} catch {
		return null;
	}
	try {
		if (!fs.fstatSync(fd).isFile()) return null;
		return readFdBounded(fd, limit);
	} catch {
		return null;
	} finally {
		fs.closeSync(fd);
	}
}
