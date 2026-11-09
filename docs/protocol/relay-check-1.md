# relay-check/1

How a program proves that the thing listening on a loopback port is **the relay this user started**, and not another
program that took the port while the relay was down. The reference implementation is `plugins/omp-private-relay`
(`src/relay.ts` answers, `src/listener-check.ts` asks).

Words in capitals (MUST, SHOULD, MAY) have their usual RFC 2119 meaning.

## 1. Why it exists

Both relay listeners are plain TCP ports on `127.0.0.1`. When the relay is not running (a crash, a restart, a login that
starts another program first) another account on the same machine can bind the port. Anything that then sends a browser
there, or tells an omp session to connect there, would hand that account room links and cookies. Checking *who owns the
listening socket* helps, but the platform tools cannot always show it (see below); this proof does not depend on them.

## 2. The key

At every start the relay writes a fresh random key: 32 bytes from a cryptographic random source, as 64 lowercase hex
characters, to `<check directory>/check.key`.

| Item | Rule |
|---|---|
| Directory | a real directory owned by the user, mode `0700`, created if missing; the relay refuses to start the check (and answers `503`) when it is a symlink, someone else's or open to others |
| File | regular, owned by the user, mode `0600`, written through a temporary file and renamed. A verifier MUST refuse a key file that is a symlink, not a regular file, not owned by the user or open to others |
| Lifetime | replaced at every start; an old key never proves a new relay |

## 3. The challenge

`GET /__relay_check?nonce=<hex>` on **either** listener, where `nonce` is 32 to 128 lowercase hex characters chosen fresh
by the verifier (16 random bytes are enough).

The relay answers `200` with `{"mac": "<hex>"}` where

```
mac = hex( HMAC-SHA256( key, "omp-private-relay-check/1:" + port + ":" + nonce ) )
```

`port` is the port the request arrived on (decimal), so an answer from the host listener cannot be replayed as an answer
from the guest listener.

The relay answers only a **direct loopback client**: `Host` MUST be exactly `127.0.0.1:<port>`, and the request MUST NOT
carry `Origin`, `Tailscale-User-Login`, `X-Forwarded-For` or `Forwarded`; otherwise `403`. A malformed nonce gets `400`;
a relay that has no key (it could not write one) gets `503`.

## 4. Verifying

1. Read the key (section 2, with the file checks).
2. Send the challenge to `http://127.0.0.1:<port>/__relay_check?nonce=<fresh nonce>`. A verifier MUST NOT follow
   redirects and SHOULD time out within a few seconds.
3. Compute the expected `mac` and compare in **constant time**. Anything else (a different value, a non-200 status, a
   malformed body, no answer) means "not this relay".

A program that only knows a port number cannot answer: it does not have the key, and the key is not derivable from
anything it can observe, because each answer is under a fresh nonce.

## 5. Authenticated counts

`GET /__relay_stats?nonce=<hex>` on the **host** listener, under the same restrictions as the challenge, answers `200` with
`{"body": "<the counts as a JSON text>", "mac": "<hex>"}` where

```
mac = hex( HMAC-SHA256( key, "omp-private-relay-check/1:stats:" + port + ":" + nonce + ":" + body ) )
```

A verifier that wants to *show* the counts (`omp-private-relay status`) believes them only when this MAC verifies over exactly
those bytes under a fresh nonce, reads at most 64 KiB (65,536 bytes) of the answer, counting bytes as they arrive and abandoning the stream before keeping a chunk that would pass the bound (a multibyte character, or one split across chunks, is neither miscounted nor corrupted), and does not follow redirects. A proof of identity made in
an earlier request does not authenticate a later response: another program could have replaced the relay in between.

## 6. What it does not prove

- A process that runs as the same user can read the key file and answer. The protocol protects against **other accounts**
  and against programs that are not the relay; a same-user process is trusted by the whole design.
- The proof is about the moment it is asked. A verifier that wants to rely on it for a link it hands out SHOULD ask just
  before handing it out, and SHOULD also check who owns the listening socket where the platform shows it
  (`lsof` on macOS; `/proc/net/tcp` or `ss -e` on Linux), treating "cannot see the owner" as unknown, never as "ours".
- A browser page that is already open can still be talking to whatever holds the port; the proof cannot reach into it.

## 7. Versioning

`relay-check/1` changes only by this document. The prefix in the MAC input carries the version; a verifier that knows a
different prefix sees a wrong proof and fails closed.
