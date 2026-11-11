/** Every test file raises the per-test timeout itself (see timeout.ts); a file that forgets fails here, not on a slow machine. */
import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

describe("per-file test timeout", () => {
	test("every *.test.ts imports and calls useLongTimeouts()", () => {
		const files = readdirSync(import.meta.dir).filter(name => name.endsWith(".test.ts"));
		expect(files.length).toBeGreaterThan(1);
		const missing = files.filter(name => {
			const source = readFileSync(path.join(import.meta.dir, name), "utf8");
			return !/^import \{ useLongTimeouts \} from "\.\/timeout";$/m.test(source) || !/^useLongTimeouts\(\);$/m.test(source);
		});
		expect(missing).toEqual([]);
	});
});
