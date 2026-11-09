// A stand-in for the omp binary: enough of its command line for init, doctor and the launcher, and a log of what it was
// asked, so tests can prove what was (and was not) run. `--version` answers like omp when BUN_BE_BUN is not set and
// like a plain Bun when it is, which is exactly the trap the plugin has to avoid.
import * as fs from "node:fs";
import * as path from "node:path";

export interface FakeOmp {
	bin: string;
	log: string;
	/** Lines the fake was asked to run, one per call. */
	calls(): string[];
}

export function writeFakeOmp(dir: string, opts: { version?: string; relayUrl?: string } = {}): FakeOmp {
	const bin = path.join(dir, "fake bin", "omp"); // a space in the path, on purpose
	const log = path.join(dir, "fake-omp.log");
	fs.mkdirSync(path.dirname(bin), { recursive: true });
	fs.writeFileSync(
		bin,
		`#!/bin/sh
if [ "$1" = "--version" ]; then
  if [ -n "\${BUN_BE_BUN:-}" ]; then echo 1.4.4; else echo "omp/${opts.version ?? "18.18.0"}"; fi
  exit 0
fi
if [ "$1" = "config" ]; then
  echo "BUN_BE_BUN=\${BUN_BE_BUN:-unset} $*" >> '${log}'
  if [ "$2" = "get" ]; then echo '${opts.relayUrl ?? ""}'; fi
  exit 0
fi
echo "BUN_BE_BUN=\${BUN_BE_BUN:-unset} $*" >> '${log}'
exit 0
`,
		{ mode: 0o755 },
	);
	return { bin, log, calls: () => (fs.existsSync(log) ? fs.readFileSync(log, "utf8").trim().split("\n").filter(Boolean) : []) };
}
