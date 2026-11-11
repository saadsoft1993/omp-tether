/**
 * The import ledger: a JSON file OUTSIDE the banks (default `~/.config/omp-memory-import/ledger.json`, 0600).
 *
 * One entry per imported source item, keyed `<source>|<originKey>`. State `imported` means a row was written;
 * `tombstoned` means the row was deleted afterwards (by the user, not by `undo`) and the item must never be
 * imported again. Every row also carries its originKey / importRunId, so the entries can be rebuilt from the banks;
 * tombstones cannot (their rows are gone), which is why this file is the record of them.
 */
import { closeSync, constants, existsSync, linkSync, mkdirSync, openSync, readdirSync, readFileSync, renameSync, statSync, unlinkSync, writeSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { isRecord, Refusal, type SourceId, type SourceMode, writeJsonAtomic } from "./model";
import { fail, readJson } from "./sink";

export const DEFAULT_LEDGER = path.join(homedir(), ".config", "omp-memory-import", "ledger.json");

export interface LedgerEntry {
	source: SourceId;
	originKey: string;
	bank: string;
	rowId: string;
	hash: string;
	runId: string;
	importedAt: string;
	state: "imported" | "tombstoned";
	tombstonedAt?: string;
}

export interface RunRecord {
	runId: string;
	mode: SourceMode;
	at: string;
	imported: number;
	banks: string[];
	undoneAt?: string;
}

export interface LedgerFile {
	version: 1;
	entries: Record<string, LedgerEntry>;
	runs: Record<string, RunRecord>;
	/** Banks whose database this tool created (no bank existed before its first write): bank -> the run and when. */
	banksCreated: Record<string, { runId: string; at: string }>;
}

export const entryKey = (source: SourceId, originKey: string): string => `${source}|${originKey}`;

export function emptyLedger(): LedgerFile {
	return { version: 1, entries: {}, runs: {}, banksCreated: {} };
}

export function loadLedger(file: string): LedgerFile {
	if (!existsSync(file)) return emptyLedger();
	const parsed = readJson(file, "the import ledger");
	if (!isRecord(parsed) || parsed.version !== 1 || !isRecord(parsed.entries) || !isRecord(parsed.runs)) {
		fail("not an import ledger (version 1)", file);
	}
	const ledger = parsed as unknown as LedgerFile;
	return { ...ledger, banksCreated: ledger.banksCreated ?? {} }; // a ledger from before `banksCreated` has none
}

export function saveLedger(file: string, ledger: LedgerFile): void {
	writeJsonAtomic(file, ledger, 0o600);
}

interface LockRecord {
	pid: number;
	token: string;
	startedAt: string;
}

function readLock(file: string): { raw: string; record: LockRecord | null } | null {
	let raw: string;
	try {
		raw = readFileSync(file, "utf8");
	} catch {
		return null; // gone in the meantime
	}
	try {
		const parsed: unknown = JSON.parse(raw);
		if (isRecord(parsed) && typeof parsed.pid === "number" && typeof parsed.token === "string" && typeof parsed.startedAt === "string") {
			return { raw, record: { pid: parsed.pid, token: parsed.token, startedAt: parsed.startedAt } };
		}
	} catch {
		// not a lock record
	}
	return { raw, record: null };
}

const hasCode = (error: unknown, code: string): boolean => error instanceof Error && "code" in error && error.code === code;

/** Whether a process with this pid exists: `kill(pid, 0)` only asks the system; nothing is signalled. */
function processExists(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return !hasCode(error, "ESRCH");
	}
}

/** Writes the whole record to a private file, then publishes it under `file` with link(): exclusive, and never seen half written. */
function publishLock(file: string, token: string, record: string): boolean {
	const temp = `${file}.${token}.tmp`;
	const fd = openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
	try {
		writeSync(fd, record);
	} finally {
		closeSync(fd);
	}
	try {
		linkSync(temp, file);
		return true;
	} catch (error) {
		if (hasCode(error, "EEXIST")) return false;
		throw error;
	} finally {
		unlinkSync(temp);
	}
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** Removes what an earlier, interrupted acquisition may have left (reclaim markers, moved stale locks, temp files) once it is a day old. */
function pruneLockLeftovers(file: string): void {
	const dir = path.dirname(file);
	const base = path.basename(file);
	try {
		for (const name of readdirSync(dir)) {
			if (!name.startsWith(`${base}.`) || !/\.(reclaimed-[0-9a-f]+|stale-[^.]+|[^.]+\.tmp)$/.test(name)) continue;
			const leftover = path.join(dir, name);
			if (Date.now() - statSync(leftover).mtimeMs > DAY_MS) unlinkSync(leftover);
		}
	} catch {
		// housekeeping only
	}
}

/**
 * Takes the lock `file` for `token`. No process ever removes a lock that is not provably a dead process's:
 *  - a lock is published with link() of a fully written file, so it exists whole or not at all;
 *  - a live holder (or an unreadable record) refuses;
 *  - a stale lock (its pid is gone) is reclaimed by the one process that creates the marker `<lock>.reclaimed-<hash of that
 *    record>` (O_EXCL; every acquisition has a unique token, so a record can be reclaimed once, ever). Only that process
 *    moves the lock away with rename(), and it checks that what it moved is the record it inspected. The others wait and look again.
 *    The marker stays (it is what keeps a late reader of the same stale record from moving a live replacement) and is
 *    removed after a day.
 */
async function acquireLock(file: string, token: string, record: string): Promise<void> {
	for (let attempt = 0; attempt < 100; attempt += 1) {
		if (publishLock(file, token, record)) {
			pruneLockLeftovers(file);
			return;
		}
		const held = readLock(file);
		if (held === null) continue; // released between the two calls: try again
		if (held.record === null) throw new Refusal(`${file} exists and is not a lock record; if no import or undo is running, remove it`);
		if (processExists(held.record.pid)) {
			throw new Refusal(
				`another import or undo holds the lock ${file} (process ${held.record.pid}, since ${held.record.startedAt}); wait for it to finish. If that process is not an importer any more, remove the file`,
			);
		}
		const marker = `${file}.reclaimed-${new Bun.CryptoHasher("sha256").update(held.raw).digest("hex").slice(0, 32)}`;
		try {
			closeSync(openSync(marker, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600));
		} catch (error) {
			if (!hasCode(error, "EEXIST")) throw error;
			await Bun.sleep(20); // another process is reclaiming this very record: let it finish, then look again
			continue;
		}
		const moved = `${file}.stale-${token}`;
		try {
			renameSync(file, moved);
		} catch (error) {
			if (hasCode(error, "ENOENT")) continue;
			throw error;
		}
		if (readLock(moved)?.raw === held.raw) {
			unlinkSync(moved); // the dead process's own record, which is what we inspected
			continue; // the path is free: publish
		}
		// not what was inspected (someone replaced it by hand): put it back if the path is still free, and take nothing
		try {
			linkSync(moved, file);
			unlinkSync(moved);
		} catch {
			throw new Refusal(`the lock ${file} changed while it was being reclaimed; the file that was there is now ${moved} (left untouched); if no import or undo is running, remove both`);
		}
		throw new Refusal(`the lock ${file} changed while it was being reclaimed; wait for the other import or undo to finish`);
	}
	throw new Refusal(`${file} is being reclaimed by another process, or an earlier reclaim was interrupted; if no import or undo is running, remove ${file}.reclaimed-* and ${file}`);
}

/**
 * Runs `fn` while this process holds the importer lock, a file next to the ledger (`<ledger>.lock`, 0600, holding the pid, a
 * token and the start time; see `acquireLock` for the protocol). A write operation (import --apply, undo --apply) takes it
 * before it reads the ledger, so two of them never plan against the same state. A lock whose process no longer exists is
 * replaced; nothing is ever signalled or killed. A lock held by a live process (this one included), or one that cannot be read,
 * refuses the operation and names the file. The lock is removed when `fn` ends, whatever the way, and only if it is still ours.
 */
export async function withImporterLock<T>(ledgerFile: string, fn: () => Promise<T>): Promise<T> {
	const file = `${ledgerFile}.lock`;
	mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
	const token = Bun.randomUUIDv7();
	await acquireLock(file, token, `${JSON.stringify({ pid: process.pid, token, startedAt: new Date().toISOString() })}\n`);
	try {
		return await fn();
	} finally {
		if (readLock(file)?.record?.token === token) unlinkSync(file);
	}
}
