# legacy/

Byte-for-byte backups of the original ScamShield files as they existed at the
start of Phase 2 (verified against the Phase 1 audit listing):

| File | Size | Notes |
|---|---|---|
| `server.py` | 12,727 bytes | Original backend. The active `../server.py` differs by one change: the `/` route serves `frontend/index.html` instead of `index.html`. |
| `test.py` | 217 bytes | Original manual Gemini smoke script (not a pytest test). Still unchanged at the repo root. |
| `index.html.txt` | 99,072 bytes, 1,982 lines | Original frontend including the two stray trailing lines (`index.html`, `Displaying index.html.`). The active `../frontend/index.html` is identical minus those two lines. |

Do not edit these files. They are kept as a reference until the modular
backend and ES-module frontend reach parity, after which they can be removed.
