/**
 * A UUID, in the contexts where `crypto.randomUUID` does not exist.
 *
 * `crypto.randomUUID` is only exposed in a **secure context** -- https,
 * localhost, or a file. Open the app at `http://192.168.1.x:5273`, which is what
 * reaching it from a phone on the same network means, and `crypto.randomUUID` is
 * `undefined`. `crypto` itself is not: `getRandomValues` has never been
 * restricted that way, so the same id can be built from it.
 *
 * This took the whole app down rather than one feature. Ids are made while
 * `EditorProvider` seeds its state, and a throw there is a throw before anything
 * renders -- so the symptom is a blank page with nothing in it to explain why.
 *
 * **These ids are not a security boundary**, which is what makes a fallback
 * acceptable here and would make it unacceptable for a token. They are the
 * client-side scoping tags the server groups sources by, and `state/editor.tsx`
 * says as much: they become server-issued once there is a tenant model. Nothing
 * is protected by guessing one -- the storage scope that actually separates
 * viewers lives on the server and is not something a client can name.
 */

/** Bytes to a version-4 UUID, per RFC 9562. Split out because it is the only
 *  arithmetic here and the only part worth checking. */
export function uuidFromBytes(bytes: Uint8Array): string {
  const versioned = Uint8Array.from(bytes)
  versioned[6] = (versioned[6] & 0x0f) | 0x40
  versioned[8] = (versioned[8] & 0x3f) | 0x80

  const hex = Array.from(versioned, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}

export function randomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }

  const bytes = new Uint8Array(16)
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes)
  } else {
    // Reached only by a browser old enough to have neither, which is also a
    // browser that will fail on something else first.
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }

  return uuidFromBytes(bytes)
}
