// Child process for the install-lock tests: `hold <target> <ms>` takes the install lock and keeps it (the lock file stays
// empty), `install <target> <tag> <count> <go-file>` waits for the go file and then installs <count> times.
import * as fs from "node:fs";
import * as path from "node:path";
import { getFlock, LOCK_EX, LOCK_NB } from "../../../src/flock";
import { installTree, sha256, treeHash } from "../../../src/web-asset";

const [mode, target, a, b, c] = Bun.argv.slice(2) as [string, string, string, string, string];

if (mode === "hold") {
	const flock = getFlock();
	if (!flock) throw new Error("no flock");
	const fd = fs.openSync(path.join(path.dirname(target), `.${path.basename(target)}.install-lock`), fs.constants.O_RDWR | fs.constants.O_CREAT, 0o600);
	if (flock(fd, LOCK_EX | LOCK_NB) !== 0) throw new Error("could not lock");
	console.log("held");
	await Bun.sleep(Number(a));
} else {
	while (!fs.existsSync(c)) await Bun.sleep(5);
	for (let i = 0; i < Number(b); i++) {
		const files = new Map([["index.html", Buffer.from(`${a}${i}`)], ["NOTICE", Buffer.from("notice")]]);
		installTree(files, { ompTag: "v1.0.0", bun: "1", asset: "a", sha256: "", treeSha256: treeHash(files), noticeSha256: sha256(Buffer.from("notice")), bytes: 0, files: 2 }, target);
	}
}
