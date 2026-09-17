# Privacy Hygiene Pattern Guard Design

## Goal

Remove concrete identity and workstation strings from the repository hygiene test while preserving stronger, reusable detection of privacy leaks.

## Scope

- Replace the two value-specific assertions in `tests/test_repository_hygiene.py`.
- Enumerate every tracked repository file with `git ls-files -z` and apply one guard.
- Detect Windows user-profile paths, including JSON-escaped paths, with a generic pattern.
- Detect email addresses and permit only synthetic addresses under the reserved `example.com`, `example.org`, and `example.net` domains.
- Delete the third-party course guideline that contains the repository's only non-synthetic email address. Keep the owner's archived work and necessary scholarly attribution unchanged.

## Design

Define a small helper in the test module that accepts text and returns categorized privacy violations. The repository test will apply it to every path reported by Git and fail with file-specific diagnostics that do not echo the matched identity value. Patterns and allowlist entries remain generic and contain no real identity data. Files are decoded as UTF-8 with replacement so a future non-text blob cannot silently escape the scan.

Focused regression tests will construct synthetic violations at runtime and prove that a Windows profile path and a non-allowlisted email are rejected, while a reserved-domain email is accepted. Because the regression tests exercise the same helper as the repository scan, removing either detector makes the synthetic test fail.

## Validation

Run the focused hygiene test first, followed by the repository-required Ruff, Mypy, Python coverage, and Node test commands. Confirm the final diff contains no concrete personal name, workstation username, or personal email address.
