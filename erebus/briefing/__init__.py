"""Daily briefings.

The briefing is the one part of Erebus that is *about* the operator rather than
about the machine, so it is built around remembering rather than around
generating. A briefing that only produces motivational text is noise by the
third day; one that knows what you said you would do, and what the numbers say
you actually did, is not.

Three sources feed it:

    profile.py   who you are and what you are trying to do - static, edited by
                 hand, the thing that makes the output yours and not generic
    journal.py   what has happened since - append only, so it can quote you
    health.py    what your wearable says, normalised across devices

compose.py assembles those into a prompt and hands it to the local LLM in the
Blackwall register.
"""
