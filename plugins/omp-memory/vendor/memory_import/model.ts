/** Shared types and small helpers of the memory importer. */
import { randomBytes } from "node:crypto";
import { closeSync, constants, fchmodSync, fsyncSync, mkdirSync, openSync, realpathSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

/** A command that stops before it writes anything because a rule or a check said no (exit status 1 from the command line). */
export class Refusal extends Error {}

/** Wrong flags or arguments: the command was not understood (exit status 2 from the command line). */
export class UsageError extends Refusal {}

/**
 * `~/...` for every path below the home directory in `text`, so that what is printed does not carry the account name. A home
 * that is a symbolic link is recognised both as written in $HOME and by its real path (the importer reports realpaths).
 */
export function tildeText(text: string, home: string = homedir()): string {
	let real = home;
	try {
		real = realpathSync(home);
	} catch {
		// a home that cannot be resolved is shown as written
	}
	let out = text;
	for (const prefix of new Set([home, real])) {
		if (prefix !== "" && prefix !== "/") out = out.split(`${prefix.replace(/\/+$/, "")}/`).join("~/");
	}
	return out;
}

export type SourceId = "claude-code" | "hindsight" | "claude-mem";
export type SourceMode = "claude-memory" | "hindsight" | "claude-mem-summaries";
export type SourceKind = "memory" | "summary";

export const MODE_SOURCE: Readonly<Record<SourceMode, { source: SourceId; sourceKind: SourceKind }>> = {
	"claude-memory": { source: "claude-code", sourceKind: "memory" },
	hindsight: { source: "hindsight", sourceKind: "memory" },
	"claude-mem-summaries": { source: "claude-mem", sourceKind: "summary" },
};

/** Where a source item should land. `unassigned` is a real bank that omp never recalls from (review queue). */
export const UNASSIGNED_BANK = "unassigned";
export const GLOBAL_BANK = "default";

/** One thing to import, as read from a source, before redaction. */
export interface SourceItem {
	source: SourceId;
	sourceKind: SourceKind;
	originKey: string;
	origin: string;
	/** sha256 of the normalised source content (volatile fields stripped). */
	originHash: string;
	name: string | null;
	description: string | null;
	claudeType: string | null;
	content: string;
	/** ISO timestamp kept as the row `timestamp`. */
	timestamp: string;
	/** Real folder path (realpath when it exists), or null when none could be resolved. */
	cwd: string | null;
	cwdExists: boolean;
	cwdRaw: string | null;
	/** How the folder was found: jsonl, decode, project-map, session, ... or why not. */
	folderHow: string;
	/** Source-specific extras shown in dry-run (never written except the listed provenance keys). */
	extra: Record<string, string | number | boolean | null>;
	importance: number;
	memoryType: string;
}

export interface ParseError {
	where: string;
	error: string;
}

export interface UnresolvedFolder {
	where: string;
	reason: string;
	candidates: string[];
	items: number;
}

export interface Collected {
	items: SourceItem[];
	errors: ParseError[];
	/** Problems that did not stop an item from being read (front matter oddities, unknown type). */
	warnings: ParseError[];
	unresolved: UnresolvedFolder[];
	/** Skips decided by the source reader itself (wrong fact type, mnemopi origin, MEMORY.md ...). */
	excluded: Record<string, number>;
}

export interface ReportCounts {
	found: number;
	imported: number;
	skippedDuplicate: number;
	skippedTombstone: number;
	skippedUnchanged: number;
	changedSource: number;
	errors: number;
	skippedHeld: number;
	skippedUnassigned: number;
	/** Would be imported, but its bank is outside --only-new-banks / --banks: not written, not in the ledger. */
	skippedNotTargeted: number;
	/** Binary content (a NUL byte, or more than 2% replacement characters): never imported, listed in `binary`. */
	skippedBinary: number;
	/** Longer than the redaction step accepts (1,000,000 characters): never imported, listed in `tooLarge`. */
	skippedTooLarge: number;
	/** Items that this run would write (a dry run's plan; the number `state.json` records). */
	wouldImport: number;
	excludedBySource: number;
}

export interface Report {
	runId: string;
	command: string;
	source: SourceMode;
	apply: boolean;
	counts: ReportCounts;
	perBank: { bank: string; cwd: string | null; imported: number; wouldImport: number; skipped: number; notTargeted: number }[];
	unresolved: UnresolvedFolder[];
	errors: { where: string; error: string }[];
	warnings: { where: string; error: string }[];
	byFolder: Record<string, Record<string, number>>;
	byType: Record<string, number>;
	excluded: Record<string, number>;
	changed: { originKey: string; bank: string }[];
	tombstoned: { originKey: string; bank: string }[];
	/** Items held back (proxy residual findings and hold rules): originKey and finding kinds only, never text. */
	held: { originKey: string; kinds: string[] }[];
	/** Items that are not text: originKey and why, never content. */
	binary: { originKey: string; reason: string }[];
	/** Items over the size limit: originKey, their length, the limit and the sentence to show. */
	tooLarge: { originKey: string; chars: number; limit: number; reason: string }[];
	redaction: Record<string, number>;
	library: unknown;
	/** `dry-run` only: sha256 of the plan (items to write, changed or held, by bank, origin key and hash); see engine.ts. */
	planDigest?: string;
	samplesFile?: string;
	generatedAt: string;
	extra?: Record<string, unknown>;
}

export const sha256 = (text: string): string => new Bun.CryptoHasher("sha256").update(text).digest("hex");

export function emptyCounts(): ReportCounts {
	return {
		found: 0,
		imported: 0,
		skippedDuplicate: 0,
		skippedTombstone: 0,
		skippedUnchanged: 0,
		changedSource: 0,
		errors: 0,
		skippedHeld: 0,
		skippedUnassigned: 0,
		skippedNotTargeted: 0,
		skippedBinary: 0,
		skippedTooLarge: 0,
		wouldImport: 0,
		excludedBySource: 0,
	};
}

/** Collapse line endings and trailing blanks so a re-save without edits hashes the same. */
export function normaliseText(text: string): string {
	return text
		.replace(/\r\n?/g, "\n")
		.split("\n")
		.map(line => line.trimEnd())
		.join("\n")
		.trim();
}

/** First `max` characters on one line, for logs and samples (never more than ~80 chars of memory text). */
export function snippet(text: string, max = 80): string {
	const flat = text.replace(/\s+/g, " ").trim();
	return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

export function newRunId(now: Date = new Date()): string {
	const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
	return `imp-${stamp}-${Bun.randomUUIDv7().slice(-6)}`;
}

/**
 * Atomic write with the given mode: a new temp file in the same directory, then rename. The temp name is random and the file
 * is created exclusively without following links (`O_EXCL | O_NOFOLLOW`), so a file or link left at a guessed name is never
 * written through; the mode is set on the open descriptor.
 */
export function writeJsonAtomic(file: string, value: unknown, mode = 0o600): void {
	mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
	let temp = "";
	let fd = -1;
	for (let attempt = 0; fd < 0; attempt += 1) {
		temp = `${file}.${randomBytes(8).toString("hex")}.tmp`;
		try {
			fd = openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, mode);
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== "EEXIST" || attempt >= 8) throw error;
		}
	}
	try {
		fchmodSync(fd, mode);
		writeFileSync(fd, `${JSON.stringify(value, null, 1)}\n`);
		fsyncSync(fd);
		closeSync(fd);
		fd = -1;
		renameSync(temp, file);
	} catch (error) {
		if (fd >= 0) closeSync(fd);
		try {
			unlinkSync(temp);
		} catch {
			// already renamed or never created
		}
		throw error;
	}
}

export function bump(map: Record<string, number>, key: string, by = 1): void {
	map[key] = (map[key] ?? 0) + by;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}
