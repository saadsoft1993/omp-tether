/** The embedder the importer gives the Mnemopi library as its embedding provider (a child process by default). */
import path from "node:path";

export interface Embedder {
	embed(texts: readonly string[]): Promise<number[][]>;
	close(): void;
}

const WORKER = path.join(import.meta.dir, "embed_worker.ts");

interface WorkerReply {
	id: number;
	vectors?: number[][];
	error?: string;
}

/** Spawn the embedding worker for `model` (it loads the library package in `pkgDir`). `close()` kills it, whatever it is doing. */
export function createWorkerEmbedder(pkgDir: string, model: string): Embedder {
	const child = Bun.spawn([process.execPath, WORKER], {
		stdin: "pipe",
		stdout: "pipe",
		stderr: "ignore",
		env: {
			...process.env,
			// the library embeds nothing locally when it believes it is under a test runner
			NODE_ENV: "production",
			BUN_ENV: "production",
			MEMORY_IMPORT_LIB_PKG: pkgDir,
			MEMORY_IMPORT_EMBED_MODEL: model,
		},
	});
	const reader = child.stdout.getReader();
	const decoder = new TextDecoder();
	let pending = "";
	let nextId = 0;
	const readLine = async (): Promise<string> => {
		for (;;) {
			const newline = pending.indexOf("\n");
			if (newline >= 0) {
				const line = pending.slice(0, newline);
				pending = pending.slice(newline + 1);
				return line;
			}
			const chunk = await reader.read();
			if (chunk.done) throw new Error("embedding worker exited");
			pending += decoder.decode(chunk.value, { stream: true });
		}
	};
	return {
		async embed(texts) {
			nextId += 1;
			child.stdin.write(`${JSON.stringify({ id: nextId, texts })}\n`);
			child.stdin.flush();
			const reply: WorkerReply = JSON.parse(await readLine());
			if (reply.id !== nextId) throw new Error("embedding worker answered out of order");
			if (reply.error !== undefined || reply.vectors === undefined) throw new Error(reply.error ?? "no vectors");
			return reply.vectors;
		},
		close() {
			child.kill("SIGKILL");
		},
	};
}
