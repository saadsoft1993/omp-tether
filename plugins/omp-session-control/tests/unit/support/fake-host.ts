// A stand-in for the parts of omp that the extension touches, so the extension can be exercised without omp.
// The real-omp checks live in tests/integration. Timers behave like omp's ctx.setTimeout: contained and unref'd.
import * as fs from "node:fs";
import * as path from "node:path";
import ompSessionControl from "../../../src/omp-session-control";

type Handler = (event: unknown, ctx: FakeContext) => unknown;

export interface FakeContext {
	mode: "tui" | "rpc" | "print" | "json";
	agent: { kind: "main" | "sub" };
	sessionManager: { getSessionId(): string };
	ui: { notify(message: string, type?: string): void };
	isIdle(): boolean;
	abort(): void;
	setTimeout(cb: () => void, ms: number): NodeJS.Timeout;
	clearTimer(timer: NodeJS.Timeout): void;
	getPendingPlanReview?(): PlanEvent | undefined;
	resolvePlanReview?(reviewId: string, choice: string, input?: { feedback?: string; role?: string }): boolean;
	compact?(instructions?: string): Promise<void>;
	model?: FakeModel;
	models?: { list(): FakeModel[]; current(): FakeModel | undefined; resolve(spec: string): FakeModel | undefined };
	sessionModes?: {
		getState(): FakeModeState;
		set(request: Record<string, unknown>): Promise<unknown>;
	};
}

export interface FakeModel {
	provider: string;
	id: string;
	name?: string;
	reasoning?: boolean;
	thinking?: { efforts: string[] };
}
export type FakeModeState = Record<"plan" | "vibe" | "goal" | "loop", { status: string }>;

/** Which of omp's session-command APIs the fake host has. `true` gives all of them. */
export interface SessionApis {
	compact?: boolean;
	models?: boolean;
	setModel?: boolean;
	thinking?: boolean;
	modes?: boolean;
}

export const MODEL_A: FakeModel = { provider: "anthropic", id: "claude-opus-5-5", name: "Claude Opus 5.5", reasoning: true, thinking: { efforts: ["minimal", "low", "medium", "high"] } };
export const MODEL_B: FakeModel = { provider: "openai", id: "gpt-6.1-sol", name: "GPT 6.1 Sol", reasoning: true, thinking: { efforts: ["low", "high"] } };
export const MODEL_C: FakeModel = { provider: "local", id: "plain", name: "Plain" };

/** omp's PlanReviewRequestedEvent. */
export interface PlanEvent {
	type: "plan_review_requested";
	reviewId: string;
	sessionId: string;
	title: string;
	planFilePath: string;
	planContent: string;
	options: Array<{ id: string; label: string; disabled: boolean }>;
	tiers?: Array<{ role: string; provider: string; model: string; thinkingLevel?: string }>;
}

export const PLAN_OPTIONS = [
	{ id: "execute", label: "Approve and execute", disabled: false },
	{ id: "compact", label: "Approve and compact context", disabled: false },
	{ id: "keep", label: "Approve and keep context (~10K / 200K)", disabled: false },
	{ id: "refine", label: "Refine plan", disabled: false },
	{ id: "save", label: "Save and quit", disabled: false },
];

export interface HostOptions {
	mode?: FakeContext["mode"];
	kind?: "main" | "sub";
	/** Env set while the extension factory runs (it reads settings once, at load). */
	env?: Record<string, string | undefined>;
	/** Directory to use as the control directory; created with `prepare` below if requested. */
	dir?: string;
	/** Create the directory (0700) and the enabled marker before loading. */
	enable?: boolean;
	/** Multiplies every ctx.setTimeout delay, so tests can exercise 10 s deadlines in a fraction of a second. */
	timerScale?: number;
	/** Give the context omp's plan-review methods (getPendingPlanReview, resolvePlanReview). Off by default, like an older omp. */
	planReview?: boolean;
	/** Give the context and pi omp's session-command APIs (compact, models, setModel, thinking, sessionModes). */
	session?: boolean | SessionApis;
}

export class FakeHost {
	sessionId = "01a1b65b-1111-7000-8000-000000000001";
	name: string | undefined;
	busy = false;
	aborts = 0;
	notices: Array<{ message: string; type?: string }> = [];
	timerErrors: unknown[] = [];
	uncaught: unknown[] = [];
	commands = new Map<string, { handler: (args: string, ctx: FakeContext) => Promise<void> }>();
	handlers = new Map<string, Handler[]>();
	/** Optional failures injected into the fake omp. */
	failSetName: "throw" | "reject" | "keep" | undefined;
	failIsIdle = false;
	failAbort = false;
	failSessionId = false;
	/** What the injected getSessionId failure throws: an Error by default, anything for hostile-value tests. */
	failValue: unknown = new Error("injected getSessionId failure");
	/** The host's open plan review, as getPendingPlanReview reports it. */
	pending: PlanEvent | undefined;
	/** Every resolvePlanReview call that reached the host. */
	resolveCalls: Array<{ reviewId: string; choice: string; input: unknown }> = [];
	/** Make the host refuse every answer. */
	refuseAnswers = false;
	/** Keep the review pending after an accepted answer (a compaction runs behind the overlay). */
	keepPendingAfterAnswer = false;
	failGetPending = false;
	/** State and injected behaviour of the fake session-command APIs. */
	currentModel: FakeModel | undefined = MODEL_A;
	available: FakeModel[] = [MODEL_A, MODEL_B, MODEL_C];
	/** Configured model roles, as ctx.models.resolve("@role") answers them. */
	roles: Record<string, FakeModel | undefined> = {};
	thinking: string | undefined = "high";
	thinkingCeiling: string | undefined;
	modes: FakeModeState = { plan: { status: "inactive" }, vibe: { status: "inactive" }, goal: { status: "inactive" }, loop: { status: "inactive" } };
	compactCalls: Array<string | undefined> = [];
	setModelCalls: FakeModel[] = [];
	thinkingCalls: string[] = [];
	modeCalls: Array<Record<string, unknown>> = [];
	/** How the fake compaction ends: resolve, throw, or stay open until `releaseCompact` is called. */
	compactBehaviour: "ok" | "throw" | "hang" | "swallowed" | "late-event" = "ok";
	/** Compactions that really happened (a session_compact event was emitted for each). */
	compactionsDone = 0;
	releaseCompact: (() => void) | undefined;
	setModelResult: boolean | "throw" | "ignore" = true;
	/** While set, setModel waits for it before it changes anything (omp awaits before it mutates). */
	setModelGate: Promise<void> | undefined;
	setThinkingThrows = false;
	modeResult: unknown = undefined;
	modeThrows = false;
	failModels = false;
	private reviewCounter = 0;
	readonly dir: string;
	readonly root: string;
	readonly ctx: FakeContext;
	readonly pi: Record<string, unknown>;
	private timers = new Set<NodeJS.Timeout>();
	private restoreEnv: Array<() => void> = [];
	private onUncaught = (error: unknown) => this.uncaught.push(error);

	private constructor(readonly opts: HostOptions) {
		// Unix socket paths are limited to about 100 bytes: keep the scratch root short.
		this.root = fs.mkdtempSync(path.join("/tmp", "osc-"));
		this.dir = opts.dir ?? path.join(this.root, "ctl");
		this.ctx = {
			mode: opts.mode ?? "tui",
			agent: { kind: opts.kind ?? "main" },
			sessionManager: {
				getSessionId: () => {
					if (this.failSessionId) throw this.failValue;
					return this.sessionId;
				},
			},
			ui: { notify: (message, type) => void this.notices.push({ message, type }) },
			isIdle: () => {
				if (this.failIsIdle) throw new Error("injected isIdle failure");
				return !this.busy;
			},
			abort: () => {
				if (this.failAbort) throw new Error("injected abort failure");
				this.aborts += 1;
				this.busy = false;
			},
			setTimeout: (cb, ms) => {
				const t = setTimeout(() => {
					this.timers.delete(t);
					try {
						cb();
					} catch (error) {
						this.timerErrors.push(error);
					}
				}, ms * (opts.timerScale ?? 1));
				t.unref();
				this.timers.add(t);
				return t;
			},
			clearTimer: t => {
				clearTimeout(t);
				this.timers.delete(t);
			},
		};
		if (opts.planReview) {
			this.ctx.getPendingPlanReview = () => {
				if (this.failGetPending) throw new Error("injected getPendingPlanReview failure");
				return this.pending ? structuredClone(this.pending) : undefined;
			};
			// The same refusals as omp's interactive-mode resolvePlanReview.
			this.ctx.resolvePlanReview = (reviewId, choice, input) => {
				this.resolveCalls.push({ reviewId, choice, input });
				const p = this.pending;
				if (this.refuseAnswers || !p || p.reviewId !== reviewId) return false;
				const option = p.options.find(o => o.id === choice);
				if (!option || option.disabled) return false;
				if (choice === "refine" && !input?.feedback?.trim()) return false;
				if (input?.role !== undefined) {
					const approves = choice === "execute" || choice === "compact" || choice === "keep";
					if (!approves || !p.tiers?.some(t => t.role === input.role)) return false;
				}
				if (!this.keepPendingAfterAnswer) this.pending = undefined;
				return true;
			};
		}
		const apis: SessionApis = opts.session === true ? { compact: true, models: true, setModel: true, thinking: true, modes: true } : (opts.session ?? {});
		if (apis.compact) {
			this.ctx.compact = async instructions => {
				this.compactCalls.push(instructions);
				if (this.compactBehaviour === "throw") throw new Error("injected compact failure: /home/secret/path");
				if (this.compactBehaviour === "hang") await new Promise<void>(resolve => (this.releaseCompact = resolve));
				// omp's interactive controller swallows a failed or cancelled compaction: ctx.compact() resolves, and
				// no session_compact event follows. "swallowed" is that; the others emit the event as omp does.
				if (this.compactBehaviour === "swallowed") return;
				const emitDone = async () => {
					this.compactionsDone += 1;
					await this.emit("session_compact", { type: "session_compact", compactionEntry: { type: "compaction" }, fromExtension: false });
				};
				if (this.compactBehaviour === "late-event") setTimeout(() => void emitDone(), 30);
				else await emitDone();
			};
		}
		if (apis.models) {
			this.ctx.models = {
				list: () => {
					if (this.failModels) throw new Error("injected models failure");
					return structuredClone(this.available);
				},
				current: () => this.currentModel,
				resolve: spec => (spec.startsWith("@") ? structuredClone(this.roles[spec.slice(1)]) : undefined),
			};
			Object.defineProperty(this.ctx, "model", { get: () => this.currentModel, enumerable: true });
		}
		if (apis.modes) {
			this.ctx.sessionModes = {
				getState: () => structuredClone(this.modes),
				set: async request => {
					this.modeCalls.push(request);
					if (this.modeThrows) throw new Error("injected set failure");
					if (this.modeResult !== undefined) return this.modeResult;
					const key = request.mode as keyof FakeModeState;
					const status = request.action === "enter" || request.action === "resume" ? "active" : request.action === "pause" ? "paused" : "inactive";
					const changed = this.modes[key].status !== status;
					this.modes[key].status = status;
					return { ok: true, changed, state: structuredClone(this.modes) };
				},
			};
		}
		this.pi = {
			on: (event: string, handler: Handler) => {
				this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
			},
			registerCommand: (name: string, options: { handler: (args: string, ctx: FakeContext) => Promise<void> }) => {
				this.commands.set(name, options);
			},
			getSessionName: () => this.name,
			setSessionName: async (name: string) => {
				if (this.failSetName === "throw") throw new Error("injected setSessionName failure");
				if (this.failSetName === "reject") return Promise.reject(new Error("injected rejection"));
				if (this.failSetName !== "keep") this.name = name;
			},
			pi: { settings: {} },
		};
		if (apis.setModel) {
			this.pi.setModel = async (model: FakeModel) => {
				this.setModelCalls.push(model);
				await this.setModelGate;
				if (this.setModelResult === "throw") throw new Error("injected setModel failure: /home/secret/path");
				if (this.setModelResult === true) this.currentModel = this.available.find(m => m.provider === model.provider && m.id === model.id);
				return this.setModelResult === "ignore" ? true : this.setModelResult;
			};
		}
		if (apis.thinking) {
			this.pi.getThinkingLevel = () => this.thinking;
			this.pi.setThinkingLevel = (level: string) => {
				this.thinkingCalls.push(level);
				if (this.setThinkingThrows) throw new Error("injected setThinkingLevel failure");
				this.thinking = this.thinkingCeiling ?? level;
			};
		}
	}

	static async start(opts: HostOptions = {}): Promise<FakeHost> {
		const host = new FakeHost(opts);
		if (opts.enable) {
			fs.mkdirSync(host.dir, { recursive: true, mode: 0o700 });
			fs.chmodSync(host.dir, 0o700);
			fs.writeFileSync(path.join(host.dir, "enabled"), "");
		}
		process.on("uncaughtException", host.onUncaught);
		process.on("unhandledRejection", host.onUncaught);
		const env = { OMP_SESSION_CONTROL_DIR: host.dir, OMP_SESSION_CONTROL_IDLE_MS: undefined, ...opts.env };
		for (const [key, value] of Object.entries(env)) {
			const old = process.env[key];
			host.restoreEnv.push(() => (old === undefined ? delete process.env[key] : (process.env[key] = old)));
			if (value === undefined) delete process.env[key];
			else process.env[key] = value;
		}
		await ompSessionControl(host.pi as never);
		return host;
	}

	/** Timers scheduled through ctx.setTimeout that have neither fired nor been cleared. */
	pendingTimers(): number {
		return this.timers.size;
	}

	async fire(event: string): Promise<void> {
		for (const handler of this.handlers.get(event) ?? []) await handler({ type: event }, this.ctx);
	}

	/** Opens a review in the fake host and delivers plan_review_requested, as omp does (pending is set first). */
	async openReview(over: Partial<PlanEvent> = {}, deliver = true): Promise<PlanEvent> {
		this.reviewCounter += 1;
		const event: PlanEvent = {
			type: "plan_review_requested",
			reviewId: `review-${this.reviewCounter}`,
			sessionId: this.sessionId,
			title: "Add a widget",
			planFilePath: "local://PLAN.md",
			planContent: "# Plan\n\n1. Do it\n",
			options: structuredClone(PLAN_OPTIONS),
			...over,
		};
		this.pending = event;
		if (deliver) await this.emit("plan_review_requested", structuredClone(event));
		return event;
	}

	/** Delivers plan_review_resolved for `event`; the host's own pending review is left alone. */
	async resolveEvent(event: { reviewId: string; sessionId: string }, choice: string | undefined = undefined, by = "local"): Promise<void> {
		await this.emit("plan_review_resolved", { type: "plan_review_resolved", reviewId: event.reviewId, sessionId: event.sessionId, choice, by });
	}

	async emit(event: string, payload: unknown): Promise<void> {
		for (const handler of this.handlers.get(event) ?? []) await handler(payload, this.ctx);
	}

	/** Delivers a cancellable `session_before_*` event; true when a handler answered `{cancel: true}`. */
	async cancelled(event: string, payload: Record<string, unknown> = {}): Promise<boolean> {
		let cancel = false;
		for (const handler of this.handlers.get(event) ?? []) {
			const result = (await handler({ type: event, ...payload }, this.ctx)) as { cancel?: boolean } | undefined;
			if (result?.cancel === true) cancel = true;
		}
		return cancel;
	}

	async run(command: string, args = ""): Promise<string> {
		const before = this.notices.length;
		await this.commands.get(command)?.handler(args, this.ctx);
		return this.notices.slice(before).map(n => n.message).join("\n");
	}

	registry(): { file: string; data: Record<string, unknown> } | undefined {
		if (!fs.existsSync(this.dir)) return undefined;
		const files = fs.readdirSync(this.dir).filter(f => f.endsWith(".json") && !f.includes(".tmp"));
		if (files.length !== 1) return undefined;
		const file = path.join(this.dir, files[0] as string);
		return { file, data: JSON.parse(fs.readFileSync(file, "utf8")) };
	}

	async waitFor<T>(fn: () => T | undefined | false | null, what: string, timeoutMs = 5000): Promise<T> {
		const deadline = Date.now() + timeoutMs;
		for (;;) {
			const value = fn();
			if (value) return value;
			if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
			await new Promise(r => setTimeout(r, 25));
		}
	}

	async dispose(): Promise<void> {
		await this.fire("session_shutdown").catch(() => {});
		for (const t of this.timers) clearTimeout(t);
		for (const restore of this.restoreEnv) restore();
		process.off("uncaughtException", this.onUncaught);
		process.off("unhandledRejection", this.onUncaught);
		fs.rmSync(this.root, { recursive: true, force: true });
	}
}
