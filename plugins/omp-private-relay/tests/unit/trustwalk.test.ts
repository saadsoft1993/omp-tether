// The trust walk of the web client's path (verification pass 2, findings 1 and 2). Each case builds its tree at run time in a
// scratch directory and asks the walk, with the group database and the ACL tools replaced where the answer must not depend
// on this machine.
import { afterAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { groupIsPrivate, servedPathProblem, trustWalk } from "../../src/trustwalk";

const roots: string[] = [];
const scratch = (): string => {
	const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "oprm-trust-")));
	roots.push(root);
	return root;
};
afterAll(() => {
	for (const r of roots) fs.rmSync(r, { recursive: true, force: true });
});

/** A directory with exactly this mode, whatever the umask; the directories created on the way are 0700. */
function mk(dir: string, mode = 0o700): string {
	const missing: string[] = [];
	for (let d = dir; !fs.existsSync(d); d = path.dirname(d)) missing.unshift(d);
	for (const d of missing) {
		fs.mkdirSync(d);
		fs.chmodSync(d, 0o700);
	}
	fs.chmodSync(dir, mode);
	return dir;
}

const ME = process.getuid?.() ?? 0;
const MY_GID = (root: string): number => fs.statSync(root).gid;

/** An `ls -ldn` answer without ACLs, and no getfacl: what a machine with no ACLs says. */
const noAcls = (cmd: string[]) => (cmd[0] === "ls" ? { code: 0, out: cmd.slice(cmd.indexOf("--") + 1).map(d => `drwx------ 2 1 1 64 Jan  1 00:00 ${d}`).join("\n") } : null);

/** The group database as a function of the gid the scratch directories really have. */
const groups = (gid: number, group: string, passwd: string) => (f: string) => (f === "/etc/group" ? group.replaceAll("GID", String(gid)) : f === "/etc/passwd" ? passwd.replaceAll("GID", String(gid)) : null);
const linux = { platform: "linux" as const, run: noAcls };

describe("which groups count as private to this user", () => {
	const passwd = `me:x:${ME}:GID::/home/me:/bin/sh\nbob:x:${ME + 1}:2000::/home/bob:/bin/sh\nroot:x:0:0::/root:/bin/sh\n`;

	test("only a group whose every member, primary and supplementary, is this user or root", () => {
		const own = groups(1001, "me:x:GID:\n", `me:x:${ME}:GID::/home/me:/bin/sh\n`);
		expect(groupIsPrivate(1001, ME, own)).toBe(true);
		expect(groupIsPrivate(1001, ME, groups(1001, "me:x:GID:me\n", `me:x:${ME}:GID::/h:/bin/sh\n`))).toBe(true);
		expect(groupIsPrivate(0, ME, groups(0, "root:x:GID:root\n", "root:x:0:GID::/root:/bin/sh\n"))).toBe(true);
		// one primary member (this user) plus one DIFFERENT supplementary member: not private (the earlier code counted members, not who they were)
		expect(groupIsPrivate(1001, ME, groups(1001, "me:x:GID:bob\n", passwd))).toBe(false);
		// somebody else whose PRIMARY group it is, with nobody listed
		expect(groupIsPrivate(1001, ME, groups(1001, "x:x:GID:\n", `me:x:${ME}:2000::/h:/bin/sh\nbob:x:${ME + 1}:GID::/h:/bin/sh\n`))).toBe(false);
		// a group that belongs to somebody else entirely
		expect(groupIsPrivate(1001, ME, groups(1001, "bob:x:GID:\n", `bob:x:${ME + 1}:GID::/h:/bin/sh\n`))).toBe(false);
		// a supplementary member the database cannot resolve, a group that is not listed, unreadable files
		expect(groupIsPrivate(1001, ME, groups(1001, "me:x:GID:ghost\n", `me:x:${ME}:GID::/h:/bin/sh\n`))).toBe(false);
		expect(groupIsPrivate(1002, ME, own)).toBe(false);
		expect(groupIsPrivate(1001, ME, () => null)).toBe(false);
	});
});

describe("directories passed through", () => {
	test("a tree only this user can write is trusted, and the walk reports the real path it reached", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "b", "web"));
		const walk = trustWalk(web, linux);
		expect(walk.problem).toBeNull();
		expect(walk.real).toBe(web);
		expect(walk.directories.at(-1)).toBe(web);
		expect(walk.directories[0]).toBe("/");
	});

	test("a missing leaf is fine (nothing to serve yet), a missing ancestor's own ancestors are still checked", () => {
		const root = scratch();
		mk(path.join(root, "a"), 0o777);
		const walk = trustWalk(path.join(root, "a", "later", "web"), { ...linux, uid: ME });
		expect(walk.problem).toMatch(/writable by others/);
		const ok = trustWalk(path.join(root, "nothing", "yet"), linux);
		expect(ok).toMatchObject({ problem: null, real: null });
	});

	test("others-writable is refused unless sticky; a directory of another account is refused", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		fs.chmodSync(path.join(root, "a"), 0o777);
		expect(servedPathProblem(web, linux)).toMatch(/writable by others/);
		fs.chmodSync(path.join(root, "a"), 0o1777);
		expect(servedPathProblem(web, linux)).toBeNull();
		fs.chmodSync(path.join(root, "a"), 0o700);
		expect(servedPathProblem(web, { ...linux, uid: ME + 4242 })).toMatch(/belongs to another account/);
	});

	test("a group-writable ancestor is refused unless its group has no other member; macOS never qualifies", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		fs.chmodSync(path.join(root, "a"), 0o770);
		const gid = MY_GID(root);
		const own = groups(gid, "me:x:GID:\n", `me:x:${ME}:GID::/h:/bin/sh\n`);
		const shared = groups(gid, "staff:x:GID:me,bob\n", `me:x:${ME}:${gid + 1}::/h:/bin/sh\nbob:x:${ME + 1}:${gid + 1}::/h:/bin/sh\n`);
		const oneOther = groups(gid, "me:x:GID:bob\n", `me:x:${ME}:GID::/h:/bin/sh\nbob:x:${ME + 1}:${gid + 1}::/h:/bin/sh\n`);
		expect(servedPathProblem(web, { ...linux, read: own })).toBeNull();
		expect(servedPathProblem(web, { ...linux, read: shared })).toMatch(/group that has other members/);
		expect(servedPathProblem(web, { ...linux, read: oneOther })).toMatch(/group that has other members/);
		expect(servedPathProblem(web, { ...linux, read: () => null })).toMatch(/group that has other members/);
		expect(servedPathProblem(web, { platform: "darwin", read: own, run: darwinNoAcls })).toMatch(/group that has other members/);
	});
});

/** `ls -lde` for directories with no ACL entries. */
function darwinNoAcls(cmd: string[]): { code: number; out: string } {
	return { code: 0, out: cmd.slice(cmd.indexOf("--") + 1).map(d => `drwx------  2 me  staff  64 Jan  1 00:00 ${d}`).join("\n") };
}

describe("symbolic links are followed, and where they lead is checked", () => {
	test("a link that looks safe but leads into a tree with a group-writable ancestor is refused (the destination's ancestry is walked)", () => {
		const root = scratch();
		const shared = mk(path.join(root, "shared"), 0o777);
		const store = mk(path.join(shared, "store", "web"));
		fs.chmodSync(path.join(shared, "store"), 0o700);
		mk(path.join(root, "home"));
		fs.symlinkSync(store, path.join(root, "home", "web"));
		const problem = servedPathProblem(path.join(root, "home", "web"), linux);
		expect(problem).toMatch(/writable by others/);
		expect(problem).toContain(shared);
	});

	test("a link into a clean tree is followed and the real path is reported", () => {
		const root = scratch();
		const real = mk(path.join(root, "data", "web"));
		mk(path.join(root, "home"));
		fs.symlinkSync(path.join(root, "data"), path.join(root, "home", "d"));
		const walk = trustWalk(path.join(root, "home", "d", "web"), linux);
		expect(walk).toMatchObject({ problem: null, real });
		expect(walk.directories).toContain(path.join(root, "data"));
	});

	test("a relative link with .. is resolved from the directory that holds it, and the directory it reaches is checked", () => {
		const root = scratch();
		mk(path.join(root, "a", "deep"));
		const exposed = mk(path.join(root, "b"), 0o777);
		mk(path.join(exposed, "web"));
		fs.symlinkSync("../../b/web", path.join(root, "a", "deep", "link"));
		expect(servedPathProblem(path.join(root, "a", "deep", "link"), linux)).toMatch(/writable by others/);
		fs.chmodSync(exposed, 0o700);
		expect(trustWalk(path.join(root, "a", "deep", "link"), linux)).toMatchObject({ problem: null, real: path.join(exposed, "web") });
	});

	test("a .. after a link is taken from the real directory the link reached, as the kernel does", () => {
		const root = scratch();
		mk(path.join(root, "x", "y", "z"));
		mk(path.join(root, "w"));
		fs.symlinkSync(path.join(root, "x", "y", "z"), path.join(root, "w", "l"));
		// w/l/../.. is x, not w's parent
		expect(trustWalk(`${root}/w/l/../..`, linux)).toMatchObject({ problem: null, real: path.join(root, "x") });
		// and that is where the group-writable bit is looked for
		fs.chmodSync(path.join(root, "x", "y"), 0o777);
		expect(servedPathProblem(`${root}/w/l/../..`, linux)).toMatch(/writable by others/);
	});

	test("loops and long chains end in a refusal, not a hang", () => {
		const root = scratch();
		fs.symlinkSync(path.join(root, "b"), path.join(root, "a"));
		fs.symlinkSync(path.join(root, "a"), path.join(root, "b"));
		expect(servedPathProblem(path.join(root, "a"), linux)).toMatch(/too many symbolic links/);
		let previous = mk(path.join(root, "end"));
		for (let i = 0; i < 45; i++) {
			const link = path.join(root, `l${i}`);
			fs.symlinkSync(previous, link);
			previous = link;
		}
		expect(servedPathProblem(previous, linux)).toMatch(/too many symbolic links/);
	});

	test("trailing slashes, dot components and doubled slashes change nothing", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		for (const variant of [`${web}/`, `${root}//a/./web/`, `${root}/a/web/../web`, `${root}/a/../a/web`]) {
			expect(trustWalk(variant, linux), variant).toMatchObject({ problem: null, real: web });
		}
		fs.chmodSync(path.join(root, "a"), 0o777);
		expect(servedPathProblem(`${root}//a/./web/`, linux)).toMatch(/writable by others/);
	});

	test("a file where a directory is needed is refused", () => {
		const root = scratch();
		fs.writeFileSync(path.join(root, "f"), "x");
		expect(servedPathProblem(path.join(root, "f", "web"), linux)).toMatch(/not a directory/);
		expect(servedPathProblem(path.join(root, "f"), linux)).toMatch(/not a directory/);
	});

	test("the system's own links (/tmp and /var on macOS, a link for a home directory) are root's and are followed", () => {
		// whichever of these exist here, a walk through them must not be refused for being links
		for (const p of ["/tmp", "/var", "/etc"]) if (fs.existsSync(p)) expect(trustWalk(p, linux).problem, p).toBeNull();
	});
});

describe("access control lists", () => {
	test("macOS: an allow entry for another principal that can change the directory is refused, deny entries and read-only grants are not", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		const dirs = (cmd: string[]) => cmd.slice(cmd.indexOf("--") + 1);
		const withEntries = (target: string, entries: string[]) => (cmd: string[]) => ({
			code: 0,
			out: dirs(cmd).map(d => `drwx------+ 2 me  staff  64 Jan  1 00:00 ${d}${d === target ? `\n${entries.map((e, i) => ` ${i}: ${e}`).join("\n")}` : ""}`).join("\n"),
		});
		const mac = (target: string, entries: string[]) => ({ platform: "darwin" as const, userName: "me", run: withEntries(target, entries) });
		const a = path.join(root, "a");
		expect(servedPathProblem(web, mac(a, ["user:other allow add_file,delete_child"]))).toMatch(/lets another principal change it/);
		expect(servedPathProblem(web, mac(a, ["group:everyone allow write"]))).toMatch(/lets another principal change it/);
		expect(servedPathProblem(web, mac(web, ["user:other allow chown"]))).toMatch(/lets another principal change it/);
		expect(servedPathProblem(web, mac(a, ["group:everyone deny delete"]))).toBeNull();
		expect(servedPathProblem(web, mac(a, ["group:everyone allow list,search,readattr,readextattr,readsecurity"]))).toBeNull();
		expect(servedPathProblem(web, mac(a, ["user:me allow add_file"]))).toBeNull(); // this user's own entry
		// an ACL listing that cannot be run or understood is not trusted
		expect(servedPathProblem(web, { platform: "darwin", run: () => null })).toMatch(/cannot be inspected/);
		expect(servedPathProblem(web, { platform: "darwin", run: () => ({ code: 1, out: "" }) })).toMatch(/cannot be inspected/);
		expect(servedPathProblem(web, { platform: "darwin", run: () => ({ code: 0, out: "garbage\n" }) })).toMatch(/cannot be inspected/);
	});

	test.skipIf(process.platform !== "darwin")("macOS, for real: an ACL granting another user add_file on an ancestor is refused", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		expect(servedPathProblem(web)).toBeNull();
		const set = Bun.spawnSync(["/bin/chmod", "+a", "user:nobody allow add_file,delete_child", path.join(root, "a")]);
		expect(set.exitCode).toBe(0);
		expect(servedPathProblem(web)).toMatch(/ACL entry/);
		Bun.spawnSync(["/bin/chmod", "-N", path.join(root, "a")]);
		expect(servedPathProblem(web)).toBeNull();
	});

	test.skipIf(process.platform !== "darwin")("macOS, for real: a write grant that is only a copy marked `inherited` is refused after its source is gone", () => {
		const root = scratch();
		const parent = mk(path.join(root, "p"));
		expect(Bun.spawnSync(["/bin/chmod", "+a", "group:everyone allow write,file_inherit,directory_inherit", parent]).exitCode).toBe(0);
		const web = mk(path.join(parent, "web"));
		expect(Bun.spawnSync(["/bin/chmod", "-a#", "0", parent]).exitCode).toBe(0); // the source is gone; the copy on `web` stays
		const listing = Bun.spawnSync(["/bin/ls", "-lde", web]).stdout.toString();
		expect(listing).toContain("inherited");
		expect(servedPathProblem(web)).toMatch(/ACL entry/);
	});

	test.skipIf(process.platform !== "darwin")("macOS, for real: an inherited entry that grants no write is read and harmless", () => {
		const root = scratch();
		const parent = mk(path.join(root, "q"));
		expect(Bun.spawnSync(["/bin/chmod", "+a", "group:everyone allow list,search,readattr,file_inherit,directory_inherit", parent]).exitCode).toBe(0);
		const web = mk(path.join(parent, "web"));
		expect(Bun.spawnSync(["/bin/chmod", "-a#", "0", parent]).exitCode).toBe(0);
		expect(Bun.spawnSync(["/bin/ls", "-lde", web]).stdout.toString()).toContain("inherited");
		expect(servedPathProblem(web)).toBeNull();
	});

	test("macOS: an `inherited` marker is read like any entry, and an entry line that is not understood is not trusted", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		const dirs = (cmd: string[]) => cmd.slice(cmd.indexOf("--") + 1);
		const withLines = (lines: string[]) => (cmd: string[]) => ({
			code: 0,
			out: dirs(cmd).map(d => `drwx------+ 2 me  staff  64 Jan  1 00:00 ${d}${d === web ? `\n${lines.join("\n")}` : ""}`).join("\n"),
		});
		const mac = (lines: string[]) => ({ platform: "darwin" as const, userName: "me", run: withLines(lines) });
		expect(servedPathProblem(web, mac([" 0: group:everyone inherited allow write"]))).toMatch(/lets another principal change it/);
		expect(servedPathProblem(web, mac([" 0: user:other inherited allow add_file,delete_child"]))).toMatch(/lets another principal change it/);
		expect(servedPathProblem(web, mac([" 0: group:everyone inherited allow list,search"]))).toBeNull();
		expect(servedPathProblem(web, mac([" 0: group:everyone inherited deny write"]))).toBeNull();
		expect(servedPathProblem(web, mac([" 0: user:me inherited allow write"]))).toBeNull();
		expect(servedPathProblem(web, mac([" 0: something odd that is not an entry"]))).toMatch(/cannot be inspected/);
		expect(servedPathProblem(web, mac([" 0: group:everyone allow list", " 1: group:everyone sometimes write"]))).toMatch(/cannot be inspected/);
	});

	test("Linux: a '+' in ls output sends the directory to getfacl; a named user or group that can write is refused; a missing getfacl is not trusted", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		const a = path.join(root, "a");
		const ls = (cmd: string[]) => cmd.slice(cmd.indexOf("--") + 1).map(d => `drwx------${d === a ? "+" : " "} 2 ${ME} 1 64 Jan  1 00:00 ${d}`).join("\n");
		const withFacl = (entries: string) => (cmd: string[]) => (cmd[0] === "ls" ? { code: 0, out: ls(cmd) } : cmd[0] === "getfacl" ? { code: 0, out: `# file: ${a}\n# owner: ${ME}\n# group: 1\nuser::rwx\n${entries}\nmask::rwx\nother::---\n` } : null);
		expect(servedPathProblem(web, { platform: "linux", run: withFacl(`user:${ME + 1}:rwx`) })).toMatch(/ACL entry \(user /);
		expect(servedPathProblem(web, { platform: "linux", run: withFacl("group:4242:-w-") })).toMatch(/ACL entry \(group /);
		expect(servedPathProblem(web, { platform: "linux", run: withFacl(`user:${ME}:rwx`) })).toBeNull();
		expect(servedPathProblem(web, { platform: "linux", run: withFacl(`user:${ME + 1}:r-x`) })).toBeNull();
		expect(servedPathProblem(web, { platform: "linux", run: cmd => (cmd[0] === "ls" ? { code: 0, out: ls(cmd) } : null) })).toMatch(/cannot be inspected/);
		expect(servedPathProblem(web, { platform: "linux", run: () => null })).toMatch(/cannot be inspected/);
	});

	test.skipIf(process.platform !== "linux" || Bun.spawnSync(["sh", "-c", "command -v setfacl && command -v getfacl"]).exitCode !== 0)("Linux, for real (when setfacl exists): a named-user ACL on an ancestor is refused", () => {
		const root = scratch();
		const web = mk(path.join(root, "a", "web"));
		expect(servedPathProblem(web)).toBeNull();
		const set = Bun.spawnSync(["setfacl", "-m", "u:nobody:rwx", path.join(root, "a")]);
		if (set.exitCode !== 0) return; // a file system without ACL support: nothing to assert
		expect(servedPathProblem(web)).toMatch(/ACL entry/);
	});
});
