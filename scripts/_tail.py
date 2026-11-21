
import io
p = "logs/follow_compare/subloop.out.log"
b = io.open(p,"rb").read()
for enc in ("utf-8","gbk"):
    try:
        t = b.decode(enc); print("ENC", enc); break
    except UnicodeDecodeError: pass
print("\n".join(t.strip().splitlines()[-6:]))
