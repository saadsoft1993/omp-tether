/**
 * Runs once before the test files (see bunfig.toml): installs the published Mnemopi library into the scratch directory the
 * tests use, if it is not there yet (needs the network the first time; a preload is not subject to a test timeout). The
 * per-test timeout is not set here: it is kept per file, so each test file sets it itself (see timeout.ts).
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { ensureTestLibrary } from "./helpers";

// The self-test result is cached under the cache directory: the tests use their own, never the user's (read at call time).
const testCache = mkdtempSync(path.join(tmpdir(), "memory-import-test-cache-"));
process.env.OMP_MEMORY_CACHE_DIR = testCache;
process.on("exit", () => rmSync(testCache, { recursive: true, force: true }));

await ensureTestLibrary();
