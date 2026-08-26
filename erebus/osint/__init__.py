"""Research tooling for investigative work.

Scoped to entities, documents, domains and media artifacts - the standard
open-source investigation toolkit. Deliberately not people-tracking: no breach
lookups, no address resolution, nothing aimed at profiling a private individual.

    archive.py   capture a page and attest to what it said, before it changes
    media.py     what a file can tell you about where it came from
    domain.py    who runs a site, and what else they run

Every module records what it did to the audit chain, so an investigation has a
provenance trail rather than a folder of screenshots nobody can date.
"""
