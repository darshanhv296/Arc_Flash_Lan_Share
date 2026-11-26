/**
 * Lightweight, zero-dependency ISO/IEC 18004 QR Code Matrix Generator (Byte mode, ECC-M)
 * Renders crisp scannable QR codes onto an HTML5 Canvas with quiet zone.
 */
(function (global) {
  "use strict";

  // GF(256) with primitive polynomial 0x11d (285)
  const EXP_TABLE = new Array(256);
  const LOG_TABLE = new Array(256);
  for (let i = 0; i < 8; i++) EXP_TABLE[i] = 1 << i;
  for (let i = 8; i < 256; i++) {
    EXP_TABLE[i] = EXP_TABLE[i - 4] ^ EXP_TABLE[i - 5] ^ EXP_TABLE[i - 6] ^ EXP_TABLE[i - 8];
  }
  for (let i = 0; i < 255; i++) LOG_TABLE[EXP_TABLE[i]] = i;

  function gexp(n) {
    while (n < 0) n += 255;
    while (n >= 256) n -= 255;
    return EXP_TABLE[n];
  }
  function glog(n) {
    if (n < 1) throw new Error("glog(" + n + ")");
    return LOG_TABLE[n];
  }

  function polyMultiply(p1, p2) {
    const res = new Array(p1.length + p2.length - 1).fill(0);
    for (let i = 0; i < p1.length; i++) {
      for (let j = 0; j < p2.length; j++) {
        res[i + j] ^= gexp(glog(p1[i]) + glog(p2[j]));
      }
    }
    return res;
  }

  function polyMod(dividend, divisor) {
    if (dividend.length < divisor.length) return dividend.slice();
    const res = dividend.slice();
    for (let i = 0; i <= res.length - divisor.length; i++) {
      if (res[i] === 0) continue;
      const ratio = glog(res[i]);
      for (let j = 0; j < divisor.length; j++) {
        res[i + j] ^= gexp(glog(divisor[j]) + ratio);
      }
    }
    return res.slice(res.length - (divisor.length - 1));
  }

  function getRsPoly(ecCount) {
    let p = [1];
    for (let i = 0; i < ecCount; i++) {
      p = polyMultiply(p, [1, gexp(i)]);
    }
    return p;
  }

  // RS block definitions for ECC Level M (0): [count, totalCodewords, dataCodewords]
  const RS_BLOCKS_M = {
    1: [[1, 26, 16]],
    2: [[1, 44, 28]],
    3: [[1, 70, 44]],
    4: [[2, 50, 32]],
    5: [[2, 67, 43]],
    6: [[4, 43, 27]],
    7: [[4, 49, 31]],
    8: [[2, 60, 38], [2, 61, 39]],
    9: [[3, 58, 36], [2, 59, 37]],
    10: [[4, 69, 43], [1, 70, 44]],
  };

  const ALIGN_POS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
  };

  function expandRsBlocks(version) {
    const spec = RS_BLOCKS_M[version];
    const blocks = [];
    for (const [count, total, data] of spec) {
      for (let i = 0; i < count; i++) blocks.push({ total, data });
    }
    return blocks;
  }

  const G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | 1;
  const G18 = (1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8) | (1 << 5) | (1 << 2) | 1;
  const G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1);

  function getBchDigit(data) {
    let digit = 0;
    while (data !== 0) {
      digit++;
      data >>>= 1;
    }
    return digit;
  }

  function bchTypeInfo(data) {
    let d = data << 10;
    while (getBchDigit(d) - getBchDigit(G15) >= 0) {
      d ^= G15 << (getBchDigit(d) - getBchDigit(G15));
    }
    return ((data << 10) | d) ^ G15_MASK;
  }

  function bchTypeNumber(data) {
    let d = data << 12;
    while (getBchDigit(d) - getBchDigit(G18) >= 0) {
      d ^= G18 << (getBchDigit(d) - getBchDigit(G18));
    }
    return (data << 12) | d;
  }

  class BitBuffer {
    constructor() {
      this.buffer = [];
      this.length = 0;
    }
    put(num, len) {
      for (let i = 0; i < len; i++) {
        this.putBit(((num >>> (len - i - 1)) & 1) === 1);
      }
    }
    putBit(bit) {
      const idx = Math.floor(this.length / 8);
      if (this.buffer.length <= idx) this.buffer.push(0);
      if (bit) this.buffer[idx] |= 0x80 >>> (this.length % 8);
      this.length++;
    }
  }

  function chooseVersion(byteLen) {
    for (let v = 1; v <= 10; v++) {
      const blocks = expandRsBlocks(v);
      const totalDataBits = blocks.reduce((acc, b) => acc + b.data * 8, 0);
      const headerBits = 4 + (v < 10 ? 8 : 16);
      if (headerBits + byteLen * 8 <= totalDataBits) {
        return v;
      }
    }
    throw new Error("QR payload too long");
  }

  function createDataBytes(version, utf8Bytes) {
    const blocks = expandRsBlocks(version);
    const totalDataCount = blocks.reduce((acc, b) => acc + b.data, 0);
    const bb = new BitBuffer();
    bb.put(4, 4); // Byte mode
    bb.put(utf8Bytes.length, version < 10 ? 8 : 16);
    for (let i = 0; i < utf8Bytes.length; i++) bb.put(utf8Bytes[i], 8);
    if (bb.length + 4 <= totalDataCount * 8) bb.put(0, 4);
    while (bb.length % 8 !== 0) bb.putBit(false);
    while (bb.buffer.length < totalDataCount) {
      bb.put(0xec, 8);
      if (bb.buffer.length >= totalDataCount) break;
      bb.put(0x11, 8);
    }

    let offset = 0;
    let maxDc = 0;
    let maxEc = 0;
    const dcData = [];
    const ecData = [];

    for (const blk of blocks) {
      const dc = blk.data;
      const ec = blk.total - dc;
      maxDc = Math.max(maxDc, dc);
      maxEc = Math.max(maxEc, ec);
      const slice = bb.buffer.slice(offset, offset + dc);
      offset += dc;
      dcData.push(slice);
      const rsPoly = getRsPoly(ec);
      const padded = slice.concat(new Array(rsPoly.length - 1).fill(0));
      ecData.push(polyMod(padded, rsPoly));
    }

    const out = [];
    for (let i = 0; i < maxDc; i++) {
      for (let r = 0; r < blocks.length; r++) {
        if (i < dcData[r].length) out.push(dcData[r][i]);
      }
    }
    for (let i = 0; i < maxEc; i++) {
      for (let r = 0; r < blocks.length; r++) {
        if (i < ecData[r].length) out.push(ecData[r][i]);
      }
    }
    return out;
  }

  function maskFn(pattern, i, j) {
    switch (pattern) {
      case 0: return (i + j) % 2 === 0;
      case 1: return i % 2 === 0;
      case 2: return j % 3 === 0;
      case 3: return (i + j) % 3 === 0;
      case 4: return (Math.floor(i / 2) + Math.floor(j / 3)) % 2 === 0;
      case 5: return ((i * j) % 2) + ((i * j) % 3) === 0;
      case 6: return (((i * j) % 2) + ((i * j) % 3)) % 2 === 0;
      case 7: return (((i * j) % 3) + ((i + j) % 2)) % 2 === 0;
      default: return false;
    }
  }

  function buildMatrix(version, dataBytes, maskPattern) {
    const n = version * 4 + 17;
    const m = Array.from({ length: n }, () => new Array(n).fill(null));

    function setProbe(row, col) {
      for (let r = -1; r <= 7; r++) {
        if (row + r < 0 || row + r >= n) continue;
        for (let c = -1; c <= 7; c++) {
          if (col + c < 0 || col + c >= n) continue;
          const dark =
            (r >= 0 && r <= 6 && (c === 0 || c === 6)) ||
            (c >= 0 && c <= 6 && (r === 0 || r === 6)) ||
            (r >= 2 && r <= 4 && c >= 2 && c <= 4);
          m[row + r][col + c] = dark;
        }
      }
    }

    setProbe(0, 0);
    setProbe(n - 7, 0);
    setProbe(0, n - 7);

    // Alignment patterns
    const pos = ALIGN_POS[version];
    for (let i = 0; i < pos.length; i++) {
      for (let j = 0; j < pos.length; j++) {
        const r = pos[i], c = pos[j];
        if (m[r][c] !== null) continue;
        for (let dr = -2; dr <= 2; dr++) {
          for (let dc = -2; dc <= 2; dc++) {
            m[r + dr][c + dc] = dr === -2 || dr === 2 || dc === -2 || dc === 2 || (dr === 0 && dc === 0);
          }
        }
      }
    }

    // Timing patterns
    for (let i = 8; i < n - 8; i++) {
      if (m[i][6] === null) m[i][6] = i % 2 === 0;
      if (m[6][i] === null) m[6][i] = i % 2 === 0;
    }

    // Type info (ECC M = 0 -> (0 << 3) | maskPattern)
    const bits = bchTypeInfo((0 << 3) | maskPattern);
    for (let i = 0; i < 15; i++) {
      const mod = ((bits >>> i) & 1) === 1;
      if (i < 6) m[i][8] = mod;
      else if (i < 8) m[i + 1][8] = mod;
      else m[n - 15 + i][8] = mod;

      if (i < 8) m[8][n - i - 1] = mod;
      else if (i < 9) m[8][15 - i - 1 + 1] = mod;
      else m[8][15 - i - 1] = mod;
    }
    m[n - 8][8] = true;

    if (version >= 7) {
      const vbits = bchTypeNumber(version);
      for (let i = 0; i < 18; i++) {
        const mod = ((vbits >>> i) & 1) === 1;
        m[Math.floor(i / 3)][(i % 3) + n - 8 - 3] = mod;
        m[(i % 3) + n - 8 - 3][Math.floor(i / 3)] = mod;
      }
    }

    // Map data bits
    let inc = -1;
    let row = n - 1;
    let bitIndex = 7;
    let byteIndex = 0;

    for (let col = n - 1; col > 0; col -= 2) {
      if (col === 6) col--;
      while (true) {
        for (let c = 0; c < 2; c++) {
          if (m[row][col - c] === null) {
            let dark = false;
            if (byteIndex < dataBytes.length) {
              dark = ((dataBytes[byteIndex] >>> bitIndex) & 1) === 1;
            }
            if (maskFn(maskPattern, row, col - c)) dark = !dark;
            m[row][col - c] = dark;
            bitIndex--;
            if (bitIndex === -1) {
              byteIndex++;
              bitIndex = 7;
            }
          }
        }
        row += inc;
        if (row < 0 || n <= row) {
          row -= inc;
          inc = -inc;
          break;
        }
      }
    }
    return m;
  }

  function lostScore(m) {
    const n = m.length;
    let score = 0;
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        let same = 0;
        const dark = m[r][c];
        for (let dr = -1; dr <= 1; dr++) {
          if (r + dr < 0 || r + dr >= n) continue;
          for (let dc = -1; dc <= 1; dc++) {
            if (c + dc < 0 || c + dc >= n || (dr === 0 && dc === 0)) continue;
            if (dark === m[r + dr][c + dc]) same++;
          }
        }
        if (same > 5) score += 3 + same - 5;
      }
    }
    for (let r = 0; r < n - 1; r++) {
      for (let c = 0; c < n - 1; c++) {
        let cnt = 0;
        if (m[r][c]) cnt++;
        if (m[r + 1][c]) cnt++;
        if (m[r][c + 1]) cnt++;
        if (m[r + 1][c + 1]) cnt++;
        if (cnt === 0 || cnt === 4) score += 3;
      }
    }
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n - 6; c++) {
        if (m[r][c] && !m[r][c + 1] && m[r][c + 2] && m[r][c + 3] && m[r][c + 4] && !m[r][c + 5] && m[r][c + 6]) {
          score += 40;
        }
      }
    }
    for (let c = 0; c < n; c++) {
      for (let r = 0; r < n - 6; r++) {
        if (m[r][c] && !m[r + 1][c] && m[r + 2][c] && m[r + 3][c] && m[r + 4][c] && !m[r + 5][c] && m[r + 6][c]) {
          score += 40;
        }
      }
    }
    let darkCount = 0;
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) if (m[r][c]) darkCount++;
    }
    const ratio = Math.abs((100 * darkCount) / (n * n) - 50) / 5;
    score += ratio * 10;
    return score;
  }

  function encodeMatrix(text) {
    const utf8 = Array.from(new TextEncoder().encode(text));
    const version = chooseVersion(utf8.length);
    const dataBytes = createDataBytes(version, utf8);
    let bestMatrix = null;
    let bestScore = Infinity;
    for (let mask = 0; mask < 8; mask++) {
      const candidate = buildMatrix(version, dataBytes, mask);
      const s = lostScore(candidate);
      if (s < bestScore) {
        bestScore = s;
        bestMatrix = candidate;
      }
    }
    return bestMatrix;
  }

  function renderToCanvas(canvas, text, sizePx) {
    const matrix = encodeMatrix(text);
    const n = matrix.length;
    const quiet = 2;
    const totalModules = n + quiet * 2;
    const scale = Math.max(2, Math.floor(sizePx / totalModules));
    const actualSize = totalModules * scale;
    canvas.width = actualSize;
    canvas.height = actualSize;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#FFFFFF";
    ctx.fillRect(0, 0, actualSize, actualSize);
    ctx.fillStyle = "#08101C";
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (matrix[r][c]) {
          ctx.fillRect((c + quiet) * scale, (r + quiet) * scale, scale, scale);
        }
      }
    }
  }

  global.ArcQrCode = {
    encodeMatrix: encodeMatrix,
    renderToCanvas: renderToCanvas,
  };
})(window);
