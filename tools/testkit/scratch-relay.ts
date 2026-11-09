// A scratch stand-in for a Collab relay, for tests only. omp publishes a hosted session to `omp collab list` once its
// relay connection is open, so integration tests need something on loopback to accept that connection. This accepts
// `GET /r/<roomId>?role=host`, holds it open and relays nothing: no guests, no frames, no state beyond the connection.
//
//   bun scratch-relay.ts [--port N]     (default: a free port; prints "READY <port>" on stdout once listening)
const ROOM_PATH = /^\/r\/([A-Za-z0-9_-]{10,64})$/;

interface RoomSocket {
	roomId: string;
}

function parsePort(argv: string[]): number {
	const i = argv.indexOf("--port");
	if (i < 0) return 0;
	const n = Number(argv[i + 1]);
	if (!Number.isInteger(n) || n < 0 || n > 65_535) throw new Error(`invalid --port ${argv[i + 1]}`);
	return n;
}

const hosts = new Map<string, Bun.ServerWebSocket<RoomSocket>>();

const server = Bun.serve<RoomSocket>({
	hostname: "127.0.0.1",
	port: parsePort(Bun.argv.slice(2)),
	fetch(req, srv) {
		const url = new URL(req.url);
		const match = ROOM_PATH.exec(url.pathname);
		if (!match || url.searchParams.get("role") !== "host") return new Response("not found", { status: 404 });
		if (srv.upgrade(req, { data: { roomId: match[1] as string } })) return undefined;
		return new Response("websocket upgrade required", { status: 426 });
	},
	websocket: {
		open(ws) {
			if (hosts.has(ws.data.roomId)) ws.close(4009, "a host is already connected for this room");
			else hosts.set(ws.data.roomId, ws);
		},
		message() {},
		close(ws) {
			if (hosts.get(ws.data.roomId) === ws) hosts.delete(ws.data.roomId);
		},
	},
});

console.log(`READY ${server.port}`);
const stop = () => {
	server.stop(true);
	process.exit(0);
};
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
