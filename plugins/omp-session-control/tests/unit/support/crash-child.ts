// Runs the extension with no uncaught-exception handler of its own in a bare process: anything that escapes a callback
// kills this process (exit code != 0), exactly as it would take down an omp session. Prints "survived" when it did not.
import * as net from "node:net";
import { FakeHost } from "./fake-host";

const host = await FakeHost.start({ enable: true });
// FakeHost registers uncaughtException listeners; drop them so a throw is fatal like in omp.
process.removeAllListeners("uncaughtException");
process.removeAllListeners("unhandledRejection");

host.failSessionId = true; // the listener comes up while omp throws
await host.fire("session_start");
await new Promise(r => setTimeout(r, 200));
host.failSessionId = false;
await host.fire("session_switch");
const reg = await host.waitFor(() => host.registry(), "registry");
host.failIsIdle = true;
host.failSetName = "reject";
for (let i = 0; i < 50; i++) {
	const sock = net.createConnection(reg.data.socket as string);
	sock.on("error", () => {});
	sock.write(`${JSON.stringify({ v: 1, id: i, nonce: reg.data.nonce, op: i % 2 ? "snapshot" : "rename", sessionId: reg.data.sessionId, generation: reg.data.generation, args: { name: "x" } })}\n{bad`);
	if (i % 3 === 0) sock.destroy();
	else sock.end();
}
await new Promise(r => setTimeout(r, 500));
await host.dispose();
console.log("survived");
process.exit(0);
