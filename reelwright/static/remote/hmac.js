/* SHA-256 and HMAC-SHA256 (FIPS 180-4, RFC 2104) in plain JS for the phone remote.
   Web Crypto (crypto.subtle) only exists on https:// and localhost, and the phone reaches Reelwright at http://<lan-ip>,
   so requests are signed with this instead. Checked against Python's hashlib/hmac by tests/test_remote.py (RFC 4231 vectors). */
(function (root) {
  "use strict";
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
  const bytes = (x) => (typeof x === "string" ? new TextEncoder().encode(x) : x); // strings are UTF-8, like Python's .encode()
  const ror = (x, n) => (x >>> n) | (x << (32 - n));

  function sha256(data) {
    const m = bytes(data), len = m.length, n = ((len + 9 + 63) >> 6) << 6; // message + 0x80 + 64-bit length, in 64-byte blocks
    const buf = new Uint8Array(n);
    buf.set(m);
    buf[len] = 0x80;
    const dv = new DataView(buf.buffer);
    dv.setUint32(n - 8, Math.floor(len / 0x20000000)); // bit length = len * 8, big-endian: high word
    dv.setUint32(n - 4, (len << 3) >>> 0);             // low word
    const H = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    const W = new Uint32Array(64);
    for (let off = 0; off < n; off += 64) {
      for (let i = 0; i < 16; i++) W[i] = dv.getUint32(off + i * 4);
      for (let i = 16; i < 64; i++) {
        const a = W[i - 15], b = W[i - 2];
        W[i] = W[i - 16] + (ror(a, 7) ^ ror(a, 18) ^ (a >>> 3)) + W[i - 7] + (ror(b, 17) ^ ror(b, 19) ^ (b >>> 10)); // wraps mod 2^32
      }
      let [a, b, c, d, e, f, g, h] = H;
      for (let i = 0; i < 64; i++) {
        const t1 = (h + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + W[i]) | 0;
        const t2 = ((ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      H[0] += a; H[1] += b; H[2] += c; H[3] += d; H[4] += e; H[5] += f; H[6] += g; H[7] += h;
    }
    const out = new Uint8Array(32);
    const ov = new DataView(out.buffer);
    H.forEach((v, i) => ov.setUint32(i * 4, v));
    return out;
  }

  function hmac(key, msg) {
    let k = bytes(key);
    if (k.length > 64) k = sha256(k);
    const m = bytes(msg), inner = new Uint8Array(64 + m.length), outer = new Uint8Array(96);
    for (let i = 0; i < 64; i++) {
      inner[i] = (k[i] || 0) ^ 0x36;
      outer[i] = (k[i] || 0) ^ 0x5c;
    }
    inner.set(m, 64);
    outer.set(sha256(inner), 64);
    return sha256(outer);
  }

  const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  const sha256Hex = (d) => hex(sha256(d));
  const hmacHex = (k, m) => hex(hmac(k, m));
  // must match remote.py: _canon() for requests, proof() for the PC's answers
  const signature = (secret, method, path, ts, nonce, body) => hmacHex(secret, `${method}\n${path}\n${ts}\n${nonce}\n${sha256Hex(body || "")}`);
  const proofOk = (secret, nonce, status, body, proof) => {
    const want = hmacHex(secret, `server\n${nonce}\n${status}\n${sha256Hex(body)}`);
    let diff = want.length ^ String(proof || "").length;
    for (let i = 0; i < want.length; i++) diff |= want.charCodeAt(i) ^ String(proof || "").charCodeAt(i);
    return diff === 0;
  };

  // pairing: the QR code + both sides' nonces → this phone's key (remote.py derive_key), so the code alone unlocks nothing later
  const deriveKey = (code, phoneNonce, pcNonce) => hmacHex(code, `key\n${phoneNonce}\n${pcNonce}`);

  const api = { sha256, hmac, hex, sha256Hex, hmacHex, signature, proofOk, deriveKey };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RWCrypto = api;
})(typeof self !== "undefined" ? self : this);
