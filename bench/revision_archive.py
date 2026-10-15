"""How small does a doc's revision history get when archived as one compressed object?

Simulates 200 saves of a ~20 KB doc (each save changes a few sentences), then compares:
  - every revision stored separately, uncompressed
  - every revision compressed on its own (roughly what Postgres TOAST does)
  - all revisions packed into one zstd object (what a monthly R2 archive would look like)

Usage: uv run --no-project --with zstandard bench/revision_archive.py
"""
import random, zlib
import zstandard

rng = random.Random(7)
words = ("the a to and of in is for on with deploy server data folder upload image editor team api "
         "cluster node postgres bucket cache request error retry queue worker schema index query").split()
sent = lambda: " ".join(rng.choice(words) for _ in range(rng.randint(6, 16))).capitalize() + "."
paras = [" ".join(sent() for _ in range(rng.randint(2, 5))) for _ in range(80)]

revs = []
for _ in range(200):
    i = rng.randrange(len(paras))
    if rng.random() < 0.7:
        paras[i] = " ".join(sent() if rng.random() < 0.2 else s for s in paras[i].split(". "))
    else:
        paras.insert(i, sent())
    revs.append("\n\n".join(paras).encode())

raw = sum(len(r) for r in revs)
solo = sum(len(zlib.compress(r, 6)) for r in revs)
packed = len(zstandard.ZstdCompressor(level=19, write_content_size=True).compress(b"\x00".join(revs)))
print(f"200 revisions of a {len(revs[-1]) / 1024:.0f} KB doc")
print(f"  uncompressed            {raw / 1024:8.0f} KB")
print(f"  each compressed alone   {solo / 1024:8.0f} KB   ({raw / solo:.1f}x)  ~ Postgres TOAST")
print(f"  one zstd archive        {packed / 1024:8.0f} KB   ({raw / packed:.0f}x)  ~ monthly R2 pack")
