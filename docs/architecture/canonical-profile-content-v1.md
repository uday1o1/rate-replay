# CanonicalProfileContentV1

Status: Frozen before normalized persistence.

The domain separator is the ASCII byte string `RateReplay.ProfileContent.v1` followed by one zero byte.
Fields are encoded in the fixed order implemented by `CanonicalProfileContentV1.to_bytes`.
Signed integers use big-endian two's-complement 64-bit encoding.
Strings must already be NFC-normalized UTF-8 and use a big-endian unsigned 32-bit byte length prefix.
Sequences use a big-endian unsigned 32-bit item count followed by their encoded items.
Optional values use a zero-byte absence tag or a one-byte presence tag followed by the value.

Readings sort by `(start_utc_ns, duration_seconds)` and duplicate keys are forbidden.
Quality flags, findings, and acknowledged warning identities have stable lexical ordering.
The content includes parser and adapter contract versions, finding and confirmation policies, billing range, tariff timezone, interval resolution, every calculation-relevant source semantic, timezone metadata, quality flags, safe findings, and warning acknowledgements.

Persistence IDs, profile IDs, account IDs, reading IDs, import IDs, upload names, object keys, row order, creation timestamps, database sequence values, job IDs, and request IDs are excluded.
They affect ownership and provenance but not calculation content.
The diagnostic golden bytes and SHA-256 value are in `data/golden/canonical-profile-content-v1.json`.
