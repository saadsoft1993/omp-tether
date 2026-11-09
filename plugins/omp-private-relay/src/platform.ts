// Who is listening on a TCP port: the platform-specific part. macOS asks lsof; Linux reads /proc/net/tcp and
// /proc/net/tcp6 (which list the owning uid of every socket, readable by everyone) and falls back to `ss -e`.
//
// The parsers are pure functions so they can be tested on captured output from both systems. The collectors return
// `unknown` rather than a guess when the platform tool is missing or cannot see the socket: an unknown owner is never
// treated as our own.
import * as fs from "node:fs";

export interface Listener {
	/** Local address as the platform prints it (`127.0.0.1`, `*`, `::`, ...). */
	address: string;
	port: number;
	uid: number | null;
	pid: number | null;
}

export type ListenerLookup = { ok: true; source: string; listeners: Listener[] } | { ok: false; reason: string };

export function isLoopbackAddress(address: string): boolean {
	return /^127\./.test(address) || address === "::1" || address === "[::1]" || address === "localhost";
}

/**
 * `lsof -nP -iTCP:<port> -sTCP:LISTEN -F pun` output: one record per process (`p<pid>`, `u<uid>`) followed by one `n`
 * line per socket (`127.0.0.1:39000`, `*:39000`, `[::1]:39000`).
 */
export function parseLsof(output: string): Listener[] {
	const listeners: Listener[] = [];
	let pid: number | null = null;
	let uid: number | null = null;
	for (const line of output.split("\n")) {
		const tag = line[0];
		const rest = line.slice(1);
		if (tag === "p") {
			pid = /^\d+$/.test(rest) ? Number(rest) : null;
			uid = null;
		} else if (tag === "u") {
			uid = /^\d+$/.test(rest) ? Number(rest) : null;
		} else if (tag === "n") {
			const m = /^(.*):(\d+)$/.exec(rest);
			if (m) listeners.push({ address: m[1] as string, port: Number(m[2]), uid, pid });
		}
	}
	return listeners;
}

function hexToIpv4(hex: string): string {
	// /proc prints the 32-bit address in host byte order: little endian on every platform we support.
	return [6, 4, 2, 0].map(i => Number.parseInt(hex.slice(i, i + 2), 16)).join(".");
}

function hexToIpv6(hex: string): string {
	// Four 32-bit words, each little endian.
	const groups: string[] = [];
	for (let w = 0; w < 4; w++) {
		const word = hex.slice(w * 8, w * 8 + 8);
		const bytes = [6, 4, 2, 0].map(i => word.slice(i, i + 2));
		groups.push(`${bytes[0]}${bytes[1]}`, `${bytes[2]}${bytes[3]}`);
	}
	const joined = groups.map(g => Number.parseInt(g, 16).toString(16)).join(":");
	if (joined === "0:0:0:0:0:0:0:0") return "::";
	if (joined === "0:0:0:0:0:0:0:1") return "::1";
	if (/^0:0:0:0:0:ffff:/.test(joined)) {
		const [hi, lo] = [Number.parseInt(groups[6] as string, 16), Number.parseInt(groups[7] as string, 16)];
		return `::ffff:${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
	}
	return joined;
}

/** The text of /proc/net/tcp (or tcp6). Only sockets in LISTEN state (`0A`) are returned. */
export function parseProcNetTcp(text: string, v6: boolean): Listener[] {
	const listeners: Listener[] = [];
	for (const line of text.split("\n").slice(1)) {
		const f = line.trim().split(/\s+/);
		// sl local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode
		if (f.length < 8 || f[3] !== "0A") continue;
		const m = /^([0-9A-Fa-f]+):([0-9A-Fa-f]{4})$/.exec(f[1] as string);
		if (!m) continue;
		const addr = m[1] as string;
		if (addr.length !== (v6 ? 32 : 8)) continue;
		const uid = /^\d+$/.test(f[7] as string) ? Number(f[7]) : null;
		listeners.push({ address: v6 ? hexToIpv6(addr) : hexToIpv4(addr), port: Number.parseInt(m[2] as string, 16), uid, pid: null });
	}
	return listeners;
}

/** `ss -Hltne` output: `LISTEN 0 512 127.0.0.1:39000 0.0.0.0:* uid:1000 ino:12345 sk:1 cgroup:... <->`. */
export function parseSs(output: string): Listener[] {
	const listeners: Listener[] = [];
	for (const line of output.split("\n")) {
		const f = line.trim().split(/\s+/);
		if (f[0] !== "LISTEN" || f.length < 5) continue;
		const m = /^(.*):(\d+)$/.exec(f[3] as string);
		if (!m) continue;
		const uidField = f.find(x => x.startsWith("uid:"));
		const pidMatch = /pid=(\d+)/.exec(line);
		listeners.push({
			address: (m[1] as string).replace(/^\[|\]$/g, ""),
			port: Number(m[2]),
			uid: uidField && /^uid:\d+$/.test(uidField) ? Number(uidField.slice(4)) : null,
			pid: pidMatch ? Number(pidMatch[1]) : null,
		});
	}
	return listeners;
}

/** What the collectors need from the system; the tests replace it. */
export interface PlatformDeps {
	platform: NodeJS.Platform;
	readFile(file: string): string | null;
	run(argv: string[]): { code: number; stdout: string } | null;
	exists(file: string): boolean;
}

export const realDeps: PlatformDeps = {
	platform: process.platform,
	readFile(file) {
		try {
			return fs.readFileSync(file, "utf8");
		} catch {
			return null;
		}
	},
	run(argv) {
		try {
			const r = Bun.spawnSync(argv, { stdout: "pipe", stderr: "ignore", stdin: "ignore", timeout: 5_000 });
			return { code: r.exitCode ?? -1, stdout: r.stdout.toString() };
		} catch {
			return null;
		}
	},
	exists: file => fs.existsSync(file),
};

function firstExisting(deps: PlatformDeps, candidates: string[]): string | null {
	return candidates.find(c => deps.exists(c)) ?? null;
}

/** Every TCP listener on `port`, on any address (a wildcard listener also answers on loopback). */
export function listenersOnPort(port: number, deps: PlatformDeps = realDeps): ListenerLookup {
	if (deps.platform === "darwin") {
		const lsof = firstExisting(deps, ["/usr/sbin/lsof", "/usr/bin/lsof"]);
		if (!lsof) return { ok: false, reason: "lsof is not installed" };
		const r = deps.run([lsof, "-nP", `-iTCP:${port}`, "-sTCP:LISTEN", "-Fpun"]);
		// lsof exits 1 when it finds nothing; any other failure means we could not look.
		if (!r || (r.code !== 0 && r.code !== 1)) return { ok: false, reason: "lsof failed" };
		return { ok: true, source: "lsof", listeners: parseLsof(r.stdout).filter(l => l.port === port) };
	}
	if (deps.platform === "linux") {
		const v4 = deps.readFile("/proc/net/tcp");
		if (v4 !== null) {
			const v6 = deps.readFile("/proc/net/tcp6");
			const all = [...parseProcNetTcp(v4, false), ...(v6 === null ? [] : parseProcNetTcp(v6, true))];
			return { ok: true, source: "/proc/net/tcp", listeners: all.filter(l => l.port === port) };
		}
		const ss = firstExisting(deps, ["/usr/bin/ss", "/bin/ss", "/usr/sbin/ss", "/sbin/ss"]);
		if (!ss) return { ok: false, reason: "neither /proc/net/tcp nor ss is available" };
		const r = deps.run([ss, "-Hltne", "sport", "=", `:${port}`]);
		if (!r || r.code !== 0) return { ok: false, reason: "ss failed" };
		return { ok: true, source: "ss", listeners: parseSs(r.stdout).filter(l => l.port === port) };
	}
	return { ok: false, reason: `listener ownership is not implemented for ${deps.platform}` };
}

export type Ownership =
	| { state: "none" }
	| { state: "ours"; listeners: Listener[] }
	| { state: "foreign"; listeners: Listener[] }
	| { state: "unknown"; reason: string };

/**
 * Whether the thing listening on `port` belongs to `uid`. "none" means the platform tool saw no listener, which is
 * only meaningful together with a failed connection: lsof run by a normal user prints nothing for a socket of another
 * account, so a caller that CAN connect must treat "none" as unknown.
 */
export function ownership(port: number, uid: number, deps: PlatformDeps = realDeps): Ownership {
	const found = listenersOnPort(port, deps);
	if (!found.ok) return { state: "unknown", reason: found.reason };
	if (found.listeners.length === 0) return { state: "none" };
	if (found.listeners.some(l => l.uid === null)) return { state: "unknown", reason: "a listener's owner cannot be seen" };
	return found.listeners.every(l => l.uid === uid) ? { state: "ours", listeners: found.listeners } : { state: "foreign", listeners: found.listeners };
}
