"""Who runs a site, and what else they run.

Six independent views of a domain, gathered concurrently:

  RDAP          registration, registrar, dates. The IETF replacement for WHOIS -
                structured JSON over HTTPS, so no scraping of free-text WHOIS
                output, which every registrar formats differently.
  DNS           where it resolves now.
  TLS           the certificate: issuer, validity, and the subject alternative
                names, which routinely list sibling domains the operator did
                not mean to connect publicly.
  CT logs       every certificate ever issued for the domain, via crt.sh. The
                single richest source of subdomains, and it reaches back to
                hosts that are long gone.
  Wayback       how far back the site is archived and when it was last seen.
  HTTP          the redirect chain and server headers.

All read-only, all public record, all from sources designed to be queried. No
port scanning, no probing beyond an ordinary HTTPS request.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("erebus.osint.domain")

USER_AGENT = "Erebus/0.1 (research; +local)"
TIMEOUT = 15.0


@dataclass
class DomainReport:
    domain: str
    registered: str | None = None
    expires: str | None = None
    updated: str | None = None
    registrar: str | None = None
    statuses: list[str] = field(default_factory=list)
    nameservers: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    reverse: dict[str, str] = field(default_factory=dict)
    certificate: dict[str, Any] = field(default_factory=dict)
    subdomains: list[str] = field(default_factory=list)
    subdomain_note: str | None = None
    wayback_first: str | None = None
    wayback_last: str | None = None
    redirects: list[str] = field(default_factory=list)
    server: str | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [self.domain]
        if self.registered or self.registrar:
            lines.append("  registration")
            if self.registered:
                lines.append(f"    created            {self.registered}")
            if self.updated:
                lines.append(f"    updated            {self.updated}")
            if self.expires:
                lines.append(f"    expires            {self.expires}")
            if self.registrar:
                lines.append(f"    registrar          {self.registrar}")
            if self.statuses:
                lines.append(f"    status             {', '.join(self.statuses[:4])}")
        if self.nameservers:
            lines.append(f"  nameservers          {', '.join(self.nameservers[:4])}")
        if self.addresses:
            lines.append("  resolves to")
            for address in self.addresses[:6]:
                host = self.reverse.get(address)
                lines.append(f"    {address:<40}{host or ''}")
        if self.certificate:
            cert = self.certificate
            lines.append("  certificate")
            lines.append(f"    issuer             {cert.get('issuer', '?')}")
            lines.append(f"    valid              {cert.get('not_before','?')}"
                         f"  ..  {cert.get('not_after','?')}")
            names = cert.get("names") or []
            if names:
                lines.append(f"    covers             {', '.join(names[:6])}"
                             + (f"  (+{len(names)-6} more)" if len(names) > 6 else ""))
        if self.subdomains or self.subdomain_note:
            lines.append(f"  subdomains seen      {len(self.subdomains)}"
                         + (f"  ({self.subdomain_note})" if self.subdomain_note else ""))
            for name in self.subdomains[:12]:
                lines.append(f"    {name}")
            if len(self.subdomains) > 12:
                lines.append(f"    ... {len(self.subdomains) - 12} more")
        if self.wayback_first or self.wayback_last:
            lines.append(f"  archived             {self.wayback_first or '?'}"
                         f"  ..  {self.wayback_last or '?'}")
        if self.redirects:
            lines.append(f"  redirects            {' -> '.join(self.redirects)}")
        if self.server:
            lines.append(f"  server               {self.server}")
        for error in self.errors:
            lines.append(f"  ! {error}")
        return "\n".join(lines)


def _clean(domain: str) -> str:
    domain = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain.split("/")[0].split("?")[0].strip(".")


# --------------------------------------------------------------------------

async def _rdap(client: httpx.AsyncClient, report: DomainReport) -> None:
    try:
        response = await client.get(f"https://rdap.org/domain/{report.domain}")
        if response.status_code == 404:
            report.errors.append("No RDAP record (domain may be unregistered).")
            return
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"RDAP unavailable ({type(exc).__name__}).")
        return

    for event in data.get("events", []):
        action, date = event.get("eventAction"), event.get("eventDate")
        if action == "registration":
            report.registered = date
        elif action == "expiration":
            report.expires = date
        elif action in ("last changed", "last update of RDAP database"):
            report.updated = report.updated or date

    report.statuses = [str(s) for s in data.get("status", [])]
    report.nameservers = [
        ns.get("ldhName", "").lower() for ns in data.get("nameservers", [])
        if ns.get("ldhName")
    ]

    for entity in data.get("entities", []):
        if "registrar" in entity.get("roles", []):
            for item in entity.get("vcardArray", [[], []])[1]:
                if item and item[0] == "fn":
                    report.registrar = item[3]
                    break


async def _dns(report: DomainReport) -> None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(report.domain, None)
        )
    except socket.gaierror as exc:
        report.errors.append(f"DNS did not resolve ({exc.strerror or exc}).")
        return

    seen = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    report.addresses = seen

    async def lookup(address: str) -> None:
        try:
            host = await loop.run_in_executor(
                None, lambda: socket.gethostbyaddr(address)[0]
            )
            report.reverse[address] = host
        except (socket.herror, socket.gaierror, OSError):
            pass

    await asyncio.gather(*(lookup(a) for a in seen[:6]))


async def _tls(report: DomainReport) -> None:
    """Read the certificate without trusting it.

    Verification is deliberately off - an expired or mismatched certificate is
    a finding, and refusing to look would discard exactly the case worth
    knowing about. But with verify_mode=CERT_NONE, Python's getpeercert()
    returns an empty dict: it only populates the parsed form for a certificate
    it validated. So the DER is taken raw and parsed with `cryptography`, which
    is already a dependency for the vault.
    """
    loop = asyncio.get_running_loop()

    def fetch() -> bytes | None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((report.domain, 443), timeout=TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=report.domain) as tls:
                return tls.getpeercert(binary_form=True)

    try:
        der = await loop.run_in_executor(None, fetch)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"TLS handshake failed ({type(exc).__name__}).")
        return
    if not der:
        return

    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID, NameOID
    except ImportError:
        report.certificate = {"note": "certificate present; install `cryptography` to read it"}
        return

    try:
        cert = x509.load_der_x509_certificate(der)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"Certificate could not be parsed ({type(exc).__name__}).")
        return

    def utc(cert_obj, field_name: str):
        """Read a validity date across cryptography versions.

        `not_valid_before_utc` arrived in cryptography 42; before that the
        attribute is `not_valid_before` and returns a naive UTC datetime.
        Distributions ship both, so support both rather than pinning.
        """
        modern = getattr(cert_obj, f"{field_name}_utc", None)
        if modern is not None:
            return modern
        legacy = getattr(cert_obj, field_name)
        return legacy.replace(tzinfo=timezone.utc)

    def name_of(name) -> str:
        parts = []
        for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
            for attribute in name.get_attributes_for_oid(oid):
                parts.append(attribute.value)
        return " / ".join(dict.fromkeys(parts))

    # Everything from here reads fields off the parsed certificate, and the
    # available attributes vary by library version - so a failure must be
    # reported rather than escaping and leaving the section mysteriously blank.
    try:
        names: list[str] = []
        try:
            san = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )
            names = sorted(set(san.value.get_values_for_type(x509.DNSName)))
        except x509.ExtensionNotFound:
            pass

        not_before = utc(cert, "not_valid_before")
        not_after = utc(cert, "not_valid_after")
        report.certificate = {
            "issuer": name_of(cert.issuer),
            "subject": name_of(cert.subject),
            "not_before": not_before.strftime("%Y-%m-%d"),
            "not_after": not_after.strftime("%Y-%m-%d"),
            "names": names,
            "serial": format(cert.serial_number, "x"),
        }
    except Exception as exc:  # noqa: BLE001
        report.errors.append(
            f"Certificate read but its fields could not be extracted "
            f"({type(exc).__name__}: {exc})."
        )
        return

    if not_after < datetime.now(timezone.utc):
        report.errors.append(f"Certificate EXPIRED on {not_after:%Y-%m-%d}.")
    if report.domain not in names and f"*.{report.domain}" not in [
        n for n in names
    ] and not any(
        n.startswith("*.") and report.domain.endswith(n[2:]) for n in names
    ):
        report.errors.append(
            f"Certificate does not cover {report.domain} - it is issued for "
            f"{', '.join(names[:3]) or 'something else'}."
        )


async def _crtsh(client: httpx.AsyncClient, report: DomainReport) -> None:
    """Certificate transparency: every cert ever issued for the domain."""
    try:
        response = await client.get(
            "https://crt.sh/",
            params={"q": f"%.{report.domain}", "output": "json"},
            timeout=25.0,
        )
        response.raise_for_status()
        rows = response.json()
    except Exception as exc:  # noqa: BLE001
        report.subdomain_note = f"crt.sh unavailable ({type(exc).__name__})"
        return

    found: set[str] = set()
    for row in rows:
        for name in str(row.get("name_value", "")).splitlines():
            name = name.strip().lower().lstrip("*.")
            if name.endswith(report.domain) and name != report.domain:
                found.add(name)
    report.subdomains = sorted(found)
    report.subdomain_note = f"from {len(rows)} certificates"


async def _wayback(client: httpx.AsyncClient, report: DomainReport) -> None:
    try:
        response = await client.get(
            "https://web.archive.org/cdx/search/cdx",
            params={"url": report.domain, "output": "json", "fl": "timestamp",
                    "collapse": "timestamp:6", "limit": "1"},
            timeout=20.0,
        )
        rows = response.json()
        if len(rows) > 1:
            report.wayback_first = rows[1][0]
        latest = await client.get(
            "https://web.archive.org/cdx/search/cdx",
            params={"url": report.domain, "output": "json", "fl": "timestamp",
                    "limit": "-1"},
            timeout=20.0,
        )
        rows = latest.json()
        if len(rows) > 1:
            report.wayback_last = rows[1][0]
    except Exception as exc:  # noqa: BLE001
        log.debug("wayback lookup failed: %s", exc)


async def _http(client: httpx.AsyncClient, report: DomainReport) -> None:
    try:
        response = await client.get(f"https://{report.domain}",
                                    follow_redirects=True)
        report.redirects = [str(r.url) for r in response.history]
        report.server = response.headers.get("server")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"HTTP request failed ({type(exc).__name__}).")


async def investigate(domain: str, audit=None,
                      subdomains: bool = True) -> DomainReport:
    """Gather everything, concurrently. One source failing never blocks another."""
    report = DomainReport(domain=_clean(domain))

    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as client:
        tasks = [
            _rdap(client, report),
            _dns(report),
            _tls(report),
            _wayback(client, report),
            _http(client, report),
        ]
        if subdomains:
            tasks.append(_crtsh(client, report))
        await asyncio.gather(*tasks, return_exceptions=True)

    if audit is not None:
        audit.record(
            "osint.domain", domain=report.domain,
            addresses=len(report.addresses), subdomains=len(report.subdomains),
        )
    return report
