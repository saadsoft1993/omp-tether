// The slash command, end to end against a fake command line tool: what it runs, what it shows, what it never does.
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import ompServices, { errorText, formatDoctor, locateCli, runCli } from "../../src/omp-services";

interface Notice {
	message: string;
	type: string;
}
type Handler = (args: string, ctx: unknown) => Promise<void>;

const dirs: string[] = [];
const saved: Record<string, string | undefined> = { OMP_SERVICES_BIN: process.env.OMP_SERVICES_BIN, BUN_BE_BUN: process.env.BUN_BE_BUN };
afterEach(() => {
	for (const d of dirs.splice(0)) fs.rmSync(d, { recursive: true, force: true });
	for (const [k, v] of Object.entries(saved)) {
		if (v === undefined) delete process.env[k];
		else process.env[k] = v;
	}
});

function scratch(): string {
	const d = fs.mkdtempSync(path.join(os.tmpdir(), "osx-"));
	dirs.push(d);
	return d;
}

/** A fake `omp-services`: logs its arguments (and BUN_BE_BUN) and answers from files next to it. */
function fakeCli(answers: Record<string, { stdout?: string; stderr?: string; code?: number; sleep?: number }>): { cli: string; calls: () => string[] } {
	const dir = scratch();
	const log = path.join(dir, "calls.log");
	const body = Object.entries(answers)
		.map(([key, a]) => {
			fs.writeFileSync(path.join(dir, `${key.replace(/\W/g, "_")}.out`), a.stdout ?? "");
			fs.writeFileSync(path.join(dir, `${key.replace(/\W/g, "_")}.err`), a.stderr ?? "");
			return `  "${key}") ${a.sleep ? `sleep ${a.sleep}; ` : ""}cat "${dir}/${key.replace(/\W/g, "_")}.out"; cat "${dir}/${key.replace(/\W/g, "_")}.err" >&2; exit ${a.code ?? 0} ;;`;
		})
		.join("\n");
	const script = `#!/bin/sh\necho "$* BUN_BE_BUN=\${BUN_BE_BUN:-}" >> "${log}"\ncase "$*" in\n${body}\n  *) echo "unexpected: $*" >&2; exit 9 ;;\nesac\n`;
	const cli = path.join(dir, "omp-services");
	fs.writeFileSync(cli, script, { mode: 0o755 });
	return { cli, calls: () => (fs.existsSync(log) ? fs.readFileSync(log, "utf8").trim().split("\n") : []) };
}

async function load(): Promise<{ run: Handler; commands: string[] }> {
	const commands: string[] = [];
	let handler: Handler | undefined;
	await ompServices({
		registerCommand: (name: string, options: { handler: Handler }) => {
			commands.push(name);
			handler = options.handler;
		},
	} as never);
	return { run: handler as Handler, commands };
}

function ctx(over: { confirm?: boolean | (() => never); hasUI?: boolean } = {}): { c: unknown; notices: Notice[]; confirms: string[] } {
	const notices: Notice[] = [];
	const confirms: string[] = [];
	const c = {
		hasUI: over.hasUI ?? true,
		ui: {
			notify: (message: string, type = "info") => void notices.push({ message, type }),
			confirm: async (title: string, message: string) => {
				confirms.push(`${title}\n${message}`);
				if (typeof over.confirm === "function") return over.confirm();
				return over.confirm ?? false;
			},
		},
	};
	return { c, notices, confirms };
}

const DOCTOR_JSON = JSON.stringify({
	version: "0.1.0",
	summary: { ok: 5, info: 1, warn: 1, fail: 1 },
	findings: [
		{ id: "python.version", level: "ok", message: "Python 3.14" },
		{ id: "heartbeat", level: "info", message: "off" },
		{ id: "unit.x", level: "fail", message: "loaded but not running", hint: "look at the log" },
		{ id: "dropin.health.p", level: "warn", message: "something" },
	],
});

describe("registration", () => {
	test("registers exactly one command and nothing else; installing changes nothing", async () => {
		const { commands } = await load();
		expect(commands).toEqual(["omp-services"]);
	});
});

describe("/omp-services doctor", () => {
	test("runs doctor --json and shows failures first, with hints, at error level", async () => {
		const { cli, calls } = fakeCli({ "doctor --json": { stdout: DOCTOR_JSON, code: 1 } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const { c, notices } = ctx();
		await run("doctor", c);
		expect(calls()).toEqual(["doctor --json BUN_BE_BUN="]);
		expect(notices).toHaveLength(1);
		const text = notices[0]?.message ?? "";
		expect(notices[0]?.type).toBe("error");
		expect(text.indexOf("fail unit.x")).toBeLessThan(text.indexOf("warn dropin.health.p"));
		expect(text).toContain("-> look at the log");
		expect(text).not.toContain("python.version");
		expect(text).toContain("5 ok, 1 info, 1 warning(s), 1 failure(s)");
	});

	test("a healthy machine says there is nothing to fix", () => {
		const shown = formatDoctor(JSON.stringify({ version: "0.1.0", summary: { ok: 3, info: 0, warn: 0, fail: 0 }, findings: [{ id: "a", level: "ok", message: "m" }] }));
		expect(shown.level).toBe("info");
		expect(shown.text).toContain("Nothing to fix.");
	});

	test("garbage output, a crash and a timeout become messages, never throws", async () => {
		expect(formatDoctor("not json").level).toBe("error");
		const bad = fakeCli({ "doctor --json": { stdout: "oops", code: 2, stderr: "boom" } });
		process.env.OMP_SERVICES_BIN = bad.cli;
		const { run } = await load();
		const a = ctx();
		await run("doctor", a.c);
		expect(a.notices[0]?.type).toBe("error");
		expect(a.notices[0]?.message).toContain("status 2");
	});

	test("the child never sees BUN_BE_BUN", async () => {
		const { cli, calls } = fakeCli({ "doctor --json": { stdout: DOCTOR_JSON } });
		process.env.OMP_SERVICES_BIN = cli;
		process.env.BUN_BE_BUN = "1";
		const { run } = await load();
		await run("doctor", ctx().c);
		expect(calls()[0]).toBe("doctor --json BUN_BE_BUN=");
	});
});

describe("/omp-services setup", () => {
	const PLAN_TEXT = "omp-services init: darwin, launchd\n\n  create ~/.config/omp-services\n\nnote: x\n";
	const DIGEST = "a".repeat(64);
	const planJson = (plan = PLAN_TEXT, changes = true) => JSON.stringify({ plan, digest: DIGEST, changes });
	const PLAN = planJson();
	const APPLY = `init --yes --expect-plan ${DIGEST}`;

	test("shows the plan, and only after a yes applies it", async () => {
		const { cli, calls } = fakeCli({ "init --plan --json": { stdout: PLAN }, [APPLY]: { stdout: "done: a\ndone: b\n\nDone." } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({ confirm: true });
		await run("setup", a.c);
		expect(a.confirms).toHaveLength(1);
		expect(a.confirms[0]).toContain("create ~/.config/omp-services");
		expect(a.confirms[0]).toContain("Apply exactly this plan?");
		expect(calls()).toEqual(["init --plan --json BUN_BE_BUN=", `${APPLY} BUN_BE_BUN=`]);
		expect(a.notices.at(-1)?.message).toContain("setup finished");
	});

	test("a no, or anything but an explicit yes, changes nothing", async () => {
		for (const answer of [false, undefined as unknown as boolean]) {
			const { cli, calls } = fakeCli({ "init --plan --json": { stdout: PLAN }, [APPLY]: { stdout: "SHOULD NOT RUN" } });
			process.env.OMP_SERVICES_BIN = cli;
			const { run } = await load();
			const a = ctx({ confirm: answer });
			await run("setup", a.c);
			expect(calls()).toEqual(["init --plan --json BUN_BE_BUN="]);
			expect(a.notices.at(-1)?.message).toContain("nothing was changed");
		}
	});

	test("a confirmation that throws is a message and changes nothing", async () => {
		const { cli, calls } = fakeCli({ "init --plan --json": { stdout: PLAN } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({
			confirm: () => {
				throw new Error("dialog closed");
			},
		});
		await run("setup", a.c);
		expect(calls()).toEqual(["init --plan --json BUN_BE_BUN="]);
		expect(a.notices.at(-1)?.type).toBe("error");
	});

	test("without a UI there is nobody to ask, so nothing runs", async () => {
		const { cli, calls } = fakeCli({ "init --plan --json": { stdout: PLAN } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({ hasUI: false });
		await run("setup", a.c);
		expect(calls()).toEqual([]);
		expect(a.notices[0]?.message).toContain("interactive");
	});

	test("nothing to do is shown without asking; a failing plan is an error", async () => {
		const done = fakeCli({ "init --plan --json": { stdout: planJson("Nothing to do: everything is already as it should be.\n", false) } });
		process.env.OMP_SERVICES_BIN = done.cli;
		const { run } = await load();
		const a = ctx({ confirm: true });
		await run("setup", a.c);
		expect(a.confirms).toEqual([]);
		const broken = fakeCli({ "init --plan --json": { stderr: "error: nope", code: 2 } });
		process.env.OMP_SERVICES_BIN = broken.cli;
		const b = ctx({ confirm: true });
		await run("setup", b.c);
		expect(b.notices[0]?.type).toBe("error");
		expect(b.confirms).toEqual([]);
	});

	test("a failing apply is reported", async () => {
		const { cli } = fakeCli({ "init --plan --json": { stdout: PLAN }, [APPLY]: { stderr: "error: could not load", code: 2 } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({ confirm: true });
		await run("setup", a.c);
		expect(a.notices.at(-1)?.type).toBe("error");
		expect(a.notices.at(-1)?.message).toContain("could not load");
	});
});

describe("usage, errors and limits", () => {
	test("no argument or an unknown one prints usage and runs nothing", async () => {
		const { cli, calls } = fakeCli({});
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx();
		await run("", a.c);
		await run("rm -rf /", a.c);
		expect(calls()).toEqual([]);
		expect(a.notices.map(n => n.message)).toEqual(["usage: /omp-services doctor | setup", "usage: /omp-services doctor | setup"]);
	});

	test("locateCli prefers an absolute OMP_SERVICES_BIN, ignores a relative one, and falls back to bin/ next to src/", () => {
		const d = scratch();
		fs.mkdirSync(path.join(d, "src"));
		fs.mkdirSync(path.join(d, "bin"));
		const fallback = path.join(d, "bin", "omp-services");
		expect(locateCli({}, path.join(d, "src", "omp-services.ts"))).toBeUndefined();
		fs.writeFileSync(fallback, "#!/bin/sh\n", { mode: 0o755 });
		expect(locateCli({}, path.join(d, "src", "omp-services.ts"))).toBe(fallback);
		expect(locateCli({ OMP_SERVICES_BIN: "relative/bin" }, path.join(d, "src", "omp-services.ts"))).toBe(fallback);
		const other = path.join(d, "other");
		fs.writeFileSync(other, "#!/bin/sh\n", { mode: 0o755 });
		expect(locateCli({ OMP_SERVICES_BIN: other }, path.join(d, "src", "omp-services.ts"))).toBe(other);
		fs.chmodSync(other, 0o644);
		expect(locateCli({ OMP_SERVICES_BIN: other }, path.join(d, "src", "omp-services.ts"))).toBe(fallback);
	});

	test("runCli enforces the deadline and never rejects", async () => {
		const { cli } = fakeCli({ "doctor --json": { stdout: "late", sleep: 5 } });
		const started = Date.now();
		const r = await runCli(cli, ["doctor", "--json"], 300);
		expect(Date.now() - started).toBeLessThan(3000);
		expect(r.failed).toContain("did not finish");
		const missing = await runCli("/nonexistent/tool", [], 1000);
		expect(missing.failed).toBeTruthy();
	});

	test("a command line that ignores SIGTERM is killed after the grace period, and the promise settles", async () => {
		const d = fs.mkdtempSync(path.join(os.tmpdir(), "osv-stubborn-"));
		const cli = path.join(d, "stubborn");
		fs.writeFileSync(cli, "#!/bin/sh\ntrap '' TERM\nexec sleep 30\n", { mode: 0o755 });
		const started = Date.now();
		const r = await runCli(cli, [], 300, process.env, 400);
		expect(Date.now() - started).toBeLessThan(5000);
		expect(r.failed).toContain("did not finish");
		fs.rmSync(d, { recursive: true, force: true });
	});

	test("two commands at once: the second is refused, not queued", async () => {
		const { cli } = fakeCli({ "doctor --json": { stdout: DOCTOR_JSON, sleep: 1 } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx();
		const b = ctx();
		const first = run("doctor", a.c);
		await new Promise(r => setTimeout(r, 100));
		await run("doctor", b.c);
		await first;
		expect(b.notices[0]?.message).toContain("still running");
		expect(a.notices[0]?.message).toContain("omp-services doctor");
	});

	test("a notice is clipped and a hostile error value cannot throw", () => {
		const many = JSON.stringify({ version: "x", summary: { ok: 0, info: 0, warn: 200, fail: 0 }, findings: Array.from({ length: 200 }, (_, i) => ({ id: `w${i}`, level: "warn", message: "m".repeat(80) })) });
		expect(formatDoctor(many).text.length).toBeLessThanOrEqual(3500);
		const hostile = { toString: () => { throw new Error("no"); } };
		expect(errorText(hostile)).toBe("unprintable error");
	});
});

describe("the confirmation is bound to the plan that runs", () => {
	const DIGEST = "b".repeat(64);
	const json = (plan: string, changes = true) => JSON.stringify({ plan, digest: DIGEST, changes });

	test("a long plan is shown whole or not applied at all, never cut", async () => {
		const long = `omp-services init: x\n${"  create /some/long/path/under/the/home/directory/file\n".repeat(300)}`;
		const { cli, calls } = fakeCli({ "init --plan --json": { stdout: json(long) } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({ confirm: true });
		await run("setup", a.c);
		expect(a.confirms).toEqual([]);
		expect(calls()).toEqual(["init --plan --json BUN_BE_BUN="]);
		expect(a.notices.at(-1)?.message).toContain("too long");
	});

	test("a plan of ordinary size is shown in full, including its last line", async () => {
		const plan = `omp-services init: x\n${Array.from({ length: 60 }, (_, i) => `  install  unit u${i}`).join("\n")}\n  record  the end marker line\n`;
		const { cli } = fakeCli({ "init --plan --json": { stdout: json(plan) }, [`init --yes --expect-plan ${DIGEST}`]: { stdout: "Done." } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({ confirm: true });
		await run("setup", a.c);
		expect(a.confirms[0]).toContain("the end marker line");
	});

	test("a plan that changed after it was shown is reported and nothing is claimed done", async () => {
		const { cli } = fakeCli({ "init --plan --json": { stdout: json("plan\n") }, [`init --yes --expect-plan ${DIGEST}`]: { stdout: "the plan changed", code: 5 } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({ confirm: true });
		await run("setup", a.c);
		expect(a.notices.at(-1)?.message).toContain("plan changed");
		expect(a.notices.at(-1)?.message).not.toContain("finished");
	});

	test("the words 'Nothing to do' inside a warning do not skip the confirmation", async () => {
		const { cli } = fakeCli({ "init --plan --json": { stdout: json("create x\nwarning: Nothing to do with this unit\n") }, [`init --yes --expect-plan ${DIGEST}`]: { stdout: "Done." } });
		process.env.OMP_SERVICES_BIN = cli;
		const { run } = await load();
		const a = ctx({ confirm: true });
		await run("setup", a.c);
		expect(a.confirms).toHaveLength(1);
	});

	test("a malformed digest or plan is refused", async () => {
		for (const bad of [JSON.stringify({ plan: "x", digest: "short", changes: true }), "not json", JSON.stringify({ plan: 1, digest: DIGEST, changes: true })]) {
			const { cli, calls } = fakeCli({ "init --plan --json": { stdout: bad } });
			process.env.OMP_SERVICES_BIN = cli;
			const { run } = await load();
			const a = ctx({ confirm: true });
			await run("setup", a.c);
			expect(calls()).toEqual(["init --plan --json BUN_BE_BUN="]);
			expect(a.confirms).toEqual([]);
		}
	});
});
