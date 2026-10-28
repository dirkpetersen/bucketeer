// A tiny store-only (no compression) zip writer for ONE file, so the macOS download can keep
// its execute bit: Finder and Safari unzip honour the Unix mode in the external attributes.
const TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(bytes) {
  let c = 0xffffffff;
  for (const b of bytes) c = TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** Zip bytes (Uint8Array) holding one file `name` with `data` (Uint8Array) and Unix `mode`, e.g. 0o755. */
export function zipOne(name, data, mode = 0o755) {
  const nameBytes = new TextEncoder().encode(name);
  const crc = crc32(data);
  const DATE = 33; // 1980-01-01, time 0: no clock, so the same input gives the same file
  const local = new Uint8Array(30 + nameBytes.length);
  const lv = new DataView(local.buffer);
  lv.setUint32(0, 0x04034b50, true);
  lv.setUint16(4, 10, true); // version needed
  lv.setUint16(6, 0x0800, true); // flags: UTF-8 name
  lv.setUint16(8, 0, true); // method: stored
  lv.setUint16(12, DATE, true);
  lv.setUint32(14, crc, true);
  lv.setUint32(18, data.length, true);
  lv.setUint32(22, data.length, true);
  lv.setUint16(26, nameBytes.length, true);
  local.set(nameBytes, 30);

  const central = new Uint8Array(46 + nameBytes.length);
  const cv = new DataView(central.buffer);
  cv.setUint32(0, 0x02014b50, true);
  cv.setUint16(4, (3 << 8) | 20, true); // made by: Unix
  cv.setUint16(6, 10, true);
  cv.setUint16(8, 0x0800, true);
  cv.setUint16(14, DATE, true);
  cv.setUint32(16, crc, true);
  cv.setUint32(20, data.length, true);
  cv.setUint32(24, data.length, true);
  cv.setUint16(28, nameBytes.length, true);
  cv.setUint32(38, ((0o100000 | mode) << 16) >>> 0, true); // external attributes: regular file + mode
  cv.setUint32(42, 0, true); // offset of the local header
  central.set(nameBytes, 46);

  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, 1, true);
  ev.setUint16(10, 1, true);
  ev.setUint32(12, central.length, true);
  ev.setUint32(16, local.length + data.length, true);

  const out = new Uint8Array(local.length + data.length + central.length + end.length);
  out.set(local, 0);
  out.set(data, local.length);
  out.set(central, local.length + data.length);
  out.set(end, local.length + data.length + central.length);
  return out;
}
