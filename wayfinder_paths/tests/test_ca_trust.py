"""The certifi trust store must not trust the compromised eMudhra CAs.

Keys for eMudhra TLS/code-signing intermediates were compromised; they chain
to the roots below (and are cross-signed by Sectigo's "AAA Certificate
Services"). httpx and requests verify against certifi, so any of these roots
or intermediates in the bundle lets a certificate issued with a stolen key
pass. Fingerprints are the certificate SHA-256 values published at
https://emudhra.we-have-a-problem.com/ (roots.json, impacted-intermediates.json).
certifi 2026.10.19 is the first release without them.
"""

import hashlib
import re
import ssl
from pathlib import Path

import certifi

from wayfinder_paths.core.utils.tls import verified_ssl_context

IMPACTED_ROOTS = {
    "7DD78D5F4F13459A83DFF9ABBBA62EDBAF6F2D102BF257FD712F4D9F2746ED8D": "emSign Root TLS CA - G3",
    "F2EDAAFF3D9B701FB4D14C9F285F8C0BAD90FC3BCC1FA07D47F4718EDABA34A5": "emSign Root CS CA - G3",
    "D7A7A0FB5D7E2731D771E9484EBCDEF71D5F0C3E0A2948782BC83EE0EA699EF4": "AAA Certificate Services",
    "86A1ECBA089C4A8D3BBE2734C612BA341D813E043CF9E8A862CD5C57A36BBE6B": "emSign ECC Root CA - G3",
}
IMPACTED_INTERMEDIATES = {
    "42C4ACCD12C4F59597925E1A08A7A90102608543809308D50EC427E118BA1493": "EM DV TLS CA - G3B",
    "45A883F349AEDFD5F3A649B5498FFB121712BA8B448F3F2A91DBC5A766FCDDBE": "EM DV TLS CA - G3B (AAA)",
    "98B896625C0C230ADEEC3EB2CF6AAD5001ECF9156A68644E82374CE5833BC25B": "EM OV TLS CA - G3B",
    "5F1DED4DD590AF74631290CF073B122A2AAC6A1FA479778A0BD43579BD832699": "EM OV TLS CA - G3B (AAA)",
    "E7568FE6E12331F4FD7DE61DA4C7559D0F8EB192A218D4741A3BA75184333838": "EM EV TLS CA - G3B",
    "579480AEA166290BD3C643589E795FC0D0E54E86505D180F1DFF99EFD3199512": "EM EV TLS CA - G3B (AAA)",
    "0164D7CEAE5BB909C272AF151FF44B1177AE9B47D9424B94F1D9558C30934D19": "EM DV TLS CA - G3C",
    "62B7F9868263F72F6911FC78CEAF72C1979158B7368BF648683BB2AA39B02E72": "EM DV TLS CA - G3C (AAA)",
    "CB6E15165AC802E9B3F993F16C25D41F4E81C21EBDE71AE1B012AC00BF9C7C3F": "EM OV TLS CA - G3C",
    "7485B51C0ED192878FA1D2F948FB529AE1842CC5086C4B67F7BE5B2039DE9489": "EM OV TLS CA - G3C (AAA)",
    "432EC25F0D28DE9006D97BF752F17BBB2CD41F669933FCB072D9A1148D0261F3": "EM EV TLS CA - G3C",
    "FD9D5C2DBBD4995AB69B8D50C439D9527B00529199C2FD01B2174EB58F07021F": "EM EV TLS CA - G3C (AAA)",
    "9E39DE2881273ACC225A149C72BA3E0F51424963669AB633EE1DC7DF5DB067B4": "EM OV CS CA - G3B",
    "A496E23AC0CAB8155FC9B117B97EAE368EFCBB9041AEC8804D5A308D2D01C2D8": "EM OV CS CA - G3B (AAA)",
    "4B2460A466F95FD40113BCDCCA2CEB29B3B093921DE45474DA7DF48874C630B1": "EM OV CS CA - G3C",
    "84112972B8E59D14560AE7E7119654474AAD119990CF3722BFC1E3B91615E1FD": "EM OV CS CA - G3C (AAA)",
}
PEM = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def bundle_fingerprints():
    text = Path(certifi.where()).read_text()
    return {
        hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
        for pem in PEM.findall(text)
    }


def test_certifi_bundle_excludes_compromised_emudhra_cas():
    fingerprints = bundle_fingerprints()
    assert fingerprints, "certifi bundle is empty or unreadable"
    impacted = {**IMPACTED_ROOTS, **IMPACTED_INTERMEDIATES}
    trusted = sorted(impacted[fp] for fp in fingerprints & impacted.keys())
    assert not trusted, f"certifi {certifi.__version__} still trusts: {trusted}"


def test_async_transport_context_trusts_only_the_certifi_bundle():
    context = verified_ssl_context()
    actual = {
        hashlib.sha256(cert).hexdigest().upper()
        for cert in context.get_ca_certs(binary_form=True)
    }
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert actual == bundle_fingerprints()
