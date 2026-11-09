import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";
import { Client } from "./support/client";
import { FakeHost } from "./support/fake-host";

let host: FakeHost;
let c: Client;
let nonce: string;
let sessionId: string;
let generation: number;

beforeEach(async () => {
	host = await FakeHost.start({ enable: true, env: { OMP_SESSION_CONTROL_IDLE_MS: "600" } });
	await host.fire("session_start");
	({ client: c, nonce, sessionId, generation } = await Client.toHost(host));
});
afterEach(async () => {
	c.close();
	await host.dispose();
});

const frame = (id: string | number, op: string, args?: unknown) => ({ v: 1, id, nonce, op, sessionId, generation, ...(args === undefined ? {} : { args }) });

describe("operations", () => {
	test("snapshot reports the live state and the protocol it speaks", async () => {
		host.name = "named";
		host.busy = true;
		const r = await c.request({ v: 1, id: 1, nonce, op: "snapshot" });
		expect(r).toMatchObject({
			v: 1,
			id: 1,
			ok: true,
			result: {
				sessionId,
				generation,
				name: "named",
				busy: true,
				pid: process.pid,
				protocol: 1,
				protocols: [1],
				ops: ["snapshot", "abort", "rename"],
				plugin: { name: "omp-session-control", version: "0.1.0" },
			},
		});
	});

	test("abort interrupts a busy session once and reports an idle one as not aborted", async () => {
		host.busy = true;
		expect((await c.request(frame(1, "abort"))).result).toEqual({ aborted: true });
		expect(host.aborts).toBe(1);
		expect((await c.request(frame(2, "abort"))).result).toEqual({ aborted: false });
		expect(host.aborts).toBe(1);
	});

	test("rename passes the cleaned name to omp and reports before and after", async () => {
		host.name = "old";
		const r = await c.request(frame(1, "rename", { name: "  new   name " }));
		expect(r.result).toEqual({ before: "old", name: "new name" });
		expect(host.name).toBe("new name");
	});

	test("rename is refused when omp keeps the old name", async () => {
		host.failSetName = "keep";
		host.name = "old";
		const r = await c.request(frame(1, "rename", { name: "new" }));
		expect(r).toMatchObject({ ok: false, code: "not_renamed", detail: { before: "old", after: "old" } });
	});

	test("an omp failure becomes an internal error and is logged; the connection and the session go on", async () => {
		host.failSetName = "throw";
		const r = await c.request(frame(1, "rename", { name: "x" }));
		expect(r).toMatchObject({ ok: false, code: "internal" });
		host.failSetName = "reject";
		expect((await c.request(frame(2, "rename", { name: "x" }))).code).toBe("internal");
		host.failSetName = undefined;
		expect((await c.request(frame(3, "rename", { name: "fine" }))).ok).toBe(true);
		expect(fs.readFileSync(path.join(host.dir, "events.log"), "utf8")).toContain('"kind":"op_error"');
		expect(host.uncaught).toEqual([]);
	});

	test("mutations run one at a time and in order, even when sent in one write", async () => {
		const order: string[] = [];
		const setName = host.pi.setSessionName as (n: string) => Promise<void>;
		host.pi.setSessionName = async (n: string) => {
			order.push(`start ${n}`);
			await new Promise(r => setTimeout(r, 40));
			await setName(n);
			order.push(`end ${n}`);
		};
		c.send([1, 2, 3].map(i => JSON.stringify(frame(`m${i}`, "rename", { name: `n${i}` }))).join("\n"));
		const ids = [(await c.next())?.id, (await c.next())?.id, (await c.next())?.id];
		expect(ids).toEqual(["m1", "m2", "m3"]);
		expect(order).toEqual(["start n1", "end n1", "start n2", "end n2", "start n3", "end n3"]);
	});

	test("more than 16 requests in flight are shed with overloaded", async () => {
		host.pi.setSessionName = () => new Promise(r => setTimeout(r, 300));
		for (let i = 0; i < 20; i++) c.send(frame(`o${i}`, "rename", { name: `n${i}` }));
		const codes = new Map<unknown, unknown>();
		for (let i = 0; i < 20; i++) {
			const r = await c.next(3000);
			if (r) codes.set(r.id, r.code ?? "ok");
		}
		expect([...codes.values()].filter(v => v === "overloaded").length).toBe(4);
	});

	test("a request is refused with disabled when the off file appears between accept and execution", async () => {
		fs.writeFileSync(path.join(host.dir, "off"), "");
		const r = await c.request({ v: 1, id: 1, nonce, op: "snapshot" });
		expect(r.code === "disabled" || r.code === "closing").toBe(true);
	});
});

describe("connections", () => {
	test("a frame split across many writes is reassembled", async () => {
		const text = `${JSON.stringify({ v: 1, id: "split", nonce, op: "snapshot" })}\n`;
		for (const part of text.match(/.{1,7}/gs) ?? []) {
			c.sock.write(part);
			await new Promise(r => setTimeout(r, 5));
		}
		expect((await c.next())?.id).toBe("split");
	});

	test("blank lines are ignored and a Windows line ending still parses", async () => {
		c.sock.write(`\n  \n${JSON.stringify({ v: 1, id: "crlf", nonce, op: "snapshot" })}\r\n`);
		expect((await c.next())?.id).toBe("crlf");
	});

	test("an idle connection is closed after the timeout, and activity resets it", async () => {
		const started = Date.now();
		for (let i = 0; i < 3; i++) {
			await new Promise(r => setTimeout(r, 300));
			expect((await c.request({ v: 1, id: i, nonce, op: "snapshot" })).ok).toBe(true);
		}
		expect(c.closed).toBe(false);
		expect(Date.now() - started).toBeGreaterThan(850); // longer than one idle period: activity kept it open
		await host.waitFor(() => c.closed, "the idle close", 3000);
	});

	test("a fifth client gets too_many_clients, and a freed slot is reusable", async () => {
		const more = [await Client.connect(host.registry()?.data.socket as string)];
		for (let i = 0; i < 2; i++) more.push(await Client.connect(host.registry()?.data.socket as string));
		for (const m of more) expect((await m.request({ v: 1, id: 1, nonce, op: "snapshot" })).ok).toBe(true);
		const fifth = await Client.connect(host.registry()?.data.socket as string);
		expect((await fifth.next())?.code).toBe("too_many_clients");
		more[0]?.close();
		await new Promise(r => setTimeout(r, 150));
		const again = await Client.connect(host.registry()?.data.socket as string);
		expect((await again.request({ v: 1, id: 2, nonce, op: "snapshot" })).ok).toBe(true);
		for (const m of [...more, fifth, again]) m.close();
	});

	test("a client that never reads its replies is cut off instead of growing a buffer", async () => {
		const sock = net.createConnection(host.registry()?.data.socket as string);
		sock.on("error", () => {}); // the extension hangs up on us: EPIPE here is the expected outcome
		await new Promise(r => sock.once("connect", r));
		sock.pause();
		const line = `${JSON.stringify({ v: 1, id: "x".repeat(100), nonce, op: "snapshot" })}\n`;
		for (let i = 0; i < 4000 && !sock.destroyed; i++) {
			sock.write(line.repeat(20));
			await new Promise(r => setTimeout(r, 1));
		}
		expect(sock.destroyed).toBe(true); // the extension closed the connection: it did not keep buffering for us
		sock.destroy();
		const probe = await Client.connect(host.registry()?.data.socket as string);
		expect((await probe.request({ v: 1, id: 1, nonce, op: "snapshot" })).ok).toBe(true);
		probe.close();
		expect(host.uncaught).toEqual([]);
	});
});
