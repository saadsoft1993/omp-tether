// Runs the extension with a FIFO where it expects a plain file, in a child process: opening a FIFO for writing blocks
// until a reader appears, and a blocked synchronous call inside omp cannot be rescued by a guard or a timer. The parent
// test kills this process after a deadline; "done" on stdout means nothing blocked.
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { FakeHost } from "./fake-host";

const mode = process.argv[2];
const host = await FakeHost.start({ enable: mode === "log" || mode === "off" });
const mkfifo = (file: string) => spawnSync("mkfifo", [file], { stdio: "ignore" });

if (mode === "log") {
	mkfifo(path.join(host.dir, "events.log"));
	await host.fire("session_start");
	await host.waitFor(() => host.registry(), "the registry");
	await host.fire("session_switch"); // logs again
} else if (mode === "enabled") {
	fs.mkdirSync(host.dir, { mode: 0o700 });
	mkfifo(path.join(host.dir, "enabled"));
	await host.fire("session_start");
	console.log(await host.run("session-control", "on"));
} else if (mode === "off") {
	await host.fire("session_start");
	await host.waitFor(() => host.registry(), "the registry");
	mkfifo(path.join(host.dir, "off"));
	fs.rmSync(path.join(host.dir, "off"), { force: true });
	mkfifo(path.join(host.dir, "off"));
	console.log(await host.run("session-control", "off"));
}
await host.dispose();
console.log("done");
process.exit(0);
