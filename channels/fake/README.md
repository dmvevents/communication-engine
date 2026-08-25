# channels/fake — the dry-run adapter (implemented)

The one adapter that ships today. In-memory, no network, no credentials — it exists so an
adopter can exercise the whole pipeline (store → classify → journal → outbox) before any
platform adapter lands, and so adapter authors have a complete, minimal `CONTRACT.md`
implementation to copy.

- `adapter.py` — every contract method and nothing else. `seed()` injects messages for
  `poll()`; `fail_health` makes `health()` fail on demand (contract rule 5: a health check
  that can only pass is a defect).
- Conformance-tested by `tests/test_extensibility.py` (`ShippedFakeAdapterTest`).

Try it: set `"adapter": "fake"` in `settings.json`, or run

```sh
python3 -m unittest tests.test_portability -k EndToEnd -v
```
