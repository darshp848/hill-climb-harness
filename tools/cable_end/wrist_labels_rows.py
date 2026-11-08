"""Hand labels for real/wrist/*.png (wsheet_*.jpg), 10 per row. a usb_a, c usb_c, n none, u unsure (not used)."""
import collections
import json

ROWS = """
a n u u a a u n u u
n c u c u u n c a a
a n n a n c n c u u
n u u u u a a c u u
c u n u u n n u n u
n n n n a n c c n n
n u a a u c n u a a
u n u n u a n n n c
n u c a n a n c n a
n n c u n n u u u a
n a u n n n n n n c
u u u u n a n n u n
u c n a n n u n a u
n a n a n u n u u n
u u c c u u n n n a
a n n n a n u n u u
u a n c u c c n c n
n u a u n n c u u u
u a u n u u c n n a
n u u u u c n u u u
u n n a u c n a u n
c a u a u n u n u a
n u u n u n u u a u
n u n u c n u a u n
a a c a a u u u n n
n u n u n u a a n c
a n a u n u n u c n
c n n u u a n n n u
c a n a u c u n n n
n n n u n n n n a n
a n n a n n n n c n
u c u u u a c u n a
a n n n c a u a a c
n c c n u n n u n u
c c n u c c n u n a
n n n u n u a n u u
u a u a n u c u n c
n n n a u a n n n u
n u a c u u u a c n
n a u u u a n a n a
n u u n a u a c n n
a u u n a n a u u n
n n n u u u u u n u
u n a u c n u u a c
n u n u u u u n u n
a u c a u u c a a n
c n u a u n u n a u
c u u a c n u n u u
a n a n n u c a c n
u n u c n n n n a n
n a n n c n n a n u
n n n n u n u a u n
c u u n c n u c n u
a u a a n a u u u a
a a c u n n a n n n
n n a u n n n n c u
u u n n n a c n n n
n n n u n n n n u a
n n c u n u c n n u
a a u n u u n u u n
a n n n n u n n n n
n u u u n c n u u n
n u a c n n a n n n
n c n n u c c c n c
u n n u u n n u c n
u u u u n n n n a u
u n u u n n n c a u
n n n c u n c u c n
a n n n n a c u n a
n u n a n u a n u u
"""
M = {'a': 'usb_a', 'c': 'usb_c', 'n': 'none', 'u': 'unsure'}
rows = [r.split() for r in ROWS.strip().splitlines()]
assert all(len(r) == 10 for r in rows), [i for i, r in enumerate(rows) if len(r) != 10]
flat = [M[t] for r in rows for t in r]
assert len(flat) == 700, len(flat)
labels = {str(i): v for i, v in enumerate(flat)}
json.dump(labels, open('real/wrist_labels.json', 'w'), indent=0)
print(collections.Counter(flat))
