# pairing-store/1

How a program that serves a browser decides whether that browser is *paired*: a small directory of files that records,
as hashes, which browsers a person has let in. One program writes it (the relay's own command line, or a dashboard), and
another may read it (the relay, or the same dashboard), so the format and the safety rules are fixed here. The reference
implementation is `plugins/omp-private-relay/src/pairing-store.ts`;
`plugins/omp-private-relay/tests/conformance/pairing-store-suite.ts` checks any reader and any writer against this
document.

Words in capitals (MUST, SHOULD, MAY) have their usual RFC 2119 meaning.

## 1. Scope and trust

- The writer, the reader and the person are **one user on one machine**. The store is protected by file permissions only:
  a process that runs as that user can read and change it, and can already do more harm than pairing itself allows.
- A store holds **hashes**, never a cookie value, session id or pairing token. Reading it lets nobody in: a hash cannot be
  turned back into the cookie that produced it (the cookie is 256 bits of randomness).
- What the store decides is narrow: whether *a cookie that arrived in a request* belongs to a browser a person paired.
  Whether the request comes from the right place (a loopback `Host`, a tailnet owner login, an `Origin`) is the job of
  the program that serves the browser, not of this protocol.

## 2. The directory

| Item | Rule |
|---|---|
| Location | chosen by the program's configuration; it MUST be an absolute path |
| Owner and mode | a real directory (not a symlink) owned by the user that runs the reader, with no permission bits for group or others (`mode & 0077 == 0`) |
| Files | `devices.json` (section 3), `pending.json` (section 5), `.lock` (section 6). Writers create nothing else that stays behind: a temporary file is renamed over its target or removed |
| File owner and mode | regular files (not symlinks, FIFOs or devices) owned by the same user, `mode & 0077 == 0`. Writers create them `0600` |
| Size | a reader refuses a `devices.json` larger than 4 MiB |

Only the last path component is checked. Keep the directory under a parent that only you can write to (a directory in
your home is).

## 3. `devices.json`

A JSON object. Each key is the **lowercase hexadecimal SHA-256 of a cookie value** (64 characters); each value is an
object:

```json
{
  "9f1c…64 hex characters…": { "label": "Firefox on Linux", "paired_at": 1767225600 }
}
```

| Field | Rule |
|---|---|
| key | `hex(sha256(utf8(cookie value)))`, lowercase, 64 characters. The hash is over the cookie value exactly as sent, with no decoding |
| `label` | a short description of the browser, for people. Writers store at most 80 characters and drop control and invisible formatting characters; readers MUST NOT rely on it |
| `paired_at` | integer seconds since the epoch |

A reader uses **only the keys**. A key that is not 64 lowercase hex characters can never match a hash and is ignored; the
store is not refused because of it. An empty object is a valid store: nobody is paired.

## 4. Reading: who is paired, and when the answer is "nobody"

A reader MUST fail closed. It returns the set of keys, or an **empty set with a reason** when the store cannot be
trusted. A reader MUST NOT change the store (no `chmod`, no deletion, no repair), and MUST NOT follow a symlink to
reach it. The reasons, in the order a reader checks them:

| Reason code | When |
|---|---|
| `store-missing` | the directory or `devices.json` does not exist |
| `store-symlink` | the directory or `devices.json` is a symbolic link |
| `store-foreign-owner` | the directory or file belongs to another user |
| `store-open-to-others` | the directory or file has any permission bit for group or others |
| `store-not-regular` | `devices.json` is not a regular file (a FIFO, a directory, a device). A reader MUST NOT block on it |
| `store-too-large` | `devices.json` is larger than 4 MiB |
| `store-invalid-json` | `devices.json` is not valid JSON |
| `store-not-object` | the top-level JSON value is not an object (an array, a string, `null`) |
| `store-unreadable` | any other failure to read it |

Notes:

- A reader SHOULD open the file with `O_NOFOLLOW` and check the *open descriptor*, so a file cannot be swapped between
  the check and the read. Writers replace `devices.json` by renaming (section 6), so a reader never sees a half-written
  file.
- A reader MAY cache the parsed keys while the file's inode, size and modification time are unchanged.

### Matching a cookie

A request is paired when **any** cookie in its `Cookie` header with the configured cookie name has a value whose
SHA-256 (hex, lowercase) is a key. The reader tries every cookie of that name (a stale cookie must not hide a valid one);
it ignores empty values and values longer than 512 bytes. The cookie name is the program's configuration, not part of
the store: a relay that reads a dashboard's store is configured with the dashboard's cookie name.

### Revocation

Revocation is deleting a key from `devices.json`. Because a reader re-reads when the file changes, a revoked browser is
refused on its next request. A program that keeps long-lived connections open (the relay's WebSockets) MUST re-check
them against the store **at least every 30 seconds** (the relay's default is 10) and close the connections of browsers
that are no longer paired, and of every browser when the store becomes unusable. The relay closes them with WebSocket
close code **4401**; from that moment nothing the client sends is relayed, and a connection that ignores the close frame
is cut after 5 seconds.

## 5. Writing

Only a writer creates the files. Writers MUST hold the lock (section 6) for every change, and MUST replace a file by
writing a temporary file in the same directory (created `0600`, flushed) and renaming it over the target.

A writer that finds a file it cannot parse (a zero-byte file is a truncated store, not an empty one), or that fails the owner and mode rules (this includes an existing `.lock`), MUST refuse to change the store and
MUST leave the file as it found it. It MUST NOT overwrite a store it does not understand.

A writer MAY create the directory (`0700`) and MAY tighten its own directory to `0700` when it is merely too open. It
MUST refuse a directory that is a symlink or belongs to someone else.

### Pairing tokens: `pending.json`

A JSON object. Each key is the lowercase hex SHA-256 of a **pairing token**; each value is the expiry as seconds since the
epoch (a number).

- A token is at least 256 bits from a cryptographic random source, written as URL-safe text (base64url, 43 or more
  characters). It is shown to the person once and never stored.
- A token is valid for **300 seconds** and works **once**.

### Redeeming a token

1. Take the lock. Load `pending.json`.
2. Remove the entry for the hash of the presented token, and drop every expired entry. If anything was removed, write
   `pending.json` **before** anything else: the token is spent even if the next step fails, so it can never be spent
   twice. A wrong or unknown token writes nothing (beyond dropping entries that had already expired) and never touches
   `devices.json`.
3. If the token was unknown or expired, the answer is "not paired" (no session).
4. Otherwise create a session id (a new random value of at least 256 bits, URL-safe), add
   `hex(sha256(session))` to `devices.json` with a label and `paired_at`, and return the session id. It becomes the
   browser's cookie value.

### The cookie

The program that redeems the token sets the cookie: `Set-Cookie: <name>=<session>; Path=/; Max-Age=31536000; HttpOnly;
SameSite=Strict`, plus `Secure` when the page is served over https. A program that offers redemption over the network
MUST limit failed attempts, MUST refuse a request that does not carry a custom header (so a plain cross-site form post
cannot redeem), and MUST NOT log a token or a cookie value.

### Revoking

A writer removes the keys whose hash starts with a given prefix of **at least 4 hex characters**, or all keys. A prefix
that matches more than one device is refused and removes nothing. The id that is shown to people for a device is the
first 8 characters of its hash.

## 6. Locking

Every change takes an exclusive advisory `flock(2)` lock on `<directory>/.lock` (a regular file created `0600`, owned by
the user) and holds it for the whole read-modify-write. A writer that cannot get the lock within a few seconds fails
rather than writing without it. Readers take no lock: replacement by rename is what keeps them safe.

## 7. Versioning

`pairing-store/1` changes only by this document. There is no version field in the files: a different layout would be
another directory name or `pairing-store/2`. A reader that finds a `devices.json` shaped otherwise fails closed
(section 4).

## 8. Conformance

An implementation is checked through two command contracts. The suite builds its own fixtures at run time in scratch
directories (symlinks, FIFOs, wrong modes, corrupt files, expired tokens, eight simultaneous redemptions) and never
touches anything else.

**Reader**: `<reader...> check <dir> <cookie-name> <cookie-header>` prints
`{"paired": true|false, "device": "<hash>"|null, "refused": "<reason code>"|null}` and exits 0.

**Writer** (each prints one JSON object; a refused operation exits non-zero):

| Command | Prints |
|---|---|
| `<writer...> create-token <dir>` | `{"token": "…"}` |
| `<writer...> redeem <dir> <token> <label>` | `{"session": "…"}` or `{"session": null}` |
| `<writer...> devices <dir>` | `{"devices": [{"id": "…", "label": "…", "pairedAt": N}, …]}`, oldest first |
| `<writer...> revoke <dir> <prefix\|all>` | `{"revoked": N}` |

Run it:

```
bun plugins/omp-private-relay/tests/conformance/pairing-store-suite.ts \
  --reader-json '["bun","plugins/omp-private-relay/tests/conformance/adapter.ts"]' \
  --writer-json '["bun","plugins/omp-private-relay/tests/conformance/adapter.ts"]'
```

The plugin's own tests also run the suite against deliberately flawed implementations (modes ignored, symlinks followed,
tokens stored in clear, no locking, and others) and require that each flaw is caught.
