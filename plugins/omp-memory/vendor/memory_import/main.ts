/**
 * Memory importer: Claude Code memory, the Hindsight remainder and claude-mem session summaries into omp's local
 * mnemopi banks, through the Mnemopi library (the published `@oh-my-pi/pi-mnemopi` at omp's exact version). Run with
 * Bun 1.4.4 or newer:
 *
 *   bun cli.ts <cmd> --source <claude-memory|hindsight|claude-mem-summaries> [flags]
 *
 * cmds: setup | library | scan | dry-run | import --apply | verify | undo --run ID [--apply] | doctor | project-map
 * See README.md. Exit status: 0 success; 1 a refusal (a gate, a check, the network) or a failed check; 2 wrong flags or an
 * unusable environment; 3 an unexpected failure.
 */
import { readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { runDoctor } from "./doctor";
import { checkOutputs, collect, knownFolders, type Options, runImport, runScan, runUndo, runVerify } from "./engine";
import { normaliseFolder } from "./folders";
import { readOmpVersion, resolveLibrary, setupCommand } from "./lib";
import { DEFAULT_LEDGER } from "./ledger";
import { newRunId, Refusal, type SourceMode, tildeText, UsageError } from "./model";
import { PROJECT_MAP_FORMAT, writeOutput } from "./outputs";
import { readSelfTest } from "./selftest";
import { runSetup } from "./setup";
import { describeFailure, emit } from "./sink";
import { draftProjectMap } from "./sources/hindsight";

const MODES: SourceMode[] = ["claude-memory", "hindsight", "claude-mem-summaries"];
const COMMANDS = ["setup", "library", "scan", "dry-run", "import", "verify", "undo", "doctor", "project-map"] as const;
type Command = (typeof COMMANDS)[number];

const HOME = homedir();
const FLAGS_WITH_VALUE = new Set([
	"source",
	"banks-dir",
	"ledger",
	"out",
	"run-id",
	"run",
	"global-allowlist",
	"lib-dir",
	"omp-bin",
	"omp-version",
	"registry",
	"folder-roots",
	"embedding-model",
	"claude-home",
	"claude-mem-db",
	"hindsight-export",
	"hindsight-ledger",
	"project-map",
	"python",
	"sqlite3",
	"conf",
	"release-held",
	"banks",
	"expect-plan-digest",
	"backed-up-banks",
	"exempt-banks",
]);
const BOOLEAN_FLAGS = new Set(["apply", "force-closed", "skip-unassigned", "only-new-banks", "dry-run", "no-embeddings", "json", "help", "no-plan-check", "no-backup-check"]);

export function parseArgs(argv: string[]): { command: Command; flags: Map<string, string | true> } {
	const [first, ...rest] = argv;
	if (!first || first === "--help" || !(COMMANDS as readonly string[]).includes(first)) {
		throw new UsageError(`usage: cli.ts <${COMMANDS.join("|")}> --source <${MODES.join("|")}> [flags]; see README.md`);
	}
	const flags = new Map<string, string | true>();
	for (let index = 0; index < rest.length; index += 1) {
		const arg = rest[index] ?? "";
		if (!arg.startsWith("--")) throw new UsageError(`unexpected argument ${arg}`);
		const [name, inline] = arg.slice(2).split(/=(.*)/s, 2) as [string, string | undefined];
		if (flags.has(name)) throw new UsageError(`--${name} was given more than once`);
		if (BOOLEAN_FLAGS.has(name)) {
			if (inline !== undefined) throw new UsageError(`--${name} takes no value (leave it out to turn it off)`);
			flags.set(name, true);
		} else if (FLAGS_WITH_VALUE.has(name)) {
			const value = inline ?? rest[(index += 1)];
			if (value === undefined) throw new UsageError(`--${name} needs a value`);
			flags.set(name, value);
		} else throw new UsageError(`unknown flag --${name}`);
	}
	return { command: first as Command, flags };
}

const expand = (value: string): string => (value.startsWith("~/") ? path.join(HOME, value.slice(2)) : path.resolve(value));

/**
 * `--release-held KEY[,KEY...]`, or a file with one originKey per line (`#` comments). A value is read as a file only when it
 * is written as a path (`/...`, `~/...`, `./...`, `../...`) and names a regular file; anything else is a list of keys.
 */
export function releaseList(value: string | undefined): string[] | undefined {
	if (value === undefined) return undefined;
	let text = value.replaceAll(",", "\n");
	if (/^(\/|~\/|\.\.?\/)/.test(value)) {
		try {
			const file = expand(value);
			if (statSync(file).isFile()) text = readFileSync(file, "utf8");
		} catch {
			// not a file: the value is a key
		}
	}
	return text
		.split("\n")
		.map(line => line.trim())
		.filter(line => line !== "" && !line.startsWith("#"))
		.flatMap(key => (key.startsWith("~/") ? homeForms().map(home => path.join(home, key.slice(2))) : [key]));
}

/** $HOME as written and by its real path: the forms in which a `~/` key printed by dry-run can name a folder. */
function homeForms(): string[] {
	let real = HOME;
	try {
		real = realpathSync(HOME);
	} catch {
		// shown as written
	}
	return [...new Set([HOME, real])];
}

export function buildOptions(command: Command, flags: Map<string, string | true>): Options {
	const str = (name: string): string | undefined => {
		const value = flags.get(name);
		return typeof value === "string" ? value : undefined;
	};
	const path_ = (name: string, fallback?: string): string | undefined => {
		const value = str(name);
		return value === undefined ? fallback : expand(value);
	};
	const mode = (str("source") ?? "claude-memory") as SourceMode;
	if (!MODES.includes(mode)) throw new UsageError(`--source must be one of ${MODES.join(", ")}`);
	const options: Options = {
		command: command as Options["command"],
		mode,
		banksDir: path_("banks-dir", path.join(HOME, ".omp", "agent", "memories", "mnemopi", "banks")) as string,
		ledgerFile: path_("ledger", DEFAULT_LEDGER) as string,
		out: path_("out"),
		apply: flags.get("apply") === true,
		runId: str("run-id") ?? newRunId(),
		targetRun: str("run") ?? str("run-id"),
		forceClosed: flags.get("force-closed") === true,
		globalAllowlist: path_("global-allowlist"),
		libDir: path_("lib-dir", process.env.OMP_MEMORY_LIB_DIR),
		ompBin: str("omp-bin") ?? process.env.OMP_BIN ?? "omp",
		confFile: path_("conf", process.env.OMP_MEMORY_BANKS_CONF),
		folderRoots: str("folder-roots")
			?.split(",")
			.map(root => root.trim())
			.filter(root => root !== "")
			.map(expand),
		releaseHeld: releaseList(str("release-held")),
		embeddingModel: str("embedding-model"),
		skipUnassigned: flags.get("skip-unassigned") === true,
		onlyNewBanks: flags.get("only-new-banks") === true,
		banks: str("banks")?.split(",").map(name => name.trim()).filter(name => name !== ""),
		expectPlanDigest: str("expect-plan-digest"),
		noPlanCheck: flags.get("no-plan-check") === true,
		backedUpBanks: str("backed-up-banks")?.split(",").map(name => name.trim()).filter(name => name !== ""),
		exemptBanks: str("exempt-banks")?.split(",").map(name => name.trim()).filter(name => name !== ""),
		noBackupCheck: flags.get("no-backup-check") === true,
		claudeHome: path_("claude-home", HOME) as string,
		claudeMemDb: path_("claude-mem-db", path.join(HOME, ".claude-mem", "claude-mem.db")) as string,
		hindsightExport: path_("hindsight-export"),
		hindsightLedger: path_("hindsight-ledger", path.join(HOME, ".config", "omp-memory-import", "hindsight-ledger.json")) as string,
		projectMap: path_("project-map"),
		python: str("python"),
		sqlite3: str("sqlite3"),
	};
	if (command !== "setup" && command !== "library" && command !== "doctor") checkOutputs(options);
	return options;
}

function print(value: unknown): void {
	emit.json(value);
}

async function main(argv: string[]): Promise<number> {
	const { command, flags } = parseArgs(argv);
	const options = buildOptions(command, flags);
	const str = (name: string): string | undefined => {
		const value = flags.get(name);
		return typeof value === "string" ? value : undefined;
	};
	switch (command) {
		case "setup": {
			const report = await runSetup({
				ompBin: options.ompBin,
				ompVersion: str("omp-version"),
				libDir: options.libDir,
				registry: str("registry") ?? process.env.OMP_MEMORY_NPM_REGISTRY,
				dryRun: flags.get("dry-run") === true,
				embeddings: flags.get("no-embeddings") !== true,
				embeddingModel: options.embeddingModel,
				say: line => emit.err(tildeText(line)),
			});
			emit.json(report, { tilde: tildeText });
			return report.ok ? 0 : 1;
		}
		case "library": {
			const ompVersion = readOmpVersion(options.ompBin);
			const resolution = ompVersion === null ? null : resolveLibrary(ompVersion, { libDir: options.libDir });
			print({
				bun: { path: process.execPath, version: Bun.version },
				ompBin: options.ompBin,
				ompVersion,
				location: resolution?.location ?? null,
				candidates: resolution?.candidates ?? [],
				setupCommand: setupCommand(options.ompBin),
				// read-only: what an earlier setup, `doctor` or write command cached for this exact library
				selfTest: resolution?.location ? readSelfTest(resolution.location.pkgDir, resolution.location.version) : null,
			});
			return resolution?.location ? 0 : 1;
		}
		case "scan": {
			const report = await runScan(options);
			print(report);
			return 0;
		}
		case "dry-run":
		case "import": {
			if (command === "import" && options.apply) {
				// said before the first write, so that a run that is stopped half way can still be undone by its id
				emit.err(`import run ${options.runId} starts; if it stops before it reports, check and remove its rows with: undo --run ${options.runId} --apply`);
				if (options.noPlanCheck) emit.err("warning: --no-plan-check: the plan this run executes is not compared with the dry run you saw");
				if (options.noBackupCheck) emit.err("warning: --no-backup-check: banks that already exist are written without a check that a verified backup covers them");
			}
			const report = await runImport(options);
			print(report);
			if (command === "dry-run") {
				for (const item of report.held) emit.err(tildeText(`held: ${item.originKey}\t${item.kinds.join(",")}`));
				for (const item of report.binary) emit.err(tildeText(`skipped (binary): ${item.originKey}\t${item.reason}`));
				for (const item of report.tooLarge) emit.err(tildeText(`skipped (too large): ${item.originKey}\t${item.reason}`));
			}
			return report.counts.errors > 0 && command === "import" ? 1 : 0;
		}
		case "verify": {
			const report = await runVerify(options);
			print(report);
			return report.ok ? 0 : 1;
		}
		case "undo": {
			const report = await runUndo(options);
			print(report);
			if (!options.apply) emit.err("dry run: nothing was removed; pass --apply to remove these rows");
			return report.problems.length > 0 ? 1 : 0;
		}
		case "doctor": {
			const report = await runDoctor({ libDir: options.libDir, ompBin: options.ompBin, banksDir: options.banksDir });
			print(report);
			return report.library.ok && report.selfTest?.ok === true && report.schemaMismatches === 0 && report.bankIdMismatches === 0 ? 0 : 1;
		}
		case "project-map": {
			if (!options.hindsightExport) throw new UsageError("project-map needs --hindsight-export FILE");
			const known = new Set(knownFolders(options));
			const claude = await collect({ ...options, mode: "claude-memory" });
			for (const item of claude.items) if (item.cwd !== null) known.add(normaliseFolder(item.cwd).path);
			const draft = draftProjectMap(options.hindsightExport, [...known]);
			if (options.out) writeOutput(options.out, { note: "review before use; null = unassigned", ...draft }, PROJECT_MAP_FORMAT);
			print(draft);
			return 0;
		}
	}
}

/**
 * Runs one command line and returns its exit status: 0 success, 1 a refusal, 2 wrong usage or an unusable environment, 3 an
 * unexpected failure. Whatever fails is shown through the sink (the message only, with the label policy applied).
 */
export async function runCommandLine(argv: string[]): Promise<number> {
	try {
		return await main(argv);
	} catch (error) {
		emit.err(error instanceof Refusal ? `refused: ${describeFailure(error)}` : `unexpected failure: ${describeFailure(error)}`);
		return error instanceof UsageError ? 2 : error instanceof Refusal ? 1 : 3;
	}
}
