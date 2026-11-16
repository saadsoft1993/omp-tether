// Plan review over the control socket: tracking, the pinned chunked read and the answer. The host is a fake that
// refuses what omp's resolvePlanReview refuses (see tests/unit/support/fake-host.ts).
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fitTitle } from "../../src/omp-session-control";
import { Client } from "./support/client";
import { FakeHost, PLAN_OPTIONS } from "./support/fake-host";

const TIERS = [
	{ role: "default", provider: "anthropic", model: "claude-opus-5-5" },
	{ role: "smol", provider: "anthropic", model: "claude-haiku-5-5", thinkingLevel: "low" },
];

let host: FakeHost;
let c: Client;
let nonce: string;
let sessionId: string;
let generation: number;
let seq = 0;

async function boot(opts: { planReview?: boolean; beforeStart?: (h: FakeHost) => Promise<void> } = {}) {
	host = await FakeHost.start({ enable: true, planReview: opts.planReview ?? true, env: { OMP_SESSION_CONTROL_IDLE_MS: "5000" } });
	await opts.beforeStart?.(host);
	await host.fire("session_start");
	({ client: c, nonce, sessionId, generation } = await Client.toHost(host));
}
const frame = (op: string, args?: unknown, over: Record<string, unknown> = {}) => ({
	v: 1,
	id: `p${++seq}`,
	nonce,
	op,
	sessionId,
	generation,
	...(args === undefined ? {} : { args }),
	...over,
});
const registryPlan = () => host.registry()?.data.planReview;
const answer = (args: Record<string, unknown>) => c.request(frame("plan-answer", args));

afterEach(async () => {
	c?.close();
	await host?.dispose();
});

describe("capability", () => {
	beforeEach(() => boot());

	test("the registry and the snapshot list the plan operations when the host has both methods", async () => {
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename", "plan-review", "plan-answer"]);
		expect(host.registry()?.data.planReview).toBeNull();
		expect((await c.request({ v: 1, id: 1, nonce, op: "snapshot" })).result).toMatchObject({
			ops: ["snapshot", "abort", "rename", "plan-review", "plan-answer"],
		});
	});

	test("with no review open, plan-review says so", async () => {
		expect((await c.request(frame("plan-review", { offset: 0 }))).result).toEqual({ open: false });
	});
});

describe("a host without the methods (an older omp)", () => {
	beforeEach(() => boot({ planReview: false }));

	test("the plan operations are not offered and are unknown", async () => {
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename"]);
		expect(host.registry()?.data.planReview).toBeNull();
		expect((await c.request(frame("plan-review", { offset: 0 }))).code).toBe("unknown_op");
		expect((await c.request(frame("plan-answer", { reviewId: "x", choice: "execute" }))).code).toBe("unknown_op");
		expect((await c.request({ v: 1, id: 1, nonce, op: "snapshot" })).result).toMatchObject({ ops: ["snapshot", "abort", "rename"] });
	});

	test("review events are ignored", async () => {
		await host.fire("session_start");
		await host.emit("plan_review_requested", { type: "plan_review_requested", reviewId: "r", sessionId: host.sessionId, planContent: "x", options: PLAN_OPTIONS });
		expect(host.registry()?.data.planReview).toBeNull();
		expect(host.uncaught).toEqual([]);
	});
});

describe("tracking", () => {
	beforeEach(() => boot());

	test("a review opened is published and closed by its own resolve", async () => {
		const before = Date.now();
		const review = await host.openReview({ title: "Ship it" });
		const published = (await host.waitFor(() => registryPlan(), "the review in the registry")) as Record<string, string>;
		expect(published.reviewId).toBe(review.reviewId);
		expect(published.title).toBe("Ship it");
		expect(Date.parse(published.openedAt as string)).toBeGreaterThanOrEqual(before - 1000);
		expect(String(published.openedAt)).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$/);
		host.pending = undefined;
		await host.resolveEvent(review, "execute", "local");
		expect(registryPlan()).toBeNull();
	});

	test("a resolve with another reviewId or sessionId does not clear a review the host still holds", async () => {
		const review = await host.openReview();
		await host.resolveEvent({ reviewId: "other", sessionId: review.sessionId });
		await host.resolveEvent({ reviewId: review.reviewId, sessionId: "another-session" });
		expect((registryPlan() as { reviewId: string }).reviewId).toBe(review.reviewId);
	});

	test("a delayed resolve for a replaced review leaves the replacement open", async () => {
		const first = await host.openReview({ title: "first" });
		const second = await host.openReview({ title: "second" }); // replaces it in the host
		await host.resolveEvent(first, undefined, "local"); // omp reports the first one's close after the second opened
		expect(registryPlan()).toMatchObject({ reviewId: second.reviewId, title: "second" });
		const read = await c.request(frame("plan-review", { offset: 0 }));
		expect(read.result).toMatchObject({ open: true, reviewId: second.reviewId });
	});

	test("the host is the authority: a review that closed without a resolve event is dropped at the next check", async () => {
		await host.openReview();
		host.pending = undefined;
		expect((await c.request(frame("plan-review", { offset: 0 }))).result).toEqual({ open: false });
		expect(registryPlan()).toBeNull();
	});

	test("a review that was open before the plugin bound is found at bind", async () => {
		c.close();
		await host.dispose();
		await boot({ beforeStart: async h => void (await h.openReview({ title: "early" }, false)) });
		expect(registryPlan()).toMatchObject({ title: "early" });
	});

	test("a switch clears the review and bumps the generation; a branch too", async () => {
		await host.openReview();
		host.sessionId = "01a1b65b-2222-7000-8000-000000000002";
		host.pending = undefined;
		await host.fire("session_switch");
		const reg = host.registry()?.data as Record<string, unknown>;
		expect(reg.planReview).toBeNull();
		expect(reg.generation).toBe(generation + 1);
		await host.openReview({ title: "after switch" });
		host.sessionId = "01a1b65b-3333-7000-8000-000000000003";
		await host.fire("session_branch"); // the host's pending review belongs to the old session id
		expect(host.registry()?.data.planReview).toBeNull();
	});

	test("shutdown removes everything", async () => {
		await host.openReview();
		await host.fire("session_shutdown");
		expect(host.registry()).toBeUndefined();
	});

	test("a throwing or malformed host does not break anything", async () => {
		await host.openReview();
		host.failGetPending = true;
		await host.emit("plan_review_requested", { type: "plan_review_requested", reviewId: "z".repeat(200), sessionId: host.sessionId, planContent: "x", options: PLAN_OPTIONS });
		await host.emit("plan_review_requested", null);
		await host.emit("plan_review_resolved", undefined);
		host.failGetPending = false;
		expect((registryPlan() as { reviewId: string }).reviewId).toBe(host.pending?.reviewId as string);
		expect(host.uncaught).toEqual([]);
		expect(host.timerErrors).toEqual([]);
		expect(fs.readFileSync(path.join(host.dir, "events.log"), "utf8")).toContain("plan_reconcile_error");
	});

	test("the registry stays within 4096 bytes; the title is cut to 200 characters", async () => {
		await host.openReview({ title: "t".repeat(900), reviewId: "r".repeat(128) });
		const file = host.registry()?.file as string;
		expect(fs.statSync(file).size).toBeLessThanOrEqual(4096);
		const plan = registryPlan() as { title: string; reviewId: string };
		expect(plan.title).toBe("t".repeat(200));
		expect(plan.reviewId).toBe("r".repeat(128));
	});

	test("fitTitle keeps the longest prefix that fits, by code points, and gives up when nothing fits", () => {
		expect(fitTitle("abcdef", t => t.length <= 3)).toBe("abc");
		expect(fitTitle("😀😀😀", t => t.length <= 3)).toBe("😀"); // never half a pair
		expect(fitTitle("abc", () => false)).toBeUndefined();
		expect(fitTitle("x".repeat(500), () => true)).toBe("x".repeat(200));
		expect(fitTitle("😀".repeat(200), () => true)).toBe("😀".repeat(100)); // 200 units, not 200 code points
		const edge = fitTitle(`${"x".repeat(199)}😀tail`, () => true) as string; // the pair straddles unit 200
		expect(edge).toBe("x".repeat(199));
		expect(edge.isWellFormed()).toBe(true);
		expect(fitTitle(`${"x".repeat(198)}😀tail`, () => true)).toBe(`${"x".repeat(198)}😀`); // ends exactly at 200
	});
});

/** Reads the whole plan with the cursor, as the hub does, checking every reply on the way. */
async function readAll(client: Client, args: { sessionId: string; generation: number }) {
	let offset = 0;
	let text = "";
	let meta: Record<string, unknown> | undefined;
	const sizes: number[] = [];
	for (let i = 0; i < 200; i++) {
		const r = await client.request({ v: 1, id: `rd${i}`, nonce, op: "plan-review", ...args, args: { offset } });
		sizes.push(Buffer.byteLength(JSON.stringify(r)));
		expect(r.ok).toBe(true);
		const res = r.result as Record<string, unknown>;
		expect(res.open).toBe(true);
		expect(res.offset).toBe(offset);
		const chunk = res.chunk as string;
		expect(chunk.isWellFormed()).toBe(true);
		expect(res.nextOffset).toBe(offset + chunk.length);
		meta ??= res;
		expect(res.total).toBe(meta.total);
		expect(res.reviewId).toBe(meta.reviewId);
		text += chunk;
		offset = res.nextOffset as number;
		if (res.done) {
			expect(offset).toBe(res.total as number);
			return { text, sizes, chunks: i + 1, meta };
		}
		expect(chunk.length).toBeGreaterThan(0); // progress
	}
	throw new Error("the cursor never finished");
}

describe("plan-review reads", () => {
	beforeEach(() => boot());

	const spread = (unit: string, n: number) => unit.repeat(Math.ceil(n / unit.length)).slice(0, n);
	const plans: Array<[string, (k: number) => string]> = [
		["ascii", k => `${"a".repeat(k)}${spread("# line of the plan\n", 150_000)}`],
		["cjk", k => `${"a".repeat(k)}${spread("计划：先做这个，然后做那个。", 150_000)}`],
		["emoji (pairs fall on chunk edges)", k => `${"a".repeat(k)}${"😀🚀".repeat(40_000)}`],
		["quotes, backslashes and control characters", k => `${"a".repeat(k)}${spread('"quoted" \\ back\\slash \u0000\u0001\u001f\t\r\n ', 150_000)}`],
	];
	for (const [name, make] of plans) {
		test(`a plan of 150 000 units reassembles exactly: ${name}`, async () => {
			for (const k of [0, 1, 2, 3]) {
				const planContent = make(k);
				const review = await host.openReview({ planContent, tiers: TIERS });
				const { text, sizes, chunks, meta } = await readAll(c, { sessionId, generation });
				expect(text).toBe(planContent);
				expect(chunks).toBeGreaterThan(1);
				expect(Math.max(...sizes)).toBeLessThanOrEqual(64 * 1024);
				expect(meta).toMatchObject({ reviewId: review.reviewId, title: "Add a widget", planFilePath: "local://PLAN.md", tiers: TIERS });
				expect(meta?.options).toEqual(PLAN_OPTIONS);
			}
		});
	}

	test("an empty plan is one empty, finished chunk", async () => {
		await host.openReview({ planContent: "" });
		expect((await c.request(frame("plan-review", { offset: 0 }))).result).toMatchObject({ total: 0, offset: 0, nextOffset: 0, done: true, chunk: "" });
	});

	test("tiers are null when the review has none; offset defaults to 0", async () => {
		await host.openReview();
		expect((await c.request(frame("plan-review"))).result).toMatchObject({ tiers: null, offset: 0, done: true });
	});

	test("the cursor is validated: integers from 0 to the total, never inside a character", async () => {
		await host.openReview({ planContent: "ab😀cd" }); // 6 units; the pair is units 2 and 3
		for (const offset of [-1, 1.5, 7, "0", null, Number.NaN]) {
			expect((await c.request(frame("plan-review", { offset }))).code).toBe("invalid_args");
		}
		expect((await c.request(frame("plan-review", { offset: 3 }))).code).toBe("invalid_args");
		expect((await c.request(frame("plan-review", { offset: 2 }))).result).toMatchObject({ chunk: "😀cd", done: true });
		expect((await c.request(frame("plan-review", { offset: 6 }))).result).toMatchObject({ chunk: "", done: true, nextOffset: 6 });
	});

	test("a reply never exceeds a frame: metadata that cannot fit is too_large, and a tight fit still progresses", async () => {
		await host.openReview({ planFilePath: "p".repeat(70_000) });
		expect((await c.request(frame("plan-review", { offset: 0 }))).code).toBe("too_large");
		await host.openReview({ planContent: "x".repeat(10), planFilePath: "p".repeat(65_300) }); // room for a few bytes only
		const r = await c.request(frame("plan-review", { offset: 0 }));
		if (r.ok) expect(Buffer.byteLength(JSON.stringify(r))).toBeLessThanOrEqual(64 * 1024);
		else expect(r.code).toBe("too_large");
		expect(c.closed).toBe(false);
	});

	/** Size of the reply frame the plugin builds for `plan[offset, end)`, rebuilt here from the wire shape. */
	const replySize = (id: string, review: { reviewId: string; planContent: string; planFilePath: string }, offset: number, end: number) =>
		Buffer.byteLength(
			JSON.stringify({
				v: 1,
				id,
				ok: true,
				result: {
					open: true,
					reviewId: review.reviewId,
					title: "Add a widget",
					planFilePath: review.planFilePath,
					options: PLAN_OPTIONS,
					tiers: null,
					total: review.planContent.length,
					offset,
					nextOffset: end,
					done: end === review.planContent.length,
					chunk: review.planContent.slice(offset, end),
				},
			}),
		);

	/** A review whose reply for plan[0, end) is exactly `target` bytes (the file path is padding). */
	const tightReview = async (planContent: string, end: number, target: number) => {
		const probe = { reviewId: `review-tight-${++seq}`, planContent, planFilePath: "" };
		const planFilePath = "p".repeat(target - replySize("tight", probe, 0, end));
		await host.openReview({ planContent, planFilePath, reviewId: probe.reviewId });
		return { ...probe, planFilePath };
	};
	const tightRead = async (offset: number) => c.request(frame("plan-review", { offset }, { id: "tight" }));

	test("tight budget: a single emoji that fits exactly is returned whole", async () => {
		for (const target of [65_536, 65_535]) {
			await tightReview("😀", 2, target);
			const r = await tightRead(0);
			expect(r.ok).toBe(true);
			expect(r.result).toMatchObject({ chunk: "😀", offset: 0, nextOffset: 2, done: true, total: 2 });
			expect(Buffer.byteLength(JSON.stringify(r))).toBeLessThanOrEqual(65_536);
		}
	});

	test("tight budget: BMP and astral text returns the longest prefix that fits", async () => {
		const plan = "a😀b😀c😀d";
		for (const prefix of [1, 3, 4, 6]) {
			const review = await tightReview(plan, prefix, 65_536); // plan[0, prefix) fits exactly; one more unit does not
			expect(replySize("tight", review, 0, prefix + 1)).toBeGreaterThan(65_536);
			const r = await tightRead(0);
			expect(r.ok).toBe(true); // progress is required, never too_large
			const res = r.result as { chunk: string; nextOffset: number; done: boolean };
			expect(res.chunk).toBe(plan.slice(0, prefix)); // the longest complete-character prefix that fits
			expect(res.nextOffset).toBe(prefix);
			expect(res.done).toBe(false);
			expect(Buffer.byteLength(JSON.stringify(r))).toBeLessThanOrEqual(65_536);
		}
	});

	test("tight budget: when the text does not fit even one character the answer is too_large", async () => {
		await tightReview("😀", 0, 65_536); // metadata fills the frame; the pair needs 4 more bytes
		expect((await tightRead(0)).code).toBe("too_large");
	});

	test("a multi-megabyte plan never has more than one frame of text serialized per request", async () => {
		await host.openReview({ planContent: "x".repeat(3_000_000) });
		const real = JSON.stringify;
		let widest = 0;
		let reads = 0;
		JSON.stringify = ((value: unknown, ...rest: unknown[]) => {
			const chunk = (value as { result?: { chunk?: unknown } } | null)?.result?.chunk;
			if (typeof chunk === "string") widest = Math.max(widest, chunk.length);
			return (real as (...a: unknown[]) => string)(value, ...rest);
		}) as typeof JSON.stringify;
		try {
			for (const offset of [0, 1_234_567, 2_999_000]) {
				const r = await c.request(frame("plan-review", { offset }));
				expect(r.ok).toBe(true);
				reads += 1;
			}
		} finally {
			JSON.stringify = real;
		}
		expect(reads).toBe(3);
		expect(widest).toBeGreaterThan(0);
		expect(widest).toBeLessThanOrEqual(64 * 1024);
	});

	test("the read names the session and generation like a mutation does", async () => {
		await host.openReview();
		expect((await c.request(frame("plan-review", { offset: 0 }, { sessionId: undefined }))).code).toBe("stale_session");
		expect((await c.request(frame("plan-review", { offset: 0 }, { sessionId: "other" }))).code).toBe("stale_session");
		expect((await c.request(frame("plan-review", { offset: 0 }, { generation: generation + 1 }))).code).toBe("stale_generation");
		expect((await c.request(frame("plan-review", { offset: 0 }, { generation: undefined }))).code).toBe("stale_generation");
	});

	test("a switch between two reads makes the second one fail", async () => {
		await host.openReview({ planContent: "z".repeat(150_000) });
		const first = await c.request(frame("plan-review", { offset: 0 }));
		expect(first.ok).toBe(true);
		host.sessionId = "01a1b65b-4444-7000-8000-000000000004";
		host.pending = undefined;
		await host.fire("session_switch");
		const second = await c.request(frame("plan-review", { offset: (first.result as { nextOffset: number }).nextOffset }));
		expect(second.code).toBe("stale_session");
	});

	test("the content is pinned to the review: reads of a review are not affected by later host state of the same id", async () => {
		const review = await host.openReview({ planContent: "A".repeat(100_000) });
		const first = (await c.request(frame("plan-review", { offset: 0 }))).result as { reviewId: string; nextOffset: number };
		(host.pending as { planContent: string }).planContent = "B".repeat(100_000); // same id, edited behind our back
		const second = (await c.request(frame("plan-review", { offset: first.nextOffset }))).result as { chunk: string; reviewId: string };
		expect(second.reviewId).toBe(review.reviewId);
		expect(second.chunk).toBe("A".repeat(second.chunk.length));
	});

	test("a replacement review changes the reviewId in the reply, so a reader can tell", async () => {
		const first = await host.openReview({ planContent: "q".repeat(100_000) });
		const a = (await c.request(frame("plan-review", { offset: 0 }))).result as { nextOffset: number; reviewId: string };
		const second = await host.openReview({ planContent: "r".repeat(100_000) });
		const b = (await c.request(frame("plan-review", { offset: a.nextOffset }))).result as { reviewId: string; chunk: string };
		expect(a.reviewId).toBe(first.reviewId);
		expect(b.reviewId).toBe(second.reviewId);
	});
});

describe("plan-answer", () => {
	let review: Awaited<ReturnType<FakeHost["openReview"]>>;
	beforeEach(async () => {
		await boot();
		review = await host.openReview({ tiers: TIERS });
	});

	const invalid = async (args: Record<string, unknown>) => {
		const r = await answer(args);
		expect(r).toMatchObject({ ok: false, code: "invalid_args" });
		expect(host.resolveCalls).toEqual([]);
	};

	test("approvals reach the host once, with the id and nothing else", async () => {
		const r = await answer({ reviewId: review.reviewId, choice: "execute", feedback: "ignored for an approval" });
		expect(r).toMatchObject({ ok: true, result: { accepted: true } });
		expect(host.resolveCalls).toEqual([{ reviewId: review.reviewId, choice: "execute", input: {} }]);
	});

	test("refine forwards its feedback verbatim; save and compact are accepted", async () => {
		const feedback = "  keep it short\n";
		expect((await answer({ reviewId: review.reviewId, choice: "refine", feedback })).ok).toBe(true);
		expect(host.resolveCalls[0]).toEqual({ reviewId: review.reviewId, choice: "refine", input: { feedback } });
		const next = await host.openReview();
		expect((await answer({ reviewId: next.reviewId, choice: "save" })).ok).toBe(true);
		const third = await host.openReview();
		expect((await answer({ reviewId: third.reviewId, choice: "compact" })).ok).toBe(true);
	});

	test("a role is forwarded with an approval when it names a tier", async () => {
		expect((await answer({ reviewId: review.reviewId, choice: "keep", role: "smol" })).ok).toBe(true);
		expect(host.resolveCalls[0]?.input).toEqual({ role: "smol" });
	});

	test("an accepted answer closes the review in the registry once the host says so", async () => {
		await answer({ reviewId: review.reviewId, choice: "execute" });
		expect(registryPlan()).toBeNull();
	});

	test("an answer the host accepts while the review stays open (compaction) leaves it published", async () => {
		host.keepPendingAfterAnswer = true;
		expect((await answer({ reviewId: review.reviewId, choice: "compact" })).ok).toBe(true);
		expect((registryPlan() as { reviewId: string }).reviewId).toBe(review.reviewId);
	});

	test("every validation path is invalid_args and never reaches the host", async () => {
		await invalid({ choice: "execute" });
		await invalid({ reviewId: 5, choice: "execute" });
		await invalid({ reviewId: "", choice: "execute" });
		await invalid({ reviewId: "r".repeat(129), choice: "execute" });
		await invalid({ reviewId: review.reviewId });
		await invalid({ reviewId: review.reviewId, choice: "approve" });
		await invalid({ reviewId: review.reviewId, choice: "constructor" });
		await invalid({ reviewId: review.reviewId, choice: 1 });
		await invalid({ reviewId: review.reviewId, choice: "refine" });
		await invalid({ reviewId: review.reviewId, choice: "refine", feedback: "   \n\t" });
		await invalid({ reviewId: review.reviewId, choice: "refine", feedback: 5 });
		await invalid({ reviewId: review.reviewId, choice: "refine", feedback: "x".repeat(8001) });
		await invalid({ reviewId: review.reviewId, choice: "refine", feedback: "😀".repeat(4001) }); // 8002 UTF-16 units
		await invalid({ reviewId: review.reviewId, choice: "execute", feedback: 5 });
		await invalid({ reviewId: review.reviewId, choice: "refine", feedback: "ok", role: "smol" }); // role with refine
		await invalid({ reviewId: review.reviewId, choice: "save", role: "smol" });
		await invalid({ reviewId: review.reviewId, choice: "execute", role: "unknown" }); // names no tier
		await invalid({ reviewId: review.reviewId, choice: "execute", role: "" });
		await invalid({ reviewId: review.reviewId, choice: "execute", role: 3 });
		await invalid({ reviewId: review.reviewId, choice: "execute", role: "r".repeat(65) });
	});

	test("limits are UTF-16 units and inclusive: 8000 ASCII and 4000 emoji pass, 8001 and 4001 do not", async () => {
		expect((await answer({ reviewId: review.reviewId, choice: "refine", feedback: "x".repeat(8000) })).ok).toBe(true);
		const next = await host.openReview();
		expect((await answer({ reviewId: next.reviewId, choice: "refine", feedback: "😀".repeat(4000) })).ok).toBe(true);
		const last = await host.openReview();
		for (const feedback of ["x".repeat(8001), "😀".repeat(4000) + "x", "😀".repeat(4001)]) {
			expect((await answer({ reviewId: last.reviewId, choice: "refine", feedback })).code).toBe("invalid_args");
		}
	});

	test("the registry title is cut at 200 UTF-16 units without splitting an emoji", async () => {
		await host.openReview({ title: "😀".repeat(200) });
		expect((registryPlan() as { title: string }).title).toBe("😀".repeat(100));
		await host.openReview({ title: `${"x".repeat(199)}😀` });
		const title = (registryPlan() as { title: string }).title;
		expect(title).toBe("x".repeat(199));
		expect(title.isWellFormed()).toBe(true);
	});

	test("a role with no tiers in the review is refused", async () => {
		const plain = await host.openReview();
		const r = await answer({ reviewId: plain.reviewId, choice: "execute", role: "smol" });
		expect(r).toMatchObject({ ok: false, code: "invalid_args" });
		expect(host.resolveCalls).toEqual([]);
	});

	test("a stale reviewId, a closed review and a disabled choice are not_accepted, with the fixed message", async () => {
		const stale = await answer({ reviewId: "nope", choice: "execute" });
		expect(stale).toMatchObject({ ok: false, code: "not_accepted" });
		expect(String(stale.message)).toContain("The terminal did not accept that answer");
		const disabled = await host.openReview({ options: PLAN_OPTIONS.map(o => (o.id === "keep" ? { ...o, disabled: true } : o)) });
		expect((await answer({ reviewId: disabled.reviewId, choice: "keep" })).code).toBe("not_accepted");
		expect(host.resolveCalls).toEqual([]);
		host.pending = undefined;
		expect((await answer({ reviewId: disabled.reviewId, choice: "execute" })).code).toBe("not_accepted");
		expect(registryPlan()).toBeNull();
	});

	test("a host that refuses is reported, not retried", async () => {
		host.refuseAnswers = true;
		const r = await answer({ reviewId: review.reviewId, choice: "execute" });
		expect(r).toMatchObject({ ok: false, code: "not_accepted" });
		expect(host.resolveCalls.length).toBe(1);
	});

	test("an answer for a review the terminal replaced meanwhile is not_accepted", async () => {
		const replacement = await host.openReview({ title: "replacement" });
		expect((await answer({ reviewId: review.reviewId, choice: "execute" })).code).toBe("not_accepted");
		expect((await answer({ reviewId: replacement.reviewId, choice: "execute" })).ok).toBe(true);
	});

	test("it names the session and generation, and a switch makes an old answer stale", async () => {
		const next = await host.openReview();
		const r = await c.request(frame("plan-answer", { reviewId: next.reviewId, choice: "execute" }, { sessionId: "other" }));
		expect(r.code).toBe("stale_session");
		const g = await c.request(frame("plan-answer", { reviewId: next.reviewId, choice: "execute" }, { generation: generation + 1 }));
		expect(g.code).toBe("stale_generation");
		expect(host.resolveCalls).toEqual([]);
	});

	test("answers are serialized with the other changes", async () => {
		const order: string[] = [];
		const setName = host.pi.setSessionName as (n: string) => Promise<void>;
		host.pi.setSessionName = async (n: string) => {
			order.push("rename start");
			await new Promise(r => setTimeout(r, 60));
			await setName(n);
			order.push("rename end");
		};
		const orig = host.ctx.resolvePlanReview as NonNullable<typeof host.ctx.resolvePlanReview>;
		host.ctx.resolvePlanReview = (...a) => {
			order.push("answer");
			return orig(...a);
		};
		c.send(JSON.stringify(frame("rename", { name: "n" })));
		c.send(JSON.stringify(frame("plan-answer", { reviewId: review.reviewId, choice: "execute" })));
		expect((await c.next())?.ok).toBe(true);
		expect((await c.next())?.ok).toBe(true);
		expect(order).toEqual(["rename start", "rename end", "answer"]);
	});

	test("the reply and events.log carry no plan text or feedback", async () => {
		const secret = "SECRET-FEEDBACK-1234";
		host.failGetPending = false;
		await answer({ reviewId: review.reviewId, choice: "refine", feedback: secret });
		const log = fs.existsSync(path.join(host.dir, "events.log")) ? fs.readFileSync(path.join(host.dir, "events.log"), "utf8") : "";
		expect(log).not.toContain(secret);
		expect(log).not.toContain("# Plan");
	});
});
