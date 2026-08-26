"""Operational security: what Erebus knows, who reached it, and what it did.

Erebus holds an investigator's notes, sources and health data, and it can run
programs. That makes it worth protecting in its own right rather than as an
afterthought:

    vault.py   encryption at rest for anything personal
    audit.py   a tamper-evident record of every action and every connection
    guard.py   token rotation, lockout, standing down, and purging

The audit log is the piece that matters most and is easiest to skip. Something
that executes commands on your machine and leaves no trace of having done so is
not auditable after the fact - and "what did it run at 3am, and who asked it
to" is exactly the question you want answerable.
"""
