// Is this directory one that only this user (or root) can change? Used for the web client's tree, which a browser runs
// scripts from: if another account could replace one file in it, that account could read every room link the browser opens.
//
// The path is walked the way the kernel resolves it: component by component with lstat, following symbolic links (their
// targets are read and walked too) and `..` (taken from the real directory reached so far). Every directory passed through,
// the ones that hold a link and the ones a link leads into, must belong to this user or root and must not be writable by
// others (a sticky directory such as /tmp is the exception, because its entries cannot be renamed by others) and must not be
// writable by a group unless that group is shown to have no member but this user. Every link must belong to this user or
// root. On top of the mode bits, access control lists are inspected (macOS: `ls -lde`; Linux: `getfacl`, when `ls` shows
// that an ACL exists): an entry that lets another principal write makes the directory untrusted, deny entries are harmless,
// and an ACL that cannot be inspected is never trusted.
//
// Out of scope on purpose: root, and processes running as this user. Either can change anything this user can.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const MAX_LINKS = 40;

/** The macOS ACL permission names that let a principal change or replace a directory or what is in it. */
const ACL_WRITE = new Set(["write", "append", "delete", "delete_child", "writeattr", "writeextattr", "writesecurity", "chown", "add_file", "add_subdirectory"]);

export interface TrustDeps {
	uid?: number;
	platform?: NodeJS.Platform;
	/** The text of a file, or null when it cannot be read (the group database). */
	read?: (file: string) => string | null;
	/** Runs a program with a fixed environment; null when it cannot run. */
	run?: (cmd: string[]) => { code: number; out: string } | null;
	/** The name of this user, for the ACL entries that name this user. */
	userName?: string;
	cwd?: string;
}

export interface TrustWalk {
	/** Why the path is not trusted, or null. */
	problem: string | null;
	/** Every directory passed through or led into, as the walk resolved them, root first. */
	directories: string[];
	/** The resolved path when the walk reached an existing directory; null when a component does not exist (yet). */
	real: string | null;
}

function defaultRead(file: string): string | null {
	try {
		return fs.readFileSync(file, "utf8");
	} catch {
		return null;
	}
}

function defaultRun(argv: string[]): { code: number; out: string } | null {
	try {
		const r = Bun.spawnSync(argv, { stdout: "pipe", stderr: "pipe", env: { LC_ALL: "C", PATH: "/bin:/usr/bin:/usr/sbin:/sbin" }, timeout: 10_000 });
		return { code: r.exitCode ?? -1, out: r.stdout.toString() };
	} catch {
		return null;
	}
}

/**
 * Whether `gid` is a group whose every member (the primary members from /etc/passwd and the listed members from /etc/group)
 * is `uid` or root. A group that is missing from the file (a directory service, LDAP) or a member that cannot be resolved
 * is not private: the answer cannot be shown, so it is no.
 */
export function groupIsPrivate(gid: number, uid: number, read: (file: string) => string | null = defaultRead): boolean {
	const groupFile = read("/etc/group");
	const passwdFile = read("/etc/passwd");
	if (groupFile === null || passwdFile === null) return false;
	const trusted = new Set([uid, 0]);
	const users = new Map<string, number>();
	for (const line of passwdFile.split("\n")) {
		const f = line.split(":");
		if (f.length < 4) continue;
		users.set(f[0] as string, Number(f[2]));
		if (f[3] === String(gid) && !trusted.has(Number(f[2]))) return false; // somebody else has it as their primary group
	}
	const line = groupFile.split("\n").find(l => l.split(":")[2] === String(gid));
	if (!line) return false;
	for (const member of (line.split(":")[3] ?? "").split(",").filter(Boolean)) {
		const id = users.get(member);
		if (id === undefined || !trusted.has(id)) return false;
	}
	return true;
}

function directoryProblem(st: fs.Stats, where: string, uid: number, platform: NodeJS.Platform, read: (file: string) => string | null): string | null {
	if (st.uid !== uid && st.uid !== 0) return `${where} belongs to another account`;
	if (st.mode & 0o1000) return null; // sticky: its entries cannot be renamed or removed by others
	if (st.mode & 0o002) return `${where} can be changed by any account (writable by others)`;
	if (st.mode & 0o020) {
		// Broad groups ('admin', 'staff', 'users') have members a file-based lookup cannot list, so macOS never qualifies.
		if (platform === "darwin" || !groupIsPrivate(st.gid, uid, read)) return `${where} can be changed by a group that has other members (run: chmod g-w ${where})`;
	}
	return null;
}

function aclProblemDarwin(dirs: string[], deps: Required<Pick<TrustDeps, "run" | "userName">>): string | null {
	const r = deps.run(["/bin/ls", "-lde", "--", ...dirs]);
	if (!r || r.code !== 0) return "the access control lists cannot be inspected (ls failed)";
	let current: string | null = null;
	let seen = 0;
	for (const line of r.out.split("\n")) {
		if (/^[-dlbcpsw?][-rwxsStTl]{9}[@+.: ]?\s/.test(line)) {
			// ls sorts its operands, so an entry is matched by the name it ends with
			current = dirs.filter(d => line.endsWith(` ${d}`)).sort((a, b) => b.length - a.length)[0] ?? null;
			if (current === null) return "the access control lists cannot be inspected (unexpected ls output)";
			seen++;
			continue;
		}
		// An entry copied from a parent says `inherited` between the principal and allow/deny, and stays after its source is
		// gone; it grants exactly what a direct entry does.
		const m = /^\s*\d+:\s+(\S+)\s+(?:inherited\s+)?(allow|deny)\s+(.*?)\s*$/.exec(line);
		if (!m) {
			// a numbered line that is not an entry we understand could be a grant: it is never trusted
			if (current !== null && /^\s*\d+:/.test(line)) return "the access control lists cannot be inspected (an entry that is not understood)";
			continue;
		}
		if (current === null) continue;
		if (m[2] !== "allow" || m[1] === `user:${deps.userName}`) continue;
		const bad = (m[3] as string).split(",").map(p => p.trim()).filter(p => ACL_WRITE.has(p));
		if (bad.length) return `${current} has an ACL entry (${m[1]} allow ${bad.sort().join(",")}) that lets another principal change it`;
	}
	if (seen !== dirs.length) return "the access control lists cannot be inspected (unexpected ls output)";
	return null;
}

function aclProblemLinux(dirs: string[], uid: number, run: NonNullable<TrustDeps["run"]>): string | null {
	// `ls` marks a path that has an ACL with a plus sign after its mode; only those are examined further
	const listing = run(["ls", "-ldn", "--", ...dirs]);
	if (!listing || listing.code !== 0) return "the access control lists cannot be inspected (ls failed)";
	const withAcl: string[] = [];
	let seen = 0;
	for (const line of listing.out.split("\n")) {
		const m = /^([-dlbcpsw?][-rwxsStTl]{9})([@+.: ]?)\s/.exec(line);
		if (!m) continue;
		seen++;
		if (m[2] === "+") {
			const hit = dirs.filter(d => line.endsWith(` ${d}`)).sort((a, b) => b.length - a.length)[0];
			if (hit === undefined) return "the access control lists cannot be inspected (unexpected ls output)";
			withAcl.push(hit);
		}
	}
	if (seen !== dirs.length) return "the access control lists cannot be inspected (unexpected ls output)";
	if (withAcl.length === 0) return null;
	const acl = run(["getfacl", "-p", "-n", "--", ...withAcl]);
	if (!acl || acl.code !== 0) return `${withAcl[0]} has an ACL that cannot be inspected (is getfacl installed?)`;
	let current = "";
	for (const line of acl.out.split("\n")) {
		const file = /^# file: (.*)$/.exec(line);
		if (file) {
			current = file[1] as string;
			continue;
		}
		const entry = /^(user|group):(\d+):([r-][w-][x-])/.exec(line);
		if (!entry) continue;
		if (entry[1] === "user" && Number(entry[2]) === uid) continue;
		if ((entry[3] as string)[1] === "w") return `${current || withAcl[0]} has an ACL entry (${entry[1]} ${entry[2]}) that lets another principal change it`;
	}
	return null;
}

/** Walks `target` as described in the header. A missing component ends the walk: what exists before it has been checked. */
export function trustWalk(target: string, deps: TrustDeps = {}): TrustWalk {
	const uid = deps.uid ?? process.getuid?.() ?? -1;
	const platform = deps.platform ?? process.platform;
	const read = deps.read ?? defaultRead;
	const run = deps.run ?? defaultRun;
	const directories: string[] = [];
	const bad = (problem: string): TrustWalk => ({ problem, directories, real: null });

	// not path.resolve: that would fold `..` away lexically, and after a symbolic link the kernel takes `..` from the real directory
	const absolute = target.startsWith("/") ? target : `${deps.cwd ?? process.cwd()}/${target}`;
	const queue = absolute.split("/").filter(Boolean);
	let current = "/";
	let links = 0;
	let rootStat: fs.Stats;
	try {
		rootStat = fs.lstatSync("/");
	} catch {
		return bad("/ cannot be examined");
	}
	const rootProblem = directoryProblem(rootStat, "/", uid, platform, read);
	if (rootProblem) return bad(rootProblem);
	directories.push("/");
	let missing = false;

	while (queue.length) {
		const part = queue.shift() as string;
		if (part === ".") continue;
		if (part === "..") {
			current = path.dirname(current); // `current` is a real directory, so its parent is what the kernel would use
			continue;
		}
		const candidate = current === "/" ? `/${part}` : `${current}/${part}`;
		let st: fs.Stats;
		try {
			st = fs.lstatSync(candidate);
		} catch (err) {
			if ((err as NodeJS.ErrnoException).code === "ENOENT") {
				missing = true;
				break;
			}
			return bad(`${candidate} cannot be examined`);
		}
		if (st.isSymbolicLink()) {
			if (++links > MAX_LINKS) return bad(`too many symbolic links on the way to ${target}`);
			if (st.uid !== uid && st.uid !== 0) return bad(`the symbolic link ${candidate} belongs to another account`);
			let raw: Buffer;
			try {
				raw = fs.readlinkSync(candidate, { encoding: "buffer" });
			} catch {
				return bad(`the symbolic link ${candidate} cannot be read`);
			}
			const link = raw.toString("utf8");
			if (!Buffer.from(link, "utf8").equals(raw)) return bad(`the symbolic link ${candidate} has a target that is not text`);
			if (link.startsWith("/")) current = "/";
			queue.unshift(...link.split("/").filter(Boolean));
			continue;
		}
		if (!st.isDirectory()) return bad(`${candidate} is not a directory`);
		const problem = directoryProblem(st, candidate, uid, platform, read);
		if (problem) return bad(problem);
		current = candidate;
		directories.push(candidate);
	}

	const unique = [...new Set(directories)];
	if (unique.some(d => d.includes("\n"))) return bad("a path with a newline in it cannot be checked for ACLs");
	const aclProblem =
		platform === "darwin"
			? aclProblemDarwin(unique, { run, userName: deps.userName ?? os.userInfo().username })
			: platform === "linux"
				? aclProblemLinux(unique, uid, run)
				: null;
	if (aclProblem) return { problem: aclProblem, directories, real: null };
	return { problem: null, directories, real: missing ? null : current };
}

/** The reason `dir` is not safe to serve from or execute, or null (also null when it does not exist yet). */
export function servedPathProblem(dir: string, deps?: TrustDeps): string | null {
	return trustWalk(dir, deps).problem;
}
