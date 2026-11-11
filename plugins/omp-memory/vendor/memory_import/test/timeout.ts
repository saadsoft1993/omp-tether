/**
 * Bun gives every test and hook 5 seconds unless the test file itself raises the limit: `setDefaultTimeout` in a
 * preload or in a shared module applies to ONE file (the first), because the limit is kept per file (Bun 1.4.4), and
 * `[test] timeout` in bunfig.toml is not read. Several tests start real processes (an open-bank holder, the Python
 * redaction bridge, the library), and a busy machine needs more than 5 seconds for three of them in a row. So every test
 * file calls `useLongTimeouts()` at its top; `timeouts.test.ts` fails when a file does not.
 */
import { setDefaultTimeout } from "bun:test";

export const TEST_TIMEOUT_MS = 120_000;

export const useLongTimeouts = (): void => setDefaultTimeout(TEST_TIMEOUT_MS);
