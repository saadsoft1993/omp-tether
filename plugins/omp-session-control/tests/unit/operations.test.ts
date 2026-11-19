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
	host.pi.sendUserMessage = async (text: string) => { host.messages.push(text); host.busy = true; };
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
				ops: ["snapshot", "abort", "rename", "send-message", "send-status", "ask-review", "ask-answer"],
				plugin: { name: "omp-session-control", version: "0.4.1" },
			},
		});
	});

	test("messages settle on admission, queue while busy, and do not block stop", async () => {
		expect((await c.request(frame(1, "send-message", { text: "first", messageId: "first" }))).result).toEqual({ state: "admitted" });
		expect((await c.request(frame(2, "send-message", { text: "second", messageId: "second" }))).result).toEqual({ state: "admitted" });
		expect((await c.request(frame(3, "send-message", { text: "first", messageId: "first" }))).result).toEqual({ state: "admitted", duplicate: true });
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
		expect((await c.request(frame(5, "ask-answer", { askId: "ask-1", text: "answer", messageId: "m" }))).result).toEqual({ state: "admitted", duplicate: true });
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
			expect((await c.request(frame("old", "send-message", { text: "again", messageId: "m0" }))).result).toEqual({ state: "admitted" });
			now += 15 * 60_000;
			expect((await c.request(frame("ttl", "send-message", { text: "expired", messageId: "m256" }))).result).toEqual({ state: "admitted" });
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
		expect(reply.result).toEqual({ state: "admitted" });
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
			expect(response.result).toEqual({ state: "admitted" });
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

	test("failed receipts are retained and unknown status never sends", async () => {
		host.pi.sendUserMessage = async () => { throw new Error("failed"); };
		expect((await c.request(frame(1, "send-message", { text: "hello", messageId: "retry" }))).result).toEqual({ state: "failed", reason: "omp did not accept the message" });
		expect((await c.request(frame(2, "send-status", { messageId: "retry" }))).result).toEqual({ state: "failed", reason: "omp did not accept the message" });
		expect((await c.request(frame(3, "send-status", { messageId: "unknown" }))).result).toEqual({ state: "unknown" });
		expect(host.messages).toEqual([]);
	});

	test("void send waits for matching message admission", async () => {
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); };
		const pending = c.request(frame(1, "send-message", { text: "hello", messageId: "void-host" }));
		await host.waitFor(() => host.messages.length === 1, "send started");
		await host.emit("message_start", { message: { role: "user", content: [{ type: "text", text: "hello" }] } });
		expect((await pending).result).toEqual({ state: "admitted" });
	});

	test("pending delivery later fails without resending and does not block abort", async () => {
		const gate = Promise.withResolvers<void>();
		const original = host.ctx.setTimeout;
		host.ctx.setTimeout = (cb, ms) => original(cb, ms === 5000 ? 20 : ms);
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); host.busy = true; return gate.promise; };
		expect((await c.request(frame(1, "send-message", { text: "slow", messageId: "slow" }))).result).toEqual({ state: "pending" });
		expect((await c.request(frame(2, "send-status", { messageId: "slow" }))).result).toEqual({ state: "pending" });
		expect((await c.request(frame(3, "abort"))).result).toEqual({ aborted: true });
		gate.reject(new Error("no credentials"));
		await new Promise(resolve => setTimeout(resolve, 10));
		expect((await c.request(frame(4, "send-message", { text: "slow", messageId: "slow" }))).result).toEqual({ state: "failed", reason: "omp did not accept the message", duplicate: true });
		expect(host.messages).toEqual(["slow"]);
	});

	test("send starts only after an earlier mutation settles", async () => {
		const gate = Promise.withResolvers<void>();
		const order: string[] = [];
		host.pi.setSessionName = async () => { order.push("rename"); await gate.promise; };
		host.pi.sendUserMessage = async () => { order.push("send"); };
		c.send([frame(31, "rename", { name: "x" }), frame(32, "send-message", { text: "hello", messageId: "ordered" })].map(JSON.stringify).join("\n"));
		await host.waitFor(() => order.length > 0, "rename started");
		expect(order).toEqual(["rename"]);
		gate.resolve();
		await c.next(); await c.next();
		expect(order).toEqual(["rename", "send"]);
	});

	test("model mention rewritten by omp confirms admission before the turn ends", async () => {
		Object.assign(host.ctx.sessionManager, { getBranch: () => [{ type: "custom", customType: "model_mention", data: { agent: "m1", selector: "anthropic/model", name: "Model" } }] });
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); return Promise.withResolvers<void>().promise; };
		const pending = c.request(frame(1, "send-message", { text: "Ask ^anthropic/model now", messageId: "mention" }));
		await host.waitFor(() => host.messages.length === 1, "send started");
		await host.emit("message_start", { message: { role: "user", content: [{ type: "text", text: 'Ask <model agent="m1" name="Model"/> now' }] } });
		expect((await pending).result).toEqual({ state: "admitted" });
	});

	for (const [submitted, canonical] of [["anthropic/model:auto", "anthropic/model"], ["anthropic/model:med", "anthropic/model:medium"]]) {
		test(`model selector ${submitted} admits after canonicalization`, async () => {
			host.ctx.models = { list: () => [{ provider: "anthropic", id: "model" }], current: () => undefined, resolve: () => undefined };
			Object.assign(host.ctx.sessionManager, { getBranch: () => [{ type: "custom", customType: "model_mention", data: { agent: "m1", selector: canonical, name: "Model" } }] });
			host.pi.sendUserMessage = (text: string) => { host.messages.push(text); return Promise.withResolvers<void>().promise; };
			const pending = c.request(frame(1, "send-message", { text: `Ask ^${submitted} now`, messageId: "alias" }));
			await host.waitFor(() => host.messages.length === 1, "send started");
			await host.emit("message_start", { message: { role: "user", content: [{ type: "text", text: 'Ask <model agent="m1" name="Model"/> now' }] } });
			expect((await pending).result).toEqual({ state: "admitted" });
		});
	}

	test("a literal model tag is preserved while a mention expands", async () => {
		Object.assign(host.ctx.sessionManager, { getBranch: () => [{ type: "custom", customType: "model_mention", data: { agent: "m1", selector: "anthropic/model", name: "Model" } }] });
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); return Promise.withResolvers<void>().promise; };
		const tag = '<model agent="m1" name="Model"/>';
		const pending = c.request(frame(1, "send-message", { text: `Show ${tag} and ask ^anthropic/model now`, messageId: "literal" }));
		await host.waitFor(() => host.messages.length === 1, "send started");
		await host.emit("message_start", { message: { role: "user", content: [{ type: "text", text: `Show ${tag} and ask ${tag} now` }] } });
		expect((await pending).result).toEqual({ state: "admitted" });
	});

	test("exact model IDs do not match an abbreviated selector for a different model", async () => {
		host.ctx.models = { list: () => [{ provider: "anthropic", id: "model:ma" }, { provider: "anthropic", id: "model:max" }], current: () => undefined, resolve: () => undefined };
		Object.assign(host.ctx.sessionManager, { getBranch: () => [{ type: "custom", customType: "model_mention", data: { agent: "m1", selector: "anthropic/model:max", name: "Model" } }] });
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); return Promise.withResolvers<void>().promise; };
		let settled = false;
		const pending = c.request(frame(1, "send-message", { text: "Ask ^anthropic/model:ma now", messageId: "exact" })).then(result => { settled = true; return result; });
		await host.waitFor(() => host.messages.length === 1, "send started");
		await host.emit("message_start", { message: { role: "user", content: [{ type: "text", text: 'Ask <model agent="m1" name="Model"/> now' }] } });
		await new Promise(resolve => setTimeout(resolve, 20));
		expect(settled).toBe(false);
		await host.emit("message_start", { message: { role: "user", content: [{ type: "text", text: "Ask ^anthropic/model:ma now" }] } });
		expect((await pending).result).toEqual({ state: "admitted" });
	});

	test("in-flight message receipt survives control being disabled", async () => {
		const gate = Promise.withResolvers<void>();
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); return gate.promise; };
		c.send(frame(1, "send-message", { text: "hello", messageId: "disable-race" }));
		await host.waitFor(() => host.messages.length === 1, "send started");
		fs.writeFileSync(path.join(host.dir, "off"), "");
		await host.waitFor(() => !host.registry(), "control closed");
		gate.resolve();
		fs.unlinkSync(path.join(host.dir, "off"));
		await host.waitFor(() => host.registry(), "control reopened");
		c.close();
		({ client: c, nonce, sessionId, generation } = await Client.toHost(host));
		expect((await c.request(frame(2, "send-status", { messageId: "disable-race" }))).result).toEqual({ state: "admitted" });
		expect(host.messages).toEqual(["hello"]);
	});

	test("one ask cannot accept two distinct answers", async () => {
		await host.emit("tool_execution_start", { toolName: "ask", toolCallId: "question", args: { html: "<form></form>" } });
		expect((await c.request(frame(1, "ask-answer", { askId: "question", text: "no", messageId: "first" }))).result).toEqual({ state: "admitted" });
		expect((await c.request(frame(2, "ask-answer", { askId: "question", text: "yes", messageId: "second" }))).code).toBe("question_closed");
		expect((await c.request(frame(3, "ask-answer", { askId: "question", text: "no", messageId: "first" }))).result).toMatchObject({ state: "admitted", duplicate: true });
		expect(host.messages).toEqual(["no"]);
	});

	test("abort has a reserved slot while four sends are pending", async () => {
		const clients = [c, ...await Promise.all(Array.from({ length: 3 }, () => Client.connect(host.registry()!.data.socket as string)))];
		host.pi.sendUserMessage = (text: string) => { host.messages.push(text); host.busy = true; return Promise.withResolvers<void>().promise; };
		for (let i = 0; i < clients.length; i++) clients[i]!.send(frame(`send-${i}`, "send-message", { text: `m${i}`, messageId: `m${i}` }));
		await host.waitFor(() => host.messages.length === 4, "four sends");
		const emergency = await Client.connect(host.registry()!.data.socket as string);
		try {
			const snapshot = await emergency.request({ v: 1, id: "snapshot", nonce, op: "snapshot" });
			expect(snapshot.ok).toBe(true);
			const current = snapshot.result as { sessionId: string; generation: number };
			expect((await emergency.request({ ...frame("stop", "abort"), ...current })).result).toEqual({ aborted: true });
		}
		finally { emergency.close(); for (const other of clients.slice(1)) other.close(); }
	});

	test("expired ask receipts do not retain or resend answer text", async () => {
		const now = Date.now;
		let clock = 1000000;
		Date.now = () => clock;
		try {
			await host.emit("tool_execution_start", { toolName: "ask", toolCallId: "question", args: { html: "<form></form>" } });
			await c.request(frame(1, "ask-answer", { askId: "question", text: "private", messageId: "answer" }));
			clock += 15 * 60_000;
			expect((await c.request(frame(2, "send-status", { messageId: "answer" }))).result).toEqual({ state: "unknown" });
			expect((await c.request(frame(3, "ask-answer", { askId: "question", text: "private", messageId: "answer" }))).code).toBe("question_closed");
			expect(host.messages).toEqual(["private"]);
		} finally { Date.now = now; }
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
		expect((await fifth.request(frame("reserved", "session-info"))).code).toBe("too_many_clients");
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
