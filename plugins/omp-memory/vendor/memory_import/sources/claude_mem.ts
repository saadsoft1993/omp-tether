/**
 * claude-mem `session_summaries` (observations and user_prompts are never read). The database is first copied with
 * sqlite's online backup into a private temp directory, so the live (WAL) file is never opened for writing and is
 * never locked. A summary's folder is the working directory of its Claude session: `sdk_sessions.content_session_id`
 * is the session id, and the session's `.jsonl` file sits in a project directory whose folder is resolved by
 * folders.ts. When no session file is found, the `project` name decides only if exactly one known folder has it.
 */
import { Database } from "bun:sqlite";
import { chmodSync, existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { type FolderResolution, normaliseFolder, resolveProjectFolder } from "../folders";
import { type Collected, normaliseText, sha256 } from "../model";
import { fail, guarded, onProcessEvent } from "../sink";
import { projectDirsByName } from "./claude_memory";
import type { ProjectMap } from "./hindsight";

export interface ClaudeMemOptions {
	dbPath: string;
	claudeHome: string;
	sqlite3?: string;
	/** `project name -> folder` decisions made by a reviewer (the same file the Hindsight mode uses). */
	projectMap?: ProjectMap;
	/** Existing folders that count as candidates for a project name besides the Claude project directories. */
	extraFolders?: string[];
}

interface SummaryRow {
	id: number;
	memory_session_id: string;
	project: string;
	request: string | null;
	investigated: string | null;
	learned: string | null;
	completed: string | null;
	next_steps: string | null;
	notes: string | null;
	created_at: string;
	content_session_id: string | null;
}

const FIELDS: [keyof SummaryRow, string][] = [
	["request", "Request"],
	["investigated", "Investigated"],
	["learned", "Learned"],
	["completed", "Completed"],
	["next_steps", "Next steps"],
	["notes", "Notes"],
];

const liveSnapshots = new Set<string>();
let hooked = false;

function removeAllSnapshots(): void {
	for (const dir of liveSnapshots) rmSync(dir, { recursive: true, force: true });
	liveSnapshots.clear();
}

/** A snapshot copy holds the whole database for as long as it exists: an exit or an interrupt removes it too. */
function hookCleanup(): void {
	if (hooked) return;
	hooked = true;
	onProcessEvent("exit", guarded("snapshot cleanup at exit", removeAllSnapshots));
	for (const [signal, code] of [["SIGINT", 130], ["SIGTERM", 143]] as const) {
		onProcessEvent(signal, guarded("snapshot cleanup on a signal", removeAllSnapshots, code), true);
	}
}

/** Removes a snapshot directory made by {@link snapshotDatabase}. */
export function removeSnapshot(dir: string): void {
	liveSnapshots.delete(dir);
	rmSync(dir, { recursive: true, force: true });
}

/** `file:` URI of a path for sqlite: every byte that is not a path character is percent-encoded, so a `?`, `#` or `%` in the path stays in the path. */
export const sqliteUri = (dbPath: string): string => `file:${dbPath.split("/").map(encodeURIComponent).join("/")}?mode=ro`;

/**
 * Online backup of `dbPath` into a fresh private directory (0700). Caller removes the returned directory with
 * {@link removeSnapshot}; an exit or an interrupt before that removes it as well (a kill that cannot be caught leaves it).
 */
export function snapshotDatabase(dbPath: string, sqlite3 = "sqlite3"): { dir: string; file: string } {
	const dir = mkdtempSync(path.join(tmpdir(), "memory-import-snapshot-"));
	liveSnapshots.add(dir);
	hookCleanup();
	const file = path.join(dir, "snapshot.db");
	const ran = Bun.spawnSync([sqlite3, "-readonly", sqliteUri(dbPath), `.backup '${file}'`], { stdout: "pipe", stderr: "pipe" });
	if (ran.exitCode !== 0) {
		removeSnapshot(dir);
		fail("could not snapshot the claude-mem database", dbPath); // sqlite's own message is not shown: it names paths and may quote data
	}
	chmodSync(file, 0o600);
	return { dir, file };
}

function sessionIndex(dirsByName: Map<string, string[]>): Map<string, string> {
	const index = new Map<string, string>();
	for (const [dirName, dirs] of dirsByName) {
		for (const dir of dirs) {
			let names: string[];
			try {
				names = readdirSync(dir);
			} catch {
				continue;
			}
			for (const name of names) if (name.endsWith(".jsonl")) index.set(name.slice(0, -".jsonl".length), dirName);
		}
	}
	return index;
}

export async function collectClaudeMemSummaries(options: ClaudeMemOptions): Promise<Collected> {
	const collected: Collected = { items: [], errors: [], warnings: [], unresolved: [], excluded: {} };
	if (!existsSync(options.dbPath)) {
		collected.errors.push({ where: options.dbPath, error: "claude-mem database not found" });
		return collected;
	}
	const snapshot = snapshotDatabase(options.dbPath, options.sqlite3);
	let rows: SummaryRow[];
	try {
		// The snapshot is our own private copy (a WAL-mode file cannot be opened readonly without its -shm), so open it plainly.
		const db = new Database(snapshot.file);
		try {
			rows = db
				.query(
					`SELECT s.id, s.memory_session_id, s.project, s.request, s.investigated, s.learned, s.completed,
						s.next_steps, s.notes, s.created_at, d.content_session_id
					 FROM session_summaries s LEFT JOIN sdk_sessions d ON d.memory_session_id = s.memory_session_id
					 ORDER BY s.id`,
				)
				.all() as SummaryRow[];
		} finally {
			db.close();
		}
	} catch (error) {
		collected.errors.push({ where: options.dbPath, error: `schema not recognised: ${String(error).slice(0, 200)}` });
		return collected;
	} finally {
		removeSnapshot(snapshot.dir);
	}

	const dirsByName = projectDirsByName(options.claudeHome);
	const sessions = sessionIndex(dirsByName);
	const resolutions = new Map<string, FolderResolution>();
	const resolveDir = async (dirName: string): Promise<FolderResolution> => {
		let resolution = resolutions.get(dirName);
		if (!resolution) {
			resolution = await resolveProjectFolder(dirName, dirsByName.get(dirName) ?? []);
			resolutions.set(dirName, resolution);
		}
		return resolution;
	};
	// project name -> folders, from every Claude project dir that resolved (fallback when the session file is gone)
	const byProjectName = new Map<string, Set<string>>();
	for (const dirName of dirsByName.keys()) {
		const resolution = await resolveDir(dirName);
		if (!resolution.folder) continue;
		const key = path.basename(resolution.folder.path).toLowerCase();
		byProjectName.set(key, (byProjectName.get(key) ?? new Set()).add(resolution.folder.path));
	}
	for (const extra of options.extraFolders ?? []) {
		const key = path.basename(extra).toLowerCase();
		byProjectName.set(key, (byProjectName.get(key) ?? new Set()).add(normaliseFolder(extra).path));
	}

	const unresolved = new Map<string, { reason: string; candidates: string[]; items: number }>();
	for (const row of rows) {
		const body = FIELDS.filter(([key]) => (row[key] ?? "").toString().trim() !== "").map(
			([key, label]) => `${label}: ${(row[key] as string).trim()}`,
		);
		if (body.length === 0) {
			collected.errors.push({ where: `session_summaries:${row.id}`, error: "empty summary" });
			continue;
		}
		let folder: FolderResolution["folder"] = null;
		let how = "unresolved";
		const dirName = row.content_session_id ? sessions.get(row.content_session_id) : undefined;
		if (dirName !== undefined) {
			const resolution = await resolveDir(dirName);
			folder = resolution.folder;
			how = folder ? `session-${resolution.how}` : "unresolved";
		}
		if (folder === null) {
			const mapped = options.projectMap?.projects[row.project.toLowerCase()];
			const named = [...(byProjectName.get(row.project.toLowerCase()) ?? [])];
			if (typeof mapped === "string") {
				folder = normaliseFolder(mapped);
				how = "project-map";
			} else if (named.length === 1 && named[0] !== undefined) {
				folder = { path: named[0], exists: existsSync(named[0]), raw: named[0] };
				how = "project-name";
			} else {
				const key = row.project;
				const entry = unresolved.get(key) ?? {
					reason:
						dirName === undefined
							? `session file not found; project name "${row.project}" matches ${named.length} known folders`
							: "session folder not resolved",
					candidates: named,
					items: 0,
				};
				entry.items += 1;
				unresolved.set(key, entry);
			}
		}
		const title = `Session summary ${row.created_at.slice(0, 10)} (${row.project})`;
		const content = [title, ...body].join("\n");
		const folderKey = folder?.path ?? null;
		const created = Date.parse(row.created_at);
		collected.items.push({
			source: "claude-mem",
			sourceKind: "summary",
			originKey: `summaries#${row.id}`,
			origin: `session_summaries:${row.id}`,
			originHash: sha256(normaliseText(content)),
			name: title,
			description: null,
			claudeType: null,
			content,
			timestamp: new Date(Number.isNaN(created) ? 0 : created).toISOString(),
			cwd: folderKey,
			cwdExists: folder?.exists ?? false,
			cwdRaw: folder?.raw ?? null,
			folderHow: how,
			extra: { project: row.project, memorySessionId: row.memory_session_id },
			importance: 0.6,
			memoryType: "episode",
		});
	}
	for (const [project, entry] of [...unresolved].sort(([a], [b]) => a.localeCompare(b))) {
		collected.unresolved.push({ where: `claude-mem project "${project}"`, ...entry });
	}
	return collected;
}
