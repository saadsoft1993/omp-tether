// omp-session-control: lets a local dashboard (or any program of the same user) stop the running turn of an
// interactive omp session and rename it, over a private Unix socket. Protocol: docs/protocol/control-1.md.
//
// The entry file name is the extension id (`extension-module:omp-session-control`): keep it stable. The file has no
// runtime dependencies; it imports only node:* modules and (type-only, or dynamically and optionally) omp itself.
//
// Inert by default. Nothing is created, watched or listened on unless the control directory exists, is a private
// directory of this user, and holds an `enabled` marker file. Then, in the main session of an interactive (TUI) run
// only (never in subagents, print or RPC runs), the extension opens:
//   <dir>/<random>.sock   0600 Unix socket
//   <dir>/<nonce>.json    0600 registry entry (pid, socket, sessionId, generation, protocol versions)
//   <dir>/events.log      0600, starts/stops/errors only, capped
// <dir> is 0700; that mode is the access control (the runtime exposes no peer credentials). A file named `off` in
// <dir> closes every session's control at once and stays off until it is removed.
//
// Every callback handed to a raw Node API (sockets, fs.watch, promises) is wrapped so that a throw is logged instead of
// reaching `uncaughtException`, which omp treats as fatal for the whole session. Timers use ctx.setTimeout, which omp
// isolates and clears on shutdown. tests/unit/crash-safety.test.ts audits this.
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

export const PLUGIN_NAME = "omp-session-control";
export const PLUGIN_VERSION = "0.1.0";
/** Wire protocol versions this build speaks (docs/protocol/control-1.md). */
export const PROTOCOL_VERSIONS: readonly number[] = [1];
const PROTOCOL_VERSION = 1;
export const OPS = ["snapshot", "abort", "rename"] as const;

const MAX_FRAME_BYTES = 64 * 1024;
const MAX_CLIENTS = 4;
const MAX_OUTSTANDING = 16;
const MAX_NAME_CHARS = 120;
const MAX_LOG_BYTES = 256 * 1024;
const MAX_LOG_RECORD_BYTES = 2048;
const MAX_DEBUG_TEXT = 200;
const MAX_REGISTRY_BYTES = 4096;
const MAX_WRITE_BACKLOG = 256 * 1024; // a client that never reads its replies is cut off
const MAX_CONNECTIONS = MAX_CLIENTS + 12; // beyond this, connections are dropped without a reply
const OP_TIMEOUT_MS = 10_000; // an operation omp does not finish by then gets a `timeout` reply
const FRAME_DEADLINE_MS = 10_000; // a frame must be complete this long after its first byte
const RECHECK_MS = 2_000; // while there is something to wait for, look at the files again this often (see reconcile)
const CLOSE_GRACE_MS = 2_000; // a socket we have said goodbye to is destroyed after this, whatever the peer does
export const DEFAULT_IDLE_TIMEOUT_MS = 60_000;
const MIN_IDLE_TIMEOUT_MS = 100;
const MAX_IDLE_TIMEOUT_MS = 3_600_000;
export const EXTENSION_ID = `extension-module:${path.basename(import.meta.path, path.extname(import.meta.path))}`;

const MUTATING: Readonly<Record<string, boolean>> = { snapshot: false, abort: true, rename: true };

// ---------------------------------------------------------------------------------------------------------------------
// Settings: environment > omp plugin settings (`omp plugin config set omp-session-control <key> <value>`) > default.
// omp's own loader applies neither the schema defaults nor the `env` fallback when an extension reads its settings, so
// both are done here.
// ---------------------------------------------------------------------------------------------------------------------

export interface Settings {
	/** Absolute control directory, or "" (control stays off). */
	controlDir: string;
	idleTimeoutMs: number;
	/** Also write the text of unexpected exceptions (bounded) to events.log. Off by default: that text can be sensitive. */
	debug: boolean;
	/** Human-readable notes about values that were ignored. */
	warnings: string[];
}

export interface SettingsInput {
	env: Readonly<Record<string, string | undefined>>;
	stored: Readonly<Record<string, unknown>>;
	home: string | undefined;
}

export function resolveSettings({ env, stored, home }: SettingsInput): Settings {
	const warnings: string[] = [];

	let controlDir = "";
	const dirSource = nonEmptyString(env.OMP_SESSION_CONTROL_DIR) ?? nonEmptyString(stored.controlDir);
	if (dirSource !== undefined) {
		if (path.isAbsolute(dirSource)) controlDir = path.resolve(dirSource);
		else warnings.push("controlDir must be an absolute path; control stays off");
	} else if (home && path.isAbsolute(home)) {
		controlDir = path.join(home, ".config", "omp-session-control");
	} else {
		warnings.push("HOME is not an absolute path and no controlDir is set; control stays off");
	}

	let idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS;
	for (const [label, raw] of [
		["OMP_SESSION_CONTROL_IDLE_MS", env.OMP_SESSION_CONTROL_IDLE_MS],
		["idleTimeoutMs", stored.idleTimeoutMs],
	] as const) {
		if (raw === undefined || raw === null || raw === "") continue;
		const n = typeof raw === "number" ? raw : typeof raw === "string" && /^\d+$/.test(raw.trim()) ? Number(raw) : Number.NaN;
		if (Number.isInteger(n) && n >= MIN_IDLE_TIMEOUT_MS && n <= MAX_IDLE_TIMEOUT_MS) {
			idleTimeoutMs = n;
			break;
		}
		warnings.push(`${label} must be an integer between ${MIN_IDLE_TIMEOUT_MS} and ${MAX_IDLE_TIMEOUT_MS}; ignored`);
	}
	const debugRaw = env.OMP_SESSION_CONTROL_DEBUG ?? stored.debugLog;
	const debug = debugRaw === true || (typeof debugRaw === "string" && ["1", "true", "yes", "on"].includes(debugRaw.trim().toLowerCase()));
	return { controlDir, idleTimeoutMs, debug, warnings };
}

function nonEmptyString(value: unknown): string | undefined {
	return typeof value === "string" && value.trim() !== "" ? value.trim() : undefined;
}

// ---------------------------------------------------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------------------------------------------------

export class ControlError extends Error {
	constructor(
		readonly code: string,
		message: string,
		readonly detail?: unknown,
	) {
		super(message);
	}
}

/**
 * A category for any thrown value: our own error code, the system error code (EACCES, ...), or the error's class name.
 * Never the message, and never throws. This is what events.log records by default.
 */
export function errorKind(error: unknown): string {
	try {
		if (error instanceof ControlError) return error.code;
		const code = (error as { code?: unknown } | null | undefined)?.code;
		if (typeof code === "string" && /^[A-Z][A-Z0-9_]{1,30}$/.test(code)) return code;
		const name = error instanceof Error ? error.name : typeof error;
		return /^[A-Za-z][A-Za-z0-9_.]{0,40}$/.test(name) ? name : "error";
	} catch {
		return "unprintable";
	}
}

/** Text of any thrown value; never throws itself (a thrown object may have a hostile `toString` or `message`). */
export function errorText(error: unknown): string {
	try {
		return error instanceof Error ? error.message : String(error);
	} catch {
		return "unprintable error";
	}
}

/** Trimmed name, or an error: 1-120 characters; no control characters (C0, DEL, C1, line/paragraph separators)
 * and no invisible format characters (Unicode Cf: bidi overrides, zero-width marks), which could disguise a name. */
export function cleanName(value: unknown): string {
	if (typeof value !== "string") throw new ControlError("invalid_args", "args.name must be a string");
	const name = value.replace(/ +/g, " ").trim(); // omp stores titles with runs of spaces collapsed
	const chars = [...name].length;
	if (chars < 1 || chars > MAX_NAME_CHARS) {
		throw new ControlError("invalid_args", `the name must be 1-${MAX_NAME_CHARS} characters`);
	}
	if (/[\u0000-\u001f\u007f-\u009f\u2028\u2029\p{Cf}]/u.test(name)) {
		throw new ControlError("invalid_args", "the name must not contain control or invisible characters");
	}
	return name;
}

// ---------------------------------------------------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------------------------------------------------

type DirState = { kind: "missing" } | { kind: "unsafe"; message: string } | { kind: "ok"; mode: number };

/** Looks at the control directory without creating or changing anything. */
function inspectDir(dir: string): DirState {
	let st: fs.Stats;
	try {
		st = fs.lstatSync(dir);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return { kind: "missing" };
		return { kind: "unsafe", message: `${dir} cannot be inspected (${(error as NodeJS.ErrnoException).code ?? "error"})` };
	}
	if (st.isSymbolicLink() || !st.isDirectory()) return { kind: "unsafe", message: `${dir} is not a real directory` };
	if (typeof process.getuid === "function" && st.uid !== process.getuid()) {
		return { kind: "unsafe", message: `${dir} is owned by another user` };
	}
	if ((st.mode & 0o022) !== 0) return { kind: "unsafe", message: `${dir} is writable by other users; run chmod 700 on it` };
	return { kind: "ok", mode: st.mode & 0o777 };
}

/**
 * Verify (and, with `create`, make) a private directory: a real directory, ours, not writable by others, mode 0700.
 * The checks and the mode change go through one open directory handle, so a swap of the path for a symlink between
 * the check and the change cannot make us chmod something else. Returns the directory's identity.
 */
function ensurePrivateDir(dir: string, create: boolean): fs.Stats {
	if (!dir) throw new ControlError("unsafe_path", "no absolute control directory");
	if (create) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
	let fd: number;
	try {
		fd = fs.openSync(dir, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW);
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === "ENOENT") throw new ControlError("unsafe_path", `${dir} does not exist`);
		if (code === "ELOOP" || code === "ENOTDIR") throw new ControlError("unsafe_path", `${dir} is not a real directory`);
		throw new ControlError("unsafe_path", `${dir} cannot be opened (${code ?? "error"})`);
	}
	try {
		const st = fs.fstatSync(fd);
		if (typeof process.getuid === "function" && st.uid !== process.getuid()) {
			throw new ControlError("unsafe_path", `${dir} is owned by another user`);
		}
		if ((st.mode & 0o022) !== 0) throw new ControlError("unsafe_path", `${dir} is writable by other users; run chmod 700 on it`);
		if ((st.mode & 0o777) !== 0o700) fs.fchmodSync(fd, 0o700);
		return fs.fstatSync(fd);
	} finally {
		fs.closeSync(fd);
	}
}

/** True when `file` exists in any form; fails closed (true) on an unexpected error. Never follows a symlink. */
function existsNoFollow(file: string): boolean {
	try {
		fs.lstatSync(file);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code !== "ENOENT";
	}
}

/** The `enabled` marker must be a plain file: a symlink does not count. */
function isRegularFile(file: string): boolean {
	try {
		return fs.lstatSync(file).isFile();
	} catch {
		return false;
	}
}

/**
 * Creates `file` as an empty private regular file if it is absent. Refuses anything else that is already there: opening
 * a FIFO for writing blocks until someone reads it, and a blocked synchronous call cannot be rescued from inside omp.
 */
function touchPrivateFile(file: string): void {
	try {
		if (!fs.lstatSync(file).isFile()) throw new ControlError("unsafe_path", `${file} is not a regular file`);
	} catch (error) {
		if (error instanceof ControlError) throw error;
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
	const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK, 0o600);
	try {
		if (!fs.fstatSync(fd).isFile()) throw new ControlError("unsafe_path", `${file} is not a regular file`);
	} finally {
		fs.closeSync(fd);
	}
}

/** Reads a small plain file without ever blocking; undefined for anything else (FIFO, device, symlink, too big). */
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

function removeIfPresent(file: string): void {
	try {
		fs.unlinkSync(file);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
}

function pidAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code === "EPERM";
	}
}

function writeFileAtomic(target: string, content: string): void {
	const tmp = `${target}.${crypto.randomBytes(6).toString("hex")}.tmp`;
	const fd = fs.openSync(
		tmp,
		fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW,
		0o600,
	);
	try {
		try {
			fs.writeSync(fd, content);
			fs.fsyncSync(fd);
		} finally {
			fs.closeSync(fd);
		}
		fs.renameSync(tmp, target); // replaces a symlink at `target` instead of following it
	} catch (error) {
		try {
			fs.unlinkSync(tmp);
		} catch {}
		throw error;
	}
}

/** Longest Unix socket path the platform accepts, in bytes. */
function maxSocketPathBytes(): number {
	return process.platform === "darwin" ? 103 : 107;
}

const UTF8 = new TextDecoder("utf-8", { fatal: true });
const CHURN_FILE = /(?:\.json|\.sock|\.tmp)$|^events\.log$/;

// ---------------------------------------------------------------------------------------------------------------------
// The extension
// ---------------------------------------------------------------------------------------------------------------------

interface Request {
	id: string | number;
	sessionId: string | null;
	generation: number | null;
	op: string;
	args: Record<string, unknown>;
}

interface Client {
	sock: net.Socket;
	buffer: Buffer;
	idle: Timer | undefined;
	/** Deadline for the frame being received; set while a partial frame sits in `buffer`. */
	frame: Timer | undefined;
	dead: boolean;
}

type Activation =
	| { state: "ready" }
	| { state: "inert"; reason: string }
	| { state: "off"; reason: string }
	| { state: "unsafe"; reason: string };

/**
 * Runs `load`, which imports an omp-internal module, and returns undefined when that fails: without the module the
 * extension still works, minus one feature. The imports below are dynamic on purpose (a static import would stop the
 * whole extension from loading when omp renames or removes the module) and their specifiers must stay string literals:
 * omp resolves its own modules for extensions at load time, and a specifier held in a variable is not resolved.
 */
async function optional<T>(load: () => Promise<T>): Promise<T | undefined> {
	try {
		return await load();
	} catch {
		return undefined;
	}
}

const loadPluginLoader = () => import("@oh-my-pi/pi-coding-agent/extensibility/plugins/loader");
const loadSettingsRegistry = () => import("@oh-my-pi/pi-coding-agent/config/registry");

export default async function ompSessionControl(pi: ExtensionAPI): Promise<void> {
	// -- settings -----------------------------------------------------------------------------------------------------
	const home = process.env.HOME || os.homedir();
	let stored: Record<string, unknown> = {};
	try {
		// omp merges project-level plugin overrides (a repository's .omp folder) over the user's settings for the
		// directory it is given. Ask about the home directory, so that a repository cannot move the control directory.
		const loader = await optional(loadPluginLoader);
		const result = await loader?.getPluginSettings(PLUGIN_NAME, home);
		if (result && typeof result === "object") stored = result as Record<string, unknown>;
	} catch {}
	const settings = resolveSettings({ env: process.env, stored, home });
	const CONTROL_DIR = settings.controlDir;
	const OFF_FILE = path.join(CONTROL_DIR, "off");
	const ENABLED_FILE = path.join(CONTROL_DIR, "enabled");
	const EVENT_LOG = path.join(CONTROL_DIR, "events.log");
	const IDLE_TIMEOUT_MS = settings.idleTimeoutMs;
	const SUPPORTED = process.platform !== "win32";

	let dirOk = false; // set once the directory passed ensurePrivateDir; nothing is written there before

	/**
	 * Starts, stops and errors only, as categories (see errorKind); the text of an unexpected exception is added, bounded,
	 * only with the debug setting. Capped, never follows a symlink, never opens anything but a plain file (a FIFO would
	 * block the session), only inside a checked directory. Never throws.
	 */
	const logEvent = (kind: string, data: Record<string, unknown> = {}): void => {
		if (!dirOk || !CONTROL_DIR) return;
		try {
			try {
				if (!fs.lstatSync(EVENT_LOG).isFile()) return;
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code !== "ENOENT") return;
			}
			const fd = fs.openSync(
				EVENT_LOG,
				fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
				0o600,
			);
			try {
				const st = fs.fstatSync(fd);
				if (!st.isFile() || (typeof process.getuid === "function" && st.uid !== process.getuid())) return;
				if ((st.mode & 0o777) !== 0o600) fs.fchmodSync(fd, 0o600);
				let line = `${JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, kind, ...data })}\n`;
				if (Buffer.byteLength(line) > MAX_LOG_RECORD_BYTES) line = `${JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, kind, truncated: true })}\n`;
				if (st.size + Buffer.byteLength(line) > MAX_LOG_BYTES) fs.ftruncateSync(fd, 0);
				fs.writeSync(fd, line);
			} finally {
				fs.closeSync(fd);
			}
		} catch {}
	};

	/** What to log about a failure: the category, plus bounded text only when the debug setting is on. */
	const failure = (error: unknown): Record<string, unknown> => {
		const out: Record<string, unknown> = { error: errorKind(error) };
		if (settings.debug) out.text = errorText(error).slice(0, MAX_DEBUG_TEXT);
		return out;
	};

	/** Short text for the person at the terminal: our own messages as they are, anything else as its category. */
	const userError = (error: unknown): string => (error instanceof ControlError ? error.message : errorKind(error));

	/** Runs `fn` and logs instead of throwing: nothing in here may take the session down. */
	const guarded = <A extends unknown[]>(label: string, fn: (...args: A) => void): ((...args: A) => void) => {
		return (...args: A) => {
			try {
				fn(...args);
			} catch (error) {
				logEvent("handler_error", { label, ...failure(error) });
			}
		};
	};

	/** The same for a promise nobody awaits. */
	const guardedAsync = (label: string, fn: () => Promise<void>): Promise<void> =>
		fn().catch(error => logEvent("handler_error", { label, ...failure(error) }));

	let ctx: ExtensionContext | undefined;
	let server: net.Server | undefined;
	let nonce = "";
	let socketPath = "";
	let registryPath = "";
	let generation = 0;
	let outstanding = 0;
	let mutationChain: Promise<void> = Promise.resolve();
	let dirWatcher: fs.FSWatcher | undefined;
	let selfDisabled = false;
	let stopped = false;
	const notified = new Set<string>();
	let lastFailure = "";
	/** True once control has been listening in this session; the directory is then worth waiting for. */
	let everActive = false;
	let parentWatcher: fs.FSWatcher | undefined;
	/** The one pending periodic recheck, if any. */
	let recheckTimer: Timer | undefined;
	/** "" when the live `disabledExtensions` watch works, else why not. */
	let liveDisableProblem = "";
	/** A host mutation (rename, abort) that was started and has not settled yet, even if its request timed out. */
	let hostMutationRunning = false;
	/** True once the socket is bound and the registry published; the registry is never written before. */
	let listening = false;
	/** Counts listener starts; requests accepted by an earlier listener never touch the current one's counters. */
	let listenerEpoch = 0;
	/** Identity of the control directory this listener was started in (device and inode). */
	let dirIdentity = "";
	let unsubscribeDisabled: (() => void) | undefined;
	const clients = new Set<Client>();

	const isMainTui = (c: ExtensionContext): boolean => c.mode === "tui" && c.agent.kind === "main";
	const currentSessionId = (): string => ctx?.sessionManager.getSessionId() ?? "";

	const notifyOnce = (message: string, key = message): void => {
		if (notified.has(key) || !ctx) return;
		notified.add(key);
		try {
			ctx.ui.notify(`session-control: ${message}`, "warning");
		} catch {}
	};

	// -- activation ---------------------------------------------------------------------------------------------------

	const activation = (): Activation => {
		if (!SUPPORTED) return { state: "inert", reason: "Windows is not supported" };
		if (!CONTROL_DIR) return { state: "inert", reason: settings.warnings[0] ?? "no control directory configured" };
		if (selfDisabled) return { state: "off", reason: `${EXTENSION_ID} is in disabledExtensions` };
		const dir = inspectDir(CONTROL_DIR);
		if (dir.kind === "missing") return { state: "inert", reason: `${CONTROL_DIR} does not exist` };
		if (dir.kind === "unsafe") return { state: "unsafe", reason: dir.message };
		if (!isRegularFile(ENABLED_FILE)) return { state: "inert", reason: `no enabled marker in ${CONTROL_DIR}` };
		if (existsNoFollow(OFF_FILE)) return { state: "off", reason: `${OFF_FILE} exists` };
		return { state: "ready" };
	};

	const off = (): boolean => activation().state !== "ready";

	// -- registry and sockets -----------------------------------------------------------------------------------------

	/** Registries (and their sockets) left by sessions that crashed or were SIGKILLed. */
	const removeStaleEntries = (): void => {
		for (const name of fs.readdirSync(CONTROL_DIR)) {
			if (!name.endsWith(".json")) continue;
			const file = path.join(CONTROL_DIR, name);
			try {
				const text = readSmallFile(file, MAX_REGISTRY_BYTES);
				if (text === undefined) continue;
				const reg = JSON.parse(text) as { pid?: unknown; socket?: unknown };
				if (typeof reg.pid !== "number" || reg.pid === process.pid || pidAlive(reg.pid)) continue;
				if (typeof reg.socket === "string" && path.dirname(reg.socket) === CONTROL_DIR) {
					try {
						if (fs.lstatSync(reg.socket).isSocket()) fs.unlinkSync(reg.socket);
					} catch {}
				}
				fs.unlinkSync(file);
				logEvent("removed_stale", { stalePid: reg.pid });
			} catch {}
		}
	};

	const writeRegistry = (): void => {
		if (!server || !listening || !registryPath || !ctx) return;
		writeFileAtomic(
			registryPath,
			`${JSON.stringify({
				v: PROTOCOL_VERSION,
				versions: PROTOCOL_VERSIONS,
				nonce,
				pid: process.pid,
				socket: socketPath,
				sessionId: currentSessionId(),
				generation,
				plugin: { name: PLUGIN_NAME, version: PLUGIN_VERSION },
				writtenAt: new Date().toISOString(),
			})}\n`,
		);
	};

	const clearIdle = (client: Client): void => {
		if (client.idle !== undefined) {
			try {
				ctx?.clearTimer(client.idle);
			} catch {}
			client.idle = undefined;
		}
	};

	/** (Re)arms the idle deadline. ctx.setTimeout contains a throwing callback and is cleared on shutdown. */
	const touch = (client: Client): void => {
		clearIdle(client);
		if (client.dead || !ctx) return;
		client.idle = ctx.setTimeout(() => client.sock.destroy(), IDLE_TIMEOUT_MS);
	};

	const clearFrame = (client: Client): void => {
		if (client.frame !== undefined) {
			try {
				ctx?.clearTimer(client.frame);
			} catch {}
			client.frame = undefined;
		}
	};

	/** Starts the deadline of the frame now being received; a client that cannot finish one in time is dropped. */
	const armFrame = (client: Client): void => {
		clearFrame(client);
		if (client.dead || !ctx) return;
		client.frame = ctx.setTimeout(() => client.sock.destroy(), FRAME_DEADLINE_MS);
	};

	/**
	 * Sends one last frame and closes. The socket is destroyed once the frame is flushed and, whatever the peer does
	 * with its side, after CLOSE_GRACE_MS: a peer that never closes cannot keep the descriptor.
	 */
	const sayGoodbye = (sock: net.Socket, body: Record<string, unknown>): void => {
		try {
			sock.end(`${JSON.stringify({ v: PROTOCOL_VERSION, ...body })}\n`, guarded("goodbye", () => sock.destroy()));
		} catch {
			sock.destroy();
			return;
		}
		try {
			ctx?.setTimeout(() => sock.destroy(), CLOSE_GRACE_MS);
		} catch {}
	};

	const reply = (sock: net.Socket, body: Record<string, unknown>): void => {
		if (sock.destroyed) return;
		try {
			if (sock.writableLength > MAX_WRITE_BACKLOG) {
				sock.destroy();
				return;
			}
			let text = JSON.stringify({ v: PROTOCOL_VERSION, ...body });
			if (Buffer.byteLength(text) > MAX_FRAME_BYTES) {
				const id = typeof body.id === "number" || (typeof body.id === "string" && body.id.length <= 128) ? body.id : null;
				text = JSON.stringify({ v: PROTOCOL_VERSION, id, ok: false, code: "internal", message: "the reply would exceed the frame limit" });
			}
			sock.write(`${text}\n`);
		} catch {}
	};

	const closeAll = (reason: string): void => {
		const had = server !== undefined || clients.size > 0;
		for (const client of clients) {
			client.dead = true;
			clearIdle(client);
			clearFrame(client);
			sayGoodbye(client.sock, { id: null, ok: false, code: "closing", message: reason });
		}
		clients.clear();
		if (server) {
			try {
				server.close();
			} catch {}
			server = undefined;
		}
		for (const p of [socketPath, registryPath]) {
			if (!p) continue;
			try {
				fs.unlinkSync(p);
			} catch {}
		}
		socketPath = "";
		registryPath = "";
		listening = false;
		// Whatever the closed listener still has queued or hung must not count against, or block, the next one.
		listenerEpoch += 1;
		outstanding = 0;
		mutationChain = Promise.resolve();
		if (had) logEvent("closed", { reason });
	};

	// -- operations ---------------------------------------------------------------------------------------------------

	const requireCtx = (): ExtensionContext => {
		if (!ctx) throw new ControlError("not_ready", "session not bound yet");
		return ctx;
	};

	const snapshot = (c: ExtensionContext) => ({
		sessionId: currentSessionId(),
		generation,
		name: pi.getSessionName() ?? null,
		busy: !c.isIdle(),
		pid: process.pid,
		protocol: PROTOCOL_VERSION,
		protocols: PROTOCOL_VERSIONS,
		ops: OPS,
		plugin: { name: PLUGIN_NAME, version: PLUGIN_VERSION },
	});

	const checkTarget = (req: Request): void => {
		const live = { sessionId: currentSessionId(), generation };
		if (req.sessionId !== live.sessionId) throw new ControlError("stale_session", "sessionId does not match the live session", live);
		if (req.generation !== generation) throw new ControlError("stale_generation", "generation does not match", live);
	};

	const runOp = async (req: Request): Promise<unknown> => {
		const c = requireCtx();
		switch (req.op) {
			case "snapshot":
				return snapshot(c);
			case "abort": {
				const wasBusy = !c.isIdle();
				if (wasBusy) c.abort();
				return { aborted: wasBusy };
			}
			case "rename": {
				const name = cleanName(req.args.name);
				const before = pi.getSessionName() ?? null;
				await pi.setSessionName(name);
				const after = pi.getSessionName() ?? null;
				if (after !== name) throw new ControlError("not_renamed", "omp kept the previous name", { before, after });
				return { before, name: after };
			}
			default:
				throw new ControlError("unknown_op", `unknown op ${req.op}`);
		}
	};

	/** Runs the operation, but not for longer than OP_TIMEOUT_MS: a hung omp call must not wedge control. */
	const runWithDeadline = async (c: ExtensionContext, req: Request): Promise<unknown> => {
		let timer: Timer | undefined;
		const deadline = new Promise<never>((_resolve, reject) => {
			timer = c.setTimeout(
				() => reject(new ControlError("timeout", `omp did not finish ${req.op} within ${OP_TIMEOUT_MS / 1000} s`)),
				OP_TIMEOUT_MS,
			);
		});
		// A mutation keeps the host busy until omp settles it, however long that takes: the flag is cleared by the
		// operation itself, not by this deadline, so a timed-out change cannot overlap the next one.
		const work = MUTATING[req.op]
			? (async () => {
					hostMutationRunning = true;
					try {
						return await runOp(req);
					} finally {
						hostMutationRunning = false;
					}
				})()
			: runOp(req);
		try {
			return await Promise.race([work, deadline]);
		} finally {
			if (timer !== undefined) c.clearTimer(timer);
		}
	};

	const handleLine = (client: Client, line: string): void => {
		const sock = client.sock;
		let raw: unknown;
		try {
			raw = JSON.parse(line);
		} catch {
			return reply(sock, { id: null, ok: false, code: "bad_json", message: "frame is not JSON" });
		}
		if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
			return reply(sock, { id: null, ok: false, code: "bad_request", message: "frame must be an object" });
		}
		const frame = raw as Record<string, unknown>;
		const id =
			(typeof frame.id === "number" && Number.isFinite(frame.id)) || (typeof frame.id === "string" && frame.id.length <= 128) ? frame.id : null;
		const fail = (code: string, message: string, detail?: unknown) =>
			reply(sock, { id, ok: false, code, message, ...(detail !== undefined ? { detail } : {}) });
		if (frame.v !== PROTOCOL_VERSION) return fail("bad_version", `v must be ${PROTOCOL_VERSION}`, { supported: PROTOCOL_VERSIONS });
		if (id === null) return fail("bad_request", "id must be a string (<=128) or number");
		if (frame.nonce !== nonce) return fail("bad_nonce", "nonce does not match this process");
		if (off()) {
			fail("disabled", "session control is turned off");
			closeAll("off");
			return;
		}
		if (typeof frame.op !== "string" || !Object.hasOwn(MUTATING, frame.op)) return fail("unknown_op", "unknown op");
		if (frame.args !== undefined && (typeof frame.args !== "object" || frame.args === null || Array.isArray(frame.args))) {
			return fail("invalid_args", "args must be an object");
		}
		const req: Request = {
			id,
			sessionId: typeof frame.sessionId === "string" ? frame.sessionId : null,
			generation: typeof frame.generation === "number" ? frame.generation : null,
			op: frame.op,
			args: (frame.args as Record<string, unknown> | undefined) ?? {},
		};
		if (outstanding >= MAX_OUTSTANDING) return fail("overloaded", "too many requests in flight");
		outstanding += 1;
		const epoch = listenerEpoch;
		const execute = async (): Promise<void> => {
			try {
				// A client that has gone, or a listener that was closed meanwhile, is owed no work: skip the mutation.
				if (client.dead || epoch !== listenerEpoch) return;
				if (off()) throw new ControlError("disabled", "session control is turned off");
				if (MUTATING[req.op]) {
					checkTarget(req); // re-checked when the mutation actually starts
					if (hostMutationRunning) throw new ControlError("mutation_pending", "an earlier change has not finished in omp yet");
				}
				reply(sock, { id, ok: true, result: await runWithDeadline(requireCtx(), req) });
			} catch (error) {
				// omp's own error text can hold file paths: clients get a fixed message, events.log gets the text.
				const ce =
					error instanceof ControlError ? error : new ControlError("internal", "omp failed while running the operation");
				if (!(error instanceof ControlError)) logEvent("op_error", { op: req.op, ...failure(error) });
				if (ce.code === "timeout") logEvent("op_timeout", { op: req.op });
				reply(sock, { id, ok: false, code: ce.code, message: ce.message, ...(ce.detail !== undefined ? { detail: ce.detail } : {}) });
			} finally {
				if (epoch === listenerEpoch) outstanding -= 1;
			}
		};
		if (MUTATING[req.op]) mutationChain = mutationChain.then(() => guardedAsync("mutation", execute));
		else void guardedAsync("op", execute);
	};

	const onConnection = (sock: net.Socket): void => {
		sock.on("error", guarded("socket_error", () => sock.destroy()));
		if (off()) return sayGoodbye(sock, { id: null, ok: false, code: "disabled", message: "session control is turned off" });
		if (clients.size >= MAX_CLIENTS) {
			return sayGoodbye(sock, { id: null, ok: false, code: "too_many_clients", message: `max ${MAX_CLIENTS}` });
		}
		const client: Client = { sock, buffer: Buffer.alloc(0), idle: undefined, frame: undefined, dead: false };
		clients.add(client);
		touch(client);
		sock.on(
			"data",
			guarded("data", (chunk: Buffer) => {
				if (client.dead) return;
				touch(client);
				client.buffer = Buffer.concat([client.buffer, chunk]);
				let finishedFrame = false;
				for (;;) {
					const nl = client.buffer.indexOf(0x0a);
					if (nl < 0 || nl > MAX_FRAME_BYTES) break;
					const bytes = client.buffer.subarray(0, nl);
					client.buffer = client.buffer.subarray(nl + 1);
					finishedFrame = true;
					// A failure inside one frame answers that frame and never stops the frames behind it.
					try {
						let line: string | undefined;
						try {
							line = UTF8.decode(bytes);
						} catch {
							reply(sock, { id: null, ok: false, code: "bad_json", message: "frame is not valid UTF-8" });
						}
						if (line !== undefined && line.trim()) handleLine(client, line);
					} catch (error) {
						logEvent("frame_error", failure(error));
						reply(sock, { id: null, ok: false, code: "internal", message: "omp failed while handling the frame" });
					}
					if (client.dead) return;
				}
				if (client.buffer.length > MAX_FRAME_BYTES) {
					client.dead = true;
					client.buffer = Buffer.alloc(0);
					clearFrame(client);
					sayGoodbye(sock, { id: null, ok: false, code: "too_large", message: `frame exceeds ${MAX_FRAME_BYTES} bytes` });
				} else if (client.buffer.length === 0) {
					clearFrame(client);
				} else if (finishedFrame || client.frame === undefined) {
					armFrame(client); // a new, incomplete frame has started: it must finish in time
				}
			}),
		);
		sock.on(
			"close",
			guarded("close", () => {
				client.dead = true;
				clearIdle(client);
				clearFrame(client);
				clients.delete(client);
			}),
		);
	};

	/** A start that did not complete: close whatever exists, record why, tell the user once. */
	const failStart = (kind: string, what: string, error: unknown): void => {
		logEvent(kind, failure(error));
		lastFailure = `${what} (${userError(error)})`;
		closeAll(kind);
		notifyOnce(`could not publish control: ${lastFailure}`, kind);
	};

	const start = (reason: string): void => {
		if (server || stopped || !ctx) return;
		try {
			const st = ensurePrivateDir(CONTROL_DIR, false);
			dirOk = true;
			dirIdentity = `${st.dev}:${st.ino}`;
			removeStaleEntries();
			nonce = crypto.randomBytes(16).toString("hex");
			socketPath = path.join(CONTROL_DIR, `${crypto.randomBytes(8).toString("hex")}.sock`);
			registryPath = path.join(CONTROL_DIR, `${nonce}.json`);
			if (Buffer.byteLength(socketPath) > maxSocketPathBytes()) {
				throw new ControlError("unsafe_path", `the control directory path is too long for a Unix socket (max ${maxSocketPathBytes() - 22} bytes)`);
			}
			const srv = net.createServer(guarded("connection", onConnection));
			srv.maxConnections = MAX_CONNECTIONS;
			srv.on(
				"error",
				guarded("server_error", (error: Error) => {
					logEvent("server_error", failure(error));
					if (server === srv) closeAll("server_error");
				}),
			);
			server = srv;
			const boundPath = socketPath;
			srv.listen(
				boundPath,
				guarded("listen", () => {
					if (server !== srv) return;
					// Binding, securing the socket and publishing the registry are one step: if any part fails, nothing of
					// it is left running.
					try {
						fs.chmodSync(boundPath, 0o600);
					} catch (error) {
						return failStart("socket_mode_error", "the socket could not be secured", error);
					}
					listening = true;
					try {
						writeRegistry(); // published only after the bind succeeded
					} catch (error) {
						return failStart("registry_publish_error", "the registry could not be published", error);
					}
					lastFailure = "";
					everActive = true;
					logEvent("listening", { reason, sessionId: currentSessionId(), generation });
					if (liveDisableProblem) {
						notifyOnce(
							`live disabling through disabledExtensions is unavailable (${liveDisableProblem}); use /session-control off or the off file to close control without a restart`,
							"live_disable",
						);
					}
				}),
			);
		} catch (error) {
			logEvent("start_error", failure(error));
			lastFailure = `could not start (${userError(error)})`;
			notifyOnce(lastFailure);
			server = undefined;
			socketPath = "";
			registryPath = "";
			listening = false;
		}
	};

	/** What is wrong with the running listener, or "": the directory was replaced, or its socket or registry vanished. */
	const listenerProblem = (): string => {
		if (!server) return "";
		let st: fs.Stats;
		try {
			st = fs.lstatSync(CONTROL_DIR);
		} catch {
			return "control_dir_gone";
		}
		if (`${st.dev}:${st.ino}` !== dirIdentity) return "control_dir_replaced";
		if (!listening) return "";
		try {
			if (!fs.lstatSync(socketPath).isSocket()) return "socket_replaced";
		} catch {
			return "socket_gone";
		}
		return existsNoFollow(registryPath) ? "" : "registry_gone";
	};

	/**
	 * Brings the listener in line with the files on disk. Idempotent; called on every relevant change.
	 *
	 * File-system watches can miss a change: one that happens while a watch is still being armed is lost (macOS arms
	 * asynchronously), and events can be dropped under load. So this never relies on events alone:
	 *  - a watcher is armed *before* the state is read, so a change made after the arming is either seen by the read or
	 *    delivered as an event (watch, then recheck);
	 *  - while there is something to wait for (the directory exists, or was in use), it runs again every RECHECK_MS through
	 *    ctx.setTimeout (isolated by omp, unref'd, cleared at shutdown), one timer at a time.
	 */
	const reconcile = (reason: string): void => {
		if (!ctx || stopped) return;
		let dir = inspectDir(CONTROL_DIR);
		if (dir.kind === "ok") {
			unwatchParent();
			watchDir(); // a no-op when the watch is already armed
		}
		const a = activation();
		if (a.state === "ready") {
			const problem = listenerProblem();
			if (problem) {
				// The directory under us was removed or replaced (or our files were): start again from scratch, which
				// also re-arms the watch on whatever is at the path now.
				closeAll(problem);
				unwatchDir();
				return reconcile(`recovered_${problem}`);
			}
			start(reason);
			syncRecheck(true);
			return;
		}
		if (server || clients.size) closeAll(a.state === "off" ? "off" : a.state);
		if (a.state === "unsafe") notifyOnce(a.reason);
		dir = inspectDir(CONTROL_DIR);
		if (dir.kind !== "ok") {
			unwatchDir(); // a missing or unsafe directory cannot be watched
			// A directory that was in use and vanished is waited for through its parent, so that putting it back
			// brings control back without a restart. Arm, then look again: it may have come back meanwhile.
			if (dir.kind === "missing" && everActive && watchParent() && inspectDir(CONTROL_DIR).kind !== "missing") {
				return reconcile(`${reason}_recheck`);
			}
		}
		syncRecheck(dir.kind !== "missing" || everActive);
	};

	/** Keeps exactly one periodic recheck pending while `want`, and none otherwise. */
	const syncRecheck = (want: boolean): void => {
		if (!want || stopped || !ctx) {
			if (recheckTimer !== undefined) {
				try {
					ctx?.clearTimer(recheckTimer);
				} catch {}
				recheckTimer = undefined;
			}
			return;
		}
		if (recheckTimer !== undefined) return;
		recheckTimer = ctx.setTimeout(() => {
			recheckTimer = undefined;
			try {
				reconcile("periodic");
			} catch (error) {
				logEvent("recheck_error", failure(error));
				syncRecheck(true); // a failed look must not end the watching
			}
		}, RECHECK_MS);
	};

	const unwatchParent = (): void => {
		try {
			parentWatcher?.close();
		} catch {}
		parentWatcher = undefined;
	};

	/** Arms the watch on the directory's parent; true when it was armed just now. */
	const watchParent = (): boolean => {
		if (parentWatcher || !SUPPORTED || !CONTROL_DIR) return false;
		try {
			const base = path.basename(CONTROL_DIR);
			// Only the appearance of the directory's name matters; reconcile re-validates everything about it.
			parentWatcher = fs.watch(
				path.dirname(CONTROL_DIR),
				{ persistent: false },
				guarded("parent_watch", (_kind: string, filename: string | null) => {
					if (!filename || filename === base) reconcile("parent_watch");
				}),
			);
			parentWatcher.on("error", guarded("parent_watch_error", () => unwatchParent()));
			return true;
		} catch (error) {
			logEvent("watch_error", failure(error));
			return false;
		}
	};

	const unwatchDir = (): void => {
		try {
			dirWatcher?.close();
		} catch {}
		dirWatcher = undefined;
	};

	const watchDir = (): void => {
		if (dirWatcher || !SUPPORTED || !CONTROL_DIR || inspectDir(CONTROL_DIR).kind !== "ok") return;
		try {
			// Registry writes, sockets and the log in the same directory must not loop: only the marker files matter.
			dirWatcher = fs.watch(
				CONTROL_DIR,
				{ persistent: false },
				guarded("watch", (_kind: string, filename: string | null) => {
					// Other sessions' registries and sockets, temporary files and the log are noise; our own files are not.
					const ours = filename === path.basename(registryPath) || filename === path.basename(socketPath);
					if (filename && CHURN_FILE.test(filename) && !ours) return;
					reconcile("watch");
				}),
			);
			dirWatcher.on("error", guarded("watch_error", () => unwatchDir()));
		} catch (error) {
			logEvent("watch_error", failure(error));
		}
	};

	// A live `disabledExtensions` edit suspends a global extension but never unloads it, and its open sockets keep
	// answering. Watch the setting ourselves and close on our own id. The registry module is omp-internal, so a missing
	// or changed module (or a renamed `lookup`) only loses this one feature.
	const disabledSetting = await optional(async () => (await loadSettingsRegistry()).lookup("disabledExtensions"));
	if (!disabledSetting) liveDisableProblem = "omp's settings registry is not available";
	const isSelfDisabled = (): boolean => {
		try {
			const value = disabledSetting?.get(pi.pi.settings);
			return Array.isArray(value) && value.includes(EXTENSION_ID);
		} catch {
			return false;
		}
	};
	try {
		disabledSetting?.get(pi.pi.settings); // fails here, visibly, if omp changed the shape of the setting
		unsubscribeDisabled = disabledSetting?.listen(
			pi.pi.settings,
			guarded("disabled_setting", () => {
				const now = isSelfDisabled();
				if (now === selfDisabled) return;
				selfDisabled = now;
				reconcile(now ? "disabledExtensions" : "re_enabled");
			}),
		);
	} catch (error) {
		liveDisableProblem = "the settings subscription failed";
		logEvent("listen_error", failure(error));
	}

	const bind = (c: ExtensionContext, reason: string): boolean => {
		if (!isMainTui(c)) return false;
		ctx = c;
		selfDisabled = isSelfDisabled();
		if (settings.warnings.length > 0 && !CONTROL_DIR) notifyOnce(settings.warnings[0] as string);
		reconcile(reason);
		return true;
	};

	pi.on(
		"session_start",
		guarded("session_start", (_event: unknown, c: ExtensionContext) => {
			bind(c, "session_start");
		}),
	);
	// A switch (/new, /resume, /fork) and a branch both replace the session: the registry must follow.
	const onSessionReplaced = (reason: string) => (_event: unknown, c: ExtensionContext) => {
		if (!bind(c, reason)) return;
		generation += 1;
		writeRegistry();
	};
	pi.on("session_switch", guarded("session_switch", onSessionReplaced("session_switch")));
	pi.on("session_branch", guarded("session_branch", onSessionReplaced("session_branch")));
	pi.on(
		"session_shutdown",
		guarded("session_shutdown", (_event: unknown, c: ExtensionContext) => {
			if (!isMainTui(c)) return;
			stopped = true;
			unwatchDir();
			unwatchParent();
			syncRecheck(false);
			closeAll("session_shutdown");
			try {
				unsubscribeDisabled?.();
			} catch {}
			unsubscribeDisabled = undefined;
		}),
	);

	// -- /session-control ---------------------------------------------------------------------------------------------

	const describe = (): string => {
		const a = activation();
		const lines = [
			`${PLUGIN_NAME} ${PLUGIN_VERSION}, protocol ${PROTOCOL_VERSIONS.join(", ")}`,
			`control directory: ${CONTROL_DIR || "(none)"}`,
			a.state === "ready"
				? server
					? `state: listening, ${clients.size} client(s) connected`
					: lastFailure
						? `state: failed, ${lastFailure}`
						: "state: enabled, not listening in this session"
				: `state: ${a.state} (${a.reason})`,
			liveDisableProblem
				? `live disabling through disabledExtensions: unavailable (${liveDisableProblem}); the off file still works`
				: "live disabling through disabledExtensions: available",
		];
		for (const w of settings.warnings) lines.push(`warning: ${w}`);
		return lines.join("\n");
	};

	const COMMAND_USAGE = "usage: /session-control status | on | off";

	pi.registerCommand("session-control", {
		description: "Show or change session control: status | on | off",
		getArgumentCompletions: prefix =>
			["status", "on", "off"].filter(v => v.startsWith(prefix.trim())).map(v => ({ value: v, label: v })),
		handler: async (args: string, c) => {
			const say = (message: string, type: "info" | "warning" | "error" = "info"): void => {
				try {
					c.ui.notify(message, type);
				} catch {}
			};
			try {
				if (!isMainTui(c)) return say("session-control works in the main interactive session only", "warning");
				ctx = c;
				const sub = args.trim().toLowerCase();
				if (sub === "" || sub === "status") return say(describe());
				if (!SUPPORTED || !CONTROL_DIR) {
					const a = activation();
					return say(`session control is unavailable: ${a.state === "ready" ? "unknown" : a.reason}`, "error");
				}
				if (sub === "on") {
					ensurePrivateDir(CONTROL_DIR, true);
					dirOk = true;
					touchPrivateFile(ENABLED_FILE);
					removeIfPresent(OFF_FILE);
					reconcile("command");
					return say(
						`${describe()}\nOther sessions that were already running pick this up only if they watch the directory; restart them otherwise.`,
					);
				}
				if (sub === "off") {
					if (inspectDir(CONTROL_DIR).kind !== "ok") return say("session control is already off: no control directory");
					ensurePrivateDir(CONTROL_DIR, false);
					dirOk = true;
					touchPrivateFile(OFF_FILE);
					reconcile("command");
					return say(`session control is off for every session that watches ${CONTROL_DIR}. \`/session-control on\` turns it back on.`);
				}
				say(COMMAND_USAGE, "warning");
			} catch (error) {
				say(`session-control: ${userError(error)}`, "error");
			}
		},
	});
}
