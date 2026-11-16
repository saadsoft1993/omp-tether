// Session commands over the control socket: capability detection, target pinning, validation, each host call, the
// failure replies and the long compaction. The host is a fake (tests/unit/support/fake-host.ts).
import { afterEach, describe, expect, test } from "bun:test";
import { Client } from "./support/client";
import { FakeHost, type FakeModel, type HostOptions, MODEL_A, MODEL_B, MODEL_C } from "./support/fake-host";

const ALL_OPS = ["snapshot", "abort", "rename", "session-info", "compact", "set-model", "set-thinking", "set-mode"];

let host: FakeHost;
let c: Client;
let nonce: string;
let sessionId: string;
let generation: number;
let seq = 0;

async function boot(opts: HostOptions = {}) {
	host = await FakeHost.start({ enable: true, session: true, env: { OMP_SESSION_CONTROL_IDLE_MS: "5000" }, ...opts });
	await host.fire("session_start");
	({ client: c, nonce, sessionId, generation } = await Client.toHost(host));
}
const frame = (op: string, args?: unknown, over: Record<string, unknown> = {}) => ({
	v: 1,
	id: `s${++seq}`,
	nonce,
	op,
	sessionId,
	generation,
	...(args === undefined ? {} : { args }),
	...over,
});
const call = (op: string, args?: unknown) => c.request(frame(op, args));
/** A second connection, for requests sent while the first one waits for a slow reply (replies are read in order). */
const other = async (): Promise<Client> => Client.connect(host.registry()?.data.socket as string);
const info = async () => (await call("session-info")).result as Record<string, any>;

afterEach(async () => {
	c?.close();
	await host?.dispose();
	expect(host?.uncaught ?? []).toEqual([]);
});

describe("capability detection", () => {
	test("a host with every API lists all five commands after the base operations", async () => {
		await boot();
		expect(host.registry()?.data.ops).toEqual(ALL_OPS);
		expect((await c.request({ v: 1, id: 1, nonce, op: "snapshot" })).result).toMatchObject({ ops: ALL_OPS });
	});

	test("with the plan methods too, the plan operations come first", async () => {
		await boot({ planReview: true });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename", "plan-review", "plan-answer", ...ALL_OPS.slice(3)]);
	});

	test("a host with none of the APIs offers only the base operations, and the commands are unsupported", async () => {
		await boot({ session: false });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename"]);
		for (const op of ["session-info", "compact", "set-model", "set-thinking", "set-mode"]) {
			expect((await call(op, {})).code).toBe("unsupported");
		}
	});

	test("each command is offered on its own API", async () => {
		await boot({ session: { compact: true } });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename", "compact"]);
		c.close();
		await host.dispose();
		await boot({ session: { models: true } });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename", "session-info"]);
		c.close();
		await host.dispose();
		await boot({ session: { models: true, setModel: true } });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename", "session-info", "set-model"]);
		c.close();
		await host.dispose();
		await boot({ session: { thinking: true } });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename", "session-info", "set-thinking"]);
		c.close();
		await host.dispose();
		await boot({ session: { modes: true } });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename", "session-info", "set-mode"]);
	});

	test("set-model needs both setModel and the model list; set-thinking needs both thinking methods", async () => {
		await boot({ session: { setModel: true } });
		expect(host.registry()?.data.ops).toEqual(["snapshot", "abort", "rename"]);
	});

	test("an API that disappears after the registry was written answers unsupported", async () => {
		await boot();
		delete host.ctx.compact;
		expect((await call("compact")).code).toBe("unsupported");
		expect(host.compactCalls).toEqual([]);
	});

	test("session-info on a host that only has a thinking getter reports what it can", async () => {
		await boot({ session: { thinking: true } });
		expect(await info()).toEqual({
			model: null,
			compacting: false,
			thinkingLevel: "high",
			thinkingLevels: [],
			models: [],
			mode: { plan: false, goal: false, loop: false, vibe: false },
			modeStatus: { plan: "inactive", vibe: "inactive", goal: "inactive", loop: "inactive" },
		});
	});
});

describe("target pinning", () => {
	const CASES: Array<[string, unknown]> = [
		["session-info", undefined],
		["compact", {}],
		["set-model", { provider: "openai", id: "gpt-6.1-sol" }],
		["set-thinking", { level: "low" }],
		["set-mode", { mode: "plan", action: "enter" }],
	];

	test("every command is refused for a stale session or generation, and omp is not called", async () => {
		await boot();
		for (const [op, args] of CASES) {
			expect((await c.request(frame(op, args, { sessionId: "other" }))).code).toBe("stale_session");
			expect((await c.request(frame(op, args, { generation: generation + 1 }))).code).toBe("stale_generation");
			expect((await c.request(frame(op, args, { sessionId: undefined }))).code).toBe("stale_session");
		}
		expect(host.compactCalls).toEqual([]);
		expect(host.setModelCalls).toEqual([]);
		expect(host.thinkingCalls).toEqual([]);
		expect(host.modeCalls).toEqual([]);
	});

	test("a switch between the request and its turn in the chain refuses the queued change", async () => {
		await boot({ timerScale: 0.01 });
		host.compactBehaviour = "hang";
		const slow = c.request(frame("compact"));
		await host.waitFor(() => host.compactCalls.length > 0, "the compaction to start");
		const second = await other();
		const queued = second.request(frame("set-thinking", { level: "low" }));
		host.sessionId = "01a1b65b-2222-7000-8000-000000000002";
		await host.fire("session_switch");
		host.releaseCompact?.();
		expect((await slow).ok).toBe(true);
		expect(["stale_session", "stale_generation", "mutation_pending"]).toContain((await queued).code as string);
		expect(host.thinkingCalls).toEqual([]);
		second.close();
	});
});

describe("session-info", () => {
	test("reports the model, levels, the list and the modes", async () => {
		await boot();
		host.modes.plan.status = "active";
		host.modes.goal.status = "paused";
		const r = await call("session-info");
		expect(r.ok).toBe(true);
		expect(r.result).toEqual({
			model: { provider: "anthropic", id: "claude-opus-5-5" },
			compacting: false,
			thinkingLevel: "high",
			thinkingLevels: ["off", "minimal", "low", "medium", "high"],
			models: [
				{ provider: "anthropic", id: "claude-opus-5-5", name: "Claude Opus 5.5" },
				{ provider: "openai", id: "gpt-6.1-sol", name: "GPT 6.1 Sol" },
				{ provider: "local", id: "plain", name: "Plain" },
			],
			mode: { plan: true, goal: false, loop: false, vibe: false },
			modeStatus: { plan: "active", vibe: "inactive", goal: "paused", loop: "inactive" },
		});
	});

	test("a model without reasoning has no thinking levels; no model is null", async () => {
		await boot();
		host.currentModel = MODEL_C;
		expect((await info()).thinkingLevels).toEqual([]);
		host.currentModel = undefined;
		expect((await info()).model).toBeNull();
	});

	test("unusable list entries are left out, names are cut, the list is capped", async () => {
		await boot();
		const many: FakeModel[] = Array.from({ length: 250 }, (_, i) => ({ provider: "p", id: `m${i}`, name: "n".repeat(500) }));
		host.available = [{ provider: "", id: "x" }, { provider: "p" } as unknown as FakeModel, { provider: "p", id: "y".repeat(129) }, ...many];
		const r = await info();
		expect(r.models.length).toBe(200);
		expect(r.models[0]).toEqual({ provider: "p", id: "m0", name: "n".repeat(80) });
	});

	test("a result that would not fit is shortened", async () => {
		await boot();
		host.available = Array.from({ length: 300 }, (_, i) => ({ provider: "p".repeat(128), id: `${"m".repeat(100)}${i}`.slice(0, 128), name: "n".repeat(80) }));
		const raw = await call("session-info");
		expect(raw.ok).toBe(true);
		expect(Buffer.byteLength(JSON.stringify(raw))).toBeLessThan(64 * 1024);
		expect((raw.result as any).models.length).toBeGreaterThan(0);
	});

	test("a host failure is a fixed failed reply", async () => {
		await boot();
		host.failModels = true;
		const r = await call("session-info");
		expect(r).toMatchObject({ ok: false, code: "failed" });
		expect(host.uncaught).toEqual([]);
	});
});

describe("compact", () => {
	test("compacts, with and without instructions", async () => {
		await boot();
		expect(await call("compact")).toMatchObject({ ok: true, result: { ok: true } });
		expect((await call("compact", { instructions: "keep the API names" })).ok).toBe(true);
		expect((await call("compact", { instructions: "   " })).ok).toBe(true);
		expect(host.compactCalls).toEqual([undefined, "keep the API names", undefined]);
	});

	test("instructions are validated", async () => {
		await boot();
		for (const instructions of [5, null, ["x"], "x".repeat(2001)]) {
			expect((await call("compact", { instructions })).code).toBe("invalid_args");
		}
		expect((await call("compact", { instructions: "x".repeat(2000) })).ok).toBe(true);
		expect(host.compactCalls.length).toBe(1);
	});

	test("while a turn runs it refuses and does not abort the turn", async () => {
		await boot();
		host.busy = true;
		const r = await call("compact");
		expect(r).toMatchObject({ ok: false, code: "failed" });
		expect(host.compactCalls).toEqual([]);
		expect(host.aborts).toBe(0);
	});

	test("a failing compaction is failed, with a fixed message that holds no omp text", async () => {
		await boot();
		host.compactBehaviour = "throw";
		const r = await call("compact");
		expect(r).toMatchObject({ ok: false, code: "failed" });
		expect(JSON.stringify(r)).not.toContain("secret");
		expect((r.message as string).length).toBeLessThanOrEqual(300);
	});

	test("a long compaction outlives the 10 s deadline, times out at its own, and holds back later changes until it ends", async () => {
		await boot({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } }); // 10 s becomes 100 ms, 180 s becomes 1.8 s
		host.compactBehaviour = "hang";
		const started = Date.now();
		const slow = c.request(frame("compact"));
		await host.waitFor(() => host.compactCalls.length > 0, "the compaction to start");
		await new Promise(r => setTimeout(r, 400)); // well past the ordinary deadline
		expect(Date.now() - started).toBeGreaterThanOrEqual(400);
		// reads still answer, changes are held back: the compaction has not settled in omp
		const second = await other();
		expect(await second.request(frame("session-info"))).toMatchObject({ ok: true });
		const refusedAt = Date.now();
		expect((await second.request(frame("set-thinking", { level: "low" }))).code).toBe("mutation_pending");
		expect(Date.now() - refusedAt).toBeLessThan(500); // refused at once, not queued behind the compaction
		expect(host.thinkingCalls).toEqual([]);
		host.releaseCompact?.();
		expect(await slow).toMatchObject({ ok: true, result: { ok: true } });
		expect((await second.request(frame("set-thinking", { level: "low" }))).ok).toBe(true);
		second.close();
	});

	test("a compaction that outlasts its own deadline gets timeout, and the host stays marked busy", async () => {
		await boot({ timerScale: 0.001, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } }); // 180 ms
		host.compactBehaviour = "hang";
		const r = await call("compact");
		expect(r.code).toBe("timeout");
		expect(r.message).toContain("180 s");
		expect(r.message).toContain("may still be running");
		expect((await call("set-mode", { mode: "plan", action: "enter" })).code).toBe("mutation_pending");
		host.releaseCompact?.();
		await new Promise(r => setTimeout(r, 50));
		expect((await call("set-mode", { mode: "plan", action: "enter" })).ok).toBe(true);
	});

	test("a connection waiting for a compaction is not closed as idle", async () => {
		await boot({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "100" } }); // idle 1 ms, compaction deadline 1.8 s
		host.compactBehaviour = "hang";
		const slow = c.request(frame("compact"));
		await host.waitFor(() => host.compactCalls.length > 0, "the compaction to start");
		await new Promise(r => setTimeout(r, 300));
		expect(c.closed).toBe(false);
		host.releaseCompact?.();
		expect(await slow).toMatchObject({ ok: true });
		await host.waitFor(() => c.closed, "the idle close once nothing is pending", 3000);
	});
});

describe("compact: positive evidence of completion", () => {
	test("success needs the session_compact event, and the reply waits for it", async () => {
		await boot();
		expect(await call("compact")).toMatchObject({ ok: true, result: { ok: true } });
		expect(host.compactionsDone).toBe(1);
	});

	test("an event that arrives a moment after ctx.compact resolved still counts", async () => {
		await boot();
		host.compactBehaviour = "late-event";
		expect(await call("compact")).toMatchObject({ ok: true });
	});

	test("ctx.compact resolving with no event (failed, cancelled or nothing to compact in the real TUI) is failed", async () => {
		await boot({ timerScale: 0.01 }); // the 2 s wait for the event becomes 20 ms
		host.compactBehaviour = "swallowed";
		const r = await call("compact");
		expect(r).toMatchObject({
			ok: false,
			code: "failed",
			message: "Compaction did not complete (cancelled, failed, or nothing to compact).",
		});
		expect(host.compactionsDone).toBe(0);
		expect((await info()).compacting).toBe(false);
	});

	test("an event from an earlier compaction does not count for a later one", async () => {
		await boot({ timerScale: 0.01 });
		expect((await call("compact")).ok).toBe(true);
		host.compactBehaviour = "swallowed";
		expect((await call("compact")).code).toBe("failed");
	});

	test("a cancelled or failed compaction does not leave the host marked busy", async () => {
		await boot({ timerScale: 0.01 });
		host.compactBehaviour = "swallowed";
		expect((await call("compact")).code).toBe("failed");
		expect((await call("set-thinking", { level: "low" })).ok).toBe(true);
	});
});

describe("snapshot and session-info say whether a compaction is unsettled", () => {
	test("compacting is false at rest, true while ours runs, still true after its request timed out, false once omp settles", async () => {
		await boot({ timerScale: 0.001, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } }); // our deadline is 180 ms
		const snap = async () => (await c.request({ v: 1, id: `n${++seq}`, nonce, op: "snapshot" })).result as Record<string, any>;
		expect((await snap()).compacting).toBe(false);
		expect((await info()).compacting).toBe(false);
		host.compactBehaviour = "hang";
		const slow = c.request(frame("compact"));
		const second = await other();
		await host.waitFor(() => host.compactCalls.length > 0, "the compaction to start");
		const during = (await second.request({ v: 1, id: "a", nonce, op: "snapshot" })).result as Record<string, any>;
		expect(during.compacting).toBe(true);
		expect(((await second.request(frame("session-info"))).result as any).compacting).toBe(true);
		expect((await slow).code).toBe("timeout");
		// after the request timed out the compaction is still unsettled in omp
		expect(((await second.request({ v: 1, id: "b", nonce, op: "snapshot" })).result as any).compacting).toBe(true);
		expect(((await second.request(frame("session-info"))).result as any).compacting).toBe(true);
		host.releaseCompact?.();
		await host.waitFor(async () => false, "settle", 10).catch(() => {});
		await new Promise(r => setTimeout(r, 80));
		expect(((await second.request({ v: 1, id: "c", nonce, op: "snapshot" })).result as any).compacting).toBe(false);
		expect(((await second.request(frame("session-info"))).result as any).compacting).toBe(false);
		second.close();
	});

	test("an auto-compaction started by omp shows too", async () => {
		await boot();
		await host.emit("auto_compaction_start", { type: "auto_compaction_start" });
		expect((await info()).compacting).toBe(true);
		await host.emit("auto_compaction_end", { type: "auto_compaction_end" });
		expect((await info()).compacting).toBe(false);
	});
});

describe("a session replacement cannot land while one of our changes is unsettled", () => {
	const gate = () => {
		let open!: () => void;
		const promise = new Promise<void>(resolve => (open = resolve));
		return { promise, open };
	};

	test("a switch or branch requested while set-model waits inside omp is cancelled, and allowed again after", async () => {
		await boot();
		const g = gate();
		host.setModelGate = g.promise;
		const second = await other();
		const pending = second.request(frame("set-model", { provider: "openai", id: "gpt-6.1-sol" }));
		await host.waitFor(() => host.setModelCalls.length > 0, "set-model to reach omp");
		for (const event of ["session_before_switch", "session_before_branch"]) {
			expect(await host.cancelled(event, { reason: "new" })).toBe(true);
		}
		expect(host.notices.some(n => n.message.includes("still running"))).toBe(true);
		g.open();
		expect((await pending).ok).toBe(true);
		for (const event of ["session_before_switch", "session_before_branch"]) {
			expect(await host.cancelled(event, { reason: "new" })).toBe(false);
		}
		second.close();
	});

	test("the refusal also holds after our request has timed out, until omp settles", async () => {
		await boot({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } });
		const g = gate();
		host.setModelGate = g.promise;
		expect((await call("set-model", { provider: "openai", id: "gpt-6.1-sol" })).code).toBe("timeout");
		expect(await host.cancelled("session_before_switch", { reason: "resume" })).toBe(true);
		expect(await host.cancelled("session_before_branch", { reason: "fork" })).toBe(true);
		g.open();
		await new Promise(r => setTimeout(r, 50));
		expect(await host.cancelled("session_before_switch", { reason: "resume" })).toBe(false);
		// the model change landed on the session it was meant for
		expect((await info()).model.id).toBe(MODEL_B.id);
	});

	test("a switch is not touched when nothing of ours is running, or when it is not the main interactive session", async () => {
		await boot();
		expect(await host.cancelled("session_before_switch", { reason: "new" })).toBe(false);
		expect(await host.cancelled("session_before_branch", { reason: "branch" })).toBe(false);
	});

	test("the veto holds when the terminal notice throws", async () => {
		await boot();
		const g = gate();
		host.setModelGate = g.promise;
		const second = await other();
		const pending = second.request(frame("set-model", { provider: "openai", id: "gpt-6.1-sol" }));
		await host.waitFor(() => host.setModelCalls.length > 0, "set-model to reach omp");
		host.ctx.ui.notify = () => {
			throw new Error("injected notify failure");
		};
		expect(await host.cancelled("session_before_switch", { reason: "new" })).toBe(true);
		expect(await host.cancelled("session_before_branch", { reason: "fork" })).toBe(true);
		g.open();
		await pending;
		second.close();
	});

	test("a replacement that was allowed holds changes back until it commits: the reviewer's ordering", async () => {
		await boot();
		const oldSession = host.sessionId;
		// 1. before_switch is allowed while nothing of ours runs
		expect(await host.cancelled("session_before_switch", { reason: "new" })).toBe(false);
		// 2. a set-model arrives while omp is still deferring the switch (hooks, abort, flush): it is refused, never started
		const g = gate();
		host.setModelGate = g.promise;
		const refused = await call("set-model", { provider: "openai", id: "gpt-6.1-sol" });
		expect(refused).toMatchObject({ ok: false, code: "busy" });
		expect(host.setModelCalls).toEqual([]);
		for (const [op, args] of [
			["rename", { name: "x" }],
			["compact", {}],
			["set-thinking", { level: "low" }],
			["set-mode", { mode: "plan", action: "enter" }],
		] as const) {
			expect((await call(op, args)).code).toBe("busy");
		}
		expect(host.name).toBeUndefined();
		// reads still answer
		expect((await call("session-info")).ok).toBe(true);
		// 3. the switch commits: the session is replaced and the event fires
		host.sessionId = "01a1b65b-3333-7000-8000-000000000003";
		await host.fire("session_switch");
		g.open();
		// 4. nothing of the old request reached the new session, and a request for the old session is stale
		expect(host.setModelCalls).toEqual([]);
		expect((await c.request(frame("set-model", { provider: "openai", id: "gpt-6.1-sol" }))).code).toBe("stale_session");
		expect(host.currentModel?.id).toBe(MODEL_A.id);
		expect(oldSession).not.toBe(host.sessionId);
		// changes for the new session work again
		const reg = host.registry()?.data as Record<string, any>;
		const r = await c.request(frame("set-thinking", { level: "low" }, { sessionId: reg.sessionId, generation: reg.generation }));
		expect(r.ok).toBe(true);
	});

	test("a branch commit clears the hold too", async () => {
		await boot();
		expect(await host.cancelled("session_before_branch", { reason: "branch", entryId: "e" })).toBe(false);
		expect((await call("set-thinking", { level: "low" })).code).toBe("busy");
		await host.fire("session_branch");
		const reg = host.registry()?.data as Record<string, any>;
		expect((await c.request(frame("set-thinking", { level: "low" }, { sessionId: reg.sessionId, generation: reg.generation }))).ok).toBe(true);
	});

	test("a replacement that another hook cancelled holds changes back only until the expiry", async () => {
		await boot({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } }); // 30 s becomes 300 ms
		expect(await host.cancelled("session_before_switch", { reason: "resume" })).toBe(false);
		expect((await call("set-thinking", { level: "low" })).code).toBe("busy");
		await new Promise(r => setTimeout(r, 450));
		expect((await call("set-thinking", { level: "low" })).ok).toBe(true);
	});

	test("a native change that timed out keeps its veto however long the replacement hold is", async () => {
		await boot({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } });
		const g = gate();
		host.setModelGate = g.promise;
		expect((await call("set-model", { provider: "openai", id: "gpt-6.1-sol" })).code).toBe("timeout");
		await new Promise(r => setTimeout(r, 450));
		expect(await host.cancelled("session_before_switch", { reason: "new" })).toBe(true);
		g.open();
	});

	test("a compaction of ours holds back a replacement too", async () => {
		await boot({ timerScale: 0.01, env: { OMP_SESSION_CONTROL_IDLE_MS: "3600000" } });
		host.compactBehaviour = "hang";
		const slow = c.request(frame("compact"));
		await host.waitFor(() => host.compactCalls.length > 0, "the compaction to start");
		expect(await host.cancelled("session_before_switch", { reason: "new" })).toBe(true);
		host.releaseCompact?.();
		await slow;
		expect(await host.cancelled("session_before_switch", { reason: "new" })).toBe(false);
	});
});

describe("set-model", () => {
	test("switches to a model in the available list", async () => {
		await boot();
		const r = await call("set-model", { provider: "openai", id: "gpt-6.1-sol" });
		expect(r).toMatchObject({ ok: true, result: { model: { provider: "openai", id: "gpt-6.1-sol" } } });
		expect(host.setModelCalls.map(m => m.id)).toEqual(["gpt-6.1-sol"]);
		expect((await info()).model).toEqual({ provider: "openai", id: "gpt-6.1-sol" });
		expect((await info()).thinkingLevels).toEqual(["off", "low", "high"]);
	});

	test("a model that is not available, and bad arguments, change nothing", async () => {
		await boot();
		expect((await call("set-model", { provider: "openai", id: "nope" })).code).toBe("invalid_args");
		expect((await call("set-model", { provider: "nope", id: "gpt-6.1-sol" })).code).toBe("invalid_args");
		for (const args of [{}, { provider: "openai" }, { id: "x" }, { provider: 1, id: "x" }, { provider: "", id: "x" }, { provider: "p", id: "x".repeat(129) }]) {
			expect((await call("set-model", args)).code).toBe("invalid_args");
		}
		expect(host.setModelCalls).toEqual([]);
	});

	test("omp refusing (no API key) is failed", async () => {
		await boot();
		host.setModelResult = false;
		const r = await call("set-model", { provider: "openai", id: "gpt-6.1-sol" });
		expect(r).toMatchObject({ ok: false, code: "failed" });
		expect((await info()).model.id).toBe(MODEL_A.id);
	});

	test("omp throwing is failed with a fixed message", async () => {
		await boot();
		host.setModelResult = "throw";
		const r = await call("set-model", { provider: "openai", id: "gpt-6.1-sol" });
		expect(r).toMatchObject({ ok: false, code: "failed" });
		expect(JSON.stringify(r)).not.toContain("secret");
	});

	test("omp saying yes but keeping the model is failed", async () => {
		await boot();
		host.setModelResult = "ignore";
		const r = await call("set-model", { provider: "openai", id: "gpt-6.1-sol" });
		expect(r).toMatchObject({ ok: false, code: "failed", message: "omp kept the previous model" });
	});
});

describe("set-thinking", () => {
	test("sets a level the model supports and returns what the session reports", async () => {
		await boot();
		expect(await call("set-thinking", { level: "low" })).toMatchObject({ ok: true, result: { thinkingLevel: "low" } });
		expect((await call("set-thinking", { level: "off" })).result).toEqual({ thinkingLevel: "off" });
		expect(host.thinkingCalls).toEqual(["low", "off"]);
	});

	test("a ceiling that clamps the level shows in the result", async () => {
		await boot();
		host.thinkingCeiling = "medium";
		expect((await call("set-thinking", { level: "high" })).result).toEqual({ thinkingLevel: "medium" });
	});

	test("a level the model does not support, and bad arguments, are invalid_args and change nothing", async () => {
		await boot();
		host.currentModel = MODEL_B;
		const r = await call("set-thinking", { level: "medium" });
		expect(r).toMatchObject({ ok: false, code: "invalid_args", detail: { thinkingLevels: ["off", "low", "high"] } });
		host.currentModel = MODEL_C;
		expect((await call("set-thinking", { level: "high" })).code).toBe("invalid_args");
		for (const args of [{}, { level: 1 }, { level: "" }, { level: "x".repeat(33) }, { level: "auto" }, { level: "inherit" }]) {
			expect((await call("set-thinking", args)).code).toBe("invalid_args");
		}
		expect(host.thinkingCalls).toEqual([]);
	});

	test("omp throwing is failed", async () => {
		await boot();
		host.setThinkingThrows = true;
		expect(await call("set-thinking", { level: "low" })).toMatchObject({ ok: false, code: "failed" });
	});
});

describe("set-mode", () => {
	test("plan and vibe enter and exit, idempotently", async () => {
		await boot();
		expect((await call("set-mode", { mode: "plan", action: "enter" })).result).toEqual({ changed: true });
		expect((await info()).mode.plan).toBe(true);
		expect((await call("set-mode", { mode: "plan", action: "enter" })).result).toEqual({ changed: false });
		expect((await call("set-mode", { mode: "plan", action: "exit" })).result).toEqual({ changed: true });
		expect((await call("set-mode", { mode: "vibe", action: "enter" })).result).toEqual({ changed: true });
		expect((await call("set-mode", { mode: "vibe", action: "exit" })).result).toEqual({ changed: true });
		expect(host.modeCalls).toEqual([
			{ mode: "plan", action: "enter" },
			{ mode: "plan", action: "enter" },
			{ mode: "plan", action: "exit" },
			{ mode: "vibe", action: "enter" },
			{ mode: "vibe", action: "exit" },
		]);
	});

	test("goal enters with an objective, then pauses, resumes and drops", async () => {
		await boot();
		expect((await call("set-mode", { mode: "goal", action: "enter", objective: "ship it" })).result).toEqual({ changed: true });
		expect(host.modeCalls[0]).toEqual({ mode: "goal", action: "enter", objective: "ship it" });
		expect((await call("set-mode", { mode: "goal", action: "pause" })).ok).toBe(true);
		expect((await info()).modeStatus.goal).toBe("paused");
		expect((await call("set-mode", { mode: "goal", action: "resume" })).ok).toBe(true);
		expect((await call("set-mode", { mode: "goal", action: "drop" })).ok).toBe(true);
		expect((await info()).mode.goal).toBe(false);
	});

	test("loop can only be exited", async () => {
		await boot();
		host.modes.loop.status = "active";
		expect((await call("set-mode", { mode: "loop", action: "exit" })).result).toEqual({ changed: true });
		expect((await call("set-mode", { mode: "loop", action: "enter" })).code).toBe("invalid_args");
		expect((await call("set-mode", { mode: "loop", action: "enter", objective: "again" })).code).toBe("invalid_args");
	});

	test("combinations omp does not accept, and bad arguments, never reach omp", async () => {
		await boot();
		const bad: unknown[] = [
			{},
			{ mode: "plan" },
			{ action: "enter" },
			{ mode: "nope", action: "enter" },
			{ mode: "constructor", action: "enter" },
			{ mode: "plan", action: "pause" },
			{ mode: "plan", action: "drop" },
			{ mode: "vibe", action: "resume" },
			{ mode: "goal", action: "exit" },
			{ mode: "goal", action: "enter" },
			{ mode: "goal", action: "enter", objective: "   " },
			{ mode: "goal", action: "enter", objective: 4 },
			{ mode: "goal", action: "enter", objective: "x".repeat(2001) },
			{ mode: "plan", action: "enter", objective: "hi" },
			{ mode: "goal", action: "pause", objective: "hi" },
			{ mode: 1, action: "enter" },
			{ mode: "plan", action: 1 },
		];
		for (const args of bad) expect((await call("set-mode", args)).code).toBe("invalid_args");
		expect((await call("set-mode", { mode: "goal", action: "enter", objective: "x".repeat(2000) })).ok).toBe(true);
		expect(host.modeCalls.length).toBe(1);
	});

	test("omp refusing is failed with its reason code, and only a plain code is passed on", async () => {
		await boot();
		host.modeResult = { ok: false, code: "busy", message: "a turn is running at /home/secret/path", state: host.modes };
		const r = await call("set-mode", { mode: "plan", action: "enter" });
		expect(r).toMatchObject({ ok: false, code: "failed", message: "omp refused the change: busy" });
		expect(JSON.stringify(r)).not.toContain("secret");
		host.modeResult = { ok: false, code: "Bad Code!\n", message: "x" };
		expect((await call("set-mode", { mode: "plan", action: "enter" })).message).toBe("omp refused the change: refused");
		host.modeResult = null;
		expect((await call("set-mode", { mode: "plan", action: "enter" })).code).toBe("failed");
	});

	test("omp throwing is failed", async () => {
		await boot();
		host.modeThrows = true;
		expect(await call("set-mode", { mode: "plan", action: "enter" })).toMatchObject({ ok: false, code: "failed" });
	});
});

describe("serialization", () => {
	test("changes sent back to back run in order, one at a time", async () => {
		await boot();
		const replies = await Promise.all([
			c.request(frame("set-thinking", { level: "low" })),
			c.request(frame("set-mode", { mode: "plan", action: "enter" })),
			c.request(frame("set-model", { provider: "openai", id: "gpt-6.1-sol" })),
			c.request(frame("rename", { name: "after" })),
		]);
		expect(replies.every(r => r.ok === true)).toBe(true);
		expect(host.thinkingCalls).toEqual(["low"]);
		expect(host.name).toBe("after");
		expect((await info()).model.id).toBe(MODEL_B.id);
	});

	test("the commands write nothing about their arguments to events.log", async () => {
		await boot();
		await call("compact", { instructions: "TOPSECRET-instructions" });
		await call("set-mode", { mode: "goal", action: "enter", objective: "TOPSECRET-objective" });
		host.compactBehaviour = "throw";
		await call("compact", { instructions: "TOPSECRET-instructions" });
		const log = (await Bun.file(`${host.dir}/events.log`).exists()) ? await Bun.file(`${host.dir}/events.log`).text() : "";
		expect(log).not.toContain("TOPSECRET");
		expect(log).not.toContain("/home/secret");
	});
});
