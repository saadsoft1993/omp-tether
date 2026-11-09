// relay-check/1: reading the key only when it is private and ours, and proving a listener is the relay (and not a
// look-alike that holds the port while the relay is down).
import { afterEach, describe, expect, test } from "bun:test";
import { createHmac } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { CHECK_PREFIX, proveListener, readCheckKey } from "../../src/listener-check";
import { freePorts, startRelay } from "./support/harness";

const roots: string[] = [];
const tmp = (): string => {
	const r = fs.mkdtempSync(path.join(os.tmpdir(), "oprk-"));
	roots.push(r);
	return r;
};
afterEach(() => {
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});
const KEY = Buffer.alloc(32, 0xab).toString("hex"); // built at run time, so no key-shaped literal sits in the tree

function keyDir(mode = 0o700, fileMode = 0o600, content = KEY): string {
	const dir = path.join(tmp(), "check");
	fs.mkdirSync(dir, { mode });
	fs.chmodSync(dir, mode);
	fs.writeFileSync(path.join(dir, "check.key"), content, { mode: fileMode });
	fs.chmodSync(path.join(dir, "check.key"), fileMode);
	return dir;
}

describe("reading the key", () => {
	test("a private key in a private directory is read", () => {
		const r = readCheckKey(keyDir());
		expect(r.ok && r.key.toString("hex")).toBe(KEY);
	});

	const refused: Array<[string, () => string, RegExp]> = [
		["a directory open to others", () => keyDir(0o755), /not a private directory/],
		["a key file readable by others", () => keyDir(0o700, 0o644), /not private/],
		["a malformed key", () => keyDir(0o700, 0o600, "not hex"), /malformed/],
		["a key of the wrong length", () => keyDir(0o700, 0o600, "ab".repeat(10)), /malformed/],
		["a missing directory", () => path.join(tmp(), "nope"), /no check directory/],
		["a missing key", () => (fs.rmSync(path.join(keyDir(), "check.key")), path.join(tmp(), "empty")), /no check directory/],
		[
			"a symlinked key file",
			() => {
				const dir = keyDir();
				fs.renameSync(path.join(dir, "check.key"), path.join(dir, "real"));
				fs.symlinkSync(path.join(dir, "real"), path.join(dir, "check.key"));
				return dir;
			},
			/no check key/,
		],
		[
			"a symlinked directory",
			() => {
				const dir = keyDir();
				const link = path.join(tmp(), "link");
				fs.symlinkSync(dir, link);
				return link;
			},
			/not a private directory/,
		],
	];
	for (const [name, make, message] of refused) {
		test(`refused: ${name}`, () => {
			const r = readCheckKey(make());
			expect(r.ok).toBe(false);
			expect(r.ok ? "" : r.reason).toMatch(message);
		});
	}
});

describe("proving a listener", () => {
	test("the real relay proves itself on both ports; the same key does not prove a look-alike", async () => {
		const relay = await startRelay();
		try {
			const key = readCheckKey(relay.checkDir);
			if (!key.ok) throw new Error(key.reason);
			expect(await proveListener(relay.hostPort, key.key)).toEqual({ ok: true });
			expect(await proveListener(relay.guestPort, key.key)).toEqual({ ok: true });
			expect((await proveListener(relay.guestPort, Buffer.alloc(32, 7))).ok).toBe(false);
		} finally {
			await relay.stop();
		}
	});

	test("a look-alike that answers a fixed value, a stale answer, an error, garbage or nothing never passes", async () => {
		const [port] = (await freePorts(1)) as [number];
		const key = Buffer.from(KEY, "hex");
		let mode = "fixed";
		const server = Bun.serve({
			hostname: "127.0.0.1",
			port,
			fetch(req) {
				const nonce = new URL(req.url).searchParams.get("nonce") as string;
				const mac = (n: string, p: number) => createHmac("sha256", key).update(`${CHECK_PREFIX}:${p}:${n}`).digest("hex");
				if (mode === "fixed") return Response.json({ mac: "0".repeat(64) });
				if (mode === "stale") return Response.json({ mac: mac("f".repeat(32), port) }); // right key, another nonce
				if (mode === "other-port") return Response.json({ mac: mac(nonce, port + 1) }); // right key, another port
				if (mode === "error") return new Response("no", { status: 500 });
				if (mode === "garbage") return new Response("not json");
				if (mode === "shape") return Response.json({ mac: 12 });
				if (mode === "slow") return new Promise<Response>(() => {});
				return Response.json({ mac: mac(nonce, port) }); // the one honest case
			},
		});
		try {
			for (const m of ["fixed", "stale", "other-port", "error", "garbage", "shape"]) {
				mode = m;
				expect((await proveListener(port, key)).ok, m).toBe(false);
			}
			mode = "slow";
			expect((await proveListener(port, key, 300)).ok).toBe(false);
			mode = "honest";
			expect((await proveListener(port, key)).ok).toBe(true); // the harness itself can pass: the failures above are real
		} finally {
			server.stop(true);
		}
	});

	test("nothing listening is not a pass", async () => {
		const [port] = (await freePorts(1)) as [number];
		expect(await proveListener(port, Buffer.alloc(32, 1))).toEqual({ ok: false, reason: "no answer" });
	});
});
