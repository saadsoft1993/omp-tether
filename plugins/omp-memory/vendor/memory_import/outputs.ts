/**
 * Where the importer may write, and how text from a source is shown.
 *
 * Reports, sample files and the ledger are written by renaming a temp file over the destination, so a destination that is
 * not checked can replace anything the user can write: a bank database, the ledger, an input of the run. The checks here run
 * before a command reads or writes anything else.
 */
import { lstatSync, readFileSync, realpathSync, type Stats } from "node:fs";
import path from "node:path";
import { carriesSecret } from "./hold";
import { isRecord, Refusal, tildeText, UsageError, writeJsonAtomic } from "./model";
import { hasControlCharacters, indexedLabel, resetLabelIndexes, scrubValue } from "./sink";

/** The marker every file this tool writes carries (`format`); a file is replaced only when it holds one of these. */
export const REPORT_FORMAT = "omp-memory-report/1";
export const SAMPLES_FORMAT = "omp-memory-samples/1";
export const PROJECT_MAP_FORMAT = "omp-memory-project-map/1";
const KNOWN_FORMATS: ReadonlySet<string> = new Set([REPORT_FORMAT, SAMPLES_FORMAT, PROJECT_MAP_FORMAT]);

/** Writes a report, samples file or project-map draft: the value with its format marker first, 0600, atomically. */
export function writeOutput(file: string, value: object, format: string): void {
	writeJsonAtomic(file, scrubValue({ format, ...value }), 0o600);
}

/** Largest existing file that is parsed to see whether it is a previous output of this tool. */
const MAX_EXISTING_BYTES = 64 * 1024 * 1024;

/** The real path of the longest existing prefix of `file`, with the rest appended as written (the file may not exist yet). */
export function resolveLoose(file: string): string {
	const full = path.resolve(file);
	const rest: string[] = [];
	for (let head = full; ; head = path.dirname(head)) {
		try {
			return path.join(realpathSync(head), ...rest);
		} catch {
			if (path.dirname(head) === head) return full;
			rest.unshift(path.basename(head));
		}
	}
}

function inside(dir: string, file: string): boolean {
	const relative = path.relative(dir, file);
	return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

/** Whether `file` lies in `dir`, as written and after resolving every link on both sides. */
export function isInside(dir: string, file: string): boolean {
	return inside(path.resolve(dir), path.resolve(file)) || inside(resolveLoose(dir), resolveLoose(file));
}

/** A parsed file was written by this tool: it carries one of its format markers. */
const isToolOutput = (value: unknown): boolean => isRecord(value) && typeof value.format === "string" && KNOWN_FORMATS.has(value.format);

/** Refuses an existing destination that is not a plain file holding an earlier output of this tool. */
function assertReplaceable(file: string): void {
	let stat: Stats;
	try {
		stat = lstatSync(file);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
		throw new Refusal(`cannot look at ${tildeText(file)}: ${String(error).slice(0, 160)}`);
	}
	const shown = tildeText(file);
	if (stat.isSymbolicLink()) throw new Refusal(`${shown} is a symbolic link; refusing to replace it`);
	if (!stat.isFile()) throw new Refusal(`${shown} exists and is not a regular file; refusing to replace it`);
	let previous = false;
	if (stat.size <= MAX_EXISTING_BYTES) {
		try {
			previous = isToolOutput(JSON.parse(readFileSync(file, "utf8")));
		} catch {
			// not JSON
		}
	}
	if (!previous) throw new Refusal(`${shown} exists and does not carry the format marker of this tool's reports, samples files or project-map drafts; refusing to replace it. Name a new file, or remove it first`);
}

/** The file written next to `--out` by a dry run. */
export const samplesPathFor = (out: string): string => `${out.replace(/\.json$/, "")}.samples.json`;

const secretBanks = new Set<string>();

/** Starts a command: the numbering of withheld labels begins again and no bank is known to be secret-bearing. */
export function resetLabels(): void {
	resetLabelIndexes();
	secretBanks.clear();
}

/** A label derived from a source string (a project name, a folder) as a report may show it: withheld when the RAW string carries a secret or a control character. */
export const safeLabel = (kind: string, text: string): string => (hasControlCharacters(text) || carriesSecret(text) ? indexedLabel(kind, text) : text);

/** Records that a bank id derives from a folder whose RAW path carries a secret: its id is never shown. */
export function markSecretBank(bank: string): void {
	secretBanks.add(bank);
}

/**
 * A bank id as a report names it. The FULL raw name is classified first (an existing bank called `password-123456` has no hash
 * suffix to strip); the id without its trailing hash, and what planning found in the folder it derives from, only add to that,
 * never take away from it.
 */
export const safeBankLabel = (bank: string): string =>
	secretBanks.has(bank) || hasControlCharacters(bank) || carriesSecret(bank) || carriesSecret(bank.replace(/-[0-9a-z]+$/, "")) ? indexedLabel("bank", bank) : bank;

export interface OutputPlan {
	command: string;
	banksDir: string;
	ledgerFile: string;
	out?: string;
	/** Files the run reads: a report must never replace one of them. */
	inputs: (string | undefined)[];
}

/**
 * Every file a command is going to write must lie outside the banks directory (database files and their sidecars live
 * there), must not be one of the run's inputs, and when it exists must be a regular file that holds an earlier output.
 */
export function assertOutputsSafe(plan: OutputPlan): void {
	const notInBanks = (file: string, what: string): void => {
		if (isInside(plan.banksDir, file)) throw new UsageError(`${what} ${tildeText(file)} is inside the banks directory ${tildeText(plan.banksDir)}; refusing to write there`);
	};
	if (plan.command !== "scan" && plan.command !== "project-map") notInBanks(plan.ledgerFile, "the ledger");
	if (plan.out === undefined) return;
	const outputs = [plan.out, ...(plan.command === "dry-run" ? [samplesPathFor(plan.out)] : [])];
	const protectedFiles = [...plan.inputs, plan.ledgerFile].filter((file): file is string => file !== undefined).map(resolveLoose);
	for (const file of outputs) {
		notInBanks(file, "the output file");
		if (protectedFiles.includes(resolveLoose(file))) throw new UsageError(`the output file ${tildeText(file)} is also the ledger or an input of this run; refusing to replace it`);
		assertReplaceable(file);
	}
}

export { hasControlCharacters, indexedLabel, printable } from "./sink";
