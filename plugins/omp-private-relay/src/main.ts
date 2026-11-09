// Service entry point: `omp src/main.ts` with BUN_BE_BUN=1 (omp's embedded Bun), or any Bun >= 1.4.4.
// Reads the configuration, starts the relay in the foreground and stops it on SIGINT/SIGTERM.
import { type Config, ConfigError, loadConfig } from "./config";
import { type RelayHandle, startRelay } from "./relay";
import { checkBun } from "./runtime";

export function main(): number | null {
	const problem = checkBun(Bun.version);
	if (problem) {
		console.error(problem);
		return 1;
	}
	let config: Config;
	try {
		config = loadConfig();
	} catch (err) {
		console.error(err instanceof ConfigError ? err.message : `cannot read the configuration: ${String(err)}`);
		return 1;
	}
	let relay: RelayHandle;
	try {
		relay = startRelay(config);
	} catch (err) {
		console.error(`cannot start: ${err instanceof Error ? err.message : String(err)}`);
		return 1;
	}
	let stopping = false;
	const shutdown = (): void => {
		if (stopping) return;
		stopping = true;
		relay.stop();
		process.exit(0);
	};
	process.on("SIGINT", shutdown);
	process.on("SIGTERM", shutdown);
	return null;
}

if (import.meta.main) {
	const code = main();
	if (code !== null) process.exit(code);
}
