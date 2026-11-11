/**
 * Which real folder a Claude project directory belongs to, and which omp bank that folder is.
 *
 * Order (plan v3.1): (1) the `cwd` recorded in the project's session `.jsonl` files, (2) the directory name decoded
 * into paths that exist on disk, (3) unresolved -> the `unassigned` bank, listed for review. Every folder is
 * normalised with realpath (when it exists) before the bank id is computed with omp's own `projectBankSegment`.
 */
import { existsSync, type Dirent, readdirSync, realpathSync, statSync } from "node:fs";
import path from "node:path";
import { bump } from "./model";

/** How Claude Code names a project directory: every character outside [A-Za-z0-9] becomes `-`. */
export const encodeProjectName = (folder: string): string => folder.replace(/[^A-Za-z0-9]/g, "-");

export interface Folder {
	path: string;
	exists: boolean;
	/** The path as recorded (resolved, symlinks kept): omp hashes this form, so it names the bank omp used. */
	raw: string;
}

export function normaliseFolder(folder: string): Folder {
	const resolved = path.resolve(folder).replace(/\/+$/, "") || "/";
	try {
		return { path: realpathSync.native(resolved), exists: true, raw: resolved };
	} catch {
		return { path: resolved, exists: false, raw: resolved };
	}
}

export interface FolderResolution {
	folder: Folder | null;
	how: string;
	candidates: string[];
	reason: string | null;
}

const JSONL_HEAD_BYTES = 128 * 1024;
const JSONL_FILES = 8;

/** The most common `cwd` in the first records of the newest session files, preferring the one the dir name encodes. */
export async function jsonlCwd(projectDirs: string[], dirName: string): Promise<{ cwd: string; votes: number } | null> {
	const files: { file: string; mtime: number }[] = [];
	for (const dir of projectDirs) {
		let names: string[];
		try {
			names = readdirSync(dir).filter(name => name.endsWith(".jsonl"));
		} catch {
			continue;
		}
		for (const name of names) {
			const file = path.join(dir, name);
			try {
				files.push({ file, mtime: statSync(file).mtimeMs });
			} catch {
				// vanished between listing and stat
			}
		}
	}
	files.sort((a, b) => b.mtime - a.mtime);
	const votes: Record<string, number> = {};
	for (const { file } of files.slice(0, JSONL_FILES)) {
		let head: string;
		try {
			head = await Bun.file(file).slice(0, JSONL_HEAD_BYTES).text();
		} catch {
			continue;
		}
		const lines = head.split("\n");
		lines.pop(); // the last line may be cut by the byte limit
		for (const line of lines) {
			if (!line.includes('"cwd"')) continue;
			try {
				const record: { cwd?: unknown } = JSON.parse(line);
				if (typeof record.cwd === "string" && record.cwd.startsWith("/")) {
					bump(votes, record.cwd);
					break;
				}
			} catch {
				// a cut or foreign line: keep looking
			}
		}
	}
	const ranked = Object.entries(votes).sort((a, b) => b[1] - a[1]);
	if (ranked.length === 0) return null;
	const matching = ranked.find(([cwd]) => encodeProjectName(cwd) === dirName);
	const [cwd, count] = matching ?? ranked[0] ?? ["", 0];
	return { cwd, votes: count };
}

const listCache = new Map<string, Dirent[]>();

function subdirs(dir: string): Dirent[] {
	let entries = listCache.get(dir);
	if (!entries) {
		try {
			entries = readdirSync(dir, { withFileTypes: true }).filter(entry => {
				if (entry.isDirectory()) return true;
				if (!entry.isSymbolicLink()) return false;
				try {
					return statSync(path.join(dir, entry.name)).isDirectory();
				} catch {
					return false;
				}
			});
		} catch {
			entries = [];
		}
		listCache.set(dir, entries);
	}
	return entries;
}

/** Every existing directory whose encoded path equals `dirName` (a leading `-` is the root `/`). */
export function decodeProjectDir(dirName: string, limit = 12): string[] {
	const found: string[] = [];
	const walk = (dir: string, rest: string): void => {
		if (found.length >= limit) return;
		for (const entry of subdirs(dir)) {
			const encoded = encodeProjectName(entry.name);
			if (encoded === "") continue;
			const next = path.join(dir, entry.name);
			if (rest === encoded) found.push(next);
			else if (rest.startsWith(`${encoded}-`)) walk(next, rest.slice(encoded.length + 1));
		}
	};
	if (dirName.startsWith("-")) walk("/", dirName.slice(1));
	return found;
}

export async function resolveProjectFolder(dirName: string, projectDirs: string[]): Promise<FolderResolution> {
	const recorded = await jsonlCwd(projectDirs, dirName);
	if (recorded) {
		const folder = normaliseFolder(recorded.cwd);
		return { folder, how: folder.exists ? "jsonl-cwd" : "jsonl-cwd-gone", candidates: [], reason: null };
	}
	const candidates = [...new Set(decodeProjectDir(dirName).map(candidate => normaliseFolder(candidate).path))];
	if (candidates.length === 1 && candidates[0] !== undefined) {
		return { folder: { path: candidates[0], exists: existsSync(candidates[0]), raw: candidates[0] }, how: "decoded-dirname", candidates, reason: null };
	}
	return {
		folder: null,
		how: "unresolved",
		candidates,
		reason: candidates.length === 0 ? "no session cwd and the directory name decodes to no existing folder" : "directory name decodes to several existing folders",
	};
}
