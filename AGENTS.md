# Repository instructions

- Treat all OA content as confidential and local-only.
- Never commit `data/`, browser profiles, cookies, credentials, Playwright traces, real HTML snapshots, downloaded files, databases, or runtime logs.
- OA integrations are read-only. Do not approve, reply, delete, forward, or alter OA records.
- Use synthetic or irreversibly redacted fixtures in tests.
- Store OA identifiers as text. Store archive paths relative to `data_root`.
- A container tree may be traversed through depth 10. If more children exist, enqueue `depth_limit_reached`; never report the item complete.
