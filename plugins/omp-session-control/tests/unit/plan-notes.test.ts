import { afterEach, beforeEach, expect, mock, test } from "bun:test";
import { Client } from "./support/client";
import { FakeHost } from "./support/fake-host";
import * as fs from "node:fs";
import * as path from "node:path";

mock.module("@oh-my-pi/pi-utils/dirs", () => ({ VERSION: "18.19.0" }));

let host: FakeHost;
let client: Client;
let nonce: string;
let sessionId: string;
let generation: number;
let seq = 0;

beforeEach(async () => {
	host = await FakeHost.start({ enable: true, planReview: true, session: true });
	host.pi.sendUserMessage = async (text: string) => { host.messages.push(text); };
	await host.fire("session_start");
	({ client, nonce, sessionId, generation } = await Client.toHost(host));
});
afterEach(async () => {
	client.close();
	await host.dispose();
});

const call = (op: string, args?: unknown, target?: { sessionId: string; generation: number }) => client.request({
	v: 1, id: ++seq, nonce, op, sessionId: target?.sessionId ?? sessionId, generation: target?.generation ?? generation,
	...(args === undefined ? {} : { args }),
});
const prompt = (path = "local://PLAN.md") => ({ message: { role: "developer", synthetic: true, content: [{ type: "text", text: `Plan approved.\n\n<plan path="${path}">\nsteps\n</plan>` }] } });
const info = async (target?: { sessionId: string; generation: number }) => (await call("session-info", undefined, target)).result as Record<string, any>;

for (const choice of ["execute", "keep", "compact"]) {
	test(`${choice} waits for its prompt even when an earlier turn is busy`, async () => {
		const review = await host.openReview();
		host.busy = true; // a previously queued turn is running before the approved prompt
		const args = { reviewId: review.reviewId, choice, notes: "apply these changes", requestId: `req-${choice}` };
		expect((await call("plan-notes", args)).result).toEqual({ approved: true, requestId: args.requestId, notes: "pending" });
		expect((await call("plan-notes", args)).result).toMatchObject({ approved: true, notes: "pending", duplicate: true });
		expect(host.resolveCalls).toHaveLength(1);
		// An earlier busy turn must not trigger delivery.
		expect(host.messages).toEqual([]);
		if (choice === "compact") await host.fire("auto_compaction_start");
		if (choice === "execute") {
			host.sessionId = "01a1b65b-2222-7000-8000-000000000002";
			await host.emit("session_switch", { reason: "new" });
		}
		const target = { sessionId: host.sessionId, generation: host.registry()!.data.generation as number };
		await host.emit("message_start", prompt());
		if (choice === "compact") {
			expect(host.messages).toEqual([]);
			expect((await info(target)).planNotes.state).toBe("pending");
			await host.fire("auto_compaction_end");
		}
		await host.waitFor(() => host.messages.length === 1, "notes admitted");
		expect(host.messages).toEqual(["apply these changes"]);
		expect((await info(target)).planNotes).toMatchObject({ requestId: args.requestId, reviewId: review.reviewId, choice, approved: true, state: "sent" });
		expect((await call("plan-notes", args, target)).result).toMatchObject({ approved: true, notes: "sent", duplicate: true });
	});
}

test("missing prompt times out without sending notes", async () => {
	const original = host.ctx.setTimeout;
	let expire: (() => void) | undefined;
	host.ctx.setTimeout = (cb, ms) => {
		if (ms === 1_800_000) {
			expire = cb;
			return original(() => {}, ms);
		}
		return original(cb, ms);
	};
	const review = await host.openReview();
	const args = { reviewId: review.reviewId, choice: "keep", notes: "never send", requestId: "timeout" };
	expect((await call("plan-notes", args)).result).toMatchObject({ approved: true, notes: "pending" });
	expect(expire).toBeDefined();
	expire!();
	expect((await info()).planNotes).toMatchObject({ reviewId: review.reviewId, choice: "keep", approved: true, state: "failed", reason: "the plan did not start; compaction was cancelled" });
	expect((await call("plan-notes", args)).result).toMatchObject({ approved: true, notes: "failed", duplicate: true });
	expect(host.messages).toEqual([]);
});

test("another unseen approval of the same path is refused without applying", async () => {
	const first = await host.openReview();
	expect((await call("plan-answer", { reviewId: first.reviewId, choice: "keep" })).ok).toBe(true);
	const second = await host.openReview();
	const args = { reviewId: second.reviewId, choice: "keep", notes: "do not send", requestId: "guard" };
	expect((await call("plan-notes", args)).result).toEqual({ approved: false, requestId: "guard", notes: "failed", reason: "another approval of this plan is still starting" });
	expect(host.resolveCalls).toHaveLength(1);
	expect((await info()).planNotes).toMatchObject({ requestId: "guard", reviewId: second.reviewId, choice: "keep", approved: false, state: "failed" });
	expect(host.messages).toEqual([]);
});

test("operator input before the prompt fails closed", async () => {
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "keep", notes: "private notes", requestId: "input" });
	await host.emit("input", { source: "interactive", text: "new input" });
	await host.emit("message_start", prompt());
	expect((await info()).planNotes).toMatchObject({ state: "failed", reason: "session changed before the plan started; notes not sent" });
	expect(host.messages).toEqual([]);
});

test("operator new with a different first message cannot receive later same-path notes", async () => {
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "execute", notes: "private notes", requestId: "operator-new" });
	host.sessionId = "01a1b65b-2222-7000-8000-000000000002";
	await host.emit("session_switch", { reason: "new" });
	const target = { sessionId: host.sessionId, generation: host.registry()!.data.generation as number };
	await host.emit("message_start", { message: { role: "user", content: [{ type: "text", text: "unrelated" }] } });
	await host.emit("message_start", prompt());
	expect((await info(target)).planNotes).toMatchObject({ state: "failed", reason: "session changed before the plan started; notes not sent" });
	expect(host.messages).toEqual([]);
});

test("a second session switch fails before an unrelated same-path prompt", async () => {
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "execute", notes: "private notes", requestId: "switch-twice" });
	host.sessionId = "01a1b65b-2222-7000-8000-000000000002";
	await host.emit("session_switch", { reason: "new" });
	host.sessionId = "01a1b65b-3333-7000-8000-000000000003";
	await host.emit("session_switch", { reason: "resume" });
	const target = { sessionId: host.sessionId, generation: host.registry()!.data.generation as number };
	await host.emit("message_start", prompt());
	expect((await info(target)).planNotes).toMatchObject({ state: "failed", reason: "session changed before the plan started; notes not sent" });
	expect(host.messages).toEqual([]);
});

test("disabling control cancels deferred notes", async () => {
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "keep", notes: "private notes", requestId: "off" });
	fs.writeFileSync(path.join(host.dir, "off"), "");
	await host.emit("message_start", prompt());
	expect(host.messages).toEqual([]);
	fs.unlinkSync(path.join(host.dir, "off"));
	await host.waitFor(() => host.registry(), "control to reopen");
	client.close();
	({ client, nonce, sessionId, generation } = await Client.toHost(host));
	expect((await info()).planNotes).toMatchObject({ state: "failed", reason: "control disabled" });
});

test("an unseen cancelled approval releases its path after 30 minutes", async () => {
	const original = host.ctx.setTimeout;
	let timeout: (() => void) | undefined;
	let guardExpiry: (() => void) | undefined;
	host.ctx.setTimeout = (cb, ms) => {
		if (ms === 1_800_000 && !guardExpiry) guardExpiry = cb;
		else if (ms === 1_800_000) timeout = cb;
		return original(() => {}, ms);
	};
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "compact", notes: "private notes", requestId: "cancelled" });
	expect(timeout).toBeDefined();
	expect(guardExpiry).toBeDefined();
	timeout!();
	guardExpiry!();
	expect((await info()).planNotes).toMatchObject({ state: "failed", reason: "the plan did not start; compaction was cancelled" });
	const next = await host.openReview();
	expect((await call("plan-notes", { reviewId: next.reviewId, choice: "keep", notes: "new", requestId: "after-cancel" })).result).toMatchObject({ approved: true, notes: "pending" });
});


test("disabling during an in-flight admission preserves cancellation", async () => {
	const gate = Promise.withResolvers<void>();
	host.pi.sendUserMessage = (text: string) => { host.messages.push(text); return gate.promise; };
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "keep", notes: "notes", requestId: "race" });
	await host.emit("message_start", prompt());
	await host.waitFor(() => host.messages.length === 1, "send in flight");
	fs.writeFileSync(path.join(host.dir, "off"), "");
	await host.waitFor(() => !host.registry(), "control closed");
	gate.resolve();
	fs.unlinkSync(path.join(host.dir, "off"));
	await host.waitFor(() => host.registry(), "control reopened");
	client.close();
	({ client, nonce, sessionId, generation } = await Client.toHost(host));
	expect((await info()).planNotes).toMatchObject({ state: "failed", reason: "control disabled" });
});

test("manual compaction cancellation has no end hook and input clears the guard", async () => {
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "compact", notes: "notes", requestId: "cancel" });
	// Manual compaction may emit before_compact; cancellation emits no session_compact.
	await host.emit("session_before_compact", {});
	expect((await info()).planNotes.state).toBe("pending");
	await host.emit("input", { source: "interactive", text: "continue" });
	expect((await info()).planNotes).toMatchObject({ state: "failed", reason: "session changed before the plan started; notes not sent" });
	const next = await host.openReview();
	expect((await call("plan-notes", { reviewId: next.reviewId, choice: "keep", notes: "new", requestId: "after-input" })).result).toMatchObject({ approved: true, notes: "pending" });
});

test("manual compaction success emits session_compact then the approved prompt", async () => {
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "compact", notes: "notes", requestId: "compact-success" });
	await host.emit("session_before_compact", {});
	await host.emit("session_compact", {});
	expect(host.messages).toEqual([]);
	await host.emit("message_start", prompt());
	await host.waitFor(() => host.messages.length === 1, "notes admitted");
	expect((await info()).planNotes.state).toBe("sent");
});

test("unsettled notes become uncertain", async () => {
	const original = host.ctx.setTimeout;
	host.ctx.setTimeout = (cb, ms) => original(cb, ms === 5000 ? 20 : ms);
	host.pi.sendUserMessage = (text: string) => { host.messages.push(text); return Promise.withResolvers<void>().promise; };
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "keep", notes: "notes", requestId: "uncertain" });
	await host.emit("message_start", prompt());
	await new Promise(resolve => setTimeout(resolve, 50));
	expect((await info()).planNotes).toMatchObject({ state: "uncertain", reason: "notes may not have been delivered; check the session" });
});

test("notes send rejection reports failure", async () => {
	host.pi.sendUserMessage = async () => { throw new Error("refused"); };
	const review = await host.openReview();
	await call("plan-notes", { reviewId: review.reviewId, choice: "keep", notes: "notes", requestId: "failed" });
	await host.emit("message_start", prompt());
	await new Promise(resolve => setTimeout(resolve, 10));
	expect((await info()).planNotes).toMatchObject({ state: "failed", reason: "omp did not accept the message" });
});
