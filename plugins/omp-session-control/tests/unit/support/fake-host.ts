// A stand-in for the parts of omp that the extension touches, so the extension can be exercised without omp.
// The real-omp checks live in tests/integration. Timers behave like omp's ctx.setTimeout: contained and unref'd.
import * as fs from "node:fs";
import * as path from "node:path";
import ompSessionControl from "../../../src/omp-session-control";

type Handler = (event: unknown, ctx: FakeContext) => void | Promise<void>;

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
}

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
