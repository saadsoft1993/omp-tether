import * as net from "node:net";
import type { FakeHost } from "./fake-host";

/** One connection, line-delimited JSON; resolves each reply in order. */
export class Client {
	private buf = "";
	private frames: Array<Record<string, unknown>> = [];
	private waiting: Array<() => void> = [];
	closed = false;

	private constructor(readonly sock: net.Socket) {
		sock.setEncoding("utf8");
		sock.on("data", (d: string) => {
			this.buf += d;
			for (let nl = this.buf.indexOf("\n"); nl >= 0; nl = this.buf.indexOf("\n")) {
				this.frames.push(JSON.parse(this.buf.slice(0, nl)));
				this.buf = this.buf.slice(nl + 1);
			}
			this.wake();
		});
		const end = () => {
			this.closed = true;
			this.wake();
		};
		sock.on("close", end);
		sock.on("error", end);
	}

	static connect(socketPath: string): Promise<Client> {
		return new Promise((resolve, reject) => {
			const sock = net.createConnection(socketPath);
			sock.once("connect", () => resolve(new Client(sock)));
			sock.once("error", reject);
		});
	}

	static async toHost(host: FakeHost): Promise<{ client: Client; nonce: string; sessionId: string; generation: number }> {
		const reg = await host.waitFor(() => host.registry(), "the registry");
		const client = await Client.connect(reg.data.socket as string);
		return { client, nonce: reg.data.nonce as string, sessionId: reg.data.sessionId as string, generation: reg.data.generation as number };
	}

	private wake() {
		for (const w of this.waiting.splice(0)) w();
	}

	send(frame: unknown) {
		this.sock.write(`${typeof frame === "string" ? frame : JSON.stringify(frame)}\n`);
	}

	async next(timeoutMs = 3000): Promise<Record<string, unknown> | null> {
		const deadline = Date.now() + timeoutMs;
		for (;;) {
			const f = this.frames.shift();
			if (f) return f;
			if (this.closed || Date.now() > deadline) return null;
			await new Promise<void>(r => {
				const t = setTimeout(r, 50);
				this.waiting.push(() => {
					clearTimeout(t);
					r();
				});
			});
		}
	}

	async request(frame: unknown): Promise<Record<string, unknown>> {
		this.send(frame);
		const r = await this.next();
		if (!r) throw new Error("no reply");
		return r;
	}

	close() {
		this.sock.destroy();
	}
}
