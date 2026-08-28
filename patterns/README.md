# Extracted patterns

Four ideas from the sponsor-operations platform described in the
[case study](../README.md), rebuilt here as standalone Python.

**Read this first.** These are illustrations, not the product:

- **They are rewrites, not extracts of production code.** The production
  implementations are PostgreSQL functions operating on a real schema. These are
  in-memory Python versions written to make the *rule* legible on its own.
- **They contain no business data.** Product names, catalogs, prices, caps, and
  actor names are invented placeholders chosen to make tests readable.
- **They are not production-ready libraries.** Each one deliberately omits the
  persistence, transaction, and concurrency machinery that makes the real version
  safe, and each module's docstring says which part it is omitting and why that part
  matters. A single-threaded in-memory ledger cannot demonstrate a lock; it can
  demonstrate the ordering discipline the lock depends on.

| Module | Rule | Tests |
|---|---|---|
| [`slot_capacity`](slot_capacity/capacity.py) | Per-issue and per-week caps behind one scope key; carts checked as a group; holds occupy capacity until they lapse | [`test_slot_capacity.py`](../tests/test_slot_capacity.py) |
| [`revision_bound_approval`](revision_bound_approval/approval.py) | Two-person sign-off keyed to `(item, revision, leg)`, so an edit un-approves structurally | [`test_revision_bound_approval.py`](../tests/test_revision_bound_approval.py) |
| [`rep_holds`](rep_holds/holds.py) | Extend adds time and is clamped by a ceiling from creation; a lapsed hold must re-win its slot; validate-all then mutate | [`test_rep_holds.py`](../tests/test_rep_holds.py) |
| [`tiered_pricing`](tiered_pricing/pricing.py) | Volume tiers, and a partial cancel that reprices the remaining cart at its new tier | [`test_tiered_pricing.py`](../tests/test_tiered_pricing.py) |

`rep_holds` builds on `slot_capacity` on purpose: hold lifecycle and capacity are
separate concerns, and the hold desk asks the same ledger the storefront asks.

```bash
pip install pytest
python -m pytest -q
```

MIT licensed. See [../LICENSE](../LICENSE).
