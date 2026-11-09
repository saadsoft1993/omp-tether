// A deliberately flawed pairing-store implementation, for testing the conformance suite itself: the suite must fail for
// exactly the checks each flaw breaks. BREAK=<flaw> selects one; without it the adapter is conformant. Standalone: it
// shares no code with src/, so a bug in the plugin cannot hide a bug here.
import { dlopen, FFIType } from "bun:ffi";
import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

const flaw = process.env.BREAK ?? "";
const sha = (s: string): string => createHash("sha256").update(s).digest("hex");
const out = (v: unknown): void => console.log(JSON.stringify(v));
const [command, dir = "", ...rest] = Bun.argv.slice(2);
const uid = process.getuid?.() ?? -1;

function refusal(): string | null {
	let d: fs.Stats;
	try {
		d = flaw === "follow-symlink" ? fs.statSync(dir) : fs.lstatSync(dir);
	} catch {
		return "store-missing";
	}
	if (d.isSymbolicLink()) return "store-symlink";
	if (d.mode & 0o077 && flaw !== "ignore-modes") return "store-open-to-others";
	const f = path.join(dir, "devices.json");
	let st: fs.Stats;
	try {
		st = flaw === "follow-symlink" ? fs.statSync(f) : fs.lstatSync(f);
	} catch {
		return "store-missing";
	}
	if (st.isSymbolicLink()) return "store-symlink";
	if (!st.isFile()) return "store-not-regular";
	if (st.mode & 0o077 && flaw !== "ignore-modes") return "store-open-to-others";
	if (st.uid !== uid) return "store-foreign-owner";
	return null;
}

function check(name: string, header: string): void {
	const refused = refusal();
	if (flaw === "chmod-on-read") {
		fs.chmodSync(dir, 0o700);
		for (const f of fs.readdirSync(dir)) fs.chmodSync(path.join(dir, f), 0o600);
	}
	if (refused) return out({ paired: false, device: null, refused });
	let devices: unknown;
	try {
		devices = JSON.parse(fs.readFileSync(path.join(dir, "devices.json"), "utf8"));
	} catch {
		return out({ paired: false, device: null, refused: "store-invalid-json" });
	}
	if (!devices || typeof devices !== "object" || (Array.isArray(devices) && flaw !== "array-ok")) return out({ paired: false, device: null, refused: "store-not-object" });
	const keys = new Set(Object.keys(devices));
	const values = header.split(";").map(p => p.trim()).filter(p => p.startsWith(`${name}=`)).map(p => p.slice(name.length + 1));
	for (const v of flaw === "first-cookie-only" ? values.slice(0, 1) : values) {
		if (v.length > 512 && flaw !== "no-length-cap") continue;
		const h = sha(v);
		if (keys.has(h) || (flaw === "raw-keys" && (keys.has(v) || keys.has(h.toUpperCase())))) return out({ paired: true, device: h, refused: null });
	}
	out({ paired: false, device: null, refused: null });
}

const mode = flaw === "loose-modes" ? 0o644 : 0o600;

interface Entry {
	label?: string;
	paired_at?: number;
}

function load(name: string): Record<string, unknown> {
	try {
		const raw = fs.readFileSync(path.join(dir, name), "utf8");
		if (raw === "" && flaw === "empty-ok") return {};
		return JSON.parse(raw);
	} catch (err) {
		if ((err as NodeJS.ErrnoException).code === "ENOENT" || flaw === "overwrite-corrupt") return {};
		throw new Error(`${name} is not valid JSON`);
	}
}

function save(name: string, data: unknown): void {
	const tmp = path.join(dir, `.${name}.${randomBytes(3).toString("hex")}`);
	fs.writeFileSync(tmp, JSON.stringify(data), { mode });
	fs.chmodSync(tmp, mode);
	fs.renameSync(tmp, path.join(dir, name));
	if (flaw === "temp-leftover") fs.writeFileSync(path.join(dir, `.${name}.tmp`), "x");
}

function lock(): void {
	if (flaw === "no-lock") return;
	const { symbols } = dlopen(process.platform === "darwin" ? "libSystem.B.dylib" : "libc.so.6", { flock: { args: [FFIType.i32, FFIType.i32], returns: FFIType.i32 } });
	const fd = fs.openSync(path.join(dir, ".lock"), "a+", 0o600);
	if (flaw !== "lock-loose-ok" && fs.fstatSync(fd).mode & 0o077) throw new Error("lock file is open to others");
	symbols.flock(fd, 2); // blocking, exclusive; released when the process exits
}

function ensure(): void {
	fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
	if (flaw !== "follow-symlink" && fs.lstatSync(dir).isSymbolicLink()) throw new Error("symlink");
	if (flaw !== "no-tighten") fs.chmodSync(dir, 0o700);
	else if (fs.statSync(dir).mode & 0o077) throw new Error("open directory");
}

try {
	if (command === "check") check(rest[0] ?? "", rest[1] ?? "");
	else if (command === "create-token") {
		ensure();
		lock();
		const token = randomBytes(flaw === "short-token" ? 8 : 32).toString("base64url");
		const pending = load("pending.json");
		pending[flaw === "plain-token" ? token : sha(token)] = Date.now() / 1000 + 300;
		save("pending.json", pending);
		out({ token });
	} else if (command === "redeem") {
		ensure();
		lock();
		const [token = "", label = ""] = rest;
		const pending = load("pending.json");
		const devices = load("devices.json");
		if (flaw === "no-lock") await Bun.sleep(150); // widens the read-modify-write window
		const expiry = pending[sha(token)] as number | undefined;
		if (flaw !== "reusable-token") delete pending[sha(token)];
		if (expiry === undefined || (expiry <= Date.now() / 1000 && flaw !== "accept-expired")) {
			out({ session: null });
		} else {
			const session = randomBytes(32).toString("base64url");
			devices[sha(session)] = { label: flaw === "no-label-clean" ? label : label.replace(/[\u0000-\u001f]/g, " ").slice(0, 80), paired_at: Math.floor(Date.now() / 1000) };
			save("devices.json", devices);
			save("pending.json", pending);
			out({ session });
		}
	} else if (command === "devices") {
		out({ devices: Object.entries(load("devices.json")).map(([k, v]) => ({ id: k.slice(0, 8), label: (v as Entry).label, pairedAt: (v as Entry).paired_at ?? 0 })).sort((a, b) => a.pairedAt - b.pairedAt) });
	} else if (command === "revoke") {
		ensure();
		lock();
		const prefix = rest[0] ?? "";
		const devices = load("devices.json");
		const gone = Object.keys(devices).filter(k => prefix === "all" || k.startsWith(prefix));
		if (prefix !== "all" && (prefix.length < 4 && flaw !== "short-prefix")) throw new Error("prefix too short");
		if (prefix !== "all" && gone.length > 1 && flaw !== "ambiguous-ok") throw new Error("ambiguous");
		for (const k of gone) delete devices[k];
		save("devices.json", devices);
		out({ revoked: gone.length });
	}
} catch (err) {
	out({ error: String(err) });
	process.exit(1);
}
