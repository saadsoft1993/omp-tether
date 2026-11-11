#!/usr/bin/env bun
/**
 * Entry of the memory importer: `bun cli.ts <cmd> --source <claude-memory|hindsight|claude-mem-summaries> [flags]`. The commands
 * and their flags are in main.ts, the rules in README.md. Exit status: 0 success; 1 a refusal (a gate, a check, the network) or
 * a failed check; 2 wrong flags or an unusable environment; 3 an unexpected failure.
 *
 * This file loads nothing before its handlers exist. It installs the last-resort handlers first and loads the application
 * afterwards, with a dynamic import inside a try, so that a failure while the application's own modules load or evaluate (a
 * syntax error, a missing file, a module that throws) is caught by the same handlers as one that happens later. Every such
 * failure prints one line (`unexpected failure (<kind>): <message>`), with the message passed through the sink's label policy
 * when the sink can be loaded and withheld when it cannot, and exits with status 3.
 */
import type * as Sink from "./sink"; // type only: erased, so nothing is loaded before the handlers are installed

function lastResort(kind: string, error: unknown): never {
	let message = "(message withheld)";
	try {
		// `require`, not `import`: this runs inside a handler, and a sink that cannot be loaded must fall back to the withheld text
		const sink: typeof Sink = require("./sink"); // the module's own type; `require` is untyped
		message = sink.describeFailure(error);
	} catch {
		// the sink itself failed to load
	}
	try {
		process.stderr.write(`unexpected failure (${kind}): ${message}\n`);
	} catch {
		// stderr is closed: nothing left to say it to
	}
	process.exit(3);
}

process.on("uncaughtException", error => lastResort("an uncaught exception", error));
process.on("unhandledRejection", reason => lastResort("an unhandled rejection", reason));

try {
	// a dynamic import on purpose: a static one would fail before the handlers above exist (see the header)
	const { runCommandLine } = await import("./main");
	process.exit(await runCommandLine(process.argv.slice(2)));
} catch (error) {
	lastResort("the importer could not be loaded or stopped", error);
}
