/**
 * The importer's commands: scan, dry-run, import, verify, undo. See main.ts for the flags and README.md for the rules.
 *
 * Re-running is safe: an item is identified by its originKey, never by content. Per item, in this order:
 * tombstoned in the ledger -> skippedTombstone; the ledger (or the bank's own rows) knows it and the source is
 * unchanged -> skippedUnchanged; known but the source changed -> changedSource (reported, never applied); the same
 * (redacted) content already in that bank or earlier in this run -> skippedDuplicate; held (proxy residual findings or hold rules, unless released) ->
 * skippedHeld; otherwise it is written.
 */
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import {
	type ApplyResult,
	openReadonly,
	applyBank,
	type BankState,
	bankEmbeddingModel,
	bankOpenBy,
	bankPath,
	holdsOnlyImporterContent,
	importerRows,
	type RunRow,
	emptyBankState,
	isImporterRow,
	existingIds,
	forgetRows,
	listBanks,
	readBankState,
	type RowToWrite,
	rowsOfRun,
} from "./banks";
import { createWorkerEmbedder, type Embedder } from "./embedder";
import { projectBankSegment } from "./bank_id";
import { encodeProjectName, normaliseFolder } from "./folders";
import { binaryReason, charsOverLimit, describeTooLarge, MAX_TEXT_CHARS } from "./content_guard";
import { carriesSecret, findHolds, PROVENANCE_SECRET, PROVENANCE_UNPRINTABLE } from "./hold";
import { assertBankWritable, bankExists, inspectLibrary, type Library, type LibraryCheck, requireLibrary } from "./lib";
import { entryKey, type LedgerEntry, type LedgerFile, loadLedger, saveLedger, withImporterLock } from "./ledger";
import { assertOutputsSafe, hasControlCharacters, indexedLabel, markSecretBank, printable, REPORT_FORMAT, resetLabels, safeBankLabel, SAMPLES_FORMAT, samplesPathFor, writeOutput } from "./outputs";
import { requireSelfTest } from "./selftest";
import {
	bump,
	type Collected,
	emptyCounts,
	GLOBAL_BANK,
	MODE_SOURCE,
	newRunId,
	Refusal,
	UsageError,
	type Report,
	type SourceItem,
	type SourceMode,
	sha256,
	snippet,
	UNASSIGNED_BANK,
} from "./model";
import { type Redacted, redactTexts } from "./redact";
import { collectClaudeMemory } from "./sources/claude_memory";
import { collectClaudeMemSummaries } from "./sources/claude_mem";
import { childFolders, collectHindsight, loadProjectMap, workspaceFolders } from "./sources/hindsight";

export interface Options {
	command: "scan" | "dry-run" | "import" | "verify" | "undo";
	mode: SourceMode;
	banksDir: string;
	ledgerFile: string;
	out?: string;
	apply: boolean;
	runId: string;
	/** For `undo` / `verify`: the run to act on. */
	targetRun?: string;
	forceClosed: boolean;
	/** `import --apply`: the `planDigest` of the dry run the user reviewed. Exactly one of this and `noPlanCheck` is required. */
	expectPlanDigest?: string;
	/** `import --apply`: skip the comparison with the dry run's plan (said on stderr by the command line). */
	noPlanCheck?: boolean;
	/** `import --apply`: the banks that a verified backup covers (may be empty). Exactly one of this and `noBackupCheck` is required. */
	backedUpBanks?: string[];
	/** `import --apply`: banks exempt from the backup requirement only while they still hold nothing but this tool's rows (re-checked right before each write). */
	exemptBanks?: string[];
	/** `import --apply`: do not require that an existing bank is covered by a verified backup. */
	noBackupCheck?: boolean;
	globalAllowlist?: string;
	/** An install root (a directory with node_modules/@oh-my-pi/pi-mnemopi) to use instead of the automatic lookup. */
	libDir?: string;
	ompBin: string;
	/** A memory-banks.conf (`<group> <root relative to home>` lines): its roots' folders resolve claude-mem project names. */
	confFile?: string;
	/** Directories whose direct sub-folders are known project folders (resolve claude-mem project names). */
	folderRoots?: string[];
	/** Target only banks whose database does not exist yet. Combines with `banks` as a union. */
	onlyNewBanks?: boolean;
	/** Target only these bank directory names. Unknown names are refused. */
	banks?: string[];
	/** originKeys a reviewer released from the hold stage (they are imported; the findings are still reported). */
	releaseHeld?: string[];
	/** Makes the embedder for a bank (default: a worker process running the library's own embedding). Tests inject one. */
	embedderFactory?: (model: string) => Embedder;
	embeddingModel?: string;
	skipUnassigned: boolean;
	claudeHome: string;
	claudeMemDb: string;
	hindsightExport?: string;
	hindsightLedger: string;
	projectMap?: string;
	python?: string;
	sqlite3?: string;
}

/** An item that is never imported, whatever the rest of the run says: see content_guard.ts. */
export type Rejection = { kind: "binary"; reason: string } | { kind: "too-large"; chars: number };

export interface Prepared {
	item: SourceItem;
	bank: string;
	content: string;
	name: string | null;
	description: string | null;
	residual: string[];
	redactionError: string | null;
	counts: Record<string, number>;
	/** Set for binary or oversize content; such an item is not redacted and its text is not kept. */
	rejection: Rejection | null;
	/**
	 * Set when a string that comes from the source and would be stored or printed (origin, key, folder, extras) carries a
	 * secret or a control character: the hold kind. Such an item is held, listed by an opaque key, and cannot be released.
	 */
	provenance: string | null;
}

/** Binary content first (a binary file of any size is binary), then text longer than the redaction step accepts. */
export function rejectionOf(item: SourceItem): Rejection | null {
	const texts = [item.content, item.name ?? "", item.description ?? ""];
	for (const text of texts) {
		const reason = binaryReason(text);
		if (reason !== null) return { kind: "binary", reason };
	}
	for (const text of texts) {
		const chars = charsOverLimit(text);
		if (chars !== null) return { kind: "too-large", chars };
	}
	return null;
}

/** Folders that are known to exist as projects but may have no Claude session: the conf's roots and `folderRoots`. */
export function knownFolders(options: Pick<Options, "confFile" | "folderRoots" | "claudeHome">): string[] {
	return [
		...(options.confFile ? workspaceFolders(options.confFile, options.claudeHome) : []),
		...(options.folderRoots ?? []).flatMap(root => childFolders(root)),
	];
}

export function collect(options: Options): Promise<Collected> | Collected {
	if (options.mode === "claude-memory") return collectClaudeMemory({ claudeHome: options.claudeHome });
	if (options.mode === "claude-mem-summaries") {
		return collectClaudeMemSummaries({
			dbPath: options.claudeMemDb,
			claudeHome: options.claudeHome,
			sqlite3: options.sqlite3,
			projectMap: loadProjectMap(options.projectMap),
			extraFolders: knownFolders(options),
		});
	}
	if (!options.hindsightExport) throw new UsageError("--hindsight-export FILE is required for --source hindsight");
	return collectHindsight({
		exportFile: options.hindsightExport,
		ledgerFile: options.hindsightLedger,
		projectMap: loadProjectMap(options.projectMap),
	});
}

function readAllowlist(file: string | undefined): Set<string> {
	if (!file) return new Set();
	const lines = readFileSync(file, "utf8");
	return new Set(
		lines
			.split("\n")
			.map(line => line.trim())
			.filter(line => line !== "" && !line.startsWith("#")),
	);
}

/**
 * The bank a folder maps to: omp's `projectBankSegment` of the folder's realpath. omp hashes the path it was
 * started in (`/tmp/x` and `/private/tmp/x` are two banks), so when the recorded, non-realpath form already has a
 * bank and the realpath form does not, that existing bank wins: it is where omp really writes for that folder.
 */
export function bankFor(item: SourceItem, allowlist: Set<string>, banksDir: string): string {
	if (allowlist.has(item.originKey)) return GLOBAL_BANK;
	if (item.cwd === null) return UNASSIGNED_BANK;
	const real = projectBankSegment(normaliseFolder(item.cwd).path);
	if (item.cwdRaw !== null && item.cwdRaw !== item.cwd) {
		const recorded = projectBankSegment(item.cwdRaw);
		if (!bankExists(banksDir, real) && bankExists(banksDir, recorded)) return recorded;
	}
	return real;
}

/**
 * The source-controlled strings that end up in bank metadata, the ledger or a report. The project directory name Claude
 * derives from the folder is left out when the folder is known and encodes to exactly that name: it says nothing the folder
 * path does not.
 */
function provenanceOf(item: SourceItem): string[] {
	const dirName = item.extra.projectDir;
	const derived =
		typeof dirName === "string" && dirName !== "" && [item.cwd, item.cwdRaw].some(folder => folder !== null && encodeProjectName(folder) === dirName) ? dirName : null;
	const own = [item.origin, item.originKey, item.cwd, item.cwdRaw, item.claudeType, ...Object.entries(item.extra).filter(([, value]) => value !== derived).map(([, value]) => value)];
	const strings = own.filter((value): value is string => typeof value === "string");
	return derived === null ? strings : strings.map(text => text.split(`/${derived}/`).join("/(project)/"));
}

/** The home directories whose prefix says nothing about an item: the account's own and the one the source was read from, each as written and by real path. */
export function homePrefixes(options: Pick<Options, "claudeHome">): string[] {
	const forms = new Set<string>();
	for (const home of [options.claudeHome, homedir()]) {
		if (home === "" || home === "/") continue;
		forms.add(home.replace(/\/+$/, ""));
		try {
			forms.add(realpathSync(home).replace(/\/+$/, ""));
		} catch {
			// a home that cannot be resolved is taken as written
		}
	}
	forms.delete("");
	return [...forms].sort((a, b) => b.length - a.length);
}

/** `text` with every home prefix (and the form Claude uses in a project directory name) replaced by `~`: only what lies below home is judged. */
function belowHome(text: string, homes: string[]): string {
	let out = text;
	for (const home of homes) out = out.split(`${home}/`).join("~/").split(encodeProjectName(home)).join("~");
	return out;
}

/** What the redaction step is asked about an item's provenance: the strings below home, one per line. */
const provenanceText = (item: SourceItem, homes: string[]): string => provenanceOf(item).map(text => belowHome(text, homes)).join("\n");

/**
 * The hold kind of an item whose provenance carries a secret or a control character, else null. Every string is judged RAW
 * (see `carriesSecret`), below the home directory: a temporary directory with a random name above it is not a finding. `scanned` is
 * the redaction step's verdict on the same text: a text it changes or cannot process holds the item. Its high-entropy residual
 * does not: a hash-like folder name is ordinary, a long token counts only next to a key, token or secret word
 * (`hold:long-token`) or in a known token shape.
 */
export function provenanceHold(item: SourceItem, homes: string[], scanned?: Redacted): string | null {
	const strings = provenanceOf(item).map(text => belowHome(text, homes));
	if (strings.some(hasControlCharacters)) return PROVENANCE_UNPRINTABLE;
	if (strings.some(carriesSecret)) return PROVENANCE_SECRET;
	if (scanned && (scanned.error !== null || scanned.text !== strings.join("\n"))) return PROVENANCE_SECRET;
	return null;
}

/**
 * Whether the folder an item's bank derives from carries a secret or a control character in its RAW path: decided here, before
 * the bank id's sanitising turns `password=1234` into `password-1234` and takes the context away. Such a bank id is never shown.
 */
function noteSecretBank(item: SourceItem, bank: string, homes: string[]): void {
	const folders = [item.cwd, item.cwdRaw].filter((folder): folder is string => folder !== null).map(folder => belowHome(folder, homes));
	if (folders.some(folder => hasControlCharacters(folder) || carriesSecret(folder))) markSecretBank(bank);
}

/** What a report says instead of the originKey of an item with a provenance hold: `held#N`, N counting such items in the order of this run. */
export const opaqueKey = (item: SourceItem): string => indexedLabel("held", `${item.source}\0${item.originKey}`);

const reportKey = (entry: Prepared): string => (entry.provenance === null ? entry.item.originKey : opaqueKey(entry.item));

/** A string from a source or a system error, for a report: withheld when it carries a secret, else without control characters and shortened. */
const shown = (text: string): string => (carriesSecret(text) ? "(withheld: it looks like a secret)" : printable(text));

async function prepare(options: Options, collected: Collected): Promise<Prepared[]> {
	const allowlist = readAllowlist(options.globalAllowlist);
	const rejections = collected.items.map(rejectionOf);
	const homes = homePrefixes(options);
	const banks = collected.items.map(item => bankFor(item, allowlist, options.banksDir));
	const texts: string[] = [];
	for (const [index, item] of collected.items.entries()) {
		if (rejections[index] === null) texts.push(item.content, item.name ?? "", item.description ?? "");
		else texts.push("", "", ""); // nothing of a rejected item's text reaches the bridge
		texts.push(provenanceText(item, homes));
	}
	const redacted: Redacted[] = await redactTexts(texts, { python: options.python });
	return collected.items.map((item, index) => {
		const [content, name, description, scanned] = [redacted[index * 4], redacted[index * 4 + 1], redacted[index * 4 + 2], redacted[index * 4 + 3]] as [
			Redacted,
			Redacted,
			Redacted,
			Redacted,
		];
		const provenance = provenanceHold(item, homes, scanned);
		noteSecretBank(item, banks[index] ?? "", homes);
		const rejection = rejections[index] ?? null;
		if (rejection !== null) {
			return {
				item,
				bank: bankFor(item, allowlist, options.banksDir),
				content: "",
				name: null,
				description: null,
				residual: provenance === null ? [] : [provenance],
				redactionError: null,
				counts: {},
				rejection,
				provenance,
			};
		}
		const counts: Record<string, number> = {};
		for (const part of [content, name, description]) for (const [kind, n] of Object.entries(part.counts)) bump(counts, kind, n);
		return {
			item,
			bank: bankFor(item, allowlist, options.banksDir),
			content: content.text,
			name: item.name === null ? null : name.text,
			description: item.description === null ? null : description.text,
			residual: [
				...new Set([
					...[content, name, description].flatMap(part => part.residual.map(([kind]) => kind)),
					...findHolds(`${content.text}\n${description.text}`),
					...(provenance === null ? [] : [provenance]),
				]),
			].sort(),
			redactionError: content.error ?? name.error ?? description.error,
			counts,
			rejection: null,
			provenance,
		};
	});
}

/** Whether a bank is a write target: no filter means every bank; the filters combine as a union. */
export function isTargeted(options: Options, bank: string, exists: boolean): boolean {
	if (!options.onlyNewBanks && options.banks === undefined) return true;
	return (options.onlyNewBanks === true && !exists) || (options.banks?.includes(bank) ?? false);
}

type Decision = "import" | "duplicate" | "tombstone" | "unchanged" | "changed" | "residual" | "error" | "skipped-unassigned" | "not-targeted" | "binary" | "too-large";

interface Decided {
	prepared: Prepared;
	decision: Decision;
	/** An existing row without a ledger entry that the ledger should adopt. */
	adopt?: { rowId: string };
}

async function classify(
	lib: Library,
	options: Options,
	prepared: Prepared[],
	ledger: LedgerFile,
): Promise<{ decided: Decided[]; states: Map<string, BankState> }> {
	const states = new Map<string, BankState>();
	const stateOf = async (bank: string): Promise<BankState> => {
		let state = states.get(bank);
		if (!state) {
			state = await readBankState(lib, options.banksDir, bank);
			states.set(bank, state);
		}
		return state;
	};
	const released = new Set(options.releaseHeld ?? []);
	const decided: Decided[] = [];
	const seen = new Set<string>();
	for (const entry of prepared) {
		const { item, bank } = entry;
		const key = entryKey(item.source, item.originKey);
		const known = ledger.entries[key];
		let state: BankState | null = null;
		// What the ledger or the bank already knows about the item.
		let prior: Decided | null = null;
		if (known?.state === "tombstoned") prior = { prepared: entry, decision: "tombstone" };
		else if (known?.state === "imported") {
			const home = await stateOf(known.bank);
			if (!home.exists || !home.rowIds.has(known.rowId)) prior = { prepared: entry, decision: "tombstone" };
			else prior = { prepared: entry, decision: known.hash === item.originHash ? "unchanged" : "changed" };
		} else {
			state = await stateOf(bank);
			const row = state.imported.get(item.originKey);
			if (row) prior = { prepared: entry, decision: row.originHash === item.originHash ? "unchanged" : "changed", adopt: { rowId: row.id } };
		}
		// An item with findings is held before any ledger decision (changed, tombstoned) is reported or sampled; only an
		// unchanged one, which this run neither shows nor writes, keeps its place.
		const held: Decision | null =
			entry.provenance !== null ? "residual" : entry.rejection !== null ? null : entry.redactionError !== null ? "error" : entry.residual.length > 0 && !released.has(item.originKey) ? "residual" : null;
		if (prior !== null && (prior.decision === "unchanged" || held === null)) {
			decided.push(prior);
			continue;
		}
		if (held !== null) {
			decided.push({ prepared: entry, decision: held });
			continue;
		}
		if (entry.rejection !== null) {
			decided.push({ prepared: entry, decision: entry.rejection.kind });
			continue;
		}
		state ??= await stateOf(bank);
		const contentKey = `${bank}\0${entry.content}`;
		if (state.contents.has(entry.content) || seen.has(contentKey)) {
			decided.push({ prepared: entry, decision: "duplicate" });
			continue;
		}
		// A bank this tool created that holds no rows (its rows were undone) is as good as new: nothing in it to protect.
		const emptiedByUs = ledger.banksCreated[bank] !== undefined && state.rowCount === 0;
		if (!isTargeted(options, bank, state.exists && !emptiedByUs)) {
			decided.push({ prepared: entry, decision: "not-targeted" });
			continue;
		}
		if (bank === UNASSIGNED_BANK && options.skipUnassigned) {
			decided.push({ prepared: entry, decision: "skipped-unassigned" });
			continue;
		}
		seen.add(contentKey);
		decided.push({ prepared: entry, decision: "import" });
	}
	return { decided, states };
}

function rowFor(entry: Prepared, runId: string, importedAt: string, id: string): RowToWrite {
	const { item } = entry;
	const metadata: Record<string, unknown> = {
		source: item.source,
		sourceKind: item.sourceKind,
		origin: item.origin,
		originKey: item.originKey,
		originHash: item.originHash,
		importRunId: runId,
		importedAt,
		claudeType: item.claudeType,
		name: entry.name,
		description: entry.description,
		cwd: item.cwd,
		tool: "import",
	};
	for (const [key, value] of Object.entries(item.extra)) if (value !== null) metadata[key] = value;
	return {
		id,
		content: entry.content,
		source: item.source,
		sourceKind: item.sourceKind,
		timestamp: item.timestamp,
		importance: item.importance,
		memoryType: item.memoryType,
		metadata,
	};
}

/** Stable row id: the first 16 hex of sha256("import\0" + source + originKey), like omp's 16-hex memory ids. */
export const rowId = (item: SourceItem): string =>
	new Bun.CryptoHasher("sha256").update(`import\0${item.source}\0${item.originKey}`).digest("hex").slice(0, 16);

function sampleKey(item: SourceItem): string {
	return `${item.source}/${item.claudeType ?? item.sourceKind}`;
}

/** The checks before a bank is written or has rows deleted: a real directory and database, the library's schema, one embedding model, and no other process holding it. */
async function requireWritableBank(lib: Library, options: Options, bank: string): Promise<BankState> {
	assertBankWritable(options.banksDir, bank);
	const state = await readBankState(lib, options.banksDir, bank);
	if (state.schemaProblems.length > 0) throw new Refusal(`bank ${safeBankLabel(bank)}: schema differs from the library: ${state.schemaProblems.join("; ")}`);
	const model = bankEmbeddingModel(state, options.embeddingModel);
	if (model.problem) throw new Refusal(`bank ${safeBankLabel(bank)}: ${model.problem}`);
	if (!options.forceClosed && state.exists) {
		const holders = bankOpenBy(options.banksDir, bank);
		if (holders === null) throw new Refusal(`bank ${safeBankLabel(bank)}: cannot tell whether omp has it open (no usable lsof); pass --force-closed once sessions are closed`);
		if (holders.length > 0) throw new Refusal(`bank ${safeBankLabel(bank)}: open in process ${holders.join(", ")}; close the omp session or pass --force-closed`);
	}
	return state;
}

async function preflight(lib: Library, check: LibraryCheck, options: Options, banks: string[], write: boolean): Promise<LibraryCheck> {
	if (write && !check.ok) throw new Refusal(`refusing to write: ${check.reasons.join("; ")}`);
	if (write) await requireSelfTest(lib, check.ompVersion ?? "unknown");
	if (write) for (const bank of banks) await requireWritableBank(lib, options, bank);
	return check;
}

/**
 * Whether `bank` may be written now, as far as the backup requirement goes; called right before its writable construction.
 * A bank that does not exist yet needs none. One that exists must be in `--backed-up-banks` (covered by a verified backup,
 * unconditionally), or in `--exempt-banks` AND still hold nothing but this tool's rows: the ledger says this tool made it and every
 * row of it carries this tool's provenance, judged now, not when the wrapper decided. An exemption is consumed by use: `written`
 * holds the banks this invocation has written, and a second write of one is refused. `--no-backup-check` skips the coverage only.
 */
export function requireBackupCoverage(options: Options, ledger: LedgerFile, bank: string, written: Set<string>): void {
	if (written.has(bank)) throw new Refusal(`bank ${safeBankLabel(bank)} was already written in this run; each bank is written once per run`);
	if (!options.noBackupCheck && existsSync(bankPath(options.banksDir, bank)) && !(options.backedUpBanks ?? []).includes(bank)) {
		const exempt = (options.exemptBanks ?? []).includes(bank) && ledger.banksCreated[bank] !== undefined && holdsOnlyImporterContent(options.banksDir, bank);
		if (!exempt) throw new Refusal(`bank ${safeBankLabel(bank)} exists but is not covered by a verified backup (made after it existed): run omp-memory backup --apply`);
	}
	written.add(bank);
}

/** A run id that is in the ledger, or on rows in any bank, is taken: reusing it would make undo and verify act on two runs at once. */
function requireFreshRunId(options: Options, ledger: LedgerFile): void {
	const id = options.runId;
	const where: string[] = [];
	if (ledger.runs[id] !== undefined || Object.values(ledger.entries).some(entry => entry.runId === id)) where.push("the ledger");
	for (const run of rowsOfRun(options.banksDir, id, { skipUnreadable: true })) where.push(`bank ${safeBankLabel(run.bank)}`);
	if (where.length > 0) throw new UsageError(`run id ${id} is already used (${where.join(", ")}); leave out --run-id for a new one`);
}

export async function runImport(options: Options): Promise<Report> {
	if (options.command === "import" && options.apply) {
		requirePlanAndBackupFlags(options);
		return withImporterLock(options.ledgerFile, () => importUnderLock(options));
	}
	return importUnderLock(options);
}

/**
 * sha256 over the sorted lines `<bank>\t<originKey>\t<originHash>\t<decision>` of every item the plan would write (`import`),
 * report as changed, or hold back (`residual`): the same tree and state give the same digest, whatever the run id, the time or
 * the output files. It is a hash, so it carries no memory text.
 */
function planDigest(decided: Decided[]): string {
	const lines = decided
		.filter(entry => entry.decision === "import" || entry.decision === "changed" || entry.decision === "residual")
		.map(({ prepared, decision }) => (prepared.provenance === null ? `${prepared.bank}\t${prepared.item.originKey}\t${prepared.item.originHash}\t${decision}` : `(withheld)\t(withheld)\t${prepared.item.originHash}\t${decision}`))
		.sort();
	return sha256(`${lines.join("\n")}\n`);
}

/** `import --apply` is given exactly one of `--expect-plan-digest HEX` / `--no-plan-check` and one of `--backed-up-banks LIST` / `--no-backup-check`: a check is never skipped silently. */
function requirePlanAndBackupFlags(options: Options): void {
	const plan = [options.expectPlanDigest !== undefined, options.noPlanCheck === true].filter(Boolean).length;
	if (plan !== 1) throw new UsageError("import --apply needs exactly one of --expect-plan-digest HEX (the planDigest of the dry run you reviewed) or --no-plan-check");
	if (options.expectPlanDigest !== undefined && !/^[0-9a-f]{64}$/.test(options.expectPlanDigest)) throw new UsageError("--expect-plan-digest must be the 64 hex characters of a dry run's planDigest");
	const backup = [options.backedUpBanks !== undefined, options.noBackupCheck === true].filter(Boolean).length;
	if (backup !== 1) throw new UsageError("import --apply needs exactly one of --backed-up-banks LIST (the banks a verified backup covers; may be empty) or --no-backup-check");
}

async function importUnderLock(options: Options): Promise<Report> {
	const write = options.command === "import";
	if (write && !options.apply) throw new UsageError("import writes rows: pass --apply (use dry-run to see the plan)");
	checkOutputs(options);
	const { lib, check } = await requireLibrary({ libDir: options.libDir, ompBin: options.ompBin });
	const ledger = loadLedger(options.ledgerFile);
	if (write) requireFreshRunId(options, ledger);
	const collected = await collect(options);
	const prepared = await prepare(options, collected);
	if (options.banks !== undefined) {
		const known = new Set([...listBanks(options.banksDir), ...prepared.map(entry => entry.bank)]);
		const unknown = options.banks.filter(name => !known.has(name));
		if (unknown.length > 0) throw new UsageError(`--banks names no known bank: ${unknown.join(", ")}`);
	}
	const { decided, states } = await classify(lib, options, prepared, ledger);
	if (write && !options.noPlanCheck) {
		// the plan THIS run would execute, compared with the one the user was shown (same function as the dry run's digest)
		const mine = planDigest(decided);
		if (mine !== options.expectPlanDigest) {
			throw new Refusal("the plan changed since the dry run (sources, ledger or banks differ from what was shown); nothing was written; run dry-run again");
		}
	}
	const report = buildReport(options, collected, decided);
	const banksToWrite = [...new Set(decided.filter(d => d.decision === "import").map(d => d.prepared.bank))].sort();
	report.library = await preflight(lib, check, options, banksToWrite, write);
	if (options.command === "dry-run") {
		const samples = sampleSet(decided);
		report.extra = { samples };
		report.planDigest = planDigest(decided);
		if (options.out) {
			report.samplesFile = samplesPathFor(options.out);
			writeOutput(report.samplesFile, { note: "redacted, at most 80 characters per sample", samples }, SAMPLES_FORMAT);
		}
	}
	if (write) await applyAll(lib, options, decided, states, ledger, report);
	if (options.out) writeOutput(options.out, report, REPORT_FORMAT);
	return report;
}

function buildReport(options: Options, collected: Collected, decided: Decided[]): Report {
	const counts = emptyCounts();
	const report: Report = {
		runId: options.runId,
		command: options.command,
		source: options.mode,
		apply: options.apply,
		counts,
		perBank: [],
		unresolved: collected.unresolved.map(entry => ({ ...entry, where: shown(entry.where), reason: shown(entry.reason), candidates: entry.candidates.map(shown) })),
		errors: collected.errors.map(error => ({ where: shown(error.where), error: shown(error.error) })),
		warnings: collected.warnings.map(warning => ({ where: shown(warning.where), error: shown(warning.error) })),
		byFolder: {},
		byType: {},
		excluded: {},
		changed: [],
		tombstoned: [],
		held: [],
		binary: [],
		tooLarge: [],
		redaction: {},
		library: null,
		generatedAt: new Date().toISOString(),
	};
	counts.found = decided.length;
	counts.errors = collected.errors.length;
	counts.excludedBySource = Object.values(collected.excluded).reduce((sum, n) => sum + n, 0);
	for (const [reason, n] of Object.entries(collected.excluded)) bump(report.excluded, shown(reason), n);
	const perBank = new Map<string, { label: string; cwd: string | null; wouldImport: number; skipped: number; notTargeted: number }>();
	for (const { prepared, decision } of decided) {
		const { item, bank } = prepared;
		const folder = prepared.provenance === null ? (item.cwd ?? `(${UNASSIGNED_BANK})`) : "(withheld)";
		const type = item.claudeType ?? item.sourceKind;
		bump((report.byFolder[folder] ??= {}), type);
		bump(report.byType, type);
		for (const [kind, n] of Object.entries(prepared.counts)) bump(report.redaction, kind, n);
		const slot = perBank.get(bank) ?? { label: safeBankLabel(bank), cwd: prepared.provenance === null ? item.cwd : null, wouldImport: 0, skipped: 0, notTargeted: 0 };
		if (decision === "import") {
			slot.wouldImport += 1;
			counts.wouldImport += 1;
		} else slot.skipped += 1;
		if (decision === "not-targeted") slot.notTargeted += 1;
		perBank.set(bank, slot);
		switch (decision) {
			case "duplicate":
				counts.skippedDuplicate += 1;
				break;
			case "tombstone":
				counts.skippedTombstone += 1;
				report.tombstoned.push({ originKey: reportKey(prepared), bank: safeBankLabel(bank) });
				break;
			case "not-targeted":
				counts.skippedNotTargeted += 1;
				break;
			case "skipped-unassigned":
				counts.skippedUnassigned += 1;
				break;
			case "unchanged":
				counts.skippedUnchanged += 1;
				break;
			case "changed":
				counts.changedSource += 1;
				report.changed.push({ originKey: reportKey(prepared), bank: safeBankLabel(bank) });
				break;
			case "binary":
				counts.skippedBinary += 1;
				if (prepared.rejection?.kind === "binary") report.binary.push({ originKey: reportKey(prepared), reason: prepared.rejection.reason });
				break;
			case "too-large":
				counts.skippedTooLarge += 1;
				if (prepared.rejection?.kind === "too-large") {
					report.tooLarge.push({ originKey: reportKey(prepared), chars: prepared.rejection.chars, limit: MAX_TEXT_CHARS, reason: describeTooLarge(prepared.rejection.chars) });
				}
				break;
			case "residual":
				counts.skippedHeld += 1;
				report.held.push({ originKey: reportKey(prepared), kinds: prepared.residual });
				break;
			case "error":
				counts.errors += 1;
				report.errors.push({ where: prepared.provenance === null ? shown(item.origin) : opaqueKey(item), error: shown(prepared.redactionError ?? "redaction failed") });
				break;
			default:
				break;
		}
	}
	// ordered by the name the report shows (numbers in numeric order): a withheld bank's real id takes no part in the order
	report.perBank = [...perBank.values()]
		.sort((a, b) => a.label.localeCompare(b.label, "en", { numeric: true }))
		.map(slot => ({ bank: slot.label, cwd: slot.cwd, imported: 0, wouldImport: slot.wouldImport, skipped: slot.skipped, notTargeted: slot.notTargeted }));
	return report;
}

/** At most 3 redacted 80-character samples per source/type; also written next to `--out` when one is given. Items with findings are never sampled, whatever was decided about them. */
function sampleSet(decided: Decided[]): Record<string, { origin: string; text: string }[]> {
	const samples: Record<string, { origin: string; text: string }[]> = {};
	for (const { prepared, decision } of decided) {
		if (decision === "error" || decision === "residual" || decision === "binary" || decision === "too-large") continue;
		if (prepared.residual.length > 0 || prepared.redactionError !== null || prepared.provenance !== null) continue;
		const list = (samples[sampleKey(prepared.item)] ??= []);
		if (list.length < 3) list.push({ origin: prepared.item.origin, text: snippet(prepared.content, 80) });
	}
	return samples;
}

/** Refuses a destination (report, samples, ledger) that lies in the banks directory, is an input of the run, or is not an earlier output. */
export function checkOutputs(options: Options): void {
	resetLabels();
	assertOutputsSafe({
		command: options.command,
		banksDir: options.banksDir,
		ledgerFile: options.ledgerFile,
		out: options.out,
		inputs: [options.hindsightExport, options.hindsightLedger, options.claudeMemDb, options.projectMap, options.globalAllowlist, options.confFile],
	});
}

async function applyAll(
	lib: Library,
	options: Options,
	decided: Decided[],
	states: Map<string, BankState>,
	ledger: LedgerFile,
	report: Report,
): Promise<void> {
	const importedAt = new Date().toISOString();
	const rebuiltRuns = new Set<string>(); // runs whose record this call rebuilt from rows
	const writtenBanks = new Set<string>(); // banks this invocation has written: each is written once
	// Adopt rows the ledger lost (the rows are the source of truth) and record tombstones found on the way.
	for (const entry of decided) {
		const { item, bank } = entry.prepared;
		const key = entryKey(item.source, item.originKey);
		if (entry.adopt && !ledger.entries[key]) {
			const row = states.get(bank)?.imported.get(item.originKey);
			ledger.entries[key] = {
				source: item.source,
				originKey: item.originKey,
				bank,
				rowId: entry.adopt.rowId,
				hash: row?.originHash ?? item.originHash,
				runId: row?.runId ?? "adopted",
				importedAt,
				state: "imported",
			};
			// the rows are the truth: a run whose record never reached the ledger (a stop after the commit) is rebuilt, so that verify covers it
			if (row?.runId) {
				const known = ledger.runs[row.runId];
				if (known === undefined) ledger.runs[row.runId] = { runId: row.runId, mode: options.mode, at: importedAt, imported: 1, banks: [bank] };
				else if (!known.banks.includes(bank)) ledger.runs[row.runId] = { ...known, imported: known.imported + 1, banks: [...known.banks, bank] };
				else if (rebuiltRuns.has(row.runId)) ledger.runs[row.runId] = { ...known, imported: known.imported + 1 };
				rebuiltRuns.add(row.runId);
			}
		}
		if (entry.decision === "tombstone" && ledger.entries[key]?.state === "imported") {
			const existing = ledger.entries[key];
			if (existing) ledger.entries[key] = { ...existing, state: "tombstoned", tombstonedAt: importedAt };
		}
	}
	const byBank = new Map<string, Decided[]>();
	for (const entry of decided) if (entry.decision === "import") byBank.set(entry.prepared.bank, [...(byBank.get(entry.prepared.bank) ?? []), entry]);
	const banksDone: string[] = [];
	for (const [bank, entries] of [...byBank].sort(([a], [b]) => a.localeCompare(b))) {
		const state = states.get(bank) ?? emptyBankState();
		const { model } = bankEmbeddingModel(state, options.embeddingModel);
		const rows = entries.map(entry => rowFor(entry.prepared, options.runId, importedAt, rowId(entry.prepared.item)));
		// One embedding worker per bank: its memory is released when the bank is done.
		const embedder = options.embedderFactory ? options.embedderFactory(model) : createWorkerEmbedder(lib.pkgDir, model);
		let result: ApplyResult;
		let existedAtWrite = true; // whether the bank was there right before this run's own write (set by the check below)
		try {
			result = await applyBank(lib, options.banksDir, bank, rows, {
				model,
				importedAt,
				embedder,
				// right before the transaction: the bank may have been opened (or made, or changed) since the plan was read
				beforeWrite: async () => {
					const now = await requireWritableBank(lib, options, bank);
					if (bankEmbeddingModel(now, options.embeddingModel).model !== model) throw new Refusal(`bank ${safeBankLabel(bank)}: its embedding model changed since the plan was made`);
					existedAtWrite = existsSync(bankPath(options.banksDir, bank));
					requireBackupCoverage(options, ledger, bank, writtenBanks);
				},
			});
		} finally {
			embedder.close();
		}
		// recorded only for a bank this run itself made: absent when the plan was read AND absent right before the write, and written without an error
		if (result.error === null && !state.exists && !existedAtWrite) ledger.banksCreated[bank] ??= { runId: options.runId, at: importedAt };
		const slot = report.perBank.find(entry => entry.bank === safeBankLabel(bank));
		if (result.error !== null) {
			report.errors.push({ where: `bank ${safeBankLabel(bank)}`, error: result.error });
			report.counts.errors += 1;
			continue;
		}
		banksDone.push(bank);
		if (slot) slot.imported = result.inserted.length;
		report.counts.imported += result.inserted.length;
		for (const entry of entries) {
			const { item } = entry.prepared;
			const id = rowId(item);
			const entryRow: LedgerEntry = {
				source: item.source,
				originKey: item.originKey,
				bank,
				rowId: id,
				hash: item.originHash,
				runId: options.runId,
				importedAt,
				state: "imported",
			};
			ledger.entries[entryKey(item.source, item.originKey)] = entryRow;
		}
		ledger.runs[options.runId] = {
			runId: options.runId,
			mode: options.mode,
			at: importedAt,
			imported: (ledger.runs[options.runId]?.imported ?? 0) + result.inserted.length,
			banks: banksDone,
		};
		saveLedger(options.ledgerFile, ledger);
	}
	saveLedger(options.ledgerFile, ledger);
}

// ---------------------------------------------------------------------------------------------------------
// undo

export interface UndoReport {
	runId: string;
	apply: boolean;
	removed: { bank: string; rows: number }[];
	alreadyGone: number;
	tombstoned: number;
	problems: string[];
}

export async function runUndo(options: Options): Promise<UndoReport> {
	checkOutputs(options);
	if (!options.targetRun) throw new UsageError("undo needs --run ID");
	return options.apply ? withImporterLock(options.ledgerFile, () => undoUnderLock(options)) : undoUnderLock(options);
}

/**
 * Rebuilds, in memory, the ledger records of runs whose rows are in a bank but whose run record is not in the ledger (a stop between a
 * commit and the ledger save): from the rows alone, so it works when the source files are gone. Only rows that are provably this tool's
 * and carry a run id count. `only` limits it to one run. Returns the run ids that were rebuilt; nothing is written here.
 */
function recoverRuns(options: Options, ledger: LedgerFile, only?: string): string[] {
	const byRun = new Map<string, RunRow[]>();
	for (const row of importerRows(options.banksDir, only)) {
		if (ledger.runs[row.runId] === undefined) byRun.set(row.runId, [...(byRun.get(row.runId) ?? []), row]);
	}
	const now = new Date().toISOString();
	for (const [runId, rows] of byRun) {
		const mode = (Object.keys(MODE_SOURCE) as SourceMode[]).find(candidate => MODE_SOURCE[candidate].source === rows[0]?.source);
		if (mode === undefined) continue;
		for (const row of rows) {
			const key = entryKey(MODE_SOURCE[mode].source, row.originKey);
			ledger.entries[key] ??= {
				source: MODE_SOURCE[mode].source,
				originKey: row.originKey,
				bank: row.bank,
				rowId: row.id,
				hash: row.originHash,
				runId,
				importedAt: row.importedAt ?? now,
				state: "imported",
			};
		}
		ledger.runs[runId] = {
			runId,
			mode,
			at: rows.map(row => row.importedAt).filter((at): at is string => at !== null).sort()[0] ?? now,
			imported: rows.length,
			banks: [...new Set(rows.map(row => row.bank))].sort(),
		};
	}
	return [...byRun.keys()];
}

async function undoUnderLock(options: Options): Promise<UndoReport> {
	const target = options.targetRun;
	if (!target) throw new UsageError("undo needs --run ID");
	const ledger = loadLedger(options.ledgerFile);
	recoverRuns(options, ledger, target);
	const report: UndoReport = { runId: target, apply: options.apply, removed: [], alreadyGone: 0, tombstoned: 0, problems: [] };
	const rows = rowsOfRun(options.banksDir, target);
	const ledgerEntries = Object.entries(ledger.entries).filter(([, entry]) => entry.runId === target && entry.state === "imported");
	for (const run of rows) {
		if (run.foreign.length > 0) {
			report.problems.push(`bank ${safeBankLabel(run.bank)}: ${run.foreign.length} row(s) carry run id ${target} but were not written by the importer (trust tier, source or provenance differ); left in place`);
		}
	}
	const handle = options.apply ? await requireLibrary({ libDir: options.libDir, ompBin: options.ompBin }) : null;
	if (handle) {
		if (!handle.check.ok) throw new Refusal(`refusing to write: ${handle.check.reasons.join("; ")}`);
		await requireSelfTest(handle.lib, handle.check.ompVersion ?? "unknown");
		// the same checks as an import's, for every bank, before any row is deleted
		for (const run of rows) if (run.ids.length > 0) await requireWritableBank(handle.lib, options, run.bank);
	}
	const selected = new Set<string>(); // rows the run id in their metadata selects
	for (const run of rows) for (const id of [...run.ids, ...run.foreign]) selected.add(`${run.bank}\0${id}`);
	const found = new Set<string>(selected); // every row of the run in a bank now: those, and every ledger-backed row found by its id
	for (const bank of new Set(ledgerEntries.map(([, entry]) => entry.bank))) {
		const ids = ledgerEntries.filter(([, entry]) => entry.bank === bank).map(([, entry]) => entry.rowId);
		for (const id of existingIds(options.banksDir, bank, ids)) found.add(`${bank}\0${id}`);
	}
	const deleted = new Set<string>(); // rows that are gone (or, in a dry run, would go)
	let incomplete = false;
	for (const run of rows) {
		if (run.ids.length === 0) continue;
		if (handle === null) {
			for (const id of run.ids) deleted.add(`${run.bank}\0${id}`);
			report.removed.push({ bank: safeBankLabel(run.bank), rows: run.ids.length });
			continue;
		}
		try {
			// repeated right before the deletion: an omp session may have opened the bank since the check above
			const state = await requireWritableBank(handle.lib, options, run.bank);
			forgetRows(handle.lib, options.banksDir, run.bank, run.ids, bankEmbeddingModel(state, options.embeddingModel).model);
		} catch (error) {
			if (!(error instanceof Refusal)) throw error;
			report.problems.push(`${error.message}; nothing was removed from it`);
			incomplete = true;
			continue;
		}
		// what was deleted is what is no longer there, not what the library says it did
		const left = existingIds(options.banksDir, run.bank, run.ids);
		const gone = run.ids.filter(id => !left.has(id));
		for (const id of gone) deleted.add(`${run.bank}\0${id}`);
		report.removed.push({ bank: safeBankLabel(run.bank), rows: gone.length });
		if (left.size > 0) {
			report.problems.push(`bank ${safeBankLabel(run.bank)}: ${left.size} row(s) of run ${target} are still present after the delete (the library did not remove them); the ledger keeps them`);
			incomplete = true;
		}
	}
	// ledger-backed rows that are still in a bank although the metadata no longer selects them (run id removed or changed): survivors, by id
	const stray = new Map<string, string[]>();
	for (const [, entry] of ledgerEntries) {
		const where = `${entry.bank}\0${entry.rowId}`;
		if (found.has(where) && !selected.has(where)) stray.set(entry.bank, [...(stray.get(entry.bank) ?? []), entry.rowId]);
	}
	for (const [bank, ids] of stray) {
		report.problems.push(
			`bank ${safeBankLabel(bank)}: ${ids.length} row(s) of run ${target} are still present but no longer carry the run's metadata (ids ${ids.slice(0, 20).join(", ")}${ids.length > 20 ? ", ..." : ""}); left in place, the ledger keeps them`,
		);
	}
	for (const [key, entry] of ledgerEntries) {
		const where = `${entry.bank}\0${entry.rowId}`;
		if (deleted.has(where)) {
			if (options.apply) delete ledger.entries[key];
		} else if (!found.has(where)) {
			// The row is already gone: the user deleted it after the import, so the item stays tombstoned.
			report.alreadyGone += 1;
			if (options.apply) {
				ledger.entries[key] = { ...entry, state: "tombstoned", tombstonedAt: new Date().toISOString() };
				report.tombstoned += 1;
			}
		} else if (found.has(where)) {
			// a ledger-backed row is still in its bank (not deleted, or not provably the importer's): the run is not finished
			incomplete = true;
		}
	}
	const run = ledger.runs[target];
	if (options.apply && run && !incomplete) ledger.runs[target] = { ...run, undoneAt: new Date().toISOString() };
	if (options.apply) saveLedger(options.ledgerFile, ledger);
	if (rows.length === 0 && ledgerEntries.length === 0) report.problems.push(`no rows or ledger entries for run ${target}`);
	if (options.out) writeOutput(options.out, report, REPORT_FORMAT);
	return report;
}

// ---------------------------------------------------------------------------------------------------------
// verify

export interface BankVerification {
	bank: string;
	expected: number;
	actual: number;
	missingEmbedding: number;
	wrongEmbeddingModel: number;
	missingFts: number;
	missingProvenance: number;
	missingAnnotations: number;
	notConsolidated: number;
	otherRows: number;
	/** Rows the ledger names for the run that the bank does not hold (by row id, not by count). */
	missingRows: number;
	/** Rows of the run in the bank that the ledger does not name. */
	unexpectedRows: number;
	/** Rows whose provenance (origin key, hash, run, source) differs from the ledger's entry. */
	provenanceMismatch: number;
	integrity: string;
}

export interface VerifyReport {
	runId: string | null;
	ok: boolean;
	banks: BankVerification[];
	problems: string[];
	parseErrors: { where: string; error: string }[];
	libraryOk: boolean;
	/** Runs found in the banks (rows with this tool's provenance) that the ledger had no record of; they were verified from their rows. */
	recoveredRuns: string[];
}

export async function runVerify(options: Options): Promise<VerifyReport> {
	checkOutputs(options);
	const ledger = loadLedger(options.ledgerFile);
	// runs whose record never reached the ledger are rebuilt from the banks' rows (nothing is written), so that they are verified too
	const recoveredRuns = recoverRuns(options, ledger, options.targetRun);
	const runs = options.targetRun
		? [options.targetRun]
		: Object.values(ledger.runs)
				.filter(run => run.mode === options.mode && run.undoneAt === undefined)
				.map(run => run.runId);
	const report: VerifyReport = { runId: options.targetRun ?? null, ok: true, banks: [], problems: [], parseErrors: [], libraryOk: true, recoveredRuns };
	const { check, lib } = await inspectLibrary({ libDir: options.libDir, ompBin: options.ompBin });
	report.libraryOk = check.ok;
	if (!check.ok) report.problems.push(`library: ${check.reasons.join("; ")}`);
	if (lib === null) {
		report.ok = false;
		if (options.out) writeOutput(options.out, report, REPORT_FORMAT);
		return report;
	}
	if (runs.length === 0) report.problems.push("no runs to verify");
	const expected = new Map<string, Map<string, LedgerEntry>>();
	for (const entry of Object.values(ledger.entries)) {
		if (entry.state === "imported" && runs.includes(entry.runId)) {
			const forBank = expected.get(entry.bank) ?? new Map<string, LedgerEntry>();
			forBank.set(entry.rowId, entry);
			expected.set(entry.bank, forBank);
		}
	}
	const runRows = runs.flatMap(runId => rowsOfRun(options.banksDir, runId));
	const banks = [...new Set([...expected.keys(), ...runRows.map(run => run.bank)])].sort();
	for (const bank of banks) {
		const file = path.join(options.banksDir, bank, "mnemopi.db");
		const result: BankVerification = {
			bank: safeBankLabel(bank),
			expected: expected.get(bank)?.size ?? 0,
			actual: 0,
			missingEmbedding: 0,
			wrongEmbeddingModel: 0,
			missingFts: 0,
			missingProvenance: 0,
			missingAnnotations: 0,
			notConsolidated: 0,
			otherRows: 0,
			missingRows: 0,
			unexpectedRows: 0,
			provenanceMismatch: 0,
			integrity: "not run",
		};
		if (!existsSync(file)) {
			report.problems.push(`bank ${result.bank}: database missing`);
			report.banks.push(result);
			continue;
		}
		const db = openReadonly(file);
		try {
			const state = await readBankState(lib, options.banksDir, bank);
			const { model } = bankEmbeddingModel(state, options.embeddingModel);
			const rows = db
				.query("SELECT id, source, trust_tier, metadata_json, consolidated_at FROM working_memory WHERE json_valid(metadata_json) AND json_extract(metadata_json, '$.importRunId') IN (" + runs.map(() => "?").join(",") + ")")
				.all(...runs) as { id: string; source: string | null; trust_tier: string | null; metadata_json: string; consolidated_at: string | null }[];
			result.actual = rows.length;
			const total = db.query("SELECT COUNT(*) AS n FROM working_memory").get() as { n: number };
			result.otherRows = total.n - rows.length;
			const wanted = expected.get(bank) ?? new Map<string, LedgerEntry>();
			const seen = new Set<string>();
			for (const row of rows) {
				const meta: Record<string, unknown> = JSON.parse(row.metadata_json);
				const entry = wanted.get(row.id);
				seen.add(row.id);
				if (entry === undefined) result.unexpectedRows += 1;
				else if (meta.originKey !== entry.originKey || meta.originHash !== entry.hash || meta.importRunId !== entry.runId || meta.source !== entry.source || !isImporterRow(row, meta)) result.provenanceMismatch += 1;
				if (!meta.originKey || !meta.importRunId || !meta.originHash || !meta.source) result.missingProvenance += 1;
				if (row.consolidated_at === null) result.notConsolidated += 1;
				const embedding = db.query("SELECT model FROM memory_embeddings WHERE memory_id = ?").get(row.id) as { model: string } | null;
				if (embedding === null) result.missingEmbedding += 1;
				else if (embedding.model !== model) result.wrongEmbeddingModel += 1;
				if (db.query("SELECT 1 FROM fts_working WHERE id = ? LIMIT 1").get(row.id) === null) result.missingFts += 1;
				const annotation = db.query("SELECT 1 FROM annotations WHERE memory_id = ? AND kind = 'occurred_on' LIMIT 1").get(row.id);
				if (annotation === null) result.missingAnnotations += 1;
			}
			result.missingRows = [...wanted.keys()].filter(id => !seen.has(id)).length;
			const quick = db.query("PRAGMA quick_check").get() as { quick_check: string };
			result.integrity = quick.quick_check;
		} finally {
			db.close();
		}
		const bad =
			result.expected !== result.actual ||
			result.missingEmbedding + result.wrongEmbeddingModel + result.missingFts + result.missingProvenance + result.missingAnnotations + result.notConsolidated + result.missingRows + result.unexpectedRows + result.provenanceMismatch > 0 ||
			result.integrity !== "ok";
		if (bad) report.problems.push(`bank ${result.bank}: ${JSON.stringify(result)}`);
		report.banks.push(result);
	}
	try {
		const collected = await collect(options);
		report.parseErrors = collected.errors.map(error => ({ where: shown(error.where), error: shown(error.error) }));
		if (collected.errors.length > 0) report.problems.push(`source: ${collected.errors.length} source item(s) could not be read (see parseErrors)`);
	} catch (error) {
		report.problems.push(`source: ${String(error)}`);
	}
	report.ok = report.problems.length === 0;
	if (options.out) writeOutput(options.out, report, REPORT_FORMAT);
	return report;
}

export { listBanks, newRunId };

// ---------------------------------------------------------------------------------------------------------
// scan

/**
 * Counts per folder and type, parse errors and unresolved folders, without redaction, ledger or banks. Binary and oversize
 * items are recognised (they need no redaction); what only `dry-run` can find (held items, duplicates, items already
 * imported, tombstones, banks excluded by --only-new-banks) is NOT subtracted, so every `wouldImport` here is an upper bound,
 * and the report says so.
 */
export async function runScan(options: Options): Promise<Report> {
	checkOutputs(options);
	const collected = await collect(options);
	const allowlist = readAllowlist(options.globalAllowlist);
	const homes = homePrefixes(options);
	const report = buildReport(
		options,
		collected,
		collected.items.map(item => {
			const rejection = rejectionOf(item);
			const bank = bankFor(item, allowlist, options.banksDir);
			noteSecretBank(item, bank, homes);
			return {
				prepared: { item, bank, content: "", name: item.name, description: item.description, residual: [], redactionError: null, counts: {}, rejection, provenance: provenanceHold(item, homes) },
				decision: rejection?.kind ?? ("import" as const),
			};
		}),
	);
	report.extra = {
		wouldImportIsUpperBound: true,
		note: "scan does not redact or open a bank: held, duplicate, already imported and tombstoned items, and banks left out by --only-new-banks, are only found by dry-run. Every wouldImport here is an upper bound; dry-run gives the real number.",
	};
	if (options.out) writeOutput(options.out, report, REPORT_FORMAT);
	return report;
}
