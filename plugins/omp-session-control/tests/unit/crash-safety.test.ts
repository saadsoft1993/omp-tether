// A throw inside a callback that omp does not wrap (a socket handler, an fs watcher, a detached promise) surfaces as an
// uncaughtException, which omp treats as fatal for the whole session. These tests audit the source for such callbacks
// and inject failures to prove the extension contains them.
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { Client } from "./support/client";
import { FakeHost } from "./support/fake-host";

const SOURCE = path.resolve(import.meta.dir, "../../src/omp-session-control.ts");

/** Source with comments and string literals blanked, so a regex cannot match prose. Offsets are preserved. */
function code(text: string): string {
	return text
		.replace(/\/\*[\s\S]*?\*\//g, m => m.replace(/[^\n]/g, " "))
		.replace(/\/\/[^\n]*/g, m => " ".repeat(m.length))
		.replace(/`(?:[^`\\]|\\.)*`/g, m => m[0] + " ".repeat(m.length - 2) + m[0])
		.replace(/"(?:[^"\\\n]|\\.)*"/g, m => `"${" ".repeat(m.length - 2)}"`);
}

/** The text of the call whose opening parenthesis is at `open` (balanced), from the original source. */
function callText(original: string, stripped: string, open: number): string {
	let depth = 0;
	for (let i = open; i < stripped.length; i++) {
		if (stripped[i] === "(") depth++;
		else if (stripped[i] === ")" && --depth === 0) return original.slice(open, i + 1);
	}
	return original.slice(open);
}

/** Every way the source hands a callback to something that does not contain its throws. Returns what it found. */
export function auditSource(original: string): string[] {
	const stripped = code(original);
	const problems: string[] = [];
	const snippet = (index: number) => original.slice(index, index + 60).replace(/\s+/g, " ");

	const raw =
		/(^|[^.\w$])(setTimeout|setInterval|setImmediate|queueMicrotask)\s*\(|process\s*\.\s*(nextTick|on|once|addListener)\s*\(|\b\w+\s*\.\s*setTimeout\s*\(/g;
	for (const m of stripped.matchAll(raw)) {
		// `ctx.setTimeout` / `c.setTimeout` are omp's isolated timers; any other receiver is a raw timer.
		if (/\b(ctx|c)\s*\.\s*setTimeout\s*\(/.test(m[0])) continue;
		problems.push(`raw timer or hook: ${snippet(m.index)}`);
	}

	// Registrations of callbacks on any receiver: events, servers, watchers, settings listeners.
	const sites = /\b[\w.?]+\s*\.\s*(?:on|once|listen|watch)\s*\(|\bnet\.createServer\s*\(|\bfs\.watch\s*\(/g;
	for (const m of stripped.matchAll(sites)) {
		const text = callText(original, stripped, m.index + m[0].length - 1);
		if (!text.includes("guarded(")) problems.push(`unwrapped callback registration: ${snippet(m.index)}`);
	}

	// write/end/close calls that take a callback.
	for (const m of stripped.matchAll(/\.\s*(?:write|end|close|destroy)\s*\(/g)) {
		const text = callText(stripped, stripped, m.index + m[0].length - 1);
		if (/=>|function\b/.test(text) && !callText(original, stripped, m.index + m[0].length - 1).includes("guarded(")) {
			problems.push(`unwrapped completion callback: ${snippet(m.index)}`);
		}
	}

	// Callback-style fs and any promise chaining outside guardedAsync.
	for (const m of stripped.matchAll(/\bfs\s*\.\s*(?!constants\b|watch\b|promises\b)(\w+)\s*\(/g)) {
		if (!(m[1] as string).endsWith("Sync")) problems.push(`callback-style fs call: ${snippet(m.index)}`);
	}
	for (const m of stripped.matchAll(/\.\s*then\s*\(/g)) {
		if (!callText(original, stripped, m.index + m[0].length - 1).includes("guardedAsync(")) problems.push(`unguarded promise chain: ${snippet(m.index)}`);
	}
	if (/\bvoid\s+(?!guardedAsync)\w+\(/.test(stripped)) problems.push("floating promise");
	return problems;
}

describe("raw-callback audit of src/omp-session-control.ts", () => {
	const original = fs.readFileSync(SOURCE, "utf8");
	const stripped = code(original);

	test("the source has no unwrapped callback, raw timer, floating promise or callback-style fs call", () => {
		expect(auditSource(original)).toEqual([]);
	});

	test("the audit itself catches each class of mistake", () => {
		const bad: Array<[string, string]> = [
			["a raw timer", "function f() { setTimeout(() => {}, 5); }"],
			["a socket timeout", "sock.setTimeout(5, () => sock.destroy());"],
			["an unwrapped handler on any receiver", "connection.on('data', () => {});"],
			["an unwrapped end callback", "client.sock.end(frame, () => client.sock.destroy());"],
			["an async fs callback", "fs.readFile(p, () => {});"],
			["an unguarded chain", "p.then(() => {});"],
			["a process hook", "process.on('exit', () => {});"],
		];
		for (const [label, snippet] of bad) expect({ label, found: auditSource(snippet).length > 0 }).toEqual({ label, found: true });
		expect(auditSource("sock.on('data', guarded('d', () => {}));\nctx.setTimeout(() => {}, 5);")).toEqual([]);
	});

	test("timers go through ctx.setTimeout, which omp isolates and clears at shutdown", () => {
		expect(stripped).toMatch(/ctx\.setTimeout\(/);
		expect(stripped).not.toMatch(/\bsetInterval\b/);
	});

	test("only node:crypto, node:fs, node:net, node:os, node:path and omp's types are imported, and nothing is spawned", () => {
		const imports = [...original.matchAll(/from "([^"]+)"/g)].map(m => m[1]);
		expect(imports.sort()).toEqual(["@oh-my-pi/pi-coding-agent", "node:crypto", "node:fs", "node:net", "node:os", "node:path"]);
		expect(stripped).not.toMatch(/child_process|\bspawn\b|\bexec(?:Sync)?\b|\beval\b|new Function|Bun\.\$/);
	});
});

const hosts: FakeHost[] = [];
afterEach(async () => {
	for (const h of hosts.splice(0)) await h.dispose();
});
async function enabledHost(): Promise<FakeHost> {
	const h = await FakeHost.start({ enable: true });
	hosts.push(h);
	return h;
}

describe("injected failures", () => {
	test("omp throwing while the listener comes up (registry write) is logged, not fatal", async () => {
		const h = await enabledHost();
		h.failSessionId = true;
		await h.fire("session_start");
		await new Promise(r => setTimeout(r, 200));
		expect(h.uncaught).toEqual([]);
		h.failSessionId = false;
		// publishing is part of starting: a failure closes the listener and is logged as a category
		expect(fs.readFileSync(path.join(h.dir, "events.log"), "utf8")).toContain('"kind":"registry_publish_error"');
	});

	test("omp throwing during an op is an internal error reply; the next request works", async () => {
		const h = await enabledHost();
		await h.fire("session_start");
		const { client, nonce, sessionId, generation } = await Client.toHost(h);
		h.failIsIdle = true;
		expect((await client.request({ v: 1, id: 1, nonce, op: "snapshot" })).code).toBe("internal");
		expect((await client.request({ v: 1, id: 2, nonce, op: "abort", sessionId, generation })).code).toBe("internal");
		h.failIsIdle = false;
		h.busy = true;
		h.failAbort = true;
		expect((await client.request({ v: 1, id: 3, nonce, op: "abort", sessionId, generation })).code).toBe("internal");
		h.failAbort = false;
		expect((await client.request({ v: 1, id: 4, nonce, op: "abort", sessionId, generation })).result).toEqual({ aborted: true });
		client.close();
		expect(h.uncaught).toEqual([]);
	});

	test("event handlers given a broken context swallow the error", async () => {
		const h = await enabledHost();
		for (const event of ["session_start", "session_switch", "session_shutdown"]) {
			for (const handler of h.handlers.get(event) ?? []) {
				await handler({}, {} as never);
				await handler({}, { mode: "tui" } as never);
				await handler({}, null as never);
			}
		}
		expect(h.uncaught).toEqual([]);
	});

	test("a throwing ui.notify does not break the failure path that calls it", async () => {
		const h = await FakeHost.start({ env: { OMP_SESSION_CONTROL_DIR: "relative/dir" } });
		hosts.push(h);
		h.ctx.ui.notify = () => {
			throw new Error("injected notify failure");
		};
		await h.fire("session_start");
		expect(h.uncaught).toEqual([]);
	});

	test("an unwritable control directory at session_switch is contained", async () => {
		const h = await enabledHost();
		await h.fire("session_start");
		await h.waitFor(() => h.registry(), "the registry");
		fs.chmodSync(h.dir, 0o500);
		try {
			await h.fire("session_switch"); // writeRegistry cannot create its temp file
		} finally {
			fs.chmodSync(h.dir, 0o700);
		}
		expect(h.uncaught).toEqual([]);
	});

	test("hundreds of clients that connect, send half a frame or garbage and vanish do not hurt it", async () => {
		const h = await enabledHost();
		await h.fire("session_start");
		const reg = await h.waitFor(() => h.registry(), "the registry");
		const socketPath = reg.data.socket as string;
		for (let round = 0; round < 40; round++) {
			const batch = await Promise.all(Array.from({ length: 4 }, () => Client.connect(socketPath).catch(() => null)));
			batch.forEach((c, i) => {
				if (!c) return;
				c.sock.write([`{"v":1,"id":"x","nonce":"`, Buffer.from([0xff, 0xfe, 0x00, 0x0a]), "[".repeat(5000), "\n\n\n", `{"v":1,"id":1`][i % 4] as string | Buffer);
				if (i % 2) c.sock.destroy();
				else c.sock.end();
			});
			await new Promise(r => setTimeout(r, 5));
		}
		await new Promise(r => setTimeout(r, 200));
		const probe = await Client.connect(socketPath);
		expect((await probe.request({ v: 1, id: 1, nonce: reg.data.nonce, op: "snapshot" })).ok).toBe(true);
		probe.close();
		expect(h.uncaught).toEqual([]);
		expect(h.timerErrors).toEqual([]);
	});

	test("a deeply nested JSON frame is rejected, not fatal", async () => {
		const h = await enabledHost();
		await h.fire("session_start");
		const { client, nonce } = await Client.toHost(h);
		const deep = `${"[".repeat(30_000)}${"]".repeat(30_000)}`;
		const r = await client.request(`{"v":1,"id":"d","nonce":"${nonce}","op":"snapshot","args":${deep}}`.slice(0, 60_000));
		expect(r.ok).toBe(false);
		expect((await client.request({ v: 1, id: 2, nonce, op: "snapshot" })).ok).toBe(true);
		client.close();
	});
});

describe("process-level crash test", () => {
	test("a child process hosting the extension with failing omp calls and hostile clients exits 0 with no fatal error", async () => {
		const child = Bun.spawn([process.execPath, path.join(import.meta.dir, "support/crash-child.ts")], { stdout: "pipe", stderr: "pipe" });
		const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
		expect({ code, err }).toEqual({ code: 0, err: "" });
		expect(out.trim()).toBe("survived");
	});
});
