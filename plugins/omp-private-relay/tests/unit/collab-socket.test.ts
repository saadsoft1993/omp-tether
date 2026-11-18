import { afterAll, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { PLUGIN_ROOT, waitUntil } from "./support/harness";

// OMP_SRC must be an upstream checkout with the release tags and installed workspace dependencies.
const upstream = process.env.OMP_SRC ?? path.join(os.homedir(), "projects", "oh-my-pi");
const roots: string[] = [];
afterAll(() => { for (const root of roots) fs.rmSync(root, { recursive: true, force: true }); });

function patchedSocket(minor: string): string {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "patched-collab-socket-"));
	roots.push(root);
	const dir = path.join(root, "packages", "collab-web", "src", "lib");
	fs.mkdirSync(dir, { recursive: true });
	for (const name of ["socket.ts", "codec.ts", "link.ts"]) {
		const source = Bun.spawnSync(["git", "-C", upstream, "show", `v${minor}.0:packages/collab-web/src/lib/${name}`]);
		if (source.exitCode !== 0) throw new Error(`Cannot read upstream v${minor}.0 ${name}: ${source.stderr.toString()}`);
		fs.writeFileSync(path.join(dir, name), source.stdout);
	}
	const patch = path.join(PLUGIN_ROOT, "patches", `collab-web-${minor}.patch`);
	const applied = Bun.spawnSync(["git", "apply", "--include=packages/collab-web/src/lib/socket.ts", patch], { cwd: root });
	if (applied.exitCode !== 0) throw new Error(`Cannot apply upstream socket patch: ${applied.stderr.toString()}`);
	fs.symlinkSync(path.join(upstream, "node_modules"), path.join(root, "node_modules"));
	return path.join(dir, "socket.ts");
}

for (const minor of ["18.18", "18.19"]) {
	test(`patched upstream ${minor} CollabSocket treats 4409 as terminal without retry`, async () => {
		// The tagged source is extracted and patched at runtime, so a static import cannot resolve it.
		const { CollabSocket } = await import(patchedSocket(minor));
		let connections = 0;
		const server = Bun.serve({
			port: 0,
			hostname: "127.0.0.1",
			fetch(req, server) { return server.upgrade(req) ? undefined : new Response("upgrade required", { status: 400 }); },
			websocket: { open(ws) { connections++; ws.close(4409, "disconnected by host"); }, message() {} },
		});
		const socket = new CollabSocket({ wsUrl: `ws://127.0.0.1:${server.port}/r/test`, role: "guest", key: crypto.subtle.importKey("raw", new Uint8Array(32), "AES-GCM", false, ["encrypt", "decrypt"]) });
		const closes: Array<[string, boolean]> = [];
		socket.onClose = (reason: string, willReconnect: boolean) => closes.push([reason, willReconnect]);
		try {
			socket.connect();
			expect(await waitUntil(() => closes.length > 0, 2000)).toBe(true);
			expect(closes).toEqual([["disconnected by the host; reload to reconnect", false]]);
			// Observe the real platform timer: upstream's first retry is jittered between 750 and 1250 ms.
			await Bun.sleep(1600);
			expect(connections).toBe(1);
			expect(closes).toHaveLength(1);
		} finally {
			socket.close();
			server.stop(true);
		}
	}, 10_000);
}
