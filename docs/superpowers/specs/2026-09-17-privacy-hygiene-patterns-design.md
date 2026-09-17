# Privacy Hygiene Pattern Guard Design

## Goal

Remove concrete identity and workstation strings from the repository hygiene test while preserving stronger, reusable detection of privacy leaks.

## Scope

- Replace the two value-specific assertions in `tests/test_repository_hygiene.py`.
- Detect Windows user-profile paths with a generic pattern.
- Detect email addresses and permit only synthetic addresses under the reserved `example.com`, `example.org`, and `example.net` domains.
- Keep archived course materials outside this source-code guard; scholarly attribution and the owner's own document identity are unchanged.

## Design

Define a small helper in the test module that accepts text and returns categorized privacy violations. The repository test will apply it to the existing source candidates and fail with file-specific diagnostics when a violation is found. Patterns and allowlist entries remain generic and contain no real identity data.

Focused regression tests will construct synthetic violations at runtime and prove that a Windows profile path and a non-allowlisted email are rejected, while a reserved-domain email is accepted. Because the regression tests exercise the same helper as the repository scan, removing either detector makes the synthetic test fail.

## Validation

Run the focused hygiene test first, followed by the repository-required Ruff, Mypy, Python coverage, and Node test commands. Confirm the final diff contains no concrete personal name, workstation username, or personal email address.
