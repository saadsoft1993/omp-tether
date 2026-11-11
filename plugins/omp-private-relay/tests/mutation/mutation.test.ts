// Mutation check: each fix, and each guard of the pairing endpoint, is reverted in a COPY of the plugin,
// and the regression tests must fail on that copy. A test that still passes with its guard deleted proves nothing.
// Run with `make test-mutation` (each case runs two test files, so it takes a few minutes).
import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const ROOT = path.resolve(import.meta.dir, "..", "..");
const FILES = ["tests/unit/regressions.test.ts", "tests/unit/relay-token.test.ts", "tests/unit/config.test.ts", "tests/unit/web-asset.test.ts", "tests/unit/trustwalk.test.ts"];

/**
 * A mutated copy runs inside a sandbox the harness owns: <guard>/plugin (the copy) and <guard>/tmp (its TMPDIR, so every
 * scratch directory a test creates, and every relative path a mutated deletion might follow out of one, stays inside the
 * guard). A canary beside the scratch directories proves that nothing outside a scratch directory was removed.
 */
interface Sandbox {
	guard: string;
	dir: string;
	tmp: string;
	canary: string;
}

function inside(parent: string, child: string): boolean {
	const real = fs.realpathSync(child);
	const base = fs.realpathSync(parent);
	return real !== base && real.startsWith(base + path.sep);
}

function sandbox(): Sandbox {
	const guard = fs.mkdtempSync(path.join(os.tmpdir(), "oprm-guard-"));
	const dir = path.join(guard, "plugin");
	const tmp = path.join(guard, "tmp");
	fs.mkdirSync(tmp, { mode: 0o700 }); // a group-writable directory on the path would be refused by the very check under test
	fs.cpSync(ROOT, dir, { recursive: true, filter: src => !src.includes("node_modules") && !src.includes(`${path.sep}tests${path.sep}mutation`) });
	// the copy is never the shared checkout, and everything lives under the guard
	if (fs.realpathSync(dir) === fs.realpathSync(ROOT) || !inside(guard, dir) || !inside(guard, tmp)) throw new Error("the mutation copy is not isolated");
	// a test scratch directory is <tmp>/<random>; "../../x" from <scratch>/web is <tmp>/x, so a canary lives there
	const canary = path.join(tmp, "etc");
	fs.mkdirSync(canary);
	fs.writeFileSync(path.join(canary, "keep"), "x");
	return { guard, dir, tmp, canary };
}

function dispose(box: Sandbox): void {
	// the outcome of a mutated run is judged first; the guard is removed only if it still is a guard we made
	if (!path.basename(box.guard).startsWith("oprm-guard-") || !inside(os.tmpdir(), box.guard)) throw new Error("refusing to remove a directory that is not a mutation guard");
	fs.rmSync(box.guard, { recursive: true, force: true });
}

function untouched(box: Sandbox): void {
	expect(fs.readFileSync(path.join(box.canary, "keep"), "utf8"), "a mutated test removed something outside its scratch directory").toBe("x");
	expect(fs.readdirSync(box.guard).sort(), "something appeared or vanished beside the sandbox").toEqual(["plugin", "tmp"]);
}

function runTests(box: Sandbox, args: string[]): { code: number; out: string } {
	const r = Bun.spawnSync([process.execPath, "test", ...args], { cwd: box.dir, env: { PATH: process.env.PATH ?? "", HOME: box.dir, TMPDIR: box.tmp }, stdout: "pipe", stderr: "pipe", timeout: 180_000 });
	return { code: r.exitCode ?? -1, out: `${r.stdout}${r.stderr}` };
}

const escapeRegex = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Every test title in the tests the harness runs, with its file. A fragment must name exactly one of them. */
function resolveTest(fragment: string): { file: string; title: string } {
	// "<file>|<text>" names the file and the text to select by: for tests whose title is built in a loop, so it is not a literal
	const sep = fragment.indexOf("|tests/") === -1 && /^tests\/[^|]+\|/.test(fragment) ? fragment.indexOf("|") : -1;
	if (sep !== -1) return { file: fragment.slice(0, sep), title: fragment.slice(sep + 1) };
	const hits: Array<{ file: string; title: string }> = [];
	for (const file of FILES) {
		const text = fs.readFileSync(path.join(ROOT, file), "utf8");
		for (const m of text.matchAll(/\btest\(\s*"((?:[^"\\]|\\.)*)"/g)) {
			const title = JSON.parse(`"${m[1]}"`) as string;
			if (title.includes(fragment)) hits.push({ file, title });
		}
	}
	if (hits.length !== 1) throw new Error(`"${fragment}" must name exactly one test; it names ${hits.length}: ${hits.map(h => h.title).join(" | ")}`);
	return hits[0];
}

// Not mutated, on purpose: `readBounded` and `maxRequestBodySize` back up the Content-Length framing check (a body cannot
// carry more bytes than it declares), and the Origin match backs up the adapter's own Origin check; each is a second layer,
// so removing one alone changes no observable behaviour (also the size check on --asset-file, which the bounded read backs up). The pairs are mutated together where a test can see them.

/** [name, file, text that must exist, text that replaces it, a fragment of a failing test name] */
const MUTATIONS: Array<[string, string, string, string, string]> = [
	["pairing: custom header not required", "src/relay.ts", 'req.headers.get("x-omp-relay") !== "1" ||', "", "tests/unit/regressions.test.ts|no custom header (a plain cross-site form post): refused"],
	["pairing: Origin not required (neither the null check nor the match)", "src/relay.ts", "origin === null || !auth.origins().includes(origin)", "false", "tests/unit/regressions.test.ts|no Origin (plain requests with no Origin pass the identity layer, so only this check stops them): refused"],
	["pairing: Content-Length not required (chunked accepted)", "src/relay.ts", 'if (req.headers.has("transfer-encoding") || lengthHeader === null) return json(411, { error: "a Content-Length is required" });', "", "without Content-Length"],
	["pairing: budget not reserved before the awaits", "src/relay.ts", "pairFailures.length + pairInflight >= PAIR_FAILURES_PER_MINUTE", "pairFailures.length >= PAIR_FAILURES_PER_MINUTE", "stalled before their body arrives"],
	["pairing: no failure budget at all", "src/relay.ts", "pairFailures.length + pairInflight >= PAIR_FAILURES_PER_MINUTE", "false", "ten wrong links"],
	["relay: hashed assets cached as public", "src/relay.ts", '"private, max-age=31536000, immutable"', '"public, max-age=31536000, immutable"', "cached privately"],
	["relay: hostile path logged verbatim", "src/relay.ts", "const shown = url.pathname.startsWith", "const shown = url.pathname; const _unused = url.pathname.startsWith", "hostile path"],
	["relay: port may be shared with a second relay", "src/relay.ts", "reusePort: false", "reusePort: true", "second start"],
	["relay: check key published before the binds", "src/relay.ts", "const adapterRun = auth.start({", "writeCheckKey();\n\tconst adapterRun = auth.start({", "second start"],
	["token adapter: Host not checked", "src/auth/token.ts", "!hosts.has(", "false && !hosts.has(", "tests/unit/relay-token.test.ts|GET / wrong Host (DNS rebinding)"],
	["token adapter: proxy headers allowed", "src/auth/token.ts", "if (headers.has(name))", "if (false as boolean)", "tests/unit/regressions.test.ts|a proxy header: refused"],
	["pair: no listener proof", "src/cli.ts", "if (!proof.ok) {\n\t\tio.err(`No link", "if (false as boolean) {\n\t\tio.err(`No link", "does not prove to be this relay"],
	["pair: token made before the address is known", "src/cli.ts", "if (!auth.pairingLink(\"x\")) {", "await new PairingWriter(config.pairing.dir).createToken();\n\tif (!auth.pairingLink(\"x\")) {", "orphan token"],
	["status: unproven listener believed", "src/cli.ts", "if (proven.ok) running = proven.stats;", "running = proven.ok ? proven.stats : { rooms: 99, guests: 99, ready: true };", "status does not trust"],
	["rebuild-web: parent directory not created", "src/cli.ts", "ensurePrivateDir(path.dirname(config.web.dir), { tighten: false });", "", "fresh machine"],
	["init: configuration not validated before omp.bin runs", "src/cli.ts", "if (existing !== null) loadConfig(env);", "", "before it executes"],
	["config: unsafe parent directory accepted", "src/config.ts", "if (parent.mode & 0o022 && !(parent.mode & 0o1000)) throw", "if (false as boolean) throw", "directory that others can write"],
	["config: restart grace uncapped", "schemas/config-1.schema.json", '"maximum": 120000', '"maximum": 600000', "restartGraceMs above"],
	["store: zero-byte file treated as empty", "src/pairing-store.ts", 'JSON.parse(fs.readFileSync(fd, "utf8")); // a zero-byte', 'JSON.parse(fs.readFileSync(fd, "utf8") || "{}"); // a zero-byte', "zero-byte"],
	["store: loose lock file accepted", "src/pairing-store.ts", "if (fileProblem(st)) throw", "if (false as boolean) throw", "an existing lock file that others can read or write is refused"],
	["doctor: writer-side check of the store", "src/doctor.ts", "const view = new PairingReader(config.pairing.dir).read();", "const view = { refusal: null, hashes: new Set<string>() };", "doctor does not call"],
	["web: http loopback allowed on any redirect hop", "src/web-asset.ts", 'allowHttp = first.protocol === "http:";', "allowHttp = true;", "only as the starting URL"],
	["web: NOTICE not checked against its pin", "src/web-asset.ts", "if (!notice || sha256(notice) !== build.noticeSha256)", "if (false as boolean)", "an archive whose content matches but whose NOTICE differs from its pin is refused"],
	["dropins: service not pinned to the config file", "src/dropins.ts", "env: pinnedEnv(env),", "env: {},", "both the service and the proof command carry the absolute config path"],
	["dropins: foreign directory chmod-ed", "src/dropins.ts", "{ tighten: false }", "{}", "belongs to another tool"],
	["served path: group-writable parent accepted", "src/fsutil.ts", "if (st.mode & 0o022) throw new UnsafePathError(`${dir} is writable by group or others`);", "if (st.mode & 0o002) throw new UnsafePathError(`${dir} is writable by group or others`);", "group-writable parent is refused"],
	["check directory: open directory chmod-ed instead of refused", "src/fsutil.ts", "} else if (opts.refuseOpen) {", "} else if (false as boolean) {", "configured check directory"],
	["admission: no hard cap before the upgrade", "src/relay.ts", "if (sockets.size + upgrading >= limits.maxConnections) return false;", "", "hard cap on open sockets"],
	["stats: counts not authenticated", "src/listener-check.ts", "if (!timingSafeEqual(want, Buffer.from(outer.mac, \"hex\")))", "if (false as boolean)", "look-alike that holds the key"],
	["drop-ins: data root not pinned", "src/dropins.ts", "XDG_DATA_HOME: path.dirname(dataDir(env)),", "", "no XDG variables when the drop-ins are written"],
	["notice: a notice-only release is not installed", "src/cli.ts", " && have.meta.noticeSha256 === build.noticeSha256", "", "changes only NOTICE"],
	["served path: a shared group is accepted", "src/trustwalk.ts", 'if (platform === "darwin" || !groupIsPrivate(st.gid, uid, read))', "if (false as boolean)", "a group-writable ancestor is refused unless its group has no other member"],
	["served path: a primary member of a group is trusted whoever it is", "src/trustwalk.ts", "if (f[3] === String(gid) && !trusted.has(Number(f[2]))) return false;", "", "only a group whose every member"],
	["served path: a group member is trusted whoever it is", "src/trustwalk.ts", "if (id === undefined || !trusted.has(id)) return false;", "if (id === undefined) return false;", "only a group whose every member"],
	["served path: others-writable accepted", "src/trustwalk.ts", "if (st.mode & 0o002) return `${where} can be changed by any account (writable by others)`;", "", "others-writable is refused unless sticky"],
	["served path: another account's directory accepted", "src/trustwalk.ts", "if (st.uid !== uid && st.uid !== 0) return `${where} belongs to another account`;", "", "others-writable is refused unless sticky"],
	["served path: a link's target is not walked", "src/trustwalk.ts", 'if (link.startsWith("/")) current = "/";\n\t\t\tqueue.unshift(...link.split("/").filter(Boolean));', 'const real = fs.realpathSync(candidate);\n\t\t\tcurrent = path.dirname(real);\n\t\t\tqueue.unshift(path.basename(real));', "a link that looks safe but leads into a tree"],
	["served path: .. folded away before the walk", "src/trustwalk.ts", 'const absolute = target.startsWith("/") ? target : `${deps.cwd ?? process.cwd()}/${target}`;\n\tconst queue = absolute.split("/").filter(Boolean);', 'const queue = path.resolve(deps.cwd ?? process.cwd(), target).split("/").filter(Boolean);', "a .. after a link is taken from the real directory"],
	["served path: macOS ACL entries ignored", "src/trustwalk.ts", "if (bad.length) return", "if (false as boolean) return", "macOS: an allow entry for another principal"],
	["served path: an inherited macOS ACL entry is not read", "src/trustwalk.ts", "(\\S+)\\s+(?:inherited\\s+)?(allow|deny)", "(\\S+)\\s+(allow|deny)", "macOS: an `inherited` marker is read like any entry"],
	["served path: a macOS ACL line that is not understood is trusted", "src/trustwalk.ts", 'if (current !== null && /^\\s*\\d+:/.test(line)) return "the access control lists cannot be inspected (an entry that is not understood)";', "", "macOS: an `inherited` marker is read like any entry"],
	["served path: an uninspectable macOS ACL is trusted", "src/trustwalk.ts", 'if (!r || r.code !== 0) return "the access control lists cannot be inspected (ls failed)";', "if (!r || r.code !== 0) return null;", "macOS: an allow entry for another principal"],
	["served path: Linux ACL entries ignored", "src/trustwalk.ts", 'if ((entry[3] as string)[1] === "w") return', "if (false as boolean) return", "Linux: a '+' in ls output"],
	["served path: the relay serves from an unsafe tree", "src/relay.ts", "if (state.problem) return text(503,", "if (false as boolean) return text(503,", "does not serve its client from a tree another account could replace"],
	["served path: the resolved directory's identity is not re-checked", "src/relay.ts", "if (!st.isDirectory() || st.ino !== web.ino || st.dev !== web.dev) web = { ...web, at: 0 };", "if (false as boolean) web = { ...web, at: 0 };", "a web directory that is a link into a tree another account can write to"],
	["relay: an error page reads the index", "src/relay.ts", "return new Response(body, { status, headers: { ...PLAIN_HEADERS, ...extra } });", 'fs.readFileSync(path.join(config.web.dir, "index.html"), "utf8");\n\t\treturn new Response(body, { status, headers: { ...PLAIN_HEADERS, ...extra } });', "neither blocks the relay"],
	["listener proof: unbounded answer", "src/listener-check.ts", "if (total + value.byteLength > limit) return null;", "", "read only up to a small bound"],
	["stats: unbounded read", "src/listener-check.ts", "if (total + value.byteLength > limit) return null;", "", "endless stats answer"],
	["answer bound counted in characters", "src/listener-check.ts", "if (total + value.byteLength > limit) return null;", 'if (total + Buffer.from(value).toString("utf8").length > limit) return null;', "bytes, not characters"],
	["answer decoded chunk by chunk", "src/listener-check.ts", 'return Buffer.concat(chunks).toString("utf8");', 'return chunks.map(c => Buffer.from(c).toString("utf8")).join("");', "split across two chunks"],
	["asset-file: read not bounded", "src/fsutil.ts", "if (total > limit) return null;", "", "a descriptor is read up to its bound"],
	["install: no lock is taken", "src/web-asset.ts", "while (flock(fd, LOCK_EX | LOCK_NB) !== 0) {", "while (false as boolean) {", "is waited for, not taken over"],
	["install: no lock between racing installers", "src/web-asset.ts", "while (flock(fd, LOCK_EX | LOCK_NB) !== 0) {", "while (false as boolean) {", "two installers racing in separate processes"],
	["install: a loose lock file accepted", "src/web-asset.ts", "if (!st.isFile() || st.uid !== UID || st.mode & 0o077) throw", "if (false as boolean) throw", "a lock file that is not a private plain file"],
	["install: a damaged or foreign record is obeyed", "src/web-asset.ts", 'if (!rec || !isInstallName(target, rec.stage, "new") || !isInstallName(target, rec.old, "old")) {', "if (false as boolean) {", "damaged, foreign or unreadable"],
	["install: an interrupted install is not recovered first", "src/web-asset.ts", "		recoverLocked(target);\n		const stageName", "		const stageName", "interrupted between its two renames"],
	["install: staging path removed before it is created", "src/web-asset.ts", "let created = false;", "fs.rmSync(path.join(parent, `${path.basename(target)}.new.${process.pid}`), { recursive: true, force: true });\n\t\tlet created = false;", "interrupted between its two renames"],
	["notice: installed NOTICE not inspected", "src/web-asset.ts", "if (!notice || sha256(notice) !== meta.noticeSha256) return {", "if (false as boolean) return {", "a deleted or edited installed NOTICE"],
	["notice: a legacy record is certified", "src/web-asset.ts", 'if (!meta.noticeSha256) return { state: "legacy", meta };', "", "without a NOTICE hash the state is legacy"],
];

describe("the harness keeps a mutated copy inside its sandbox", () => {
	test("a copy's temporary directory is inside the guard, so a path that escapes a scratch directory lands in the sandbox, where a removal is seen", () => {
		const box = sandbox();
		try {
			const probe = Bun.spawnSync([process.execPath, "-e", 'const os=require("os"),fs=require("fs"),path=require("path");console.log(os.tmpdir());fs.rmSync(path.join(os.tmpdir(),"x","web","..","..","etc"),{recursive:true,force:true})'], { env: { PATH: process.env.PATH ?? "", HOME: box.dir, TMPDIR: box.tmp }, stdout: "pipe" });
			expect(fs.realpathSync(probe.stdout.toString().trim())).toBe(fs.realpathSync(box.tmp));
			expect(fs.existsSync(box.canary), "the removal escaped the sandbox's temporary directory's canary").toBe(false);
			expect(() => untouched(box)).toThrow(); // the check that guards every mutation sees it
			expect(fs.existsSync(ROOT) && fs.readdirSync(ROOT).includes("src"), "the shared checkout is intact").toBe(true);
		} finally {
			dispose(box);
		}
	});

	test("a directory that is not a guard of ours is never removed", () => {
		const other = fs.mkdtempSync(path.join(os.tmpdir(), "not-a-guard-"));
		try {
			expect(() => dispose({ guard: other, dir: other, tmp: other, canary: other })).toThrow(/refusing/);
			expect(fs.existsSync(other)).toBe(true);
		} finally {
			fs.rmSync(other, { recursive: true, force: true });
		}
	});
});

describe("the regression tests notice each reverted fix", () => {
	test("the unmutated copy passes completely (so a failure below is the mutation)", () => {
		const box = sandbox();
		try {
			const r = runTests(box, FILES);
			expect(r.code, r.out.slice(-1500)).toBe(0);
			untouched(box);
		} finally {
			dispose(box);
		}
	}, 900_000);

	for (const [name, file, from, to, fragment] of MUTATIONS) {
		if (process.env.MUTATION_ONLY && !new RegExp(process.env.MUTATION_ONLY).test(name)) continue; // a quick re-run of some cases
		test(`mutation: ${name}`, () => {
			const designated = resolveTest(fragment);
			const select = [designated.file, "-t", escapeRegex(designated.title)];
			const box = sandbox();
			try {
				// control: the designated test alone passes on the unmutated copy, and exactly one test ran
				const control = runTests(box, select);
				expect(control.code, `the designated test does not pass unmutated: ${control.out.slice(-800)}`).toBe(0);
				expect(control.out, "the selection ran something other than one test").toMatch(/\n\s*1 pass\n/);
				const target = path.join(box.dir, file);
				const text = fs.readFileSync(target, "utf8");
				expect(text.includes(from), `the text to mutate is not in ${file}`).toBe(true);
				fs.writeFileSync(target, text.replace(from, to));
				const r = runTests(box, select);
				expect(r.code, "the designated test still passes with the fix reverted").not.toBe(0);
				const failed = r.out.split("\n").filter(l => l.startsWith("(fail)"));
				expect(failed.length, `the designated test did not fail: ${r.out.slice(-600)}`).toBe(1);
				expect(failed[0].includes(designated.title), `a different test failed: ${failed[0]}`).toBe(true);
				untouched(box);
			} finally {
				dispose(box);
			}
		}, 300_000);
	}
});
