// Conformance suite for pairing-store/1 (docs/protocol/pairing-store-1.md).
//
// It checks an implementation through a tiny command contract and never imports it, so it runs against any
// implementation, in any language: the relay's own reader and writer, and the dashboard's. Every fixture is built at
// run time in a scratch directory; nothing outside it is touched. Dependency-free: node:* modules only.
//
//   bun tests/conformance/pairing-store-suite.ts --reader-json '["cmd","arg"]' --writer-json '["cmd","arg"]'
//   bun tests/conformance/pairing-store-suite.ts --reader "cmd arg" --writer "cmd arg"      (no spaces inside arguments)
//
// Either side may be given alone. The reader command is called   <reader...> check <dir> <cookie-name> <cookie-header>
// and prints {"paired":bool,"device":"<hash>"|null,"refused":"<code>"|null}; exit 0.
// The writer command is called (each prints one JSON object; a refused operation exits non-zero):
//   <writer...> create-token <dir>                -> {"token":"..."}
//   <writer...> redeem <dir> <token> <label>      -> {"session":"..."|null}
//   <writer...> devices <dir>                     -> {"devices":[{"id":"...","label":"...","pairedAt":N}]}
//   <writer...> revoke <dir> <prefix|all>         -> {"revoked":N}
// Exit code 0: every check passed. 1: a check failed. 2: usage error.
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface Impl {
	reader?: string[];
	writer?: string[];
}

export interface CheckResult {
	id: string;
	title: string;
	ok: boolean;
	detail?: string;
}

const sha256 = (text: string): string => createHash("sha256").update(text, "utf8").digest("hex");
const HEX64 = /^[0-9a-f]{64}$/;
const COOKIE = "pairing_probe";

interface Run {
	code: number;
	stdout: string;
	stderr: string;
	timedOut: boolean;
}

async function run(argv: string[], timeoutMs = 15_000): Promise<Run> {
	const proc = Bun.spawn(argv, { stdout: "pipe", stderr: "pipe", stdin: "ignore", env: { ...process.env, HOME: os.tmpdir() } });
	let timedOut = false;
	const timer = setTimeout(() => {
		timedOut = true;
		proc.kill("SIGKILL");
	}, timeoutMs);
	const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
	const code = await proc.exited;
	clearTimeout(timer);
	return { code, stdout, stderr, timedOut };
}

function json(r: Run): Record<string, unknown> | null {
	try {
		const v = JSON.parse(r.stdout.trim().split("\n").pop() ?? "");
		return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
	} catch {
		return null;
	}
}

interface Scratch {
	root: string;
	dir: string;
}

function scratch(): Scratch {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "ps1-"));
	return { root, dir: path.join(root, "store") };
}

function plainStore(dir: string, hashes: Record<string, unknown>): void {
	fs.mkdirSync(dir, { mode: 0o700 });
	fs.chmodSync(dir, 0o700);
	fs.writeFileSync(path.join(dir, "devices.json"), JSON.stringify(hashes), { mode: 0o600 });
	fs.chmodSync(path.join(dir, "devices.json"), 0o600);
}

const DEVICE = { label: "probe", paired_at: 1 };

interface Reading {
	paired: boolean;
	device: string | null;
	refused: string | null;
	raw: Run;
}

async function read(impl: Impl, dir: string, header: string, name = COOKIE): Promise<Reading | null> {
	const r = await run([...(impl.reader as string[]), "check", dir, name, header]);
	const out = json(r);
	if (r.timedOut || r.code !== 0 || !out) return { paired: false, device: null, refused: null, raw: r };
	return { paired: out.paired === true, device: typeof out.device === "string" ? out.device : null, refused: typeof out.refused === "string" ? out.refused : null, raw: r };
}

type Check = (impl: Impl, s: Scratch) => Promise<string | null>; // null = pass, string = why it failed

const need = (cond: boolean, why: string): string | null => (cond ? null : why);

function readerChecks(): Array<[string, string, Check]> {
	const cookie = "c".repeat(43);
	const hash = sha256(cookie);
	const header = `${COOKIE}=${cookie}`;
	const wrongShape = async (impl: Impl, s: Scratch, hashes: Record<string, unknown>, h = header): Promise<string | null> => {
		plainStore(s.dir, hashes);
		const r = await read(impl, s.dir, h);
		return need(r !== null && !r.paired && r.raw.code === 0, `paired=${r?.paired} exit=${r?.raw.code}${r?.raw.timedOut ? " (hung)" : ""}`);
	};
	return [
		[
			"R1",
			"a cookie whose SHA-256 is a key of devices.json is paired, and the answer names that hash",
			async (impl, s) => {
				plainStore(s.dir, { [hash]: DEVICE });
				const r = await read(impl, s.dir, header);
				return need(r?.paired === true && r.device === hash && r.refused === null, `paired=${r?.paired} device=${r?.device} refused=${r?.refused}`);
			},
		],
		["R2", "a cookie that is not in the store is not paired", (impl, s) => wrongShape(impl, s, { [hash]: DEVICE }, `${COOKIE}=${"d".repeat(43)}`)],
		["R3", "the right value under another cookie name is not paired", (impl, s) => wrongShape(impl, s, { [hash]: DEVICE }, `other=${cookie}`)],
		[
			"R4",
			"with two cookies of the configured name, a paired one is found even when a wrong one comes first",
			async (impl, s) => {
				plainStore(s.dir, { [hash]: DEVICE });
				const r = await read(impl, s.dir, `${COOKIE}=${"e".repeat(43)}; ${COOKIE}=${cookie}`);
				return need(r?.paired === true && r.device === hash, `paired=${r?.paired}`);
			},
		],
		["R5", "keys that are not lowercase SHA-256 hex (the raw cookie, upper case) pair nobody", (impl, s) => wrongShape(impl, s, { [cookie]: DEVICE, [hash.toUpperCase()]: DEVICE })],
		["R6", "a cookie value longer than 512 bytes is never paired, even if its hash is stored", (impl, s) => wrongShape(impl, s, { [sha256("f".repeat(600))]: DEVICE }, `${COOKIE}=${"f".repeat(600)}`)],
		[
			"R7",
			"an empty store pairs nobody and is not an error",
			async (impl, s) => {
				plainStore(s.dir, {});
				const r = await read(impl, s.dir, header);
				return need(r?.paired === false && r.refused === null && r.raw.code === 0, `paired=${r?.paired} refused=${r?.refused}`);
			},
		],
		...(
			[
				["R8", "a missing store directory is refused as store-missing", "store-missing", (s: Scratch) => void s],
				[
					"R9",
					"a devices.json readable by others is refused as store-open-to-others",
					"store-open-to-others",
					(s: Scratch) => {
						plainStore(s.dir, { [hash]: DEVICE });
						fs.chmodSync(path.join(s.dir, "devices.json"), 0o644);
					},
				],
				[
					"R10",
					"a store directory open to others is refused as store-open-to-others",
					"store-open-to-others",
					(s: Scratch) => {
						plainStore(s.dir, { [hash]: DEVICE });
						fs.chmodSync(s.dir, 0o755);
					},
				],
				[
					"R11",
					"a devices.json that is a symlink is refused as store-symlink",
					"store-symlink",
					(s: Scratch) => {
						plainStore(s.dir, { [hash]: DEVICE });
						fs.renameSync(path.join(s.dir, "devices.json"), path.join(s.root, "real.json"));
						fs.symlinkSync(path.join(s.root, "real.json"), path.join(s.dir, "devices.json"));
					},
				],
				[
					"R12",
					"a store directory that is a symlink is refused as store-symlink",
					"store-symlink",
					(s: Scratch) => {
						plainStore(path.join(s.root, "real"), { [hash]: DEVICE });
						fs.symlinkSync(path.join(s.root, "real"), s.dir);
					},
				],
				[
					"R13",
					"a devices.json that is not JSON is refused as store-invalid-json",
					"store-invalid-json",
					(s: Scratch) => {
						plainStore(s.dir, {});
						fs.writeFileSync(path.join(s.dir, "devices.json"), "{not json", { mode: 0o600 });
					},
				],
				[
					"R14",
					"a devices.json that is a JSON array is refused as store-not-object",
					"store-not-object",
					(s: Scratch) => {
						plainStore(s.dir, {});
						fs.writeFileSync(path.join(s.dir, "devices.json"), "[]", { mode: 0o600 });
					},
				],
				[
					"R15",
					"a devices.json that is a FIFO is refused as store-not-regular, without hanging",
					"store-not-regular",
					(s: Scratch) => {
						plainStore(s.dir, {});
						fs.rmSync(path.join(s.dir, "devices.json"));
						Bun.spawnSync(["mkfifo", path.join(s.dir, "devices.json")]);
					},
				],
			] as Array<[string, string, string, (s: Scratch) => void]>
		).map(([id, title, code, arrange]): [string, string, Check] => [
			id,
			title,
			async (impl, s) => {
				arrange(s);
				const r = await read(impl, s.dir, header);
				return need(r !== null && !r.paired && r.refused === code, `paired=${r?.paired} refused=${r?.refused} (wanted ${code})${r?.raw.timedOut ? " (hung)" : ""}`);
			},
		]),
		[
			"R16",
			"a reader never changes the store it refuses (no chmod, no deletion)",
			async (impl, s) => {
				plainStore(s.dir, { [hash]: DEVICE });
				fs.chmodSync(path.join(s.dir, "devices.json"), 0o644);
				fs.chmodSync(s.dir, 0o755);
				await read(impl, s.dir, header);
				const modes = [fs.statSync(s.dir).mode & 0o777, fs.statSync(path.join(s.dir, "devices.json")).mode & 0o777];
				return need(modes[0] === 0o755 && modes[1] === 0o644 && fs.readdirSync(s.dir).join() === "devices.json", `modes ${modes.map(m => m.toString(8))} files ${fs.readdirSync(s.dir)}`);
			},
		],
	];
}

// ── writer ──────────────────────────────────────────────────────────────────────────────────────

/** The suite's own reading of a store on disk: what a conforming reader would see, or why it would refuse. */
function reference(dir: string): { ok: true; devices: Record<string, { label?: unknown; paired_at?: unknown }>; pending: Record<string, unknown> } | { ok: false; why: string } {
	const uid = process.getuid?.() ?? -1;
	const d = fs.lstatSync(dir);
	if (!d.isDirectory() || d.isSymbolicLink() || d.uid !== uid || d.mode & 0o077) return { ok: false, why: `directory mode ${(d.mode & 0o777).toString(8)}` };
	const files: Record<string, Record<string, unknown>> = {};
	for (const name of ["devices.json", "pending.json"]) {
		const f = path.join(dir, name);
		if (!fs.existsSync(f)) {
			files[name] = {};
			continue;
		}
		const st = fs.lstatSync(f);
		if (!st.isFile() || st.uid !== uid || st.mode & 0o077) return { ok: false, why: `${name} mode ${(st.mode & 0o777).toString(8)}` };
		try {
			files[name] = JSON.parse(fs.readFileSync(f, "utf8"));
		} catch {
			return { ok: false, why: `${name} is not JSON` };
		}
	}
	return {
		ok: true,
		devices: files["devices.json"] as Record<string, { label?: unknown; paired_at?: unknown }>, // parsed JSON of a store the suite just checked
		pending: files["pending.json"] as Record<string, unknown>,
	};
}

const writerOk = (r: Run): boolean => r.code === 0 && !r.timedOut;

async function token(impl: Impl, dir: string): Promise<string | null> {
	const r = await run([...(impl.writer as string[]), "create-token", dir]);
	const out = json(r);
	return writerOk(r) && out && typeof out.token === "string" ? out.token : null;
}

async function redeem(impl: Impl, dir: string, t: string, label = "probe browser"): Promise<{ session: string | null; run: Run }> {
	const r = await run([...(impl.writer as string[]), "redeem", dir, t, label]);
	const out = json(r);
	return { session: writerOk(r) && out && typeof out.session === "string" ? out.session : null, run: r };
}

function writerChecks(): Array<[string, string, Check]> {
	return [
		[
			"W1",
			"create-token makes a 0700 directory and a 0600 pending.json holding only the token's SHA-256 and an expiry about five minutes away",
			async (impl, s) => {
				const t = await token(impl, s.dir);
				if (!t) return "no token";
				if (t.length < 43 || !/^[A-Za-z0-9_-]+$/.test(t)) return `token is not at least 43 URL-safe characters: ${t.length}`;
				const ref = reference(s.dir);
				if (!ref.ok) return ref.why;
				const keys = Object.keys(ref.pending);
				const expiry = ref.pending[sha256(t)];
				const now = Date.now() / 1000;
				return need(keys.length === 1 && typeof expiry === "number" && expiry > now + 280 && expiry < now + 320 && !fs.readFileSync(path.join(s.dir, "pending.json"), "utf8").includes(t), `keys=${keys.length} expiry-in=${typeof expiry === "number" ? Math.round(expiry - now) : expiry} (the token itself must not be stored)`);
			},
		],
		[
			"W2",
			"redeem spends the token: a new session whose hash is in devices.json with a label and paired_at, the token gone from pending.json, modes 0600/0700",
			async (impl, s) => {
				const t = await token(impl, s.dir);
				if (!t) return "no token";
				const { session, run: r } = await redeem(impl, s.dir, t, "probe browser");
				if (!session) return `no session (exit ${r.code})`;
				if (session.length < 43 || session === t) return "session is too short or equals the token";
				const ref = reference(s.dir);
				if (!ref.ok) return ref.why;
				const entry = ref.devices[sha256(session)];
				if (!entry || entry.label !== "probe browser" || !Number.isInteger(entry.paired_at)) return `device entry wrong: ${JSON.stringify(entry)}`;
				if (sha256(t) in ref.pending) return "token still pending after redeem";
				const stored = fs.readFileSync(path.join(s.dir, "devices.json"), "utf8");
				return need(!stored.includes(session) && !stored.includes(t), "the session id or token itself is stored");
			},
		],
		[
			"W3",
			"a token works once; an unknown token and an expired token are refused (null), and a refusal writes nothing",
			async (impl, s) => {
				const t = await token(impl, s.dir);
				if (!t) return "no token";
				if (!(await redeem(impl, s.dir, t)).session) return "first redeem failed";
				const second = await redeem(impl, s.dir, t);
				if (second.session !== null || !writerOk(second.run)) return `the same token redeemed twice (session=${second.session}, exit=${second.run.code})`;
				const before = fs.statSync(path.join(s.dir, "devices.json")).mtimeMs;
				const wrong = await redeem(impl, s.dir, "z".repeat(43));
				if (wrong.session !== null || !writerOk(wrong.run)) return "an unknown token was accepted or crashed the writer";
				const expired = "y".repeat(43);
				const pending = JSON.parse(fs.readFileSync(path.join(s.dir, "pending.json"), "utf8"));
				pending[sha256(expired)] = Date.now() / 1000 - 10;
				fs.writeFileSync(path.join(s.dir, "pending.json"), JSON.stringify(pending), { mode: 0o600 });
				const late = await redeem(impl, s.dir, expired);
				if (late.session !== null) return "an expired token was accepted";
				return need(fs.statSync(path.join(s.dir, "devices.json")).mtimeMs === before, "devices.json changed although every attempt was refused");
			},
		],
		[
			"W4",
			"a label is cut to 80 characters and loses control characters",
			async (impl, s) => {
				const t = await token(impl, s.dir);
				if (!t) return "no token";
				const { session } = await redeem(impl, s.dir, t, `evil\u001b[31m name\n${"x".repeat(200)}`);
				if (!session) return "no session";
				const ref = reference(s.dir);
				if (!ref.ok) return ref.why;
				const label = String(ref.devices[sha256(session)]?.label);
				// biome-ignore lint/suspicious/noControlCharactersInRegex: this is the check
				return need(label.length <= 80 && !/[\u0000-\u001f\u007f]/.test(label), `label ${JSON.stringify(label.slice(0, 40))} (${label.length})`);
			},
		],
		[
			"W5",
			"devices lists id (the first 8 hex characters of the hash), label and pairedAt, oldest first",
			async (impl, s) => {
				const sessions: string[] = [];
				for (const label of ["first", "second"]) {
					const t = await token(impl, s.dir);
					if (!t) return "no token";
					const { session } = await redeem(impl, s.dir, t, label);
					if (!session) return "no session";
					sessions.push(session);
					await Bun.sleep(1100); // paired_at has one-second resolution
				}
				const r = await run([...(impl.writer as string[]), "devices", s.dir]);
				const list = (json(r)?.devices ?? []) as Array<{ id: string; label: string; pairedAt: number }>;
				return need(
					writerOk(r) && list.length === 2 && list[0]?.label === "first" && list[0]?.id === sha256(sessions[0] as string).slice(0, 8) && list[1]?.label === "second",
					`devices=${JSON.stringify(list)}`,
				);
			},
		],
		[
			"W6",
			"revoke removes exactly one device by id prefix; a prefix shorter than 4 characters or matching two devices is refused and removes nothing",
			async (impl, s) => {
				const a = `abcd${"0".repeat(60)}`;
				const b = `abcd${"1".repeat(60)}`;
				const c = `ffff${"2".repeat(60)}`;
				plainStore(s.dir, { [a]: DEVICE, [b]: DEVICE, [c]: DEVICE });
				const w = impl.writer as string[];
				const short = await run([...w, "revoke", s.dir, "fff"]);
				const ambiguous = await run([...w, "revoke", s.dir, "abcd"]);
				if (short.code === 0 || ambiguous.code === 0) return "a short or ambiguous prefix was accepted";
				const ref = reference(s.dir);
				if (!ref.ok || Object.keys(ref.devices).length !== 3) return "a refused revoke changed the store";
				const one = await run([...w, "revoke", s.dir, "ffff"]);
				const after = reference(s.dir);
				return need(writerOk(one) && json(one)?.revoked === 1 && after.ok && Object.keys(after.devices).sort().join() === [a, b].join(), `revoked=${json(one)?.revoked}`);
			},
		],
		[
			"W7",
			"revoke all empties devices.json (still a valid object) and leaves pending tokens alone",
			async (impl, s) => {
				const t = await token(impl, s.dir);
				if (!t) return "no token";
				if (!(await redeem(impl, s.dir, t)).session) return "no session";
				const t2 = await token(impl, s.dir);
				const r = await run([...(impl.writer as string[]), "revoke", s.dir, "all"]);
				const ref = reference(s.dir);
				return need(writerOk(r) && json(r)?.revoked === 1 && ref.ok && Object.keys(ref.devices).length === 0 && t2 !== null && sha256(t2) in ref.pending, `exit=${r.code} ${r.stdout.trim()}`);
			},
		],
		[
			"W8",
			"a writer refuses to overwrite a store it cannot parse, and leaves it byte for byte as it was",
			async (impl, s) => {
				plainStore(s.dir, {});
				const file = path.join(s.dir, "devices.json");
				fs.writeFileSync(file, "{not json", { mode: 0o600 });
				const t = await token(impl, s.dir);
				if (!t) return "no token (create-token only touches pending.json)";
				const r = await redeem(impl, s.dir, t);
				return need(r.session === null && r.run.code !== 0 && fs.readFileSync(file, "utf8") === "{not json", `exit=${r.run.code} content=${fs.readFileSync(file, "utf8").slice(0, 20)}`);
			},
		],
		[
			"W13",
			"a zero-byte devices.json or pending.json is a truncated store: the writer refuses it and leaves it as it was",
			async (impl, s) => {
				plainStore(s.dir, {});
				const devices = path.join(s.dir, "devices.json");
				fs.writeFileSync(devices, "", { mode: 0o600 });
				const t = await token(impl, s.dir);
				if (!t) return "no token (create-token only touches pending.json)";
				const r = await redeem(impl, s.dir, t);
				if (r.session !== null || r.run.code === 0) return `redeem wrote over an empty devices.json (exit ${r.run.code})`;
				if (fs.readFileSync(devices, "utf8") !== "") return "devices.json was changed";
				const pending = path.join(s.dir, "pending.json");
				fs.writeFileSync(pending, "", { mode: 0o600 });
				const again = await run([...(impl.writer as string[]), "create-token", s.dir]);
				return need(again.code !== 0 && fs.readFileSync(pending, "utf8") === "", `create-token over an empty pending.json: exit ${again.code}`);
			},
		],
		[
			"W14",
			"an existing .lock that group or others can read or write is refused (the contract is 0600 for every file)",
			async (impl, s) => {
				plainStore(s.dir, {});
				const lock = path.join(s.dir, ".lock");
				fs.writeFileSync(lock, "", { mode: 0o666 });
				fs.chmodSync(lock, 0o666);
				const r = await run([...(impl.writer as string[]), "create-token", s.dir]);
				return need(r.code !== 0, "a writer took a lock file that other accounts can write");
			},
		],
		[
			"W9",
			"a writer tightens its own directory when it is too open, and refuses one that is a symlink",
			async (impl, s) => {
				fs.mkdirSync(s.dir, { mode: 0o755 });
				fs.chmodSync(s.dir, 0o755);
				if (!(await token(impl, s.dir))) return "refused an open directory of its own instead of tightening it";
				if ((fs.statSync(s.dir).mode & 0o777) !== 0o700) return "the directory was not tightened to 0700";
				const link = path.join(s.root, "link");
				fs.symlinkSync(s.dir, link);
				const r = await run([...(impl.writer as string[]), "create-token", link]);
				return need(r.code !== 0, "a symlinked store directory was accepted");
			},
		],
		[
			"W10",
			"leaves no temporary files behind, and writes only devices.json, pending.json and .lock",
			async (impl, s) => {
				for (let i = 0; i < 3; i++) {
					const t = await token(impl, s.dir);
					if (!t) return "no token";
					await redeem(impl, s.dir, t);
				}
				const names = fs.readdirSync(s.dir).sort();
				return need(names.every(n => ["devices.json", "pending.json", ".lock"].includes(n)), `files: ${names.join(", ")}`);
			},
		],
		[
			"W11",
			"eight redemptions at once all land: no lost update (writers serialise on the store)",
			async (impl, s) => {
				const tokens: string[] = [];
				for (let i = 0; i < 8; i++) {
					const t = await token(impl, s.dir);
					if (!t) return "no token";
					tokens.push(t);
				}
				const results = await Promise.all(tokens.map(t => redeem(impl, s.dir, t, "parallel")));
				const ref = reference(s.dir);
				const sessions = results.map(r => r.session).filter((x): x is string => x !== null);
				return need(sessions.length === 8 && ref.ok && sessions.every(x => sha256(x) in ref.devices) && Object.keys(ref.pending).length === 0, `sessions=${sessions.length} devices=${ref.ok ? Object.keys(ref.devices).length : ref.why}`);
			},
		],
		[
			"W12",
			"what the writer wrote is accepted by the reference reader: every device hash is 64 lowercase hex",
			async (impl, s) => {
				const t = await token(impl, s.dir);
				if (!t) return "no token";
				await redeem(impl, s.dir, t);
				const ref = reference(s.dir);
				return need(ref.ok && Object.keys(ref.devices).every(k => HEX64.test(k)), ref.ok ? "bad key shape" : ref.why);
			},
		],
	];
}

export async function runConformance(impl: Impl, only?: (id: string) => boolean): Promise<CheckResult[]> {
	const plan: Array<[string, string, Check]> = [...(impl.reader ? readerChecks() : []), ...(impl.writer ? writerChecks() : [])];
	const results: CheckResult[] = [];
	for (const [id, title, check] of plan) {
		if (only && !only(id)) continue;
		const s = scratch();
		try {
			const why = await check(impl, s);
			results.push({ id, title, ok: why === null, ...(why ? { detail: why } : {}) });
		} catch (err) {
			results.push({ id, title, ok: false, detail: `the check itself failed: ${String(err)}` });
		} finally {
			fs.rmSync(s.root, { recursive: true, force: true });
		}
	}
	return results;
}

function parseCommand(argv: string[], plain: string, asJson: string): string[] | undefined {
	const j = argv.indexOf(asJson);
	if (j >= 0) {
		const v = JSON.parse(argv[j + 1] ?? "null");
		if (!Array.isArray(v) || v.some(x => typeof x !== "string")) throw new Error(`${asJson} needs a JSON array of strings`);
		return v;
	}
	const p = argv.indexOf(plain);
	return p >= 0 ? (argv[p + 1] ?? "").split(/\s+/).filter(Boolean) : undefined;
}

if (import.meta.main) {
	let impl: Impl;
	try {
		impl = { reader: parseCommand(Bun.argv, "--reader", "--reader-json"), writer: parseCommand(Bun.argv, "--writer", "--writer-json") };
	} catch (err) {
		console.error(String(err));
		process.exit(2);
	}
	if (!impl.reader && !impl.writer) {
		console.error("give --reader and/or --writer (see the header of this file)");
		process.exit(2);
	}
	const results = await runConformance(impl);
	for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"} ${r.id} ${r.title}${r.detail ? `\n       ${r.detail}` : ""}`);
	const failed = results.filter(r => !r.ok).length;
	console.log(`${results.length - failed} passed, ${failed} failed`);
	process.exit(failed ? 1 : 0);
}
