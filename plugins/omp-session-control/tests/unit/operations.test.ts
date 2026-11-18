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
				ops: ["snapshot", "abort", "rename", "send-message", "ask-review", "ask-answer"],
				plugin: { name: "omp-session-control", version: "0.4.0" },
			},
		});
	});

	test("messages settle on admission, queue while busy, and do not block stop", async () => {
		expect((await c.request(frame(1, "send-message", { text: "first", messageId: "first" }))).result).toEqual({ accepted: true, delivery: "started" });
		expect((await c.request(frame(2, "send-message", { text: "second", messageId: "second" }))).result).toEqual({ accepted: true, delivery: "queued" });
		expect((await c.request(frame(3, "send-message", { text: "first", messageId: "first" }))).result).toEqual({ accepted: true, delivery: "started", duplicate: true });
		expect((await c.request(frame(4, "abort"))).result).toEqual({ aborted: true });
		expect(host.messages).toEqual(["first", "second"]);
	});

	test("ask answers are pinned to the currently open question", async () => {
		await host.emit("tool_execution_start", { toolName: "ask", toolCallId: "ask-1", args: { html: "<form></form>" } });
		expect((await c.request(frame(1, "ask-review"))).result).toMatchObject({ open: true, askId: "ask-1", html: "<form></form>" });
		expect((await c.request(frame(2, "ask-answer", { askId: "stale", text: "answer", messageId: "m" }))).code).toBe("changed");
		expect(host.messages).toEqual([]);
		expect((await c.request(frame(3, "ask-answer", { askId: "ask-1", text: "answer", messageId: "m" }))).ok).toBe(true);
		await host.emit("tool_execution_end", { toolName: "ask", toolCallId: "ask-1" });
		expect((await c.request(frame(4, "ask-review"))).result).toEqual({ open: false });
		expect((await c.request(frame(5, "ask-answer", { askId: "ask-1", text: "answer", messageId: "m" }))).result).toEqual({ accepted: true, delivery: "started", duplicate: true });
		expect((await c.request(frame(6, "ask-answer", { askId: "different", text: "answer", messageId: "m" }))).code).toBe("invalid_args");
		expect((await c.request(frame(7, "send-message", { text: "answer", messageId: "m" }))).code).toBe("invalid_args");
		expect(host.messages).toEqual(["answer"]);
	});

	test("message IDs expire and the oldest of 257 is evicted", async () => {
		host.pi.sendUserMessage = async (text: string) => { host.messages.push(text); };
		const realNow = Date.now;
		let now = 1_000_000;
		Date.now = () => now;
		try {
			for (let i = 0; i < 257; i++) {
				expect((await c.request(frame(i, "send-message", { text: `m${i}`, messageId: `m${i}` }))).ok).toBe(true);
				now++;
			}
			expect((await c.request(frame("old", "send-message", { text: "again", messageId: "m0" }))).result).toEqual({ accepted: true, delivery: "started" });
			now += 15 * 60_000;
			expect((await c.request(frame("ttl", "send-message", { text: "expired", messageId: "m256" }))).result).toEqual({ accepted: true, delivery: "started" });
			expect(host.messages.length).toBe(259);
		} finally {
			Date.now = realNow;
		}
	});

	test("a session switch clears message IDs", async () => {
		host.pi.sendUserMessage = async (text: string) => { host.messages.push(text); };
		await c.request(frame(1, "send-message", { text: "first", messageId: "same" }));
		host.sessionId = "01a1b65b-2222-7000-8000-000000000002";
		await host.fire("session_switch");
		const reg = host.registry()!.data;
		const reply = await c.request({ ...frame(2, "send-message", { text: "second", messageId: "same" }), sessionId: host.sessionId, generation: reg.generation });
		expect(reply.result).toEqual({ accepted: true, delivery: "started" });
		expect(host.messages).toEqual(["first", "second"]);
	});

	test("different plugin instances have independent message IDs", async () => {
		host.pi.sendUserMessage = async (text: string) => { host.messages.push(text); };
		const other = await FakeHost.start({ enable: true });
		other.pi.sendUserMessage = async (text: string) => { other.messages.push(text); };
		await other.fire("session_start");
		const peer = await Client.toHost(other);
		try {
			await c.request(frame(1, "send-message", { text: "one", messageId: "same" }));
			const response = await peer.client.request({ v: 1, id: 2, nonce: peer.nonce, sessionId: peer.sessionId, generation: peer.generation, op: "send-message", args: { text: "two", messageId: "same" } });
			expect(response.result).toEqual({ accepted: true, delivery: "started" });
			expect(other.messages).toEqual(["two"]);
		} finally {
			peer.client.close();
			await other.dispose();
		}
	});

	test("oversized ask payloads expose no contents", async () => {
		await host.emit("tool_execution_start", { toolName: "ask", toolCallId: "large", args: { html: "é".repeat(17000) } });
		expect((await c.request(frame(1, "ask-review"))).result).toEqual({ open: true, askId: "large", tooLarge: true });
		expect(host.registry()?.data.askReview).toMatchObject({ askId: "large" });
	});

	test("legacy questions are returned without HTML and old end events cannot close a newer ask", async () => {
		const questions = [{ id: "colour", question: "Colour?", options: [{ label: "Blue" }] }];
		await host.emit("tool_execution_start", { toolName: "ask", toolCallId: "old", args: { questions } });
		await host.emit("tool_execution_start", { toolName: "ask", toolCallId: "new", args: { questions } });
		await host.emit("tool_execution_end", { toolName: "ask", toolCallId: "old" });
		expect((await c.request(frame(1, "ask-review"))).result).toEqual({ open: true, askId: "new", questions });
	});

	test("a quick send rejection is reported and the id can be retried", async () => {
		host.pi.sendUserMessage = async () => { throw new Error("failed"); };
		expect((await c.request(frame(1, "send-message", { text: "hello", messageId: "retry" }))).ok).toBe(false);
		host.pi.sendUserMessage = async () => {};
		expect((await c.request(frame(2, "send-message", { text: "hello", messageId: "retry" }))).result).toEqual({ accepted: true, delivery: "started" });
	});

	test("a void-returning omp 18.18 host admits messages", async () => {
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); };
		expect((await c.request(frame(1, "send-message", { text: "hello", messageId: "void-host" }))).result).toEqual({ accepted: true, delivery: "started" });
		expect(host.messages).toEqual(["hello"]);
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
