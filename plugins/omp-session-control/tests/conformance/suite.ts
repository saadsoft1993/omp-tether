// Conformance suite for the session-control/1 protocol (docs/protocol/control-1.md).
//
// It talks to a live implementation through its control directory only: it reads the registry, connects to the socket
// and exchanges frames. It never imports the extension, so it runs against any implementation of the protocol.
// Dependency-free: node:* modules only. Run it with Bun (or any runtime that supports TypeScript):
//
//   bun tests/conformance/suite.ts --dir <control dir> [--pid N] [--idle-ms N] [--mutate] [--exclusive] [--off-switch]
//
// Groups (read-only checks always run):
//   --mutate      also run abort and rename against the session (rename changes the session name)
//   --exclusive   also run checks that need every client slot free (the 4-client limit)
//   --idle-ms N   also check that a silent connection is closed after about N milliseconds
//   --off-switch  also create and remove the `off` file in the directory (closes the listener, then it comes back)
// Exit code 0: every check passed. 1: a check failed. 2: usage error or no session found.
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";

export interface Options {
	dir: string;
	pid?: number;
	idleMs?: number;
	mutate?: boolean;
	exclusive?: boolean;
	offSwitch?: boolean;
}

export interface CheckResult {
	id: string;
	title: string;
	ok: boolean;
	detail?: string;
}

interface Registry {
	file: string;
	data: Record<string, unknown>;
}

type Frame = Record<string, unknown>;

class Conn {
	private buffer = "";
	private frames: Frame[] = [];
	private waiters: Array<() => void> = [];
	closed = false;

	private constructor(readonly sock: net.Socket) {
		sock.setEncoding("utf8");
		sock.on("data", (chunk: string) => {
			this.buffer += chunk;
			for (;;) {
				const nl = this.buffer.indexOf("\n");
				if (nl < 0) break;
				const line = this.buffer.slice(0, nl);
				this.buffer = this.buffer.slice(nl + 1);
				try {
					this.frames.push(JSON.parse(line) as Frame);
				} catch {
					this.frames.push({ unparseable: line });
				}
			}
			this.wake();
		});
		const end = () => {
			this.closed = true;
			this.wake();
		};
		sock.on("close", end);
		sock.on("error", end);
	}

	static connect(socketPath: string): Promise<Conn> {
		return new Promise((resolve, reject) => {
			const sock = net.createConnection(socketPath);
			sock.once("connect", () => resolve(new Conn(sock)));
			sock.once("error", reject);
		});
	}

	private wake(): void {
		for (const w of this.waiters.splice(0)) w();
	}

	send(frame: unknown): void {
		this.sock.write(`${typeof frame === "string" ? frame : JSON.stringify(frame)}\n`);
	}

	/** Next frame, or null when the peer closed first or the deadline passed. */
	async next(timeoutMs = 5000): Promise<Frame | null> {
		const deadline = Date.now() + timeoutMs;
		for (;;) {
			const frame = this.frames.shift();
			if (frame) return frame;
			if (this.closed || Date.now() >= deadline) return null;
			await new Promise<void>(resolve => {
				const t = setTimeout(resolve, Math.max(1, deadline - Date.now()));
				this.waiters.push(() => {
					clearTimeout(t);
					resolve();
				});
			});
		}
	}

	async request(frame: unknown, timeoutMs = 5000): Promise<Frame> {
		this.send(frame);
		const reply = await this.next(timeoutMs);
		if (!reply) throw new Error("no reply (connection closed or timed out)");
		return reply;
	}

	/** Resolves true once the peer closes the connection. */
	async waitClosed(timeoutMs: number): Promise<boolean> {
		const deadline = Date.now() + timeoutMs;
		while (!this.closed && Date.now() < deadline) {
			await new Promise<void>(resolve => {
				const t = setTimeout(resolve, 50);
				this.waiters.push(() => {
					clearTimeout(t);
					resolve();
				});
			});
		}
		return this.closed;
	}

	close(): void {
		this.sock.destroy();
	}
}

/** Reads a small plain file without blocking and without following a symlink; undefined for anything else. */
function readSmallFile(file: string, maxBytes: number): string | undefined {
	try {
		if (!fs.lstatSync(file).isFile()) return undefined;
		const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
		try {
			const st = fs.fstatSync(fd);
			if (!st.isFile() || st.size > maxBytes) return undefined;
			return fs.readFileSync(fd, "utf8");
		} finally {
			fs.closeSync(fd);
		}
	} catch {
		return undefined;
	}
}

function readRegistries(dir: string): Registry[] {
	const out: Registry[] = [];
	let names: string[] = [];
	try {
		names = fs.readdirSync(dir);
	} catch {}
	for (const name of names) {
		if (!name.endsWith(".json")) continue;
		const file = path.join(dir, name);
		try {
			const text = readSmallFile(file, 4096);
			if (text === undefined) continue;
			const data = JSON.parse(text) as unknown;
			if (data && typeof data === "object" && !Array.isArray(data)) out.push({ file, data: data as Record<string, unknown> });
		} catch {}
	}
	return out;
}

function pickRegistry(dir: string, pid: number | undefined): Registry | undefined {
	const regs = readRegistries(dir).filter(r => pid === undefined || r.data.pid === pid);
	return regs.length === 1 ? regs[0] : undefined;
}

const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

async function waitFor<T>(fn: () => T | undefined | false, timeoutMs: number): Promise<T | undefined> {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const value = fn();
		if (value) return value;
		if (Date.now() >= deadline) return undefined;
		await sleep(100);
	}
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

function expect(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

function isSymlink(file: string): boolean {
	try {
		return fs.lstatSync(file).isSymbolicLink();
	} catch {
		return false;
	}
}

/** Marks a run that stopped at its prerequisites: no connection was made, no later group ran. */
function refuse(results: CheckResult[]): CheckResult[] {
	results.push({
		id: "S0",
		title: "the directory and registry passed their boundary checks before anything was connected",
		ok: false,
		detail: "a prerequisite failed; no connection was made and no later check ran",
	});
	return results;
}

/** Runs every check that applies to `opts`. Never throws for a failing check; a failure is a result. */
export async function runConformance(opts: Options): Promise<CheckResult[]> {
	const results: CheckResult[] = [];
	const check = async (id: string, title: string, fn: () => Promise<void> | void): Promise<void> => {
		try {
			await fn();
			results.push({ id, title, ok: true });
		} catch (error) {
			results.push({ id, title, ok: false, detail: error instanceof Error ? error.message : String(error) });
		}
	};

	if (!fs.existsSync(opts.dir) && !isSymlink(opts.dir)) {
		return [{ id: "R0", title: "exactly one live registry entry is found", ok: false, detail: `${opts.dir} does not exist` }];
	}
	// Nothing below connects to anything until the directory and the files in it have passed their checks.
	await check("R1", "control directory is a real directory (not a symlink), private (0700) and owned by the current user", () => {
		const st = fs.lstatSync(opts.dir);
		expect(!st.isSymbolicLink() && st.isDirectory(), "not a real directory");
		expect((st.mode & 0o777) === 0o700, `mode is ${(st.mode & 0o777).toString(8)}`);
		if (typeof process.getuid === "function") expect(st.uid === process.getuid(), "owned by another user");
	});
	if (results.some(r => !r.ok)) return refuse(results);

	const reg = pickRegistry(opts.dir, opts.pid);
	if (!reg) {
		results.push({
			id: "R0",
			title: "exactly one live registry entry is found",
			ok: false,
			detail: `no single registry${opts.pid === undefined ? "" : ` for pid ${opts.pid}`} in ${opts.dir} (pass --pid when several sessions run)`,
		});
		return results;
	}
	const nonce = String(reg.data.nonce);
	const socketPath = String(reg.data.socket);
	// ---- registry and files ------------------------------------------------------------------------------------------
	await check("R2", "registry file is <nonce>.json, a plain 0600 file under 4096 bytes", () => {
		const st = fs.lstatSync(reg.file);
		expect(st.isFile(), "not a regular file");
		expect((st.mode & 0o777) === 0o600, `mode is ${(st.mode & 0o777).toString(8)}`);
		expect(st.size <= 4096, `${st.size} bytes`);
		expect(path.basename(reg.file) === `${nonce}.json`, "file name does not match the nonce");
	});
	await check("R3", "registry fields: v, versions, nonce, pid, socket, sessionId, generation", () => {
		const d = reg.data;
		expect(d.v === 1, "v must be 1");
		expect(Array.isArray(d.versions) && d.versions.includes(1), "versions must list 1");
		expect(typeof d.nonce === "string" && /^[0-9a-f]{32}$/.test(d.nonce), "nonce must be 32 lowercase hex characters");
		expect(Number.isInteger(d.pid) && (d.pid as number) > 0, "pid must be a positive integer");
		expect(typeof d.socket === "string" && path.isAbsolute(d.socket), "socket must be an absolute path");
		expect(path.dirname(d.socket as string) === opts.dir, "socket must live in the control directory");
		expect(typeof d.sessionId === "string" && d.sessionId.length > 0, "sessionId must be a non-empty string");
		expect(Number.isInteger(d.generation) && (d.generation as number) >= 0, "generation must be a non-negative integer");
	});
	await check("R4", "socket is a Unix socket with mode 0600", () => {
		const st = fs.lstatSync(socketPath);
		expect(st.isSocket(), "not a socket");
		expect((st.mode & 0o777) === 0o600, `mode is ${(st.mode & 0o777).toString(8)}`);
		if (typeof process.getuid === "function") expect(st.uid === process.getuid(), "owned by another user");
	});
	await check("R5", "the registry pid is alive", () => {
		process.kill(reg.data.pid as number, 0);
	});

	if (results.some(r => !r.ok)) return refuse(results);

	// An implementation may still be tearing down connections from an earlier check, and may drop new ones meanwhile.
	const open = async (): Promise<Conn> => {
		let last: unknown;
		for (let attempt = 0; attempt < 15; attempt++) {
			try {
				return await Conn.connect(socketPath);
			} catch (error) {
				last = error;
				await sleep(200);
			}
		}
		throw last;
	};
	/** Runs `fn` on a fresh connection and always closes it. */
	const withConn = async <T>(fn: (c: Conn) => Promise<T>): Promise<T> => {
		const c = await open();
		try {
			return await fn(c);
		} finally {
			c.close();
		}
	};
	const snap = (c: Conn, id: string | number = "snap") => c.request({ v: 1, id, nonce, op: "snapshot" });
	const live = await withConn(async c => {
		const r = await snap(c);
		return isObj(r.result) ? r.result : {};
	}).catch(() => ({}) as Record<string, unknown>);
	const target = { sessionId: live.sessionId as string, generation: live.generation as number };

	// ---- snapshot and framing ----------------------------------------------------------------------------------------
	await check("P1", "snapshot returns the documented fields and agrees with the registry", () =>
		withConn(async c => {
			const r = await snap(c);
			expect(r.v === 1 && r.ok === true && r.id === "snap", `unexpected envelope ${JSON.stringify(r)}`);
			const s = r.result;
			expect(isObj(s), "result must be an object");
			expect(s.sessionId === reg.data.sessionId, "sessionId differs from the registry");
			expect(s.pid === reg.data.pid, "pid differs from the registry");
			expect(Number.isInteger(s.generation), "generation must be an integer");
			expect(typeof s.busy === "boolean", "busy must be a boolean");
			expect(s.name === null || typeof s.name === "string", "name must be a string or null");
			expect(s.protocol === 1, "protocol must be 1");
			expect(Array.isArray(s.protocols) && s.protocols.includes(1), "protocols must list 1");
			expect(Array.isArray(s.ops) && ["snapshot", "abort", "rename"].every(op => (s.ops as unknown[]).includes(op)), "ops must list snapshot, abort, rename");
			expect(isObj(s.plugin) || s.plugin === undefined, "plugin must be an object when present");
		}),
	);
	await check("P2", "a reply echoes a numeric id as a number and a string id as a string", () =>
		withConn(async c => {
			const a = await c.request({ v: 1, id: 7, nonce, op: "snapshot" });
			const b = await c.request({ v: 1, id: "seven", nonce, op: "snapshot" });
			expect(a.id === 7, `numeric id came back as ${JSON.stringify(a.id)}`);
			expect(b.id === "seven", `string id came back as ${JSON.stringify(b.id)}`);
		}),
	);
	await check("P3", "pipelined requests in one write all get a reply, matched by id", () =>
		withConn(async c => {
			c.send(`${JSON.stringify({ v: 1, id: "a", nonce, op: "snapshot" })}\n${JSON.stringify({ v: 1, id: "b", nonce, op: "snapshot" })}\n${JSON.stringify({ v: 1, id: "c", nonce, op: "snapshot" })}`);
			const ids = new Set<unknown>();
			for (let i = 0; i < 3; i++) ids.add((await c.next())?.id);
			expect(ids.has("a") && ids.has("b") && ids.has("c"), `got ids ${JSON.stringify([...ids])}`);
		}),
	);
	await check("P4", "a frame that is not JSON gets bad_json with id null, and the connection stays usable", () =>
		withConn(async c => {
			const r = await c.request("{not json");
			expect(r.ok === false && r.code === "bad_json" && r.id === null, JSON.stringify(r));
			expect((await snap(c)).ok === true, "connection unusable after bad_json");
		}),
	);
	await check("P5", "a frame that is not an object gets bad_request", () =>
		withConn(async c => {
			for (const frame of ["[1,2]", "7", '"text"', "null"]) {
				const r = await c.request(frame);
				expect(r.ok === false && r.code === "bad_request", `${frame} -> ${JSON.stringify(r)}`);
			}
		}),
	);
	await check("P6", "an unsupported v gets bad_version with the supported list, echoing the id", () =>
		withConn(async c => {
			const r = await c.request({ v: 2, id: "x", nonce, op: "snapshot" });
			expect(r.ok === false && r.code === "bad_version" && r.id === "x", JSON.stringify(r));
			expect(isObj(r.detail) && Array.isArray(r.detail.supported) && r.detail.supported.includes(1), "detail.supported must list 1");
		}),
	);
	await check("P7", "a missing, mistyped or over-long id gets bad_request with id null", () =>
		withConn(async c => {
			for (const id of [undefined, null, true, {}, "x".repeat(129)]) {
				const r = await c.request({ v: 1, id, nonce, op: "snapshot" });
				expect(r.ok === false && r.code === "bad_request", `id ${JSON.stringify(id)?.slice(0, 20)} -> ${JSON.stringify(r)}`);
				expect(r.id === null, `an unusable id must not be echoed, got ${JSON.stringify(r.id)?.slice(0, 20)}`);
			}
		}),
	);
	await check("P8", "a wrong or missing nonce gets bad_nonce", () =>
		withConn(async c => {
			for (const n of ["0".repeat(32), undefined, 5]) {
				const r = await c.request({ v: 1, id: "n", nonce: n, op: "snapshot" });
				expect(r.ok === false && r.code === "bad_nonce", `nonce ${JSON.stringify(n)} -> ${JSON.stringify(r)}`);
			}
		}),
	);
	await check("P9", "an unknown op gets unknown_op, including names inherited from Object.prototype", () =>
		withConn(async c => {
			for (const op of ["shutdown", "", "constructor", "__proto__", "toString", "hasOwnProperty", 7, null]) {
				const r = await c.request({ v: 1, id: "o", nonce, op });
				expect(r.ok === false && r.code === "unknown_op", `op ${JSON.stringify(op)} -> ${JSON.stringify(r)}`);
			}
		}),
	);
	await check("P10", "args that are not an object get invalid_args", () =>
		withConn(async c => {
			for (const args of [[], "x", 5, null]) {
				const r = await c.request({ v: 1, id: "a", nonce, op: "snapshot", args });
				expect(r.ok === false && r.code === "invalid_args", `args ${JSON.stringify(args)} -> ${JSON.stringify(r)}`);
			}
		}),
	);
	await check("P11", "a mutation with the wrong sessionId or generation is refused with the live values and does nothing", () =>
		withConn(async c => {
			const base = { v: 1, nonce, op: "rename", args: { name: "conformance probe" } };
			const missing = await c.request({ ...base, id: "m" });
			expect(missing.ok === false && missing.code === "stale_session", JSON.stringify(missing));
			const wrongSession = await c.request({ ...base, id: "s", sessionId: "00000000-0000-0000-0000-000000000000", generation: target.generation });
			expect(wrongSession.ok === false && wrongSession.code === "stale_session", JSON.stringify(wrongSession));
			expect(isObj(wrongSession.detail) && wrongSession.detail.sessionId === target.sessionId, "detail must carry the live sessionId");
			const wrongGen = await c.request({ ...base, id: "g", sessionId: target.sessionId, generation: target.generation + 7 });
			expect(wrongGen.ok === false && wrongGen.code === "stale_generation", JSON.stringify(wrongGen));
			expect(isObj(wrongGen.detail) && wrongGen.detail.generation === target.generation, "detail must carry the live generation");
			const abort = await c.request({ v: 1, id: "ab", nonce, op: "abort", sessionId: target.sessionId, generation: target.generation + 7 });
			expect(abort.ok === false && abort.code === "stale_generation", `abort: ${JSON.stringify(abort)}`);
			expect(((await snap(c)).result as Frame).name === live.name, "the name changed although the request was refused");
		}),
	);
	await check("P12", "rename rejects empty, over-long, control and invisible-format names with invalid_args", () =>
		withConn(async c => {
			const bad: unknown[] = ["", "   ", "a\nb", "tab\there", "x".repeat(121), 5, null, "prod\u202eved", "a\u200bb", "x\u2066y", "nul\u0000l", "del\u007f", "c1\u0085", "ls\u2028x"];
			for (const name of bad) {
				const r = await c.request({ v: 1, id: "e", nonce, op: "rename", sessionId: target.sessionId, generation: target.generation, args: { name } });
				expect(r.ok === false && r.code === "invalid_args", `${JSON.stringify(name)} -> ${JSON.stringify(r)}`);
			}
			const noArgs = await c.request({ v: 1, id: "e2", nonce, op: "rename", sessionId: target.sessionId, generation: target.generation });
			expect(noArgs.ok === false && noArgs.code === "invalid_args", `no args -> ${JSON.stringify(noArgs)}`);
		}),
	);
	await check("P13", "a frame over 64 KiB gets too_large and the connection is closed", () =>
		withConn(async c => {
			c.send("x".repeat(64 * 1024 + 10));
			const r = await c.next();
			expect(r && r.ok === false && r.code === "too_large", JSON.stringify(r));
			expect(await c.waitClosed(3000), "connection still open");
		}),
	);
	await check("P14", "a frame of exactly 64 KiB (one line, not JSON) is a bad_json, not too_large", () =>
		withConn(async c => {
			const r = await c.request("y".repeat(64 * 1024));
			expect(r.ok === false && r.code === "bad_json", JSON.stringify(r));
		}),
	);
	await check("P15", "the implementation survives abrupt disconnects and garbage, then still answers", async () => {
		for (let i = 0; i < 20; i++) {
			const c = await open();
			c.sock.write(i % 2 ? Buffer.from([0xff, 0xfe, 0x00, 0x0a]) : '{"v":1,"id":"x","nonce":"');
			c.close();
		}
		await sleep(200);
		await withConn(async c => expect((await snap(c)).ok === true, "no answer after the abuse"));
	});

	// ---- optional groups --------------------------------------------------------------------------------------------
	if (opts.exclusive) {
		await check("L1", "a fifth simultaneous client gets too_many_clients and is closed", async () => {
			const held: Conn[] = [];
			try {
				for (let i = 0; i < 4; i++) {
					// Slots held by connections that just closed may take a moment to free up.
					let reply: Frame | undefined;
					let c = await open();
					for (let attempt = 0; attempt < 20; attempt++) {
						reply = await snap(c, `h${i}`).catch(() => undefined);
						if (reply && reply.code !== "too_many_clients") break;
						c.close();
						await sleep(200);
						c = await open();
					}
					expect(reply?.ok === true, `client ${i} refused: ${JSON.stringify(reply)}`);
					held.push(c);
				}
				const fifth = await open();
				try {
					const r = await fifth.next();
					expect(r && r.ok === false && r.code === "too_many_clients", JSON.stringify(r));
					expect(await fifth.waitClosed(3000), "fifth connection still open");
				} finally {
					fifth.close();
				}
			} finally {
				for (const c of held) c.close();
				await sleep(300); // let the implementation notice the closed connections before the next check
			}
		});
	}
	if (opts.idleMs !== undefined) {
		const idleMs = opts.idleMs;
		await check("L2", `a silent connection is closed after about ${idleMs} ms`, () =>
			withConn(async c => {
				expect((await snap(c)).ok === true, "snapshot failed");
				const started = Date.now();
				expect(await c.waitClosed(idleMs + 5000), "connection was never closed");
				const took = Date.now() - started;
				expect(took >= idleMs - 500 && took <= idleMs + 5000, `closed after ${took} ms`);
			}),
		);
	}
	if (opts.mutate) {
		await check("M1", "abort on an idle session returns {aborted:false}", () =>
			withConn(async c => {
				const now = (await snap(c)).result as Frame;
				expect(now.busy === false, "the session is busy: run the mutation checks against an idle session");
				const r = await c.request({ v: 1, id: "ab", nonce, op: "abort", sessionId: target.sessionId, generation: target.generation });
				expect(r.ok === true && isObj(r.result) && r.result.aborted === false, JSON.stringify(r));
			}),
		);
		await check("M2", "rename sets the name, reports before and after, and collapses runs of spaces", () =>
			withConn(async c => {
				const before = ((await snap(c)).result as Frame).name;
				const r = await c.request({ v: 1, id: "rn", nonce, op: "rename", sessionId: target.sessionId, generation: target.generation, args: { name: "  Conformance   probe ✓ " } });
				expect(r.ok === true && isObj(r.result), JSON.stringify(r));
				expect(r.result.name === "Conformance probe ✓", `name is ${JSON.stringify(r.result.name)}`);
				expect(r.result.before === before, "before does not match the earlier snapshot");
				expect(((await snap(c)).result as Frame).name === "Conformance probe ✓", "snapshot does not show the new name");
			}),
		);
		await check("M3", "two mutations sent back to back both complete, in order", () =>
			withConn(async c => {
				const send = (id: string, name: string) => c.send({ v: 1, id, nonce, op: "rename", sessionId: target.sessionId, generation: target.generation, args: { name } });
				send("first", "Conformance one");
				send("second", "Conformance two");
				const got = [await c.next(), await c.next()];
				expect(got.every(r => r?.ok === true), JSON.stringify(got));
				expect(got[0]?.id === "first" && got[1]?.id === "second", `replies out of order: ${JSON.stringify(got.map(r => r?.id))}`);
				expect(((await snap(c)).result as Frame).name === "Conformance two", "the last rename must win");
			}),
		);
	}
	if (opts.offSwitch) {
		await check("O1", "the off file closes the listener and open connections; removing it brings control back", async () => {
			const off = path.join(opts.dir, "off");
			const old = await open();
			// Created exclusively and without following a symlink; only a marker this check made is ever removed.
			let created = false;
			try {
				expect((await snap(old)).ok === true, "snapshot failed");
				try {
					fs.closeSync(fs.openSync(off, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600));
					created = true;
				} catch (error) {
					throw new Error(`cannot create the off marker (it already exists, or the directory refuses it): ${(error as NodeJS.ErrnoException).code}`);
				}
				const gone = await waitFor(() => !fs.existsSync(reg.file) && !fs.existsSync(socketPath), 10_000);
				expect(gone, "registry and socket still exist 10 s after the off file appeared");
				const r = await old.next(3000);
				expect(r === null || r.code === "closing" || r.code === "disabled", `open connection got ${JSON.stringify(r)}`);
				expect(await old.waitClosed(3000), "open connection was not closed");
			} finally {
				old.close();
				if (created) fs.rmSync(off, { force: true });
			}
			const back = await waitFor(() => pickRegistry(opts.dir, opts.pid), 10_000);
			expect(back, "registry did not come back after the off file was removed");
			expect(back.data.nonce !== nonce, "the nonce must change when the listener restarts");
		});
	}
	return results;
}

function parseArgs(argv: string[]): Options | undefined {
	const opts: Partial<Options> = {};
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === "--dir") opts.dir = argv[++i];
		else if (a === "--pid") opts.pid = Number(argv[++i]);
		else if (a === "--idle-ms") opts.idleMs = Number(argv[++i]);
		else if (a === "--mutate") opts.mutate = true;
		else if (a === "--exclusive") opts.exclusive = true;
		else if (a === "--off-switch") opts.offSwitch = true;
		else return undefined;
	}
	if (!opts.dir || !path.isAbsolute(opts.dir)) return undefined;
	if (opts.pid !== undefined && !Number.isInteger(opts.pid)) return undefined;
	if (opts.idleMs !== undefined && !Number.isInteger(opts.idleMs)) return undefined;
	return opts as Options;
}

if (import.meta.main) {
	const opts = parseArgs(process.argv.slice(2));
	if (!opts) {
		console.error("usage: suite.ts --dir <absolute control dir> [--pid N] [--idle-ms N] [--mutate] [--exclusive] [--off-switch]");
		process.exit(2);
	}
	const results = await runConformance(opts);
	let n = 0;
	for (const r of results) {
		n += 1;
		console.log(`${r.ok ? "ok" : "not ok"} ${n} - ${r.id} ${r.title}${r.ok ? "" : `\n  # ${r.detail}`}`);
	}
	const failed = results.filter(r => !r.ok).length;
	console.log(`1..${n}\n# ${n - failed} passed, ${failed} failed`);
	process.exit(results.some(r => r.id === "R0" && !r.ok) ? 2 : failed ? 1 : 0);
}
