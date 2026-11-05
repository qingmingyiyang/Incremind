import base64
from datetime import timedelta
import json
from os import fstat
from typing import NoReturn

from asn1crypto import core, parser
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.verification import ExtensionPolicy, PolicyBuilder, Store, VerificationError


ANDROID_KEY_DESCRIPTION = x509.ObjectIdentifier('1.3.6.1.4.1.11129.2.1.17')


class AttestationError(ValueError):
    pass


class _Level(core.Enumerated):
    _map = {0: 'software', 1: 'tee', 2: 'strongbox'}


class _BootState(core.Enumerated):
    _map = {0: 'verified', 1: 'self_signed', 2: 'unverified', 3: 'failed'}


class _KeyDescription(core.Sequence):
    _fields = [('version', core.Integer), ('level', _Level), ('key_version', core.Integer),
        ('key_level', _Level), ('challenge', core.OctetString), ('unique_id', core.OctetString),
        ('software', core.Sequence), ('hardware', core.Sequence)]


class _RootOfTrust(core.Sequence):
    _fields = [('boot_key', core.OctetString), ('locked', core.Boolean), ('state', _BootState),
        ('boot_hash', core.OctetString, {'optional': True})]


class _Package(core.Sequence):
    _fields = [('name', core.OctetString), ('version', core.Integer)]


class _Packages(core.SetOf):
    _child_spec = _Package


class _Digests(core.SetOf):
    _child_spec = core.OctetString


class _Integers(core.SetOf):
    _child_spec = core.Integer


class _AppIdentity(core.Sequence):
    _fields = [('packages', _Packages), ('digests', _Digests)]


def _reject() -> NoReturn:
    raise AttestationError('mobile_attestation_invalid')


def _tags(sequence):
    # Android 使用高编号的显式上下文标签，底层 DER 结构由 asn1crypto 解析。
    fields, remaining = {}, sequence.contents
    while remaining:
        size = parser.peek(remaining)
        tag_class, method, number, _header, body, trailer = parser.parse(remaining[:size], strict=True)
        if tag_class != 2 or method != 1 or trailer or number in fields:
            _reject()
        fields[number] = body
        remaining = remaining[size:]
    return fields


class AndroidAttestation:
    def __init__(self, *, roots, revocations, package, signing_digest):
        self.roots = Store(x509.load_pem_x509_certificates(roots.read_bytes()))
        self.revocations = revocations
        self.package = package.encode('ascii')
        self.signing_digest = signing_digest

    def _entries(self, now):
        try:
            with self.revocations.open('rb') as source:
                age = now.timestamp() - fstat(source.fileno()).st_mtime
                if not -300 <= age < timedelta(hours=48).total_seconds():
                    raise AttestationError('mobile_revocations_unavailable')
                entries = json.load(source)['entries']
                if not isinstance(entries, dict):
                    raise AttestationError('mobile_revocations_unavailable')
                return entries
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise AttestationError('mobile_revocations_unavailable') from None

    def assert_not_revoked(self, serials, now):
        entries = self._entries(now)
        if any(serial in entries for serial in serials):
            raise AttestationError('mobile_attestation_revoked')

    def verify(self, encoded_chain, challenge, now):
        try:
            certs = [x509.load_der_x509_certificate(base64.b64decode(value, validate=True)) for value in encoded_chain]
            path = (PolicyBuilder().store(self.roots).time(now).max_chain_depth(8)
                .extension_policies(ee_policy=ExtensionPolicy.permit_all(), ca_policy=ExtensionPolicy.webpki_defaults_ca())
                .build_client_verifier().verify(certs[0], certs[1:]).chain)
            if (len(path) < 2 or any(ANDROID_KEY_DESCRIPTION in {item.oid for item in cert.extensions} for cert in path[1:])):
                _reject()
            extension = path[0].extensions.get_extension_for_oid(ANDROID_KEY_DESCRIPTION).value
            key = path[0].public_key()
            description = _KeyDescription.load(extension.value, strict=True)
            if (description['level'].native not in {'tee', 'strongbox'} or description['key_level'].native not in {'tee', 'strongbox'}
                    or description['challenge'].native != challenge
                    or not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1)):
                _reject()
            hardware, software = _tags(description['hardware']), _tags(description['software'])
            if (set(software).intersection({1, 2, 5, 10, 702, 704}) or 709 in hardware
                    or core.Integer.load(hardware[702], strict=True).native != 0
                    or core.Integer.load(hardware[2], strict=True).native != 3
                    or core.Integer.load(hardware[10], strict=True).native != 1
                    or set(_Integers.load(hardware[1], strict=True).native) != {2}
                    or 4 not in _Integers.load(hardware[5], strict=True).native
                    or 600 in hardware or 600 in software):
                _reject()
            boot = _RootOfTrust.load(hardware[704], strict=True)
            app = _AppIdentity.load(core.OctetString.load(software[709], strict=True).native, strict=True)
            if (not boot['locked'].native or boot['state'].native != 'verified'
                    or [value['name'].native for value in app['packages']] != [self.package]
                    or app['digests'].native != [self.signing_digest]):
                _reject()
        except (VerificationError, ValueError, TypeError, IndexError, KeyError, x509.ExtensionNotFound):
            raise AttestationError('mobile_attestation_invalid') from None
        serials = [format(cert.serial_number, 'x') for cert in path]
        self.assert_not_revoked(serials, now)
        return key, serials
