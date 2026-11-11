/**
 * The Mnemopi library the importer writes through, plus the guards that prove it is the one omp itself uses.
 *
 * The importer never builds a schema or hand-inserts rows: it opens a bank with the library's `Mnemopi` class,
 * writes with the library's own restore path (`beam.importFromDict`), indexes with the library's annotation and
 * embedding functions, and deletes with `forgetWorking`.
 *
 * The library is the published npm package `@oh-my-pi/pi-mnemopi`, at EXACTLY the version `omp --version` prints, from
 * (in this order) an explicit install root (`--lib-dir`), omp's own plugin directory (`$OMP_PLUGINS_DIR` or
 * `~/.omp/plugins`), or `<cache>/lib/<version>` made by `setup` (`~/.cache/omp-memory/lib/<version>`). A different
 * version is never used: the command refuses and names the `setup` command. No omp source tree is needed or read.
 * Before any write the importer also checks, read-only, that the SQL it relies on is present in the loaded package and
 * in the omp binary, and that every bank's core tables have the columns a fresh library database has.
 */
import type { Database } from "bun:sqlite";
import { existsSync, lstatSync, mkdtempSync, readFileSync, realpathSync, rmSync, type Stats, statSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { projectBankSegment } from "./bank_id";
import type { Embedder } from "./embedder";
import { Refusal } from "./model";
import { safeBankLabel } from "./outputs";
import { redactMemorySecrets } from "./omp_redact";

export const LIBRARY_PACKAGE = "@oh-my-pi/pi-mnemopi";
/** Written by `setup` into a cache install once it is complete; a cache directory without it is a half install. */
export const SETUP_MARKER = "omp-memory-setup.json";

export interface LibBeam {
	readonly db: Database;
	readonly config: { workingMemoryLimit: number };
	readonly annotations: { add(memoryId: string, kind: string, value: string): unknown };
	/** The library's own native write (it trims expired native rows); the self-test uses it to prove the trim exemption. */
	remember(content: string, options?: Record<string, unknown>): string;
	importFromDict(
		data: Record<string, unknown>,
		force?: boolean,
	): { working_memory: { inserted: number; skipped: number } };
}

export interface LibMnemopi {
	readonly beam: LibBeam;
	readonly db: Database;
	readonly runtimeOptions: unknown;
	flushExtractions(): Promise<void>;
	close(): void;
}

export interface Library {
	/** The directory holding `node_modules/@oh-my-pi/pi-mnemopi`. */
	root: string;
	/** The package directory the library was loaded from. */
	pkgDir: string;
	version: string;
	kind: LibraryKind;
	Mnemopi: new (options: Record<string, unknown>) => LibMnemopi;
	scheduleEmbedding(beam: LibBeam, items: readonly { memoryId: string; content: string }[]): void;
	withMnemopiRuntimeOptions<T>(options: unknown, fn: () => T): T;
	forgetWorking(beam: LibBeam, memoryId: string): boolean;
	redactMemorySecrets(text: string): string;
	projectBankSegment(projectRoot: string): string;
}

// ---------------------------------------------------------------------------------------------------------
// Where the library is looked for

export type LibraryKind = "explicit" | "omp-plugins" | "cache";

/** omp's plugin directory: `$OMP_PLUGINS_DIR`, else `~/.omp/plugins`. */
export function ompPluginsDir(env: NodeJS.ProcessEnv = process.env): string {
	return env.OMP_PLUGINS_DIR ? path.resolve(env.OMP_PLUGINS_DIR) : path.join(homedir(), ".omp", "plugins");
}

/** This tool's cache: `$OMP_MEMORY_CACHE_DIR`, else `$XDG_CACHE_HOME/omp-memory`, else `~/.cache/omp-memory`. */
export function memoryCacheDir(env: NodeJS.ProcessEnv = process.env): string {
	if (env.OMP_MEMORY_CACHE_DIR) return path.resolve(env.OMP_MEMORY_CACHE_DIR);
	if (env.XDG_CACHE_HOME && path.isAbsolute(env.XDG_CACHE_HOME)) return path.join(env.XDG_CACHE_HOME, "omp-memory");
	return path.join(homedir(), ".cache", "omp-memory");
}

/** Where `setup` installs the library for an omp version. */
export function cacheLibraryRoot(version: string, env: NodeJS.ProcessEnv = process.env): string {
	return path.join(memoryCacheDir(env), "lib", version);
}

export const packageDirOf = (root: string): string => path.join(root, "node_modules", ...LIBRARY_PACKAGE.split("/"));

export interface Candidate {
	kind: LibraryKind;
	root: string;
	pkgDir: string;
	/** The version of the installed package, null when it is not installed. */
	version: string | null;
	usable: boolean;
	/** Why it cannot be used ("" when usable). */
	why: string;
}

export interface LibraryLocation {
	kind: LibraryKind;
	root: string;
	pkgDir: string;
	version: string;
}

interface PackageJson {
	version?: string;
	dependencies?: Record<string, string>;
}

function readJson(file: string): PackageJson | null {
	try {
		const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
		return typeof parsed === "object" && parsed !== null ? (parsed as PackageJson) : null;
	} catch {
		return null;
	}
}

const EXACT = /^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$/;

/** What is wrong with the install at `root` for `version`; [] when the package and its pinned dependencies are in place. */
export function installProblems(root: string, version: string): { found: string | null; problems: string[] } {
	const pkgDir = packageDirOf(root);
	const pkg = readJson(path.join(pkgDir, "package.json"));
	if (pkg === null) return { found: null, problems: ["@oh-my-pi/pi-mnemopi is not installed there"] };
	const found = typeof pkg.version === "string" ? pkg.version : null;
	if (found !== version) return { found, problems: [`it is ${found ?? "of unknown version"}, omp is ${version}`] };
	const problems: string[] = [];
	if (!existsSync(path.join(pkgDir, "src", "index.ts"))) problems.push("src/index.ts is missing from the package");
	for (const [dep, range] of Object.entries(pkg.dependencies ?? {})) {
		const installed = [path.join(pkgDir, "node_modules", dep), path.join(root, "node_modules", dep)]
			.map(dir => readJson(path.join(dir, "package.json"))?.version)
			.find(value => typeof value === "string");
		if (installed === undefined) problems.push(`dependency ${dep} is not installed`);
		else if (EXACT.test(range) && installed !== range) problems.push(`dependency ${dep} is ${installed}, the package pins ${range}`);
	}
	return { found, problems };
}

export interface Resolution {
	ompVersion: string;
	location: LibraryLocation | null;
	candidates: Candidate[];
}

/**
 * The library for `ompVersion`. With `libDir` (an install root) only that is considered: a configured location is used or
 * refused, never replaced by another. Otherwise omp's plugin directory, then the cache directory `setup` fills.
 */
export function resolveLibrary(
	ompVersion: string,
	options: { libDir?: string; env?: NodeJS.ProcessEnv } = {},
): Resolution {
	const env = options.env ?? process.env;
	const roots: { kind: LibraryKind; root: string }[] = options.libDir
		? [{ kind: "explicit", root: path.resolve(options.libDir) }]
		: [
				{ kind: "omp-plugins", root: ompPluginsDir(env) },
				{ kind: "cache", root: cacheLibraryRoot(ompVersion, env) },
			];
	const candidates: Candidate[] = [];
	let location: LibraryLocation | null = null;
	for (const { kind, root } of roots) {
		const { found, problems } = installProblems(root, ompVersion);
		if (problems.length === 0 && kind === "cache" && !existsSync(path.join(root, SETUP_MARKER))) {
			problems.push("an incomplete install (no completion marker; run setup again)");
		}
		const candidate: Candidate = {
			kind,
			root,
			pkgDir: packageDirOf(root),
			version: found,
			usable: problems.length === 0,
			why: problems.join("; "),
		};
		candidates.push(candidate);
		if (candidate.usable && location === null) location = { kind, root, pkgDir: candidate.pkgDir, version: ompVersion };
	}
	return { ompVersion, location, candidates };
}

/** The command that installs the library; `OMP_MEMORY_SETUP_COMMAND` is set by the omp-memory launcher. */
export function setupCommand(ompBin?: string): string {
	if (process.env.OMP_MEMORY_SETUP_COMMAND) return process.env.OMP_MEMORY_SETUP_COMMAND;
	const cli = path.join(import.meta.dir, "cli.ts");
	return `bun ${cli} setup${ompBin && ompBin !== "omp" ? ` --omp-bin ${ompBin}` : ""}`;
}

const cache = new Map<string, Promise<Library>>();

export function loadLibrary(location: LibraryLocation): Promise<Library> {
	let loaded = cache.get(location.pkgDir);
	if (!loaded) {
		loaded = importLibrary(location);
		cache.set(location.pkgDir, loaded);
	}
	return loaded;
}

async function importLibrary(location: LibraryLocation): Promise<Library> {
	const src = path.join(location.pkgDir, "src");
	// Dynamic imports: the package is chosen at run time; it is not a dependency of this folder.
	const core = await import(path.join(src, "index.ts"));
	const helpers = await import(path.join(src, "core", "beam", "helpers.ts"));
	const runtime = await import(path.join(src, "core", "runtime-options.ts"));
	const store = await import(path.join(src, "core", "beam", "store.ts"));
	return {
		root: location.root,
		pkgDir: location.pkgDir,
		version: location.version,
		kind: location.kind,
		Mnemopi: core.Mnemopi,
		scheduleEmbedding: helpers.scheduleEmbedding,
		withMnemopiRuntimeOptions: runtime.withMnemopiRuntimeOptions,
		forgetWorking: store.forgetWorking,
		redactMemorySecrets,
		projectBankSegment,
	};
}

// ---------------------------------------------------------------------------------------------------------
// Version and write-path parity with the installed omp

/**
 * What the importer relies on in the SQL of the library (and of the omp binary, which carries the same package). Matching
 * is structural: letter case and layout (spaces, line breaks, `VALUES (...)` against `SELECT ...`) do not matter, and
 * a column list only has to CONTAIN the columns the importer writes, so a library that adds a column still matches. This is a
 * quick, read-only hint of where a difference is; what decides whether a library may be written through is the behavioural
 * self-test (selftest.ts), which performs the importer's write sequence through the loaded library.
 */
export interface ParityRequirement {
	name: string;
	/** Statements of which at least one must appear, as written in normal form (single spaces). */
	anyOf?: readonly string[];
	/** A statement whose parenthesised list must contain every one of `columns` (an INSERT's columns, a CREATE TABLE's names). */
	columnsOf?: { head: string; columns: readonly string[] };
	/** One statement that satisfies the requirement, for stand-ins of the omp binary in tests. */
	sample: string;
}

/** Every column `toRestoreRow` (banks.ts) writes. */
const RESTORE_COLUMNS = [
	"id",
	"content",
	"source",
	"timestamp",
	"session_id",
	"importance",
	"metadata_json",
	"valid_until",
	"superseded_by",
	"scope",
	"recall_count",
	"last_recalled",
	"created_at",
	"veracity",
	"consolidated_at",
	"memory_type",
	"embed_text",
	"author_id",
	"author_type",
	"channel_id",
	"trust_tier",
	"event_date",
	"event_date_precision",
	"temporal_tags",
] as const;

export const PARITY_REQUIREMENTS: readonly ParityRequirement[] = [
	{
		name: "restore insert (library import path)",
		columnsOf: { head: "insert into working_memory", columns: RESTORE_COLUMNS },
		sample: `INSERT INTO working_memory (${RESTORE_COLUMNS.join(", ")}) VALUES (${RESTORE_COLUMNS.map(() => "?").join(", ")})`,
	},
	{
		name: "embedding store",
		anyOf: [
			"insert or replace into memory_embeddings(memory_id, embedding_json, model) values (?, ?, ?)",
			"insert or replace into memory_embeddings(memory_id, embedding_json, model) select ?, ?, ?",
		],
		sample: "INSERT OR REPLACE INTO memory_embeddings(memory_id, embedding_json, model)\n\tSELECT ?, ?, ?",
	},
	{
		name: "annotation store",
		anyOf: ["insert or ignore into annotations (memory_id, kind, value, source, confidence) values (?, ?, ?, ?, ?)"],
		sample: "INSERT OR IGNORE INTO annotations (memory_id, kind, value, source, confidence) VALUES (?, ?, ?, ?, ?)",
	},
	{
		name: "forget",
		anyOf: ["delete from working_memory where id = ? and session_id = ?"],
		sample: "DELETE FROM working_memory WHERE id = ? AND session_id = ?",
	},
	{
		name: "working_memory table",
		columnsOf: {
			head: "create table if not exists working_memory",
			columns: ["id", "content", "embed_text", "source", "timestamp", "session_id", "importance", "metadata_json", "veracity", "memory_type", "consolidated_at"],
		},
		sample: "CREATE TABLE IF NOT EXISTS working_memory (id TEXT PRIMARY KEY, content TEXT NOT NULL, embed_text TEXT DEFAULT NULL, source TEXT, timestamp TEXT, session_id TEXT DEFAULT 'default', importance REAL DEFAULT 0.5, metadata_json TEXT, veracity TEXT DEFAULT 'unknown', memory_type TEXT DEFAULT 'unknown', consolidated_at TEXT)",
	},
	{
		name: "trim exemption for durable rows",
		anyOf: ["and trust_tier is not 'imported'"],
		sample: "AND trust_tier IS NOT 'IMPORTED'",
	},
];

/** The package files the requirements are searched in, relative to the package directory. */
export const PARITY_FILES = [
	"src/core/beam/store.ts",
	"src/core/beam/schema.ts",
	"src/core/annotations.ts",
	"src/core/beam/helpers.ts",
] as const;

/** An omp smaller than this is a launcher script or a package entry, not the compiled binary that carries the SQL. */
const COMPILED_OMP_MIN_BYTES = 5 * 1024 * 1024;

export interface LibraryCheck {
	ok: boolean;
	ompBin: string;
	ompVersion: string | null;
	libraryVersion: string | null;
	libraryPath: string | null;
	libraryKind: LibraryKind | null;
	/** Every place that was looked at, with the reason it was not used. */
	candidates: Candidate[];
	parity: {
		checked: string[];
		missingInLibrary: string[];
		missingInOmp: string[];
		/** "scanned", or why the omp side was not scanned (omp is not a compiled binary, or unreadable). */
		ompBinary: string;
		/** True only when the omp binary was scanned and carries the SQL; an unscanned omp is not claimed to match. */
		ompParityVerified: boolean;
	} | null;
	reasons: string[];
	/** Things that did not stop the check but weaken what it proves. */
	warnings: string[];
	setupCommand: string;
}

function semver(text: string): string | null {
	const match = /(\d+)\.(\d+)\.(\d+)/.exec(text);
	return match ? `${match[1]}.${match[2]}.${match[3]}` : null;
}

/** `omp --version` as x.y.z, or null. BUN_BE_BUN is removed so that omp does not behave as plain Bun. */
export function readOmpVersion(ompBin: string): string | null {
	const env = { ...process.env };
	delete env.BUN_BE_BUN;
	try {
		const ran = Bun.spawnSync([ompBin, "--version"], { stdout: "pipe", stderr: "pipe", env });
		return ran.exitCode === 0 ? semver(ran.stdout.toString()) : null;
	} catch {
		return null;
	}
}

function resolveBinary(ompBin: string): string {
	if (ompBin.includes("/")) return path.resolve(ompBin);
	return Bun.which(ompBin) ?? ompBin;
}

/**
 * A statement in normal form as a regular expression, so that letter case and layout do not matter: any run of spaces
 * matches any whitespace (line breaks, tabs), and any whitespace, or none, may sit next to a parenthesis or a comma.
 */
export function sqlRegex(statement: string): RegExp {
	const compact = statement.replace(/\s+/g, " ").replace(/ ?([(),]) ?/g, "$1").trim();
	let source = "";
	for (const char of compact) {
		if (char === " ") source += "\\s+";
		else if (char === "(" || char === ")" || char === ",") source += `\\s*\\${char}\\s*`;
		else source += char.replace(/[.*+?^${}|[\]\\]/g, "\\$&");
	}
	return new RegExp(source, "i");
}

const requirementRegexes = new Map<string, RegExp[]>();
function regexesOf(requirement: ParityRequirement): RegExp[] {
	let found = requirementRegexes.get(requirement.name);
	if (!found) {
		found = (requirement.anyOf ?? []).map(sqlRegex);
		requirementRegexes.set(requirement.name, found);
	}
	return found;
}

/** The names (first words) of every parenthesised, comma-separated list that follows `head` in `text`, one array per place. */
function listsAfter(text: string, head: string): string[][] {
	const opener = new RegExp(`${sqlRegex(head).source}\\s*\\(`, "gi");
	const lists: string[][] = [];
	for (const match of text.matchAll(opener)) {
		const from = match.index + match[0].length;
		const names: string[] = [];
		let depth = 1;
		let segmentStart = from;
		const limit = Math.min(text.length, from + 20_000);
		for (let at = from; at < limit && depth > 0; at += 1) {
			const char = text[at];
			if (char === "(") depth += 1;
			else if ((char === ")" && (depth -= 1) === 0) || (char === "," && depth === 1)) {
				const word = /^\s*[`"[]?([A-Za-z_]\w*)/.exec(text.slice(segmentStart, at));
				if (word?.[1]) names.push(word[1].toLowerCase());
				segmentStart = at + 1;
			}
		}
		lists.push(names);
	}
	return lists;
}

/** The requirements that `text` (package source, or an omp binary read as latin1) does not meet. */
export function unmetRequirements(text: string, requirements: readonly ParityRequirement[] = PARITY_REQUIREMENTS): string[] {
	return requirements
		.filter(requirement => {
			if (requirement.anyOf && !regexesOf(requirement).some(regex => regex.test(text))) return true;
			const columns = requirement.columnsOf;
			if (columns) return !listsAfter(text, columns.head).some(names => columns.columns.every(column => names.includes(column)));
			return false;
		})
		.map(requirement => requirement.name);
}

function parityOfOmp(ompBin: string): { missing: string[]; ompBinary: string } {
	const file = resolveBinary(ompBin);
	let size: number;
	try {
		size = statSync(file).size;
	} catch {
		return { missing: [], ompBinary: `not scanned: cannot read ${file}` };
	}
	if (size < COMPILED_OMP_MIN_BYTES && !readSmallScriptHasSql(file)) {
		return { missing: [], ompBinary: "not scanned: omp is a launcher script, not the compiled binary" };
	}
	// latin1 keeps one character per byte, so the SQL text (ASCII) is found wherever it sits in the binary.
	return { missing: unmetRequirements(readFileSync(file).toString("latin1")), ompBinary: "scanned" };
}

/** A small file that mentions the SQL at all is scanned like a binary (the test stand-ins for omp are such files). */
function readSmallScriptHasSql(file: string): boolean {
	try {
		return /insert\s+into\s+working_memory/i.test(readFileSync(file, "utf8"));
	} catch {
		return false;
	}
}

/**
 * Find the library for the installed omp and check it. `lib` is null when no install matches omp's version exactly; the
 * check then says where it looked and which `setup` command installs it. Nothing is written.
 */
export async function inspectLibrary(options: {
	libDir?: string;
	ompBin?: string;
}): Promise<{ check: LibraryCheck; lib: Library | null }> {
	const ompBin = options.ompBin ?? "omp";
	const reasons: string[] = [];
	const command = setupCommand(options.ompBin);
	const check: LibraryCheck = {
		ok: false,
		ompBin,
		ompVersion: null,
		libraryVersion: null,
		libraryPath: null,
		libraryKind: null,
		candidates: [],
		parity: null,
		reasons,
		warnings: [],
		setupCommand: command,
	};
	const ompVersion = readOmpVersion(ompBin);
	if (ompVersion === null) {
		reasons.push(`could not read a version from "${ompBin} --version"; put omp on PATH or pass --omp-bin`);
		return { check, lib: null };
	}
	check.ompVersion = ompVersion;
	const resolution = resolveLibrary(ompVersion, { libDir: options.libDir });
	check.candidates = resolution.candidates;
	if (resolution.location === null) {
		const looked = resolution.candidates
			.map(candidate => `${candidate.pkgDir} (${candidate.why})`)
			.join("; ");
		reasons.push(
			`no ${LIBRARY_PACKAGE}@${ompVersion} (the version of omp) is installed; looked at: ${looked}. Run \`${command}\` to install exactly that version`,
		);
		return { check, lib: null };
	}
	const lib = await loadLibrary(resolution.location);
	check.libraryVersion = lib.version;
	check.libraryPath = lib.pkgDir;
	check.libraryKind = lib.kind;
	const names = PARITY_REQUIREMENTS.map(requirement => requirement.name);
	const missingInLibrary = unmetRequirements(PARITY_FILES.map(file => readFileSync(path.join(lib.pkgDir, file), "utf8")).join("\n"));
	const omp = parityOfOmp(ompBin);
	const ompParityVerified = omp.ompBinary === "scanned";
	check.parity = { checked: names, missingInLibrary, missingInOmp: omp.missing, ompBinary: omp.ompBinary, ompParityVerified };
	if (missingInLibrary.length > 0) reasons.push(`the library package lacks: ${missingInLibrary.join(", ")}`);
	if (omp.missing.length > 0) reasons.push(`omp binary lacks: ${omp.missing.join(", ")}`);
	if (!ompParityVerified) {
		check.warnings.push(
			`the SQL of omp's own binary was not checked (${omp.ompBinary}): parity with omp is not verified here; the exact version match of the library and the self-test are the only evidence`,
		);
	}
	check.ok = reasons.length === 0;
	return { check, lib };
}

/** Like `inspectLibrary`, but refuses (with the reasons, including the `setup` command) when there is no usable library. */
export async function requireLibrary(options: {
	libDir?: string;
	ompBin?: string;
}): Promise<{ check: LibraryCheck; lib: Library }> {
	const { check, lib } = await inspectLibrary(options);
	if (lib === null) throw new Refusal(check.reasons.join("; "));
	return { check, lib };
}

// ---------------------------------------------------------------------------------------------------------
// Schema check

const CORE_TABLES = [
	"working_memory",
	"episodic_memory",
	"memory_embeddings",
	"annotations",
	"consolidation_log",
	"scratchpad",
	"facts",
	"triples",
] as const;

export interface CoreSchema {
	tables: Record<string, string[]>;
	/** Every trigger by name, with its definition in normal form. */
	triggers: Record<string, string>;
	/** Every view by name, with its definition in normal form. */
	views: Record<string, string>;
	fts: boolean;
}

export function readCoreSchema(db: Database): CoreSchema {
	const tables: Record<string, string[]> = {};
	for (const table of CORE_TABLES) {
		const rows = db.query(`PRAGMA table_info(${table})`).all() as { name: string; type: string; pk: number }[];
		tables[table] = rows.map(row => `${row.name}:${row.type}:${row.pk}`).sort();
	}
	const triggers: Record<string, string> = {};
	const views: Record<string, string> = {};
	const objects = db.query("SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')").all() as {
		type: string;
		name: string;
		sql: string | null;
	}[];
	// definitions with every run of whitespace collapsed, so that layout alone is never a difference
	for (const object of objects) (object.type === "trigger" ? triggers : views)[object.name] = (object.sql ?? "").replace(/\s+/g, " ").trim();
	const fts = db.query("SELECT 1 FROM sqlite_master WHERE name = 'fts_working' AND sql LIKE '%fts5%'").get() !== null;
	return { tables, triggers, views, fts };
}

/** The core schema a brand-new library database has (made in a throwaway directory, never under a banks dir). */
export async function freshCoreSchema(lib: Library): Promise<CoreSchema> {
	const dir = mkdtempSync(path.join(tmpdir(), "memory-import-fresh-"));
	try {
		const memory = new lib.Mnemopi(libraryOptions(path.join(dir, "mnemopi.db"), "fresh", DEFAULT_EMBEDDING_MODEL));
		const schema = readCoreSchema(memory.db);
		memory.close();
		return schema;
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
}

export function schemaDiff(fresh: CoreSchema, actual: CoreSchema): string[] {
	const diffs: string[] = [];
	for (const table of CORE_TABLES) {
		const want = fresh.tables[table] ?? [];
		const have = actual.tables[table] ?? [];
		const missing = want.filter(column => !have.includes(column));
		const extra = have.filter(column => !want.includes(column));
		if (missing.length > 0) diffs.push(`${table}: missing ${missing.join(", ")}`);
		if (extra.length > 0) diffs.push(`${table}: unexpected ${extra.join(", ")}`);
	}
	for (const [name, sql] of Object.entries(fresh.triggers)) {
		const have = actual.triggers[name];
		if (have === undefined) diffs.push(`trigger ${name} missing`);
		else if (have !== sql) diffs.push(`trigger ${name} differs from the library's definition`);
	}
	// a trigger or view the library does not make can rewrite rows as they are written or deleted
	for (const name of Object.keys(actual.triggers)) if (fresh.triggers[name] === undefined) diffs.push(`unexpected trigger ${name}`);
	for (const [name, sql] of Object.entries(fresh.views)) {
		const have = actual.views[name];
		if (have === undefined) diffs.push(`view ${name} missing`);
		else if (have !== sql) diffs.push(`view ${name} differs from the library's definition`);
	}
	for (const name of Object.keys(actual.views)) if (fresh.views[name] === undefined) diffs.push(`unexpected view ${name}`);
	if (fresh.fts && !actual.fts) diffs.push("fts_working is not an FTS5 table");
	return diffs;
}

/** omp's default embedding model (mnemopi.embeddingVariant "english", coding-agent/src/mnemopi/config.ts). */
export const DEFAULT_EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5";

/**
 * Options omp's `createMemory` passes (state.ts), minus anything that could consolidate, extract or rebuild.
 * With an `embedder`, embeddings are made by it (as the library's embedding provider) under `embeddingModel`.
 */
export function libraryOptions(
	dbPath: string,
	bank: string,
	embeddingModel: string,
	embedder?: Embedder,
): Record<string, unknown> {
	return {
		dbPath,
		bank,
		sessionId: bank,
		authorId: "coding-agent",
		authorType: "agent",
		channelId: bank,
		...(embedder
			? {
					embeddings: {
						model: embeddingModel,
						provider: {
							async *embed(texts: readonly string[]) {
								yield await embedder.embed(texts);
							},
						},
					},
				}
			: { embeddingModel }),
		llm: false,
		proactiveLinking: false,
		polyphonicRecall: false,
		enhancedRecall: false,
		reconcile: false,
	};
}

export function bankDbPath(banksDir: string, bank: string): string {
	return path.join(banksDir, bank, "mnemopi.db");
}

export function bankExists(banksDir: string, bank: string): boolean {
	return existsSync(bankDbPath(banksDir, bank));
}

/**
 * Refuses a bank the importer must not write: a name that is not one plain directory name, a bank directory or a database
 * file (or its -wal, -shm or -journal file) that is a symbolic link or not a regular file, a database with several hard
 * links, a bank directory that does not sit directly inside the banks directory once links are resolved. `banksDir`
 * itself is the user's and may be a link. A bank that does not exist yet passes: the library creates it. The check is made
 * right before the library opens the bank; a link made after that cannot be excluded (see the README).
 */
export function assertBankWritable(banksDir: string, bank: string): void {
	const label = safeBankLabel(bank);
	if (bank === "" || bank === "." || bank === ".." || /[\\/\0]/.test(bank)) {
		throw new Refusal(`bank ${JSON.stringify(label)}: not a plain directory name`);
	}
	const dir = path.join(banksDir, bank);
	const inspect = (target: string, kind: "directory" | "file"): boolean => {
		let stat: Stats;
		try {
			stat = lstatSync(target);
		} catch (error) {
			if (error instanceof Error && "code" in error && error.code === "ENOENT") return false;
			throw error;
		}
		const name = kind === "directory" ? "its directory" : path.basename(target); // the directory's name is the bank id
		if (stat.isSymbolicLink()) throw new Refusal(`bank ${label}: ${name} is a symbolic link; the importer writes only real bank directories and databases inside the banks directory`);
		if (kind === "directory" ? !stat.isDirectory() : !stat.isFile()) throw new Refusal(`bank ${label}: ${name} is not a regular ${kind}`);
		if (kind === "file" && stat.nlink > 1) throw new Refusal(`bank ${label}: ${name} has several hard links`);
		return true;
	};
	if (!inspect(dir, "directory")) return;
	if (path.dirname(realpathSync(dir)) !== realpathSync(banksDir)) throw new Refusal(`bank ${label}: its directory is not inside the banks directory`);
	for (const suffix of ["", "-wal", "-shm", "-journal"]) inspect(`${bankDbPath(banksDir, bank)}${suffix}`, "file");
}
