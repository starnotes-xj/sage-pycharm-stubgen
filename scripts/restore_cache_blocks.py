"""One-off: rewrite a translation cache through _restore_code_blocks."""
import json
import sys

sys.path.insert(0, r"C:\Users\星记\Documents\CTF练习\sage-pycharm-stubgen\src")
from sage_pycharm_stubgen.translate import _restore_code_blocks

for path in [
    r"C:\Users\星记\.sage-pycharm-stubgen\translations.json",
    r"C:\Users\星记\Documents\CTF练习\sage-pycharm-stubgen\src\sage_pycharm_stubgen\translations.json",
]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    data = payload["translations"]
    changed = 0
    for key, value in data.items():
        restored = _restore_code_blocks(key, value)
        if restored != value:
            data[key] = restored
            changed += 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(path, "entries:", len(data), "changed:", changed)
