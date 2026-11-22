"""scindex container: header, entry list, rows (payload = string section + FlatBuffers).

File layout
  version(4) | hash16 | n_rows(4) | n_entries(4) | entries[n]x16 | flags[n] (+ name if flag==1)
  | marker(4)=1 | name_len(4) | table_name | n_rows(4) | rows[n_rows]
Entry : (gid, 0, 0, 0x80000000) is a GlobalID; other entries identify files (e.g. "sc/ui.sc")
Row   : idx(4) | hash8 | size(4) | payload
        idx = position of the row's own gid entry in the entry list
Payload: H-1 (1 byte) | zero padding | string_section_len(4) | strings | (pad<<28 | fb_len)(4) | pad | flatbuffer
        (the fb is aligned to 8 bytes relative to the file start)
"""
import struct

from .flatbuf import _u32

VERSION = 1
GID_FLAG = 0x80000000


class Container:
	def __init__(self):
		self.version = VERSION
		self.hash16 = b"\0" * 16
		self.entries = []  # [(a,b,c,d)]
		self.flags = None  # None -> all zero
		self.flagnames = {} # {entry_no: name}  (flag == 1 -> u32 length + name follow)
		self.marker = 1
		self.table_name = ""
		self.rows = []  # [dict(idx, hash8, strings, fb, raw?)]

	# ---- okuma ----
	@staticmethod
	def read(data):
		c = Container()
		p = 0
		c.version = _u32(data, p); p += 4
		if c.version != VERSION:
			raise SystemExit("unsupported version: %d" % c.version)
		c.hash16 = bytes(data[p:p + 16]); p += 16
		n1, n2 = struct.unpack_from("<II", data, p); p += 8
		for _ in range(n2):
			c.entries.append(struct.unpack_from("<IIII", data, p)); p += 16
		fl = bytearray()
		for i in range(n2):
			f = data[p]; p += 1; fl.append(f)
			if f == 1:
				ln = _u32(data, p); p += 4
				c.flagnames[i] = bytes(data[p:p + ln]).decode("utf-8"); p += ln
		c.flags = bytes(fl)
		c.marker = _u32(data, p); p += 4
		ln = _u32(data, p); p += 4
		c.table_name = data[p:p + ln].decode("utf-8"); p += ln
		cnt = _u32(data, p); p += 4
		if cnt != n1:
			raise SystemExit("row count (%d) does not match header (%d)" % (cnt, n1))
		for _ in range(cnt):
			idx = _u32(data, p); p += 4
			h8 = bytes(data[p:p + 8]); p += 8
			sz = _u32(data, p); p += 4
			pa = p
			pay = bytes(data[p:p + sz]); p += sz
			row = {"idx": idx, "hash8": h8, "raw": pay, "abs": pa}
			try:
				row["strings"], row["fb"] = Container.split_payload(pay, pa)
			except Exception:
				row["strings"], row["fb"] = None, None
			c.rows.append(row)
		if p != len(data):
			raise SystemExit("%d trailing bytes at end of file" % (len(data) - p))
		return c

	@staticmethod
	def split_payload(pay, pa):
		H = pay[0] + 1
		if any(pay[1:H]) or (pa + H) % 8:
			raise ValueError("unexpected payload header")
		sl = _u32(pay, H); p = H + 4
		sec = pay[p:p + sl]; p += sl
		v = _u32(pay, p); p += 4
		pad, size = v >> 28, v & 0x0FFFFFFF
		p += pad
		fb = pay[p:p + size]
		if p + size != len(pay) or (pa + p) % 8:
			raise ValueError("inconsistent flatbuffer bounds")
		strings, q = [], 0
		while q < len(sec):
			n = _u32(sec, q); q += 4
			strings.append(sec[q:q + n].decode("utf-8")); q += n
		return strings, bytes(fb)

	# ---- yazma ----
	@staticmethod
	def make_payload(strings, fb, pa):
		H = ((-pa) % 8) or 8
		sec = b"".join(struct.pack("<I", len(s.encode("utf-8"))) + s.encode("utf-8") for s in strings)
		pay = bytes([H - 1]) + b"\0" * (H - 1) + struct.pack("<I", len(sec)) + sec
		q = pa + len(pay)
		pad = (-(q + 4)) % 8
		return pay + struct.pack("<I", (pad << 28) | len(fb)) + b"\0" * pad + fb

	def write(self):
		n, n2 = len(self.rows), len(self.entries)
		out = bytearray()
		out += struct.pack("<I", self.version) + self.hash16 + struct.pack("<II", n, n2)
		for e in self.entries:
			out += struct.pack("<IIII", *e)
		fl = self.flags if self.flags is not None else b"\0" * n2
		for i in range(n2):
			out.append(fl[i])
			if fl[i] == 1:
				nb = self.flagnames.get(i, "").encode("utf-8")
				out += struct.pack("<I", len(nb)) + nb
		nm = self.table_name.encode("utf-8")
		out += struct.pack("<I", self.marker) + struct.pack("<I", len(nm)) + nm
		out += struct.pack("<I", n)
		for r in self.rows:
			out += struct.pack("<I", r["idx"]) + r["hash8"]
			pa = len(out) + 4 # absolute file offset of the payload
			if r.get("fb") is not None and r.get("strings") is not None:
				pay = Container.make_payload(r["strings"], r["fb"], pa)
			else:
				pay = r["raw"]
			out += struct.pack("<I", len(pay)) + pay
		return bytes(out)

	# ---- helpers ----
	def row_gid(self, i):
		return self.entries[self.rows[i]["idx"]][0]
